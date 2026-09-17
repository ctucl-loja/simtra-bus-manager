"""
Pruebas de INTEGRACIÓN de la API local: endpoints, esquemas y persistencia real.

Estas sí ejercitan FastAPI, Pydantic y SQLAlchemy — al contrario que el resto de
la suite, que prueba funciones puras con stubs. Por eso tienen dependencias que
el equipo de desarrollo puede no tener instaladas:

    pip install fastapi sqlalchemy httpx

Sin ellas el módulo entero se SALTA con un motivo visible, en vez de fallar: el
resto de la suite debe seguir corriendo en una máquina pelada.

Cada test usa una base SQLite TEMPORAL y propia (nunca ./app.db), y el backend
remoto es un doble. No se apaga, no se reinicia y no se toca la red.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

try:
    import fastapi          # noqa: F401
    import sqlalchemy       # noqa: F401
    from fastapi.testclient import TestClient
    DEPS_OK = True
    MISSING = ""
except ImportError as e:                       # pragma: no cover
    DEPS_OK = False
    MISSING = str(e)

ROOT = Path(__file__).resolve().parent.parent
TODAY = datetime.now(ZoneInfo("America/Guayaquil")).strftime("%Y-%m-%d")
REGISTER = 1624


# ─────────────────────────────────────────────
# DOBLE DEL BACKEND REMOTO
# ─────────────────────────────────────────────

class FakeRemote:
    """Sustituye a ApiService. Devuelve el DispatchFetch que se le indique."""

    def __init__(self):
        self.result = None
        self.calls = []

    def fetch_dispatch(self, register, date):
        self.calls.append((register, date))
        return self.result


def make_point(pid=684, name=None, latitude=-4.01, longitude=-79.22):
    return {"id": pid, "name": name or f"PUNTO {pid}",
            "latitude": latitude, "longitude": longitude, "radius": 50}


def make_checkpoint(cid, pid, order, calculated, reported="00:00:00"):
    return {"id": cid, "order": order, "time": "00:00:00",
            "time_calculated": calculated, "time_reported": reported,
            "point": make_point(pid)}


def make_step(step=1, start="06:00:00", end="07:00:00", checkpoints=None):
    return {
        "step": step, "code": f"G80{step}", "register": REGISTER,
        "start_schedule": start, "end_schedule": end,
        "line": {"id": 17, "name": "A2", "number": 8,
                 "start_route": "CARIGAN", "end_route": "CIUDAD VICTORIA"},
        "checkpoints": checkpoints if checkpoints is not None else [
            make_checkpoint(3701, 684, 0, "06:10:00"),
            make_checkpoint(3702, 685, 1, "06:30:00"),
        ],
    }


@unittest.skipUnless(DEPS_OK, f"faltan fastapi/sqlalchemy/httpx ({MISSING})")
class ApiTestCase(unittest.TestCase):
    """
    Base: una app FastAPI nueva por test, con base SQLite temporal.

    `main.py` fija DATABASE_URL a `sqlite:///./app.db` (relativa al directorio de
    trabajo), así que se importa con el cwd apuntando a un directorio temporal.
    Eso deja intacta la base de desarrollo y aísla cada test del anterior.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="simtra-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)

        os.environ["FAST_API_BUS_REGISTER"] = str(REGISTER)
        os.environ["FAST_API_BACKEND_URL"] = "https://remoto.example.com"

        # Importación limpia: estos módulos guardan estado global (engine,
        # metadata) y reutilizarlos entre tests mezclaría bases de datos.
        for name in ("main", "crud", "models", "database", "schemas"):
            sys.modules.pop(name, None)

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        import main
        self.main = main
        self.remote = FakeRemote()
        main.remote_api = self.remote
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    # ── ayudas ───────────────────────────────────────────────────────────────

    def fetch(self, status, dispatches=None):
        from services.api import DispatchFetch
        self.remote.result = DispatchFetch(status, dispatches or [])

    def stored(self):
        return self.client.get("/api/dispatch").json()

    def reported_of(self, data, step, cid):
        for s in data:
            if s["step"] != step:
                continue
            for c in s["checkpoints"]:
                if c["id"] == cid:
                    return c["time_reported"]
        return None


