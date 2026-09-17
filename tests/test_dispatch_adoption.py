"""
Coordinación entre la recarga manual de la pantalla y el monitor de geocercas.

FastAPI y simtra-bus-monitor son procesos systemd SEPARADOS: no comparten
memoria. El monitor se entera de una recarga por el canal local de eventos y
adopta el despacho ya GUARDADO (`GET /api/dispatch`), no volviendo a bajarlo del
backend remoto.

Aquí se prueba el lado del monitor con la API local simulada. Lo que se protege:

  1. Adoptar el itinerario nuevo actualiza contexto temporal Y geocercas, aunque
     el bus siga en el mismo tramo.
  2. Adoptar NO borra la deduplicación del día: un checkpoint ya reportado no se
     vuelve a reportar, así que no se repite el aviso de llegada.
  3. Estar DENTRO de una geocerca que sobrevive a la recarga no se relee como
     una entrada nueva.
  4. Una revisión que no es más nueva no se adopta (nada de bucles).
  5. Una carga del monitor iniciada antes de la recarga declara su
     `base_revision` y no pisa el itinerario nuevo.
"""

import unittest
from datetime import datetime

import _bootstrap  # noqa: F401

import bus_monitor as monitor
from _fixtures import make_checkpoint, make_step


TODAY = datetime.now().strftime("%Y-%m-%d")


def dispatch_body(data, revision=1, date=None, register=None):
    return {
        "id": 1,
        "date": date or TODAY,
        "register": monitor.BUS_REGISTER if register is None else register,
        "data": data,
        "revision": revision,
        "created_at": "2026-09-17T06:00:00Z",
    }


def itinerary_a():
    """Recorrido original: puntos 684 / 685 / 686."""
    return [make_step(step=1, start="00:00:00", end="23:59:59", checkpoints=[
        make_checkpoint(3701, 684, 0, "06:10:00"),
        make_checkpoint(3702, 685, 1, "06:30:00"),
        make_checkpoint(3703, 686, 2, "06:50:00"),
    ])]


def itinerary_b():
    """Recorrido recargado: MISMO step, otros puntos (690 nuevo, 686 fuera)."""
    return [make_step(step=1, start="00:00:00", end="23:59:59", checkpoints=[
        make_checkpoint(3701, 684, 0, "06:12:00"),
        make_checkpoint(3710, 690, 1, "06:35:00"),
    ])]


class AdoptionTestCase(unittest.TestCase):
    """Base: aísla el estado global del monitor y simula la API local."""

    def setUp(self):
        monitor.reset_daily_state()
        monitor.LAST_EVENT_ID = 0
        self.addCleanup(monitor.reset_daily_state)
        self.addCleanup(setattr, monitor, "LAST_EVENT_ID", 0)

        self.local_dispatch = None
        self.original_read = monitor.read_local_dispatch
        monitor.read_local_dispatch = lambda: self.local_dispatch
        self.addCleanup(setattr, monitor, "read_local_dispatch", self.original_read)

        self.monitor_ref = [monitor.GeofenceMonitor([])]

    def geofence_ids(self):
        return sorted(g["id"] for g in self.monitor_ref[0].geofences)


