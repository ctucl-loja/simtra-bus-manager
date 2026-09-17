"""
Recarga manual del itinerario: validación y fusión de marcaciones.

Funciones puras (services/dispatch_refresh.py), sin red ni base de datos. Los
dos comportamientos que se protegen aquí son los que, si se rompen, le borran
trabajo real al conductor:

  1. Una lista con contenido INVÁLIDO no es un día sin despachos. Confundirlos
     vaciaría el itinerario a mitad de jornada por un error del backend.
  2. Las marcaciones locales todavía no subidas se conservan por IDENTIDAD
     (step, checkpoint_id) — nunca por posición del array, que trasladaría una
     llegada a otro recorrido.
"""

import unittest

import _bootstrap  # noqa: F401

import dispatch_refresh as refresh
from _fixtures import make_checkpoint, make_step


class ValidateDispatchesTest(unittest.TestCase):
    def test_lista_vacia_es_valida(self):
        """Día sin despachos: es un dato real, no un fallo."""
        self.assertTrue(refresh.validate_dispatches([]))

    def test_despacho_completo_es_valido(self):
        self.assertTrue(refresh.validate_dispatches([make_step()]))

    def test_lo_que_no_es_lista_se_rechaza(self):
        for value in (None, {}, "texto", 42, [make_step()][0]):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(refresh.validate_dispatches(value))

    def test_step_sin_numero(self):
        step = make_step()
        del step["step"]
        self.assertFalse(refresh.validate_dispatches([step]))

    def test_step_con_horario_imposible(self):
        """'25:00:00' generaría una ventana temporal que no existe."""
        for start, end in (("25:00:00", "07:00:00"), ("06:00:00", "06:99:00"), ("", "07:00:00")):
            with self.subTest(start=start, end=end):
                self.assertFalse(refresh.validate_dispatches([make_step(start=start, end=end)]))

    def test_step_sin_checkpoints(self):
        self.assertFalse(refresh.validate_dispatches([make_step(checkpoints=[])]))

    def test_checkpoint_sin_id(self):
        ckpt = make_checkpoint()
        del ckpt["id"]
        self.assertFalse(refresh.validate_dispatches([make_step(checkpoints=[ckpt])]))

    def test_checkpoint_sin_coordenadas_utilizables(self):
        """Sin coordenadas no hay geocerca: el bus no marcaría nunca ese punto."""
        for latitude in (None, "abc", float("nan"), 91):
            with self.subTest(latitude=latitude):
                ckpt = make_checkpoint()
                ckpt["point"]["latitude"] = latitude
                self.assertFalse(refresh.validate_dispatches([make_step(checkpoints=[ckpt])]))

    def test_steps_repetidos(self):
        self.assertFalse(refresh.validate_dispatches([make_step(step=1), make_step(step=1)]))

    def test_una_lista_con_basura_no_es_un_dia_sin_despachos(self):
        resultado = refresh.validate_dispatches([{"cualquier": "cosa"}])
        self.assertFalse(resultado)
        self.assertIsNotNone(resultado.reason)

    def test_el_motivo_es_texto_para_la_pantalla(self):
        resultado = refresh.validate_dispatches("no soy una lista")
        self.assertIn("formato", resultado.reason.lower())


class LocalReportsTest(unittest.TestCase):
    def build(self, reported="06:31:12"):
        return [make_step(step=1, checkpoints=[
            make_checkpoint(3701, 684, 0, "06:10:00"),
            make_checkpoint(3702, 685, 1, "06:30:00", time_reported=reported),
        ])]

    def test_solo_las_marcaciones_pendientes_de_subir(self):
        """
        Una marcación ya subida al servidor es el servidor quien debe
        devolverla: resucitarla aquí reintroduciría un dato que el backend pudo
        haber corregido a propósito.
        """
        local = self.build()
        self.assertEqual(refresh.local_reports(local, {3702}), {(1, 3702): "06:31:12"})
        self.assertEqual(refresh.local_reports(local, {9999}), {})
        self.assertEqual(refresh.local_reports(local, set()), {})

    def test_00_00_00_no_es_una_marcacion(self):
        self.assertEqual(refresh.local_reports(self.build("00:00:00"), {3702}), {})

    def test_la_identidad_incluye_el_step(self):
        local = self.build()
        self.assertIn((1, 3702), refresh.local_reports(local, {3702}))

    def test_tolera_formas_inesperadas(self):
        for value in (None, "texto", [{"step": "x"}], [None]):
            with self.subTest(value=value):
                self.assertEqual(refresh.local_reports(value, {3702}), {})