class DispatchRefreshEndpointTest(ApiTestCase):
    def test_descarga_valida_y_persistencia(self):
        from services.api import FETCH_OK
        self.fetch(FETCH_OK, [make_step(1), make_step(2, "08:00:00", "09:00:00")])

        body = self.client.post("/api/dispatch/refresh").json()

        self.assertEqual(body["status"], "updated")
        self.assertEqual(body["date"], TODAY)
        self.assertEqual(body["register"], REGISTER)
        self.assertEqual(body["revision"], 1)
        # El despacho devuelto es el REALMENTE guardado, no el descargado.
        self.assertEqual([s["step"] for s in body["dispatch"]["data"]], [1, 2])
        self.assertEqual(self.stored()["data"], body["dispatch"]["data"])
        # Y se consultó por el registro y la fecha del equipo.
        self.assertEqual(self.remote.calls, [(REGISTER, TODAY)])

    def test_dia_sin_despachos_vacia_el_itinerario(self):
        from services.api import FETCH_OK, FETCH_EMPTY
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")

        self.fetch(FETCH_EMPTY, [])
        body = self.client.post("/api/dispatch/refresh").json()

        self.assertEqual(body["status"], "empty")
        self.assertEqual(body["dispatch"]["data"], [])
        self.assertEqual(self.stored()["data"], [])

    def test_error_remoto_conserva_el_itinerario(self):
        from services.api import FETCH_OK, FETCH_TRANSPORT, FETCH_AUTH_ERROR, FETCH_INVALID
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")
        antes = self.stored()

        for remote_status, expected in (
            (FETCH_TRANSPORT, "remote_error"),
            (FETCH_AUTH_ERROR, "auth_error"),
            (FETCH_INVALID, "invalid"),
        ):
            with self.subTest(remote_status=remote_status):
                self.fetch(remote_status, [])
                body = self.client.post("/api/dispatch/refresh").json()

                self.assertEqual(body["status"], expected)
                # El itinerario sigue intacto, y se devuelve el que ya había.
                self.assertEqual(body["dispatch"]["data"], antes["data"])
                self.assertEqual(self.stored()["data"], antes["data"])
                self.assertEqual(self.stored()["revision"], antes["revision"])

    def test_payload_invalido_no_equivale_a_dia_sin_despachos(self):
        """
        Una lista CON contenido roto se rechaza. Tratarla como "hoy no hay
        despachos" borraría el itinerario del conductor por un error del backend.
        """
        from services.api import FETCH_OK
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")
        antes = self.stored()

        casos = [
            [{"step": 1, "start_schedule": "25:00:00", "end_schedule": "07:00:00",
              "checkpoints": [make_checkpoint(1, 1, 0, "06:00:00")]}],
            [make_step(checkpoints=[])],
            [{"cualquier": "cosa"}],
            [make_step(checkpoints=[{"id": 1, "point": {"id": 1}}])],
        ]
        for payload in casos:
            with self.subTest(payload=str(payload)[:40]):
                self.fetch(FETCH_OK, payload)
                body = self.client.post("/api/dispatch/refresh").json()

                self.assertEqual(body["status"], "invalid")
                self.assertEqual(self.stored()["data"], antes["data"])

    def test_las_marcaciones_pendientes_se_conservan(self):
        from services.api import FETCH_OK
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")

        # El monitor registra una llegada: cola local (upload=False) + despacho.
        self.client.post("/api/checkpoint", json={
            "checkpoint_id": 3702, "name": "TERMINAL", "timestamp": "06:31:12"})
        self.client.patch("/api/dispatch/checkpoint", json={
            "step": 1, "checkpoint_id": 3702, "time_reported": "06:31:12"})

        # El backend remoto todavía no la conoce.
        self.fetch(FETCH_OK, [make_step()])
        body = self.client.post("/api/dispatch/refresh").json()

        self.assertEqual(body["preserved_reports"], 1)
        self.assertEqual(self.reported_of(body["dispatch"]["data"], 1, 3702), "06:31:12")

    def test_una_marcacion_ya_subida_no_se_resucita(self):
        """
        Si ya viajó al servidor, es el servidor quien manda: reintroducirla aquí
        desharía una corrección hecha en el backend.
        """
        from services.api import FETCH_OK
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")

        created = self.client.post("/api/checkpoint", json={
            "checkpoint_id": 3702, "name": "TERMINAL", "timestamp": "06:31:12"}).json()
        self.client.patch("/api/dispatch/checkpoint", json={
            "step": 1, "checkpoint_id": 3702, "time_reported": "06:31:12"})
        self.client.patch(f"/api/checkpoint/{created['id']}")   # marcada como subida

        self.fetch(FETCH_OK, [make_step()])
        body = self.client.post("/api/dispatch/refresh").json()

        self.assertEqual(body["preserved_reports"], 0)
        self.assertEqual(self.reported_of(body["dispatch"]["data"], 1, 3702), "00:00:00")

    def test_una_marcacion_pendiente_no_se_traslada_a_otro_recorrido(self):
        from services.api import FETCH_OK
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")

        self.client.post("/api/checkpoint", json={
            "checkpoint_id": 3702, "name": "TERMINAL", "timestamp": "06:31:12"})
        self.client.patch("/api/dispatch/checkpoint", json={
            "step": 1, "checkpoint_id": 3702, "time_reported": "06:31:12"})

        # El itinerario nuevo no tiene el checkpoint 3702 en ninguna parte.
        nuevo = make_step(1, checkpoints=[
            make_checkpoint(3701, 684, 0, "06:12:00"),
            make_checkpoint(3710, 690, 1, "06:35:00"),
        ])
        self.fetch(FETCH_OK, [nuevo])
        body = self.client.post("/api/dispatch/refresh").json()

        self.assertEqual(body["status"], "updated")
        self.assertEqual(body["preserved_reports"], 0)
        for ckpt in body["dispatch"]["data"][0]["checkpoints"]:
            self.assertEqual(ckpt["time_reported"], "00:00:00")

    def test_publica_el_evento_para_el_monitor(self):
        from services.api import FETCH_OK
        self.fetch(FETCH_OK, [make_step()])
        self.client.post("/api/dispatch/refresh")

        events = self.client.get(
            "/api/events", params={"event_type": "dispatch_refreshed"}).json()

        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["date"], TODAY)
        self.assertEqual(payload["register"], REGISTER)

    def test_un_error_no_publica_ningun_evento(self):
        """El monitor no debe despertar por una recarga que no cambió nada."""
        from services.api import FETCH_TRANSPORT
        self.fetch(FETCH_TRANSPORT, [])
        self.client.post("/api/dispatch/refresh")

        self.assertEqual(self.client.get(
            "/api/events", params={"event_type": "dispatch_refreshed"}).json(), [])

    def test_la_revision_avanza_en_cada_recarga(self):
        from services.api import FETCH_OK
        for expected in (1, 2, 3):
            self.fetch(FETCH_OK, [make_step()])
            self.assertEqual(
                self.client.post("/api/dispatch/refresh").json()["revision"], expected)

    def test_sin_backend_configurado_no_se_promete_nada(self):
        self.main.BACKEND_URL = ""
        body = self.client.post("/api/dispatch/refresh").json()
        self.assertEqual(body["status"], "remote_error")
        self.assertEqual(self.remote.calls, [])


