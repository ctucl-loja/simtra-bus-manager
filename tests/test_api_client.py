"""
Cliente del backend remoto: solo device-api con X-API-Key.

Rutas, métodos, cabeceras y códigos verificados contra
buslytics-backend/src/modules/device-api/device-api.controller.ts (prefijo
global `api`). No hay login, JWT ni rutas antiguas: si alguna prueba ve una
cabecera Authorization o una URL fuera de /api/device-api, la migración se
rompió.
"""

import logging
import unittest

import _bootstrap  # noqa: F401

import requests

from api import (
    ApiService, DEFAULT_TIMEOUT,
    FETCH_OK, FETCH_EMPTY, FETCH_AUTH_ERROR, FETCH_CONFIG_ERROR, FETCH_TRANSPORT, FETCH_INVALID,
    SEND_OK, SEND_CONFLICT, SEND_REJECTED, SEND_AUTH_ERROR, SEND_CONFIG_ERROR,
    SEND_NOT_FOUND, SEND_RETRY,
)

API_KEY = "bl_dev_SECRETA_de_prueba"
BASE = "https://api.example.com"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_json=False, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json
        self.text = text

    def json(self):
        if self._raise_json:
            raise ValueError("no es JSON")
        return self._payload


class FakeSession:
    """Registra cada llamada; devuelve respuestas de una cola."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def request(self, method, url, json=None, headers=None, timeout=None, params=None):
        self.requests.append({"method": method, "url": url, "json": json,
                              "headers": headers, "timeout": timeout, "params": params})
        if not self.responses:
            raise AssertionError(f"llamada inesperada: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, *args, **kwargs):              # pragma: no cover
        raise AssertionError("device-api no hace login: no debe haber POST de autenticación")


def build(responses=None, api_key=API_KEY, url=BASE):
    service = ApiService(url, api_key)
    session = FakeSession(responses)
    service._session = session
    return service, session


def no_dispatch_404(register=1624, date="2026-08-25"):
    """Cuerpo real de AllExceptionsFilter para getByRegisterAndDate sin filas."""
    return FakeResponse(404, {
        "statusCode": 404,
        "message": f"No dispatch found for vehicle {register} on date {date}",
        "error": "Not Found",
        "timestamp": "2026-08-25T12:00:00.000Z",
        "path": f"/api/device-api/dispatch/{register}?date={date}",
    })


class ConstructorTest(unittest.TestCase):
    def test_solo_url_y_api_key(self):
        service = ApiService(BASE + "/", API_KEY)
        self.assertEqual(service.api_url, BASE)
        self.assertEqual(service.device_api_key, API_KEY)
        self.assertFalse(hasattr(service, "jwt"))
        self.assertFalse(hasattr(service, "get_jwt"))

    def test_no_hace_red_al_construir(self):
        service, session = build()
        self.assertEqual(session.requests, [])

    def test_sin_url_ni_clave_no_lanza_ni_toca_la_red(self):
        for url, key, expected in ((None, API_KEY, "FAST_API_BACKEND_URL"),
                                   (BASE, None, "FAST_API_DEVICE_API_KEY"),
                                   (BASE, "   ", "FAST_API_DEVICE_API_KEY")):
            with self.subTest(url=url, key=key):
                service, session = build(api_key=key, url=url)
                self.assertIn(expected, service.configuration_error())
                self.assertEqual(service.fetch_dispatch(1, "2026-08-25").status, FETCH_CONFIG_ERROR)
                self.assertEqual(service.post_gps({}, 1).status, SEND_CONFIG_ERROR)
                self.assertEqual(service.post_passenger({}).status, SEND_CONFIG_ERROR)
                self.assertEqual(service.update_dispatch({}, 1).status, SEND_CONFIG_ERROR)
                self.assertEqual(session.requests, [])


class DeviceApiRoutesTest(unittest.TestCase):
    """Ruta, método, cabeceras y cuerpo de cada operación."""

    def assert_device_call(self, call, method, path):
        self.assertEqual(call["method"], method)
        self.assertEqual(call["url"], BASE + path)
        self.assertEqual(call["headers"]["X-API-Key"], API_KEY)
        self.assertNotIn("Authorization", call["headers"])
        self.assertEqual(call["timeout"], DEFAULT_TIMEOUT)
        self.assertNotIn(API_KEY, call["url"])

    def test_fetch_dispatch(self):
        service, session = build([FakeResponse(200, {"result": [{"step": 1}]})])
        service.fetch_dispatch(1624, "2026-08-25")
        call = session.requests[0]
        self.assert_device_call(call, "GET", "/api/device-api/dispatch/1624")
        self.assertEqual(call["params"], {"date": "2026-08-25"})

    def test_post_gps(self):
        body = {"timestamp": 1787659200, "latitude": -4.0, "longitude": -79.0, "speed": 8.0}
        service, session = build([FakeResponse(201)])
        self.assertTrue(service.post_gps(body, 1624))
        self.assert_device_call(session.requests[0], "POST", "/api/device-api/gps/1624")
        self.assertEqual(session.requests[0]["json"], body)

    def test_post_passenger(self):
        body = {"register": 1624, "timestamp": "2026-08-25T07:00:00-05:00",
                "latitude": -4.0, "longitude": -79.0}
        service, session = build([FakeResponse(201)])
        self.assertTrue(service.post_passenger(body))
        self.assert_device_call(session.requests[0], "POST", "/api/device-api/passenger")

    def test_update_dispatch_lleva_register_en_la_ruta(self):
        body = {"id": 3701, "time_reported": "06:10:00"}
        service, session = build([FakeResponse(200)])
        self.assertTrue(service.update_dispatch(body, 1624))
        self.assert_device_call(session.requests[0], "PATCH", "/api/device-api/dispatch/1624")
        self.assertEqual(session.requests[0]["json"], body)

    def test_get_vehicle_bloqueado_sin_red(self):
        """device-api no tiene ruta de vehículo: no se inventa ni se usa la antigua."""
        service, session = build()
        self.assertIsNone(service.get_vehicle(1624))
        self.assertIsNone(service.get_vehicle(1624))
        self.assertEqual(session.requests, [])


class SendResultTest(unittest.TestCase):
    def check(self, method, status, expected, ok_status):
        service, _ = build([FakeResponse(status)])
        result = method(service)
        self.assertEqual(result.status, expected, f"HTTP {status}")
        self.assertEqual(bool(result), expected == SEND_OK)
        if status != ok_status:
            self.assertEqual(result.http_status, status)

    def test_post_gps_solo_ok_con_201(self):
        cases = {201: SEND_OK, 200: SEND_RETRY, 400: SEND_REJECTED, 401: SEND_AUTH_ERROR,
                 403: SEND_AUTH_ERROR, 404: SEND_NOT_FOUND, 429: SEND_RETRY, 500: SEND_RETRY,
                 503: SEND_RETRY}
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.check(lambda s: s.post_gps({"timestamp": 1}, 1624), status, expected, 201)

    def test_post_passenger_solo_ok_con_201(self):
        for status, expected in {201: SEND_OK, 200: SEND_RETRY, 400: SEND_REJECTED,
                                 401: SEND_AUTH_ERROR, 500: SEND_RETRY}.items():
            with self.subTest(status=status):
                self.check(lambda s: s.post_passenger({"a": 1}), status, expected, 201)

    def test_update_dispatch_200_ok_409_conflicto(self):
        for status, expected in {200: SEND_OK, 201: SEND_RETRY, 409: SEND_CONFLICT,
                                 404: SEND_NOT_FOUND, 403: SEND_AUTH_ERROR, 500: SEND_RETRY}.items():
            with self.subTest(status=status):
                self.check(lambda s: s.update_dispatch({"id": 1}, 1624), status, expected, 200)

    def test_errores_de_red_no_se_propagan(self):
        for error in (requests.ConnectionError("sin señal"), requests.Timeout("lento")):
            with self.subTest(error=type(error).__name__):
                service, _ = build([error])
                result = service.post_gps({}, 1624)
                self.assertEqual(result.status, SEND_RETRY)
                self.assertTrue(result.stops_queue)

    def test_stops_queue(self):
        service, _ = build([FakeResponse(400), FakeResponse(404), FakeResponse(401)])
        self.assertFalse(service.post_gps({}, 1).stops_queue)
        self.assertFalse(service.post_gps({}, 1).stops_queue)
        self.assertTrue(service.post_gps({}, 1).stops_queue)


class FetchDispatchTest(unittest.TestCase):
    """Un día sin despachos vacía el itinerario; un error NO debe tocarlo."""

    def test_respuesta_con_despachos(self):
        service, _ = build([FakeResponse(200, {"statusCode": 200, "result": [{"step": 1}]})])
        result = service.fetch_dispatch(1624, "2026-08-25")
        self.assertEqual(result.status, FETCH_OK)
        self.assertEqual(result.dispatches, [{"step": 1}])
        self.assertTrue(result.ok)

    def test_404_no_dispatch_found_es_dia_vacio(self):
        """Contrato real: getByRegisterAndDate lanza NotFound si no hay filas."""
        service, _ = build([no_dispatch_404()])
        result = service.fetch_dispatch(1624, "2026-08-25")
        self.assertEqual(result.status, FETCH_EMPTY)
        self.assertTrue(result.ok)
        self.assertEqual(result.dispatches, [])

    def test_200_con_lista_vacia_tambien_es_dia_vacio(self):
        service, _ = build([FakeResponse(200, {"result": []})])
        self.assertEqual(service.fetch_dispatch(1624, "2026-08-25").status, FETCH_EMPTY)

    def test_otro_404_no_es_dia_vacio(self):
        casos = [
            FakeResponse(404, {"statusCode": 404, "message": "Cannot GET /api/device-api/dispatch/1624",
                               "error": "Not Found"}),
            FakeResponse(404, raise_json=True, text="<html>"),
            FakeResponse(404, {"message": ["x"]}),
        ]
        for response in casos:
            with self.subTest(payload=response._payload):
                service, _ = build([response])
                self.assertEqual(service.fetch_dispatch(1624, "2026-08-25").status, FETCH_TRANSPORT)

    def test_fallo_de_red_no_es_un_dia_vacio(self):
        service, _ = build([requests.ConnectionError("sin señal")])
        result = service.fetch_dispatch(1624, "2026-08-25")
        self.assertEqual(result.status, FETCH_TRANSPORT)
        self.assertFalse(result.ok)

    def test_api_key_rechazada_o_bus_ajeno(self):
        for status in (401, 403):
            with self.subTest(status=status):
                service, session = build([FakeResponse(status)])
                self.assertEqual(service.fetch_dispatch(1624, "2026-08-25").status, FETCH_AUTH_ERROR)
                self.assertEqual(len(session.requests), 1)   # sin reintento ni login

    def test_error_del_servidor_es_de_transporte(self):
        for code in (500, 502, 429):
            with self.subTest(code=code):
                service, _ = build([FakeResponse(code)])
                self.assertEqual(service.fetch_dispatch(1624, "2026-08-25").status, FETCH_TRANSPORT)

    def test_cuerpo_inutilizable_es_respuesta_invalida(self):
        casos = [
            FakeResponse(200, {"data": []}),
            FakeResponse(200, {"result": {"a": 1}}),
            FakeResponse(200, "texto suelto"),
            FakeResponse(200, raise_json=True, text="<html>"),
        ]
        for response in casos:
            with self.subTest(response=response._payload):
                service, _ = build([response])
                self.assertEqual(service.fetch_dispatch(1624, "2026-08-25").status, FETCH_INVALID)


class SecretsTest(unittest.TestCase):
    def test_la_api_key_no_aparece_en_los_logs(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(lambda: logging.disable(logging.CRITICAL))

        service, _ = build([
            FakeResponse(401), requests.ConnectionError(f"{BASE}/api/device-api/gps/1"),
            FakeResponse(500), FakeResponse(404, raise_json=True, text="x"),
        ])
        with self.assertLogs("simtra", level="DEBUG") as captured:
            service.post_gps({}, 1)
            service.post_gps({}, 1)
            service.fetch_dispatch(1, "2026-08-25")
            service.fetch_dispatch(1, "2026-08-25")
            service.get_vehicle(1)

        self.assertNotIn(API_KEY, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
