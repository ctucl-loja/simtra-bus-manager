"""
data_loader: conversión de payloads para device-api y el ciclo de subida.

Todo con dobles: ni la API local ni el backend remoto se tocan. El flujo real
contra FastAPI + SQLite está en test_gps_pipeline.py.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

import data_loader
from api import (
    SendResult, SEND_OK, SEND_CONFLICT, SEND_REJECTED, SEND_AUTH_ERROR,
    SEND_CONFIG_ERROR, SEND_NOT_FOUND, SEND_RETRY,
)

GYE = ZoneInfo("America/Guayaquil")


class FakeSimtra:
    """Doble de ApiService. Cada cola devuelve resultados de una lista (o uno fijo)."""

    def __init__(self, gps=SEND_OK, dispatch=SEND_OK, passenger=SEND_OK, config_error=None):
        self.results = {"gps": gps, "dispatch": dispatch, "passenger": passenger}
        self.config_error = config_error
        self.calls = []

    def configuration_error(self):
        return self.config_error

    def _next(self, kind):
        value = self.results[kind]
        status = value.pop(0) if isinstance(value, list) else value
        return SendResult(status, {SEND_REJECTED: 400, SEND_CONFLICT: 409,
                                   SEND_AUTH_ERROR: 401, SEND_NOT_FOUND: 404}.get(status))

    def post_gps(self, data, register):
        self.calls.append(("gps", data, register))
        return self._next("gps")

    def update_dispatch(self, data, register):
        self.calls.append(("dispatch", data, register))
        return self._next("dispatch")

    def post_passenger(self, data):
        self.calls.append(("passenger", data, None))
        return self._next("passenger")

    def kinds(self):
        return [call[0] for call in self.calls]


def gps_row(row_id, **extra):
    row = {"id": row_id, "timestamp": "2026-08-25T07:00:00", "timestamp_unix": 1787659200 + row_id,
           "latitude": -4.0, "longitude": -79.0, "speed": 8.0}
    row.update(extra)
    return row


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self.marked = []
        self.rejected = []
        self.mark_ok = True
        data_loader._sent_not_marked.clear()
        self.addCleanup(data_loader._sent_not_marked.clear)

    def patch(self, **values):
        for name, value in values.items():
            previous = getattr(data_loader, name)
            setattr(data_loader, name, value)
            self.addCleanup(setattr, data_loader, name, previous)

    def install(self, fake, gps=(), passengers=(), checkpoints=()):
        def marker(kind):
            def mark(row_id):
                if not self.mark_ok:
                    return False
                self.marked.append((kind, row_id))
                return True
            return mark

        def pending(kind, rows):
            # Como la API local: lo ya marcado deja de estar pendiente.
            return lambda *a, **k: [r for r in rows
                                    if (kind, r.get("id") if isinstance(r, dict) else None) not in self.marked]

        self.patch(
            BUS_REGISTER=1624,
            simtra=fake,
            get_pending_gps=pending("gps", gps),
            get_pending_passengers=pending("passenger", passengers),
            get_pending_checkpoints=pending("checkpoint", checkpoints),
            update_gps_local_register=marker("gps"),
            update_passenger_local_register=marker("passenger"),
            update_checkpoint_local_register=marker("checkpoint"),
            reject_gps_local_register=lambda row_id, reason: self.rejected.append((row_id, reason)) or True,
        )


# ─────────────────────────────────────────────
# PAYLOADS
# ─────────────────────────────────────────────

class UnixSecondsTest(unittest.TestCase):
    def test_iso_con_zona_es_exacto(self):
        expected = int(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(data_loader.unix_seconds("2026-08-25T12:00:00+00:00"), expected)
        self.assertEqual(data_loader.unix_seconds("2026-08-25T12:00:00Z"), expected)
        self.assertEqual(data_loader.unix_seconds("2026-08-25T07:00:00-05:00"), expected)

    def test_iso_sin_zona_es_hora_de_ecuador(self):
        expected = int(datetime(2026, 8, 25, 7, 0, tzinfo=GYE).timestamp())
        self.assertEqual(data_loader.unix_seconds("2026-08-25T07:00:00"), expected)
        # Y equivale a 12:00 UTC: exactamente +5 h, ni 0 ni 10 (doble conversión).
        self.assertEqual(expected, int(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc).timestamp()))

    def test_numero_se_usa_tal_cual_en_segundos(self):
        self.assertEqual(data_loader.unix_seconds(1787659200), 1787659200)
        self.assertEqual(data_loader.unix_seconds(1787659200.9), 1787659200)

    def test_invalidos(self):
        for value in (None, "", "x", float("nan"), float("inf"), True, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(data_loader.unix_seconds(value))


class GpsPayloadTest(unittest.TestCase):
    def test_timestamp_unix_manda_sobre_el_texto_sin_zona(self):
        """El texto de SQLite perdió la zona; el Unix calculado al recibir no."""
        payload = data_loader.gps_payload({
            "timestamp": "2026-08-25T12:00:00",   # llegó como 12:00Z y SQLite quitó la zona
            "timestamp_unix": 1787659200,
            "latitude": "-4.0", "longitude": "-79.0", "speed": "12.5",
        })
        self.assertEqual(payload, {"timestamp": 1787659200, "latitude": -4.0,
                                   "longitude": -79.0, "speed": 12.5})
        self.assertIsInstance(payload["timestamp"], int)

    def test_fila_antigua_sin_unix_usa_hora_de_ecuador(self):
        payload = data_loader.gps_payload({"timestamp": "2026-08-25T07:00:00",
                                           "latitude": -4.0, "longitude": -79.0})
        self.assertEqual(payload["timestamp"], int(datetime(2026, 8, 25, 7, tzinfo=GYE).timestamp()))

    def test_velocidad_desconocida_o_invalida_se_omite(self):
        for speed in (None, "x", float("nan"), -1):
            with self.subTest(speed=speed):
                payload = data_loader.gps_payload(gps_row(1, speed=speed))
                self.assertNotIn("speed", payload)

    def test_solo_campos_del_dto_remoto(self):
        """forbidNonWhitelisted: id, upload, created_at… producirían un 400."""
        payload = data_loader.gps_payload(gps_row(1, upload=False, created_at="x", upload_error=None))
        self.assertEqual(set(payload), {"timestamp", "latitude", "longitude", "speed"})

    def test_payload_invalido_no_se_sube(self):
        casos = [
            None,
            {"timestamp": "x", "latitude": -4, "longitude": -79},
            {"timestamp": "2026-08-25T12:00:00", "latitude": "nan", "longitude": -79},
            {"timestamp": "2026-08-25T12:00:00", "latitude": -4, "longitude": None},
            {"timestamp": "2026-08-25T12:00:00", "latitude": 91, "longitude": -79},
            {"timestamp": "2026-08-25T12:00:00", "latitude": True, "longitude": -79},
        ]
        for caso in casos:
            with self.subTest(caso=caso):
                self.assertIsNone(data_loader.gps_payload(caso))


class PassengerPayloadTest(unittest.TestCase):
    def setUp(self):
        previous = data_loader.BUS_REGISTER
        data_loader.BUS_REGISTER = 1624
        self.addCleanup(setattr, data_loader, "BUS_REGISTER", previous)

    def test_fecha_local_sin_zona_se_envia_con_desfase_de_ecuador(self):
        payload = data_loader.passenger_payload({
            "id": 1, "timestamp": "2026-08-25T07:00:00", "latitude": -4.0, "longitude": -79.0,
            "direction": "ENTRY", "door": "FRONT", "upload": False, "created_at": "x",
        })
        self.assertEqual(payload, {
            "latitude": -4.0, "longitude": -79.0, "register": 1624,
            "timestamp": "2026-08-25T07:00:00-05:00",
            "direction": "ENTRY", "door": "FRONT",
        })

    def test_sin_direction_ni_door_no_se_envian_null(self):
        payload = data_loader.passenger_payload({"timestamp": "2026-08-25T07:00:00",
                                                 "latitude": -4.0, "longitude": -79.0})
        self.assertNotIn("direction", payload)
        self.assertNotIn("door", payload)


# ─────────────────────────────────────────────
# CICLO
# ─────────────────────────────────────────────

class SyncOnceTest(LoaderTestCase):
    def test_sube_gps_por_device_api_y_luego_marca_local(self):
        fake = FakeSimtra()
        self.install(fake, gps=[gps_row(7)])

        self.assertTrue(data_loader.sync_once())

        self.assertEqual(self.marked, [("gps", 7)])
        self.assertEqual(fake.calls[0][2], 1624)
        self.assertEqual(fake.calls[0][1]["timestamp"], 1787659207)

    def test_orden_checkpoints_pasajeros_gps(self):
        fake = FakeSimtra()
        self.install(fake, gps=[gps_row(1), gps_row(2)],
                     passengers=[{"id": 5, "timestamp": "2026-08-25T07:00:00", "latitude": -4, "longitude": -79}],
                     checkpoints=[{"id": 9, "checkpoint_id": 3701, "timestamp": "06:10:00"}])

        data_loader.sync_once()

        self.assertEqual(fake.kinds(), ["dispatch", "passenger", "gps", "gps"])
        self.assertEqual([c[1]["timestamp"] for c in fake.calls if c[0] == "gps"], [1787659201, 1787659202])

    def test_fallo_remoto_no_marca_y_corta_la_cola(self):
        for status in (SEND_RETRY, SEND_AUTH_ERROR, SEND_NOT_FOUND):
            with self.subTest(status=status):
                self.marked.clear()
                self.rejected.clear()
                fake = FakeSimtra(gps=status)
                self.install(fake, gps=[gps_row(1), gps_row(2), gps_row(3)])

                self.assertFalse(data_loader.sync_once())

                self.assertEqual(self.marked, [])
                self.assertEqual(self.rejected, [])
                self.assertEqual(len(fake.calls), 1)   # no insiste con los siguientes

    def test_api_key_rechazada_en_checkpoints_no_intenta_lo_demas(self):
        fake = FakeSimtra(dispatch=SEND_AUTH_ERROR)
        self.install(fake, gps=[gps_row(1)],
                     passengers=[{"id": 5, "timestamp": "2026-08-25T07:00:00", "latitude": -4, "longitude": -79}],
                     checkpoints=[{"id": 9, "checkpoint_id": 3701, "timestamp": "06:10:00"}])

        data_loader.sync_once()

        self.assertEqual(fake.kinds(), ["dispatch"])
        self.assertEqual(self.marked, [])

    def test_sin_api_key_no_se_envia_nada(self):
        fake = FakeSimtra(config_error="FAST_API_DEVICE_API_KEY no está configurada")
        self.install(fake, gps=[gps_row(1)],
                     checkpoints=[{"id": 9, "checkpoint_id": 3701, "timestamp": "06:10:00"}])

        self.assertFalse(data_loader.sync_once())
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.marked, [])

    def test_punto_rechazado_con_400_sale_de_la_cola_sin_bloquear(self):
        fake = FakeSimtra(gps=[SEND_REJECTED, SEND_OK])
        self.install(fake, gps=[gps_row(1), gps_row(2)])

        data_loader.sync_once()

        self.assertEqual(self.rejected, [(1, "rechazado por device-api (HTTP 400)")])
        self.assertEqual(self.marked, [("gps", 2)])

    def test_punto_local_inutilizable_sale_de_la_cola_sin_enviarse(self):
        fake = FakeSimtra()
        self.install(fake, gps=[gps_row(1, latitude=None), gps_row(2)])

        data_loader.sync_once()

        self.assertEqual([r[0] for r in self.rejected], [1])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.marked, [("gps", 2)])

    def test_fallo_al_marcar_local_reintenta_solo_el_marcado(self):
        fake = FakeSimtra()
        rows = [gps_row(7)]
        passengers = [{"id": 5, "timestamp": "2026-08-25T07:00:00", "latitude": -4, "longitude": -79}]
        self.install(fake, gps=rows, passengers=passengers)

        self.mark_ok = False
        data_loader.sync_once()
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(self.marked, [])

        # Siguiente ciclo: la API local vuelve; los elementos siguen pendientes.
        self.mark_ok = True
        data_loader.sync_once()

        self.assertEqual(len(fake.calls), 2)   # NO se reenvió (evita pasajero duplicado)
        self.assertEqual(sorted(self.marked), [("gps", 7), ("passenger", 5)])
        self.assertEqual(data_loader._sent_not_marked, set())

    def test_checkpoint_con_register_y_409_se_da_por_sincronizado(self):
        fake = FakeSimtra(dispatch=[SEND_OK, SEND_CONFLICT, SEND_NOT_FOUND])
        self.install(fake, checkpoints=[
            {"id": 9, "checkpoint_id": 3701, "timestamp": "06:10:00"},
            {"id": 10, "checkpoint_id": 3702, "timestamp": "06:30:00"},
            {"id": 11, "checkpoint_id": 3703, "timestamp": "06:50:00"},
        ], gps=[gps_row(1)])

        data_loader.sync_once()

        self.assertEqual(fake.calls[0][1:], ({"id": 3701, "time_reported": "06:10:00"}, 1624))
        # 404 de un despacho no bloquea al resto ni se marca.
        self.assertEqual(self.marked, [("checkpoint", 9), ("checkpoint", 10), ("gps", 1)])

    def test_presupuesto_de_tiempo_acota_la_tanda_gps(self):
        fake = FakeSimtra()
        self.install(fake, gps=[gps_row(i) for i in range(1, 6)])
        ticks = iter([0, 0, 10, 40, 50, 60, 70])

        data_loader.sync_gps(clock=lambda: next(ticks))

        self.assertEqual(len(fake.calls), 2)

    def test_sin_register_no_se_envia(self):
        fake = FakeSimtra()
        self.install(fake, gps=[gps_row(1)])
        self.patch(BUS_REGISTER=0)
        self.assertFalse(data_loader.sync_once())
        self.assertEqual(fake.calls, [])


class ImportTest(unittest.TestCase):
    def test_importar_no_arranca_el_bucle_ni_crea_log(self):
        import importlib
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                importlib.reload(data_loader)
                self.assertFalse(os.path.exists(os.path.join(tmp, "data_loader.log")))
            finally:
                os.chdir(cwd)
        self.assertTrue(callable(data_loader.run_forever))


if __name__ == "__main__":
    unittest.main()
