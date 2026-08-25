"""Contexto temporal: qué step está autorizado a recibir marcaciones."""

import unittest
from datetime import datetime

import _bootstrap  # noqa: F401

import bus_monitor as bm
from _fixtures import make_step


def at(hhmm: str) -> datetime:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 8, 25, hours, minutes, 0)


class UsableStepsTest(unittest.TestCase):
    def test_lista_vacia(self):
        self.assertEqual(bm.usable_steps([]), [])

    def test_dispatches_que_no_es_lista(self):
        for value in (None, {}, "steps", 42):
            with self.subTest(value=value):
                self.assertEqual(bm.usable_steps(value), [])

    def test_descarta_steps_no_dict(self):
        self.assertEqual(len(bm.usable_steps([make_step(), None, "x", 5])), 1)

    def test_descarta_horarios_invalidos_pero_conserva_el_resto(self):
        steps = [
            make_step(1, "06:00:00", "07:00:00"),
            make_step(2, None, "08:00:00"),
            make_step(3, "09:00:00", ""),
            make_step(4, "25:00:00", "26:00:00"),
            make_step(5, "10:00:00", "11:00:00"),
        ]
        usable = bm.usable_steps(steps)
        self.assertEqual([step["step"] for step, _, _ in usable], [1, 5])

    def test_descarta_step_sin_checkpoints_solo_si_el_horario_falla(self):
        """La ausencia de checkpoints no invalida la ventana temporal."""
        step = make_step(1, "06:00:00", "07:00:00", checkpoints=None)
        step["checkpoints"] = None
        self.assertEqual(len(bm.usable_steps([step])), 1)

    def test_descarta_recorrido_que_cruzaria_medianoche(self):
        # No se soporta: se descarta con aviso en vez de inventar una ventana.
        self.assertEqual(bm.usable_steps([make_step(1, "23:00:00", "01:00:00")]), [])


class ResolveTemporalContextTest(unittest.TestCase):
    def setUp(self):
        self.steps = [
            make_step(1, "06:00:00", "07:00:00"),
            make_step(2, "08:00:00", "09:00:00"),
            make_step(3, "10:00:00", "11:00:00"),
        ]

    def test_sin_despachos(self):
        context = bm.resolve_temporal_context([], at("08:30"))
        self.assertEqual(context.state, bm.BEFORE_FIRST_STEP)
        self.assertIsNone(context.current_step)
        self.assertIsNone(context.previous_step)

    def test_todos_los_horarios_invalidos_no_autoriza_nada(self):
        context = bm.resolve_temporal_context([make_step(1, "xx", "yy")], at("08:30"))
        self.assertEqual(context.state, bm.BEFORE_FIRST_STEP)
        self.assertIsNone(context.current_step)

    def test_antes_del_primero(self):
        context = bm.resolve_temporal_context(self.steps, at("05:00"))
        self.assertEqual(context.state, bm.BEFORE_FIRST_STEP)
        self.assertEqual(context.next_step["step"], 1)

    def test_step_activo_con_bordes_inclusivos(self):
        for moment in ("06:00", "06:30", "07:00"):
            with self.subTest(moment=moment):
                context = bm.resolve_temporal_context(self.steps, at(moment))
                self.assertEqual(context.state, bm.ACTIVE_STEP)
                self.assertEqual(context.current_step["step"], 1)

    def test_entre_steps(self):
        context = bm.resolve_temporal_context(self.steps, at("07:30"))
        self.assertEqual(context.state, bm.BETWEEN_STEPS)
        self.assertEqual(context.previous_step["step"], 1)
        self.assertEqual(context.next_step["step"], 2)
        self.assertIsNone(context.current_step)

    def test_despues_del_ultimo(self):
        context = bm.resolve_temporal_context(self.steps, at("12:00"))
        self.assertEqual(context.state, bm.AFTER_LAST_STEP)
        self.assertEqual(context.previous_step["step"], 3)
        self.assertIsNone(context.current_step)

    def test_orden_de_entrada_irrelevante(self):
        revuelto = [self.steps[2], self.steps[0], self.steps[1]]
        ordenado = bm.resolve_temporal_context(self.steps, at("08:30"))
        mezclado = bm.resolve_temporal_context(revuelto, at("08:30"))
        self.assertEqual(mezclado.state, ordenado.state)
        self.assertEqual(mezclado.current_step["step"], ordenado.current_step["step"])
        self.assertEqual(mezclado.previous_step["step"], ordenado.previous_step["step"])
        self.assertEqual(mezclado.next_step["step"], ordenado.next_step["step"])

    def test_un_horario_invalido_no_desplaza_al_resto(self):
        steps = list(self.steps) + [make_step(9, "no-es-hora", "tampoco")]
        context = bm.resolve_temporal_context(steps, at("08:30"))
        self.assertEqual(context.state, bm.ACTIVE_STEP)
        self.assertEqual(context.current_step["step"], 2)

    def test_describe_no_rompe_con_campos_ausentes(self):
        step = {"start_schedule": "06:00:00", "end_schedule": "07:00:00"}
        context = bm.resolve_temporal_context([step], at("06:30"))
        self.assertIn("ACTIVE_STEP", context.describe())


