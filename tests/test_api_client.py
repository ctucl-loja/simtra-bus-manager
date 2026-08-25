"""Cliente del backend remoto: timeouts, formas inesperadas y reintento por 401."""

import unittest

import _bootstrap  # noqa: F401

import requests

from api import ApiService, DEFAULT_TIMEOUT


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
    """Registra cada llamada; devuelve respuestas de una cola por método."""

    def __init__(self, request_responses=None, post_responses=None):
        self.request_responses = list(request_responses or [])
        self.post_responses = list(post_responses or [])
        self.requests = []
        self.posts = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append({"method": method, "url": url, "json": json,
                              "headers": headers, "timeout": timeout})
        if not self.request_responses:
            raise AssertionError(f"llamada inesperada: {method} {url}")
        response = self.request_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if not self.post_responses:
            raise AssertionError(f"login inesperado: {url}")
        response = self.post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def login_ok(token="TOKEN-1"):
    return FakeResponse(200, {"result": {"token": token}})


def build(request_responses=None, post_responses=None):
    service = ApiService("https://api.example.com", "user@example.com", "secreto")
    session = FakeSession(request_responses, post_responses)
    service._session = session
    return service, session


class ConstructorTest(unittest.TestCase):
    def test_no_hace_red_al_construir(self):
        """Antes el constructor pedía el token: sin señal, el import se colgaba."""
        service, session = build()
        self.assertEqual(service.jwt, "")
        self.assertEqual(session.posts, [])
        self.assertEqual(session.requests, [])

    def test_url_sin_barra_final(self):
        service = ApiService("https://api.example.com/", "u", "p")
        self.assertEqual(service.api_url, "https://api.example.com")

    def test_sin_url_no_lanza(self):
        service = ApiService(None, None, None)
        self.assertEqual(service.get_dispatch(1, "2026-08-25"), [])
        self.assertIsNone(service.get_vehicle(1))
        self.assertFalse(service.post_passenger({}))
        self.assertFalse(service.update_dispatch({}))


class TimeoutTest(unittest.TestCase):
    def test_toda_solicitud_lleva_timeout(self):
        service, session = build(
            request_responses=[FakeResponse(200, {"result": []})],
            post_responses=[login_ok()],
        )
        service.get_dispatch(1624, "2026-08-25")

        self.assertTrue(session.posts and session.requests)
        for call in session.posts + session.requests:
            self.assertEqual(call["timeout"], DEFAULT_TIMEOUT)


class RetryOn401Test(unittest.TestCase):
    def test_renueva_token_y_reintenta_una_vez(self):
        service, session = build(
            request_responses=[FakeResponse(401), FakeResponse(200, {"result": [{"step": 1}]})],
            post_responses=[login_ok("VIEJO"), login_ok("NUEVO")],
        )
        result = service.get_dispatch(1624, "2026-08-25")

        self.assertEqual(result, [{"step": 1}])
        self.assertEqual(len(session.requests), 2)          # original + un reintento
        self.assertEqual(service.jwt, "NUEVO")
        self.assertIn("NUEVO", session.requests[1]["headers"]["Authorization"])

    def test_no_reintenta_indefinidamente(self):
        """Un 401 persistente no puede convertirse en un bucle de autenticación."""
        service, session = build(
            request_responses=[FakeResponse(401), FakeResponse(401)],
            post_responses=[login_ok(), login_ok()],
        )
        self.assertEqual(service.get_dispatch(1624, "2026-08-25"), [])
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(len(session.posts), 2)

    def test_si_el_login_falla_no_reintenta(self):
        service, session = build(
            request_responses=[FakeResponse(401)],
            post_responses=[login_ok(), FakeResponse(500)],
        )
        self.assertEqual(service.get_dispatch(1624, "2026-08-25"), [])
        self.assertEqual(len(session.requests), 1)

    def test_login_sin_token_utilizable(self):
        for payload in (None, {}, {"result": None}, {"result": {}}, {"result": {"token": ""}},
                        {"result": "texto"}):
            with self.subTest(payload=payload):
                service, _ = build(post_responses=[FakeResponse(200, payload)])
                self.assertEqual(service.get_jwt(), "")

    def test_login_con_error_de_red(self):
        service, _ = build(post_responses=[requests.ConnectionError("sin red")])
        self.assertEqual(service.get_jwt(), "")


class ResponseShapeTest(unittest.TestCase):
    def test_get_dispatch_siempre_lista(self):
        casos = [
            FakeResponse(200, {"result": None}),
            FakeResponse(200, {"result": {"step": 1}}),
            FakeResponse(200, {"result": "texto"}),
            FakeResponse(200, {}),                    # sin 'result'
            FakeResponse(200, ["a", "b"]),            # envoltura inesperada
            FakeResponse(200, raise_json=True),       # no es JSON
            FakeResponse(500, {"result": []}),
        ]
        for response in casos:
            with self.subTest(response=response.status_code):
                service, _ = build(request_responses=[response], post_responses=[login_ok()])
                self.assertEqual(service.get_dispatch(1624, "2026-08-25"), [])

    def test_get_dispatch_devuelve_la_lista(self):
        service, _ = build(
            request_responses=[FakeResponse(200, {"result": [{"step": 0}, {"step": 1}]})],
            post_responses=[login_ok()],
        )
        self.assertEqual(len(service.get_dispatch(1624, "2026-08-25")), 2)

    def test_get_vehicle_solo_acepta_objeto(self):
        for payload in ({"result": None}, {"result": []}, {"result": "x"}, {}):
            with self.subTest(payload=payload):
                service, _ = build(request_responses=[FakeResponse(200, payload)],
                                   post_responses=[login_ok()])
                self.assertIsNone(service.get_vehicle(1624))

        service, _ = build(request_responses=[FakeResponse(200, {"result": {"plate": "ABC"}})],
                           post_responses=[login_ok()])
        self.assertEqual(service.get_vehicle(1624), {"plate": "ABC"})

    def test_errores_de_red_no_se_propagan(self):
        for error in (requests.ConnectionError("x"), requests.Timeout("y")):
            with self.subTest(error=type(error).__name__):
                service, _ = build(request_responses=[error], post_responses=[login_ok()])
                self.assertEqual(service.get_dispatch(1624, "2026-08-25"), [])

    def test_post_passenger_solo_true_con_201(self):
        for status, expected in ((201, True), (200, False), (400, False), (500, False)):
            with self.subTest(status=status):
                service, _ = build(request_responses=[FakeResponse(status)],
                                   post_responses=[login_ok()])
                self.assertIs(service.post_passenger({"a": 1}), expected)

    def test_update_dispatch_solo_true_con_200(self):
        for status, expected in ((200, True), (204, False), (500, False)):
            with self.subTest(status=status):
                service, _ = build(request_responses=[FakeResponse(status)],
                                   post_responses=[login_ok()])
                self.assertIs(service.update_dispatch({"id": 1}), expected)


class CredentialsTest(unittest.TestCase):
    def test_la_contrasena_no_aparece_en_los_logs(self):
        import logging

        logging.disable(logging.NOTSET)
        self.addCleanup(lambda: logging.disable(logging.CRITICAL))

        service, _ = build(post_responses=[FakeResponse(401)])
        with self.assertLogs("simtra", level="DEBUG") as captured:
            service.get_jwt()

        registrado = "\n".join(captured.output)
        self.assertNotIn("secreto", registrado)
        self.assertNotIn("user@example.com", registrado)


if __name__ == "__main__":
    unittest.main()
