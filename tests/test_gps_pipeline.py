"""
Integración de extremo a extremo de la cola GPS, SIN red:

    POST /api/gps (FastAPI real + SQLite temporal)
      → data_loader.sync_once() (código real)
      → ApiService real con una sesión HTTP simulada que imita device-api
      → PATCH local → la fila queda upload = 1

La API local se alcanza por TestClient; el backend remoto es un doble que
aplica el contrato confirmado en buslytics-backend (X-API-Key, 201, 401, 400).
Se salta sin fastapi/sqlalchemy/httpx.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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

import requests

import data_loader
from api import ApiService

ROOT = Path(__file__).resolve().parent.parent
REGISTER = 1624
API_KEY = "bl_dev_pipeline"


class RemoteResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class FakeDeviceApi:
    """Sesión HTTP que imita device-api. `mode` fuerza fallos."""

    def __init__(self):
        self.received = []
        self.mode = "ok"

    def request(self, method, url, json=None, headers=None, timeout=None, params=None):
        assert timeout, "toda llamada remota lleva timeout"
        assert "Authorization" not in headers
        if self.mode == "down":
            raise requests.ConnectionError("sin señal")
        if headers.get("X-API-Key") != API_KEY:
            return RemoteResponse(401, {"statusCode": 401, "message": "API key inválida"})
        if url != f"https://remoto.example.com/api/device-api/gps/{REGISTER}" or method != "POST":
            return RemoteResponse(404)
        allowed = {"timestamp", "latitude", "longitude", "speed"}
        if set(json) - allowed or not isinstance(json.get("timestamp"), int):
            return RemoteResponse(400, {"message": ["forbidNonWhitelisted / timestamp"]})
        if self.mode == "reject":
            return RemoteResponse(400, {"message": "Coordenadas inválidas"})
        self.received.append(json)
        return RemoteResponse(201, {"statusCode": 201, "message": "Punto registrado correctamente",
                                    "result": {"id": len(self.received)}})


class LocalRequests:
    """Sustituye al módulo `requests` de data_loader: lleva la API local al TestClient."""

    RequestException = requests.RequestException

    def __init__(self, client, prefix):
        self.client = client
        self.prefix = prefix
        self.fail_marks = False

    def _path(self, url):
        return url.replace(self.prefix, "")

    def get(self, url, params=None, timeout=None):
        return self.client.get(self._path(url), params=params)

    def request(self, method, url, json=None, timeout=None):
        if self.fail_marks:
            raise requests.ConnectionError("API local caída")
        return self.client.request(method, self._path(url), json=json)


@unittest.skipUnless(DEPS_OK, f"faltan fastapi/sqlalchemy/httpx ({MISSING})")
class GpsPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="simtra-pipeline-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)

        for name in ("main", "crud", "models", "database", "schemas"):
            sys.modules.pop(name, None)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        import main
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

        self.remote = FakeDeviceApi()
        service = ApiService("https://remoto.example.com", API_KEY)
        service._session = self.remote
        self.local = LocalRequests(self.client, data_loader.LOCAL_BACKEND)

        for name, value in {"simtra": service, "requests": self.local, "BUS_REGISTER": REGISTER}.items():
            previous = getattr(data_loader, name)
            setattr(data_loader, name, value)
            self.addCleanup(setattr, data_loader, name, previous)
        data_loader._sent_not_marked.clear()
        self.addCleanup(data_loader._sent_not_marked.clear)

    def post_gps(self, longitude, timestamp, speed=10.0):
        return self.client.post("/api/gps", json={
            "latitude": -4.0, "longitude": longitude, "speed": speed, "timestamp": timestamp,
        }).json()

    def rows(self):
        return {r["id"]: r for r in self.client.get("/api/gps").json()}

    def seed(self):
        self.post_gps(-79.0, "2026-08-25T12:00:00Z")
        self.post_gps(-79.00001, "2026-08-25T12:00:01Z")            # ~1 m: se guarda igual
        self.post_gps(-79.0001, "2026-08-25T07:00:02")              # sin zona = GYE
        self.post_gps(-79.0002, "2026-08-25T12:00:03+00:00", speed=0)  # detenido: se guarda igual

    def test_subida_exitosa_marca_en_sqlite(self):
        self.seed()

        self.assertTrue(data_loader.sync_once())

        base = int(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual([p["timestamp"] for p in self.remote.received],
                         [base, base + 1, base + 2, base + 3])
        self.assertEqual(self.remote.received[0],
                         {"timestamp": base, "latitude": -4.0, "longitude": -79.0, "speed": 10.0})
        self.assertEqual(self.client.get("/api/gps/pending").json(), [])
        self.assertTrue(all(r["upload"] for r in self.rows().values()))

    def test_fallo_remoto_conserva_pendientes(self):
        self.seed()
        for mode in ("down",):
            self.remote.mode = mode
            self.assertFalse(data_loader.sync_once())
        self.assertEqual(len(self.client.get("/api/gps/pending").json()), 4)

        # Vuelve la red: se suben en orden.
        self.remote.mode = "ok"
        data_loader.sync_once()
        self.assertEqual(len(self.remote.received), 4)
        self.assertEqual(self.client.get("/api/gps/pending").json(), [])

    def test_api_key_rechazada_conserva_pendientes(self):
        self.seed()
        data_loader.simtra.device_api_key = "otra-clave"
        self.assertFalse(data_loader.sync_once())
        self.assertEqual(len(self.client.get("/api/gps/pending").json()), 4)
        self.assertEqual(self.remote.received, [])

    def test_rechazo_400_saca_el_punto_sin_marcarlo_subido(self):
        self.seed()
        self.remote.mode = "reject"
        data_loader.sync_once()
        rows = self.rows()
        self.assertEqual(self.client.get("/api/gps/pending").json(), [])
        self.assertTrue(all(not r["upload"] and "HTTP 400" in r["upload_error"] for r in rows.values()))

    def test_fallo_al_marcar_local_no_reenvia_y_se_recupera(self):
        self.seed()
        self.local.fail_marks = True
        data_loader.sync_once()
        self.assertEqual(len(self.remote.received), 4)
        self.assertEqual(len(self.client.get("/api/gps/pending").json()), 4)

        self.local.fail_marks = False
        data_loader.sync_once()

        self.assertEqual(len(self.remote.received), 4)   # sin reenvío
        self.assertEqual(self.client.get("/api/gps/pending").json(), [])


if __name__ == "__main__":
    unittest.main()