class AdoptLocalDispatchTest(AdoptionTestCase):
    def test_adopta_el_itinerario_guardado(self):
        self.local_dispatch = dispatch_body(itinerary_a(), revision=3)

        self.assertTrue(monitor.adopt_local_dispatch(self.monitor_ref))
        self.assertEqual(len(monitor.get_dispatches()), 1)
        self.assertEqual(monitor.get_revision(), 3)

    def test_las_geocercas_cambian_aunque_siga_el_mismo_tramo(self):
        """
        El step sigue siendo el 1, pero sus puntos son otros. Si las geocercas no
        se reemplazan, el bus seguiría vigilando paradas que ya no existen y no
        detectaría la nueva.
        """
        self.local_dispatch = dispatch_body(itinerary_a(), revision=1)
        monitor.adopt_local_dispatch(self.monitor_ref)
        self.assertEqual(self.geofence_ids(), [684, 685, 686])

        self.local_dispatch = dispatch_body(itinerary_b(), revision=2)
        monitor.adopt_local_dispatch(self.monitor_ref)

        self.assertEqual(self.geofence_ids(), [684, 690])
        self.assertNotIn(686, self.geofence_ids())   # la vieja dejó de observarse

    def test_estar_dentro_de_una_geocerca_que_sobrevive_no_se_relee_como_entrada(self):
        """
        Reemplazar el GeofenceMonitor por uno nuevo sería más simple, pero
        olvidaría que el bus YA está dentro de la 684: la siguiente lectura GPS
        la leería como una entrada y dispararía un aviso de llegada repetido.
        """
        self.local_dispatch = dispatch_body(itinerary_a(), revision=1)
        monitor.adopt_local_dispatch(self.monitor_ref)

        # El bus entra en la 684: queda activa.
        geofence_monitor = self.monitor_ref[0]
        geofence_monitor._active[684] = "dentro"

        self.local_dispatch = dispatch_body(itinerary_b(), revision=2)
        monitor.adopt_local_dispatch(self.monitor_ref)

        self.assertEqual(self.monitor_ref[0]._active[684], "dentro")   # sigue dentro
        self.assertIsNone(self.monitor_ref[0]._active[690])            # la nueva, libre

    def test_adoptar_no_borra_la_deduplicacion_del_dia(self):
        """
        `reset_daily_state()` aquí sería un error: vaciaría CONFIRMED_CHECKPOINTS
        y el bus volvería a reportar —y a anunciar— llegadas ya registradas hoy.
        """
        self.local_dispatch = dispatch_body(itinerary_a(), revision=1)
        monitor.adopt_local_dispatch(self.monitor_ref)

        monitor.confirm_checkpoint(3701)
        self.assertIn(3701, monitor.taken_checkpoints())

        self.local_dispatch = dispatch_body(itinerary_b(), revision=2)
        monitor.adopt_local_dispatch(self.monitor_ref)

        self.assertIn(3701, monitor.taken_checkpoints())
        self.assertFalse(monitor.reserve_checkpoint(3701))   # nadie puede reportarlo otra vez

    def test_las_llegadas_del_itinerario_nuevo_se_siembran(self):
        data = itinerary_b()
        data[0]["checkpoints"][0]["time_reported"] = "06:13:40"
        self.local_dispatch = dispatch_body(data, revision=2)

        monitor.adopt_local_dispatch(self.monitor_ref)
        self.assertIn(3701, monitor.taken_checkpoints())

    # ── casos en los que NO se adopta ────────────────────────────────────────

    def test_una_revision_que_no_es_mas_nueva_no_se_adopta(self):
        self.local_dispatch = dispatch_body(itinerary_a(), revision=5)
        self.assertTrue(monitor.adopt_local_dispatch(self.monitor_ref))

        # Misma revisión, y una anterior: ninguna vuelve a aplicarse.
        self.assertFalse(monitor.adopt_local_dispatch(self.monitor_ref))
        self.local_dispatch = dispatch_body(itinerary_b(), revision=4)
        self.assertFalse(monitor.adopt_local_dispatch(self.monitor_ref))
        self.assertEqual(self.geofence_ids(), [684, 685, 686])   # sigue el itinerario A

    def test_un_despacho_de_otro_dia_no_se_adopta(self):
        self.local_dispatch = dispatch_body(itinerary_a(), revision=9, date="2020-01-01")
        self.assertFalse(monitor.adopt_local_dispatch(self.monitor_ref))
        self.assertEqual(monitor.get_dispatches(), [])

    def test_un_despacho_de_otro_bus_no_se_adopta(self):
        self.local_dispatch = dispatch_body(
            itinerary_a(), revision=9, register=monitor.BUS_REGISTER + 1)
        self.assertFalse(monitor.adopt_local_dispatch(self.monitor_ref))

    def test_sin_despacho_local_no_pasa_nada(self):
        self.local_dispatch = None
        self.assertFalse(monitor.adopt_local_dispatch(self.monitor_ref))

    def test_datos_con_forma_inesperada_no_se_adoptan(self):
        self.local_dispatch = dispatch_body({"no": "soy una lista"}, revision=9)
        self.assertFalse(monitor.adopt_local_dispatch(self.monitor_ref))
        self.assertEqual(monitor.get_dispatches(), [])