class GeofenceValidationTest(unittest.TestCase):
    def test_punto_valido_se_normaliza(self):
        geofence = bm.geofence_from_point(
            {"id": "684", "name": "  X  ", "latitude": "-4.01", "longitude": "-79.2", "radius": "50"}
        )
        self.assertEqual(geofence["id"], 684)
        self.assertEqual(geofence["latitude"], -4.01)
        self.assertEqual(geofence["radius"], 50.0)

    def test_puntos_inutilizables(self):
        casos = [
            None, {}, "punto",
            {"id": None, "latitude": 1, "longitude": 1, "radius": 50},
            {"id": 1, "latitude": None, "longitude": 1, "radius": 50},
            {"id": 1, "latitude": 1, "longitude": None, "radius": 50},
            {"id": 1, "latitude": 1, "longitude": 1, "radius": None},
            {"id": 1, "latitude": 1, "longitude": 1, "radius": 0},
            {"id": 1, "latitude": 1, "longitude": 1, "radius": -5},
            {"id": 1, "latitude": 95, "longitude": 1, "radius": 50},
            {"id": 1, "latitude": 1, "longitude": 200, "radius": 50},
            {"id": 1, "latitude": float("nan"), "longitude": 1, "radius": 50},
        ]
        for punto in casos:
            with self.subTest(punto=punto):
                self.assertIsNone(bm.geofence_from_point(punto))

    def test_punto_sin_nombre_recibe_uno_presentable(self):
        geofence = bm.geofence_from_point({"id": 7, "latitude": 1, "longitude": 1, "radius": 10})
        self.assertEqual(geofence["name"], "PUNTO 7")

    def test_merge_ignora_puntos_invalidos_y_deduplica(self):
        step = make_step(1, checkpoints=[
            {"id": 1, "order": 0, "point": {"id": 10, "latitude": 1, "longitude": 1, "radius": 50}},
            {"id": 2, "order": 1, "point": {"id": 10, "latitude": 1, "longitude": 1, "radius": 50}},
            {"id": 3, "order": 2, "point": {"id": 11, "latitude": None, "longitude": 1, "radius": 50}},
            {"id": 4, "order": 3},
            {"id": 5, "order": 4, "point": None},
        ])
        geofences = bm.merge_geofences(step)
        self.assertEqual([g["id"] for g in geofences], [10])

    def test_merge_con_step_sin_checkpoints(self):
        step = make_step(1)
        step["checkpoints"] = None
        self.assertEqual(bm.merge_geofences(step, None), [])


if __name__ == "__main__":
    unittest.main()