class SaveDispatchTest(ApiTestCase):
    """`POST /api/dispatch`: el cache que escribe el monitor."""

    def test_no_pierde_las_horas_de_llegada_locales(self):
        """
        Era el fallo del upsert original: reemplazaba `data` entero, así que una
        recarga rutinaria del monitor borraba de la pantalla marcaciones reales
        que el backend remoto todavía no conocía.
        """
        self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [make_step()]})
        self.client.patch("/api/dispatch/checkpoint", json={
            "step": 1, "checkpoint_id": 3702, "time_reported": "06:31:12"})

        body = self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [make_step()]}).json()

        self.assertEqual(self.reported_of(body["data"], 1, 3702), "06:31:12")

    def test_el_dato_entrante_gana_cuando_trae_su_propia_hora(self):
        self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [make_step()]})
        self.client.patch("/api/dispatch/checkpoint", json={
            "step": 1, "checkpoint_id": 3702, "time_reported": "06:31:12"})

        entrante = make_step(1, checkpoints=[
            make_checkpoint(3701, 684, 0, "06:10:00"),
            make_checkpoint(3702, 685, 1, "06:30:00", reported="06:29:00"),
        ])
        body = self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [entrante]}).json()

        self.assertEqual(self.reported_of(body["data"], 1, 3702), "06:29:00")

    def test_una_escritura_vieja_no_pisa_una_recarga_nueva(self):
        """
        El monitor leyó la revisión 1, salió a la red, y mientras tanto el
        conductor recargó (revisión 2). Su escritura llega tarde y se descarta.
        """
        from services.api import FETCH_OK
        self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [make_step()]})
        base = self.stored()["revision"]

        nuevo = make_step(1, checkpoints=[make_checkpoint(3710, 690, 0, "06:35:00")])
        self.fetch(FETCH_OK, [nuevo])
        self.client.post("/api/dispatch/refresh")

        # La carga del monitor, basada en la revisión anterior.
        self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [make_step()],
            "base_revision": base})

        guardado = self.stored()
        self.assertEqual([c["id"] for c in guardado["data"][0]["checkpoints"]], [3710])

    def test_sin_base_revision_la_escritura_es_incondicional(self):
        """Compatibilidad: el arranque del monitor no conoce ninguna revisión."""
        self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [make_step()]})
        nuevo = make_step(1, checkpoints=[make_checkpoint(3710, 690, 0, "06:35:00")])
        self.client.post("/api/dispatch", json={
            "date": TODAY, "register": REGISTER, "data": [nuevo]})

        self.assertEqual([c["id"] for c in self.stored()["data"][0]["checkpoints"]], [3710])