class EventPollingTest(AdoptionTestCase):
    """
    El monitor detecta la recarga por el canal local de eventos. La primera
    vuelta solo SINCRONIZA: eventos de horas atrás no deben disparar una
    adopción al arrancar el servicio.
    """

    def setUp(self):
        super().setUp()
        self.events = []
        self.requested = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        outer = self

        def fake_get(url, params=None, timeout=None):
            outer.requested.append(params)
            after = (params or {}).get("after_id", 0)
            return FakeResponse([e for e in outer.events if e["id"] > after])

        self.original_get = monitor.requests.get
        monitor.requests.get = fake_get
        self.addCleanup(setattr, monitor.requests, "get", self.original_get)

    def test_la_primera_vuelta_solo_sincroniza(self):
        self.events = [{"id": 7, "event_type": monitor.DISPATCH_REFRESHED_EVENT, "payload": {}}]
        self.local_dispatch = dispatch_body(itinerary_a(), revision=1)

        self.assertFalse(monitor.poll_dispatch_refresh(self.monitor_ref))
        self.assertEqual(monitor.LAST_EVENT_ID, 7)
        self.assertEqual(monitor.get_dispatches(), [])   # no adoptó nada

    def test_un_evento_nuevo_dispara_la_adopcion(self):
        self.events = [{"id": 7, "event_type": monitor.DISPATCH_REFRESHED_EVENT, "payload": {}}]
        monitor.poll_dispatch_refresh(self.monitor_ref)   # sincroniza

        self.events.append({"id": 8, "event_type": monitor.DISPATCH_REFRESHED_EVENT, "payload": {}})
        self.local_dispatch = dispatch_body(itinerary_b(), revision=2)

        self.assertTrue(monitor.poll_dispatch_refresh(self.monitor_ref))
        self.assertEqual(self.geofence_ids(), [684, 690])

    def test_el_polling_es_incremental(self):
        self.events = [{"id": 7, "event_type": monitor.DISPATCH_REFRESHED_EVENT, "payload": {}}]
        monitor.poll_dispatch_refresh(self.monitor_ref)
        monitor.poll_dispatch_refresh(self.monitor_ref)

        self.assertEqual([p["after_id"] for p in self.requested], [0, 7])

    def test_sin_eventos_nuevos_no_hace_nada(self):
        self.events = []
        self.assertFalse(monitor.poll_dispatch_refresh(self.monitor_ref))

    def test_la_api_caida_no_tumba_el_watcher(self):
        def boom(url, params=None, timeout=None):
            raise monitor.requests.RequestException("API local caída")

        monitor.requests.get = boom
        self.assertFalse(monitor.poll_dispatch_refresh(self.monitor_ref))


class StaleCacheGuardTest(unittest.TestCase):
    """
    Una carga del monitor iniciada ANTES de la recarga manual no puede pisarla al
    llegar tarde. El monitor declara la revisión sobre la que se basa y la API
    descarta la escritura si la almacenada es más nueva.
    """

    def setUp(self):
        monitor.reset_daily_state()
        self.addCleanup(monitor.reset_daily_state)
        self.posted = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        outer = self
        self.stored_revision = 1

        def fake_post(url, json=None, timeout=None):
            outer.posted.append(json)
            return FakeResponse({"revision": outer.stored_revision})

        self.original_post = monitor.requests.post
        monitor.requests.post = fake_post
        self.addCleanup(setattr, monitor.requests, "post", self.original_post)

    def test_la_escritura_declara_su_revision_base(self):
        monitor.cache_dispatch_locally(itinerary_a(), TODAY, base_revision=0)
        self.assertEqual(self.posted[0]["base_revision"], 0)

    def test_sin_revision_base_se_escribe_incondicionalmente(self):
        """Compatibilidad: el arranque del monitor no conoce ninguna revisión."""
        monitor.cache_dispatch_locally(itinerary_a(), TODAY)
        self.assertNotIn("base_revision", self.posted[0])

    def test_una_revision_muy_por_delante_significa_escritura_descartada(self):
        """
        Se pidió escribir sobre la revisión 0 y la API devuelve la 7: la recarga
        manual ganó. El monitor NO adopta esa revisión como propia — la tomará
        por el canal de eventos, con los datos correctos.
        """
        self.stored_revision = 7
        monitor.cache_dispatch_locally(itinerary_a(), TODAY, base_revision=0)
        self.assertEqual(monitor.get_revision(), 0)

    def test_una_escritura_aplicada_avanza_la_revision(self):
        self.stored_revision = 3
        monitor.cache_dispatch_locally(itinerary_a(), TODAY, base_revision=2)
        self.assertEqual(monitor.get_revision(), 3)

    def test_la_revision_nunca_retrocede(self):
        self.stored_revision = 5
        monitor.cache_dispatch_locally(itinerary_a(), TODAY, base_revision=4)
        self.stored_revision = 2
        monitor.cache_dispatch_locally(itinerary_a(), TODAY, base_revision=1)
        self.assertEqual(monitor.get_revision(), 5)


if __name__ == "__main__":
    unittest.main()
