"""
bus_monitor frente al backend remoto (device-api): solo una respuesta VÁLIDA
cambia el itinerario en memoria. Un error de red, de API key o una respuesta
inutilizable no borra lo cargado. Sin red real: `simtra` es un doble.
"""

import unittest

import _bootstrap  # noqa: F401

import bus_monitor as monitor
from api import (
    ApiService, DispatchFetch,
    FETCH_OK, FETCH_EMPTY, FETCH_AUTH_ERROR, FETCH_CONFIG_ERROR, FETCH_TRANSPORT, FETCH_INVALID,
)
from _fixtures import make_checkpoint, make_step


class FakeSimtra:
    def __init__(self, fetch):
        self.fetch = fetch
        self.vehicle_calls = 0

    def fetch_dispatch(self, register, date):
        return self.fetch

    def get_vehicle(self, register):
        self.vehicle_calls += 1
        return None


def itinerary():
    return [make_step(step=1, start="00:00:00", end="23:59:59", checkpoints=[
        make_checkpoint(3701, 684, 0, "06:10:00"),
    ])]


class LoadAllDispatchesTest(unittest.TestCase):
    def setUp(self):
        self.cached = []
        for name, value in {
            "read_local_revision": lambda date: None,
            "cache_dispatch_locally": lambda d, date, rev=None: self.cached.append(d),
            "cache_vehicle_locally": lambda v: self.fail("no hay ficha que cachear"),
        }.items():
            previous = getattr(monitor, name)
            setattr(monitor, name, value)
            self.addCleanup(setattr, monitor, name, previous)
        previous = monitor.simtra
        self.addCleanup(setattr, monitor, "simtra", previous)
        monitor.reset_daily_state()
        self.addCleanup(monitor.reset_daily_state)

    def load(self, fetch):
        monitor.simtra = FakeSimtra(fetch)
        return monitor.load_all_dispatches("2026-08-25")

    def test_respuesta_valida_carga_y_cachea(self):
        self.assertTrue(self.load(DispatchFetch(FETCH_OK, itinerary())))
        self.assertEqual(len(monitor.get_dispatches()), 1)
        self.assertEqual(len(self.cached), 1)
        self.assertEqual(monitor.simtra.vehicle_calls, 1)

    def test_errores_no_borran_lo_cargado(self):
        self.load(DispatchFetch(FETCH_OK, itinerary()))
        for status in (FETCH_TRANSPORT, FETCH_AUTH_ERROR, FETCH_CONFIG_ERROR, FETCH_INVALID):
            with self.subTest(status=status):
                self.assertFalse(self.load(DispatchFetch(status)))
                self.assertEqual(len(monitor.get_dispatches()), 1)
        self.assertEqual(len(self.cached), 1)   # no se cacheó nada en los errores

    def test_dia_sin_despachos_si_vacia(self):
        self.load(DispatchFetch(FETCH_OK, itinerary()))
        self.assertFalse(self.load(DispatchFetch(FETCH_EMPTY)))
        self.assertEqual(monitor.get_dispatches(), [])

    def test_cliente_del_monitor_es_device_api(self):
        self.assertIsInstance(monitor.simtra, ApiService)
        self.assertFalse(hasattr(monitor, "BACKEND_USERNAME"))
        self.assertFalse(hasattr(monitor, "BACKEND_PASSWORD"))


if __name__ == "__main__":
    unittest.main()