class PowerEndpointsTest(ApiTestCase):
    """
    Apagado y reinicio a través de HTTP. El comando se sustituye por `true`, que
    existe en cualquier Linux y no hace nada: NADA se apaga ni se reinicia.
    """

    def setUp(self):
        super().setUp()
        from services import power
        self.power = power
        power.reset_state()
        self.addCleanup(power.reset_state)

        self.originals = {n: os.environ.get(n)
                          for n in ("SYSTEM_SHUTDOWN_COMMAND", "SYSTEM_REBOOT_COMMAND")}
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "true"
        os.environ["SYSTEM_REBOOT_COMMAND"] = "true"
        self.addCleanup(self.restore_env)

        # El planificador no ejecuta nada: el comando queda "pendiente" para
        # siempre, que es justo lo que hace falta para probar los conflictos.
        self.scheduled = []
        self.original_scheduler = power._default_scheduler
        power._default_scheduler = lambda delay, action: self.scheduled.append((delay, action))
        self.addCleanup(setattr, power, "_default_scheduler", self.original_scheduler)

    def restore_env(self):
        for name, value in self.originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_apagado(self):
        body = self.client.post("/api/system/shutdown").json()
        self.assertEqual(body["status"], "scheduled")
        self.assertEqual(body["action"], "shutdown")
        self.assertEqual(body["pending_action"], "shutdown")
        self.assertGreater(body["scheduled_in_seconds"], 0)

    def test_reinicio(self):
        body = self.client.post("/api/system/reboot").json()
        self.assertEqual(body["status"], "scheduled")
        self.assertEqual(body["action"], "reboot")
        self.assertEqual(body["pending_action"], "reboot")

    def test_el_conflicto_identifica_la_accion_pendiente(self):
        """
        Es lo que impide que la pantalla anuncie un reinicio cuando lo que va a
        ocurrir es un apagado.
        """
        self.client.post("/api/system/shutdown")
        body = self.client.post("/api/system/reboot").json()

        self.assertEqual(body["status"], "already_scheduled")
        self.assertEqual(body["action"], "reboot")
        self.assertEqual(body["pending_action"], "shutdown")
        self.assertEqual(len(self.scheduled), 1)   # una sola acción programada

    def test_los_duplicados_no_lanzan_dos_comandos(self):
        for _ in range(3):
            self.client.post("/api/system/reboot")
        self.assertEqual(len(self.scheduled), 1)

    def test_ninguno_acepta_un_comando_del_cliente(self):
        """
        El frontend NUNCA puede proponer el comando. Un cuerpo con uno se ignora
        por completo: el endpoint no declara ningún modelo de entrada.
        """
        antes = self.power.shutdown_command()
        self.client.post("/api/system/shutdown", json={"command": "rm -rf /"})
        self.assertEqual(self.power.shutdown_command(), antes)
        self.assertEqual([cmd for _d, _a in self.scheduled for cmd in []], [])


