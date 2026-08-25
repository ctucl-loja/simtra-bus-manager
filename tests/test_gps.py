"""Lectura del GPS local: nunca debe tumbar el monitor ni inventar posiciones."""

import unittest

import _bootstrap  # noqa: F401

import bus_monitor as bm


class FakeResponse:
    def __init__(self, payload=None, raise_json=False, http_error=None):
        self._payload = payload
        self._raise_json = raise_json
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error:
            raise self._http_error

    def json(self):
        if self._raise_json:
            raise ValueError("no es JSON")
        return self._payload


class FakeRequests:
    """Sustituto de `requests` dentro del módulo. RequestException es la real."""

    def __init__(self, response=None, exception=None):
        import requests as real_requests

        self.RequestException = real_requests.RequestException
        self._response = response
        self._exception = exception
        self.calls = []

    def get(self, url, timeout=None, **kwargs):
        self.calls.append(("GET", url, timeout))
        if self._exception:
            raise self._exception
        return self._response


class FetchGpsTest(unittest.TestCase):
    def setUp(self):
        self._real_requests = bm.requests
        self.addCleanup(lambda: setattr(bm, "requests", self._real_requests))

    def use(self, **kwargs):
        fake = FakeRequests(**kwargs)
        bm.requests = fake
        return fake

    def test_lectura_completa(self):
        self.use(response=FakeResponse({
            "latitude": -4.01, "longitude": -79.22, "speed": 32.5,
            "timestamp": "2026-08-25T10:25:00",
        }))
        reading = bm.fetch_gps()
        self.assertEqual(reading.latitude, -4.01)
        self.assertEqual(reading.speed, 32.5)
        self.assertEqual(reading.timestamp, "2026-08-25T10:25:00")

    def test_speed_null_no_rompe_y_no_se_convierte_en_cero(self):
        """Antes esto lanzaba TypeError y mataba el proceso completo."""
        self.use(response=FakeResponse({
            "latitude": -4.01, "longitude": -79.22, "speed": None,
            "timestamp": "2026-08-25T10:25:00",
        }))
        reading = bm.fetch_gps()
        self.assertIsNotNone(reading)
        self.assertIsNone(reading.speed)   # None ≠ 0.0 ("detenido")

    def test_speed_ausente_o_no_numerica(self):
        for payload_speed in ({}, {"speed": "rapido"}, {"speed": float("nan")}):
            with self.subTest(speed=payload_speed):
                self.use(response=FakeResponse({
                    "latitude": -4.01, "longitude": -79.22, "timestamp": "t", **payload_speed,
                }))
                reading = bm.fetch_gps()
                self.assertIsNotNone(reading)
                self.assertIsNone(reading.speed)

    def test_sin_coordenadas_se_descarta_la_lectura(self):
        casos = [
            {"speed": 10},
            {"latitude": None, "longitude": -79.2},
            {"latitude": -4.0, "longitude": None},
            {"latitude": "abc", "longitude": -79.2},
            {"latitude": float("nan"), "longitude": -79.2},
            {"latitude": float("inf"), "longitude": -79.2},
        ]
        for payload in casos:
            with self.subTest(payload=payload):
                self.use(response=FakeResponse(payload))
                self.assertIsNone(bm.fetch_gps())

    def test_coordenadas_como_texto_numerico_se_aceptan(self):
        self.use(response=FakeResponse({"latitude": "-4.01", "longitude": "-79.22"}))
        reading = bm.fetch_gps()
        self.assertEqual(reading.latitude, -4.01)

    def test_sin_posicion_todavia(self):
        for payload in (None, {}, []):
            with self.subTest(payload=payload):
                self.use(response=FakeResponse(payload))
                self.assertIsNone(bm.fetch_gps())

    def test_respuesta_no_json(self):
        self.use(response=FakeResponse(raise_json=True))
        self.assertIsNone(bm.fetch_gps())

    def test_respuesta_con_forma_inesperada(self):
        for payload in ("cadena", 42, [1, 2, 3]):
            with self.subTest(payload=payload):
                self.use(response=FakeResponse(payload))
                self.assertIsNone(bm.fetch_gps())

    def test_error_de_red(self):
        import requests as real_requests

        self.use(exception=real_requests.ConnectionError("sin red"))
        self.assertIsNone(bm.fetch_gps())

    def test_siempre_se_envia_timeout(self):
        fake = self.use(response=FakeResponse({"latitude": 1, "longitude": 1}))
        bm.fetch_gps()
        self.assertEqual(fake.calls[0][2], 5)


class GeometryTest(unittest.TestCase):
    def test_is_inside_con_geocerca_validada(self):
        geofence = bm.geofence_from_point(
            {"id": 1, "latitude": -4.0, "longitude": -79.2, "radius": 100}
        )
        dentro = bm.GpsReading(latitude=-4.0, longitude=-79.2, timestamp="t", speed=None)
        fuera = bm.GpsReading(latitude=-4.1, longitude=-79.3, timestamp="t", speed=None)
        self.assertTrue(bm.is_inside(dentro, geofence))
        self.assertFalse(bm.is_inside(fuera, geofence))


if __name__ == "__main__":
    unittest.main()
