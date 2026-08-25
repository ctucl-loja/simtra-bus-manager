"""Parsing de horarios y cálculo de puntualidad."""

import unittest

import _bootstrap  # noqa: F401  (instala stubs y sys.path)

import bus_monitor as bm


class ScheduleSecondsTest(unittest.TestCase):
    def test_horario_valido(self):
        self.assertEqual(bm.schedule_seconds("00:00:00"), 0)
        self.assertEqual(bm.schedule_seconds("06:25:00"), 6 * 3600 + 25 * 60)
        self.assertEqual(bm.schedule_seconds("23:59:59"), 86399)

    def test_horario_fuera_de_rango_es_error(self):
        # '25:00:00' antes devolvía 90000 y abría una ventana temporal falsa.
        for value in ("25:00:00", "12:60:00", "12:00:60", "-1:00:00"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bm.schedule_seconds(value)

    def test_formato_invalido_es_error(self):
        for value in ("12:00", "12:00:00:00", "aa:bb:cc", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bm.schedule_seconds(value)

    def test_tipo_invalido_es_error(self):
        for value in (None, 123, [], {}):
            with self.subTest(value=value):
                with self.assertRaises((AttributeError, TypeError, ValueError)):
                    bm.schedule_seconds(value)


class SafeScheduleSecondsTest(unittest.TestCase):
    def test_devuelve_none_sin_lanzar(self):
        for value in (None, "", "25:00:00", "aa:bb:cc", 123, [], {"a": 1}):
            with self.subTest(value=value):
                self.assertIsNone(bm.safe_schedule_seconds(value))

    def test_none_no_es_cero(self):
        """Un horario ilegible no es medianoche."""
        self.assertIsNone(bm.safe_schedule_seconds("xx"))
        self.assertEqual(bm.safe_schedule_seconds("00:00:00"), 0)


class NumericHelpersTest(unittest.TestCase):
    def test_as_finite_float(self):
        self.assertEqual(bm.as_finite_float("3.5"), 3.5)
        self.assertEqual(bm.as_finite_float(0), 0.0)
        for value in (None, "", "abc", [], float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertIsNone(bm.as_finite_float(value))

    def test_as_int_rechaza_bool(self):
        self.assertEqual(bm.as_int("42"), 42)
        self.assertEqual(bm.as_int(7), 7)
        self.assertIsNone(bm.as_int(True))
        self.assertIsNone(bm.as_int(None))
        self.assertIsNone(bm.as_int("x"))


class ArrivalStatusTest(unittest.TestCase):
    def test_a_tiempo_dentro_de_la_tolerancia(self):
        result = bm.calculate_arrival_status("07:46:00", "07:45:42")
        self.assertEqual(result["status"], bm.ON_TIME)
        self.assertEqual(result["difference_seconds"], -18)

    def test_atrasado(self):
        result = bm.calculate_arrival_status("09:44:00", "09:46:15")
        self.assertEqual(result["status"], bm.LATE)
        self.assertEqual(result["difference_seconds"], 135)

    def test_adelantado(self):
        result = bm.calculate_arrival_status("10:49:00", "10:47:45")
        self.assertEqual(result["status"], bm.EARLY)
        self.assertEqual(result["difference_seconds"], -75)

    def test_bordes_exactos_de_la_tolerancia(self):
        tolerance = bm.ON_TIME_TOLERANCE_SECONDS
        self.assertEqual(bm.calculate_arrival_status("10:00:00", "10:00:30")["status"], bm.ON_TIME)
        self.assertEqual(bm.calculate_arrival_status("10:00:00", "10:00:31")["status"], bm.LATE)
        self.assertEqual(bm.calculate_arrival_status("10:00:00", "09:59:30")["status"], bm.ON_TIME)
        self.assertEqual(bm.calculate_arrival_status("10:00:00", "09:59:29")["status"], bm.EARLY)
        self.assertEqual(tolerance, 30)

    def test_cruce_de_medianoche_se_normaliza_al_desfase_corto(self):
        # Programado 23:58, llega 00:03 → 5 min tarde, no 23h55 adelantado.
        result = bm.calculate_arrival_status("23:58:00", "00:03:00")
        self.assertEqual(result["difference_seconds"], 300)
        self.assertEqual(result["status"], bm.LATE)

        # Programado 00:02, llega 23:57 → 5 min antes.
        result = bm.calculate_arrival_status("00:02:00", "23:57:00")
        self.assertEqual(result["difference_seconds"], -300)
        self.assertEqual(result["status"], bm.EARLY)

    def test_horas_invalidas_devuelven_none(self):
        """Sin clasificación inventada: el evento se emite igual, sin status."""
        for scheduled, reported in (
            (None, "07:00:00"),
            ("07:00:00", None),
            ("", "07:00:00"),
            ("aa:bb:cc", "07:00:00"),
            ("25:00:00", "07:00:00"),
        ):
            with self.subTest(scheduled=scheduled, reported=reported):
                self.assertIsNone(bm.calculate_arrival_status(scheduled, reported))


class IntEnvTest(unittest.TestCase):
    def test_valores_invalidos_caen_al_default(self):
        import os

        os.environ["TEST_INT_ENV"] = "abc"
        self.assertEqual(bm.int_env("TEST_INT_ENV", 5), 5)
        os.environ["TEST_INT_ENV"] = "0"
        self.assertEqual(bm.int_env("TEST_INT_ENV", 5), 5)
        os.environ["TEST_INT_ENV"] = "  7 "
        self.assertEqual(bm.int_env("TEST_INT_ENV", 5), 7)
        del os.environ["TEST_INT_ENV"]
        self.assertEqual(bm.int_env("TEST_INT_ENV", 5), 5)


if __name__ == "__main__":
    unittest.main()