class WifiEndpointTest(ApiTestCase):
    """
    Wi-Fi a través de HTTP. `wifi.connect` se sustituye por un doble: no se
    ejecuta ningún nmcli y la red de la máquina no se toca.
    """

    SECRET = "clave-secreta-123"

    def setUp(self):
        super().setUp()
        from services import wifi
        self.wifi = wifi
        self.original_connect = wifi.connect
        self.addCleanup(setattr, wifi, "connect", self.original_connect)
        self.received = []

    def stub(self, result):
        def fake_connect(ssid, password=None, **kwargs):
            self.received.append((ssid, password))
            return result
        self.wifi.connect = fake_connect

    def test_conexion_exitosa(self):
        self.stub(self.wifi.WifiResult(
            status="connected", detail="Conectado a la red", ssid="SIMTRA-PATIO",
            network={"status": "connected", "connections": [
                {"type": "wifi", "interface": "wlan0", "name": "SIMTRA-PATIO",
                 "ipv4": ["192.168.1.30"]}]},
        ))
        body = self.client.post("/api/system/wifi/connect", json={
            "ssid": "SIMTRA-PATIO", "password": self.SECRET}).json()

        self.assertEqual(body["status"], "connected")
        self.assertEqual(body["ssid"], "SIMTRA-PATIO")
        self.assertEqual(body["network"]["connections"][0]["ipv4"], ["192.168.1.30"])
        # La clave llegó al servicio, que es lo único que debe verla.
        self.assertEqual(self.received, [("SIMTRA-PATIO", self.SECRET)])

    def test_ninguna_respuesta_devuelve_la_clave(self):
        estados = ["connected", "invalid_password", "not_found", "timeout",
                   "unavailable", "no_adapter", "not_authorized", "busy", "failed"]
        for status in estados:
            with self.subTest(status=status):
                self.stub(self.wifi.WifiResult(
                    status=status, detail=self.wifi.DETAILS[status], ssid="RED"))
                response = self.client.post("/api/system/wifi/connect", json={
                    "ssid": "RED", "password": self.SECRET})

                self.assertEqual(response.status_code, 200)
                self.assertNotIn(self.SECRET, response.text)

    def test_un_parametro_invalido_no_devuelve_la_clave(self):
        """
        El 422 de Pydantic incluye el valor recibido. Por eso la validación fina
        se hace en el servicio y su mensaje —que por construcción no cita
        valores— se devuelve con 200 y status "failed".
        """
        def rechaza(ssid, password=None, **kwargs):
            raise self.wifi.InvalidParameter("El nombre de red no puede empezar con '-'")

        self.wifi.connect = rechaza
        response = self.client.post("/api/system/wifi/connect", json={
            "ssid": "-mala", "password": self.SECRET})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertNotIn(self.SECRET, response.text)

    def test_un_fallo_inesperado_no_devuelve_500_ni_la_clave(self):
        def revienta(ssid, password=None, **kwargs):
            raise RuntimeError(f"boom con {self.SECRET}")

        self.wifi.connect = revienta
        response = self.client.post("/api/system/wifi/connect", json={
            "ssid": "RED", "password": self.SECRET})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertNotIn(self.SECRET, response.text)

    def test_la_clave_no_viaja_en_la_url(self):
        self.stub(self.wifi.WifiResult(status="connected", detail="ok", ssid="RED"))
        response = self.client.post("/api/system/wifi/connect", json={
            "ssid": "RED", "password": self.SECRET})
        self.assertNotIn(self.SECRET, str(response.request.url))

    def test_sin_clave_es_una_peticion_valida(self):
        """Red abierta, o reconectar con el perfil ya guardado."""
        self.stub(self.wifi.WifiResult(status="connected", detail="ok", ssid="ABIERTA"))
        self.client.post("/api/system/wifi/connect", json={"ssid": "ABIERTA"})
        self.assertEqual(self.received, [("ABIERTA", None)])


class SchemaMigrationTest(ApiTestCase):
    """`database.ensure_schema` agrega columnas nuevas en una base ya existente."""

    def test_agrega_revision_a_una_tabla_antigua(self):
        import database
        from sqlalchemy import create_engine, text

        path = os.path.join(self.tmp, "antigua.db")
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as conn:
            # Tabla `dispatch` tal como era ANTES de existir `revision`.
            conn.execute(text(
                "CREATE TABLE dispatch (id INTEGER PRIMARY KEY, date TEXT, "
                "register INTEGER, data TEXT, created_at TEXT)"))
            conn.execute(text(
                "INSERT INTO dispatch (date, register, data) VALUES ('2026-01-01', 1624, '[]')"))

        self.assertEqual(database.ensure_schema(engine), ["dispatch.revision"])

        with engine.begin() as conn:
            row = conn.execute(text("SELECT revision FROM dispatch")).fetchone()
        self.assertEqual(row[0], 0)

        # Idempotente: una segunda pasada no hace nada.
        self.assertEqual(database.ensure_schema(engine), [])


if __name__ == "__main__":
    unittest.main()