class MergePendingReportsTest(unittest.TestCase):
    def remote(self):
        return [
            make_step(step=1, checkpoints=[
                make_checkpoint(3701, 684, 0, "06:10:00"),
                make_checkpoint(3702, 685, 1, "06:30:00"),
            ]),
            make_step(step=2, start="08:00:00", end="09:00:00", checkpoints=[
                make_checkpoint(3801, 684, 0, "08:10:00"),
            ]),
        ]

    def reported_of(self, dispatches, step, cid):
        for s in dispatches:
            if s["step"] != step:
                continue
            for c in s["checkpoints"]:
                if c["id"] == cid:
                    return c["time_reported"]
        return None

    def test_la_marcacion_pendiente_se_conserva(self):
        remote = self.remote()
        report = refresh.merge_pending_reports(remote, {(1, 3702): "06:31:12"})

        self.assertEqual(self.reported_of(remote, 1, 3702), "06:31:12")
        self.assertEqual(report.preserved, [(1, 3702)])
        self.assertEqual(report.dropped, [])

    def test_el_servidor_gana_cuando_ya_trae_su_version(self):
        remote = self.remote()
        remote[0]["checkpoints"][1]["time_reported"] = "06:29:00"
        refresh.merge_pending_reports(remote, {(1, 3702): "06:31:12"})
        self.assertEqual(self.reported_of(remote, 1, 3702), "06:29:00")

    def test_una_marcacion_sin_sitio_exacto_se_descarta_no_se_traslada(self):
        """
        El itinerario nuevo no tiene ese (step, checkpoint). Colocarla en el que
        ocupe la misma POSICIÓN la pondría en otro recorrido: se descarta y se
        deja constancia.
        """
        remote = self.remote()
        report = refresh.merge_pending_reports(remote, {(1, 4444): "06:31:12"})

        self.assertEqual(report.preserved, [])
        self.assertEqual(report.dropped, [(1, 4444)])
        # Y nada quedó marcado por accidente.
        for step in remote:
            for ckpt in step["checkpoints"]:
                self.assertEqual(ckpt["time_reported"], "00:00:00")

    def test_el_mismo_checkpoint_en_otro_step_no_recibe_la_marcacion(self):
        """
        El id 3701 existe en el step 1. Una marcación pendiente del step 2 con
        ese id NO debe caer sobre el step 1.
        """
        remote = self.remote()
        report = refresh.merge_pending_reports(remote, {(2, 3701): "08:15:00"})

        self.assertEqual(self.reported_of(remote, 1, 3701), "00:00:00")
        self.assertEqual(report.dropped, [(2, 3701)])

    def test_el_orden_de_los_steps_no_importa(self):
        """Fusión por identidad: el backend puede devolverlos en otro orden."""
        remote = list(reversed(self.remote()))
        refresh.merge_pending_reports(remote, {(1, 3702): "06:31:12"})
        self.assertEqual(self.reported_of(remote, 1, 3702), "06:31:12")

    def test_sin_pendientes_no_toca_nada(self):
        remote = self.remote()
        report = refresh.merge_pending_reports(remote, {})
        self.assertFalse(report.any_preserved)
        self.assertEqual(remote, self.remote())

    def test_count_checkpoints(self):
        self.assertEqual(refresh.count_checkpoints(self.remote()), 3)
        self.assertEqual(refresh.count_checkpoints([]), 0)
        self.assertEqual(refresh.count_checkpoints(None), 0)


if __name__ == "__main__":
    unittest.main()
