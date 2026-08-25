"""
Integridad de marcaciones.

Invariantes bajo prueba (entrada de geocerca → selección → reserva →
persistencia → evento → audio):

  1. Sin la escritura indispensable no hay evento ni audio.
  2. Un fallo transitorio libera la reserva: el checkpoint sigue siendo elegible.
  3. Entradas concurrentes no duplican la marcación.
  4. Reservado, confirmado y liberado son estados distintos y observables.
"""

import threading
import time
import unittest

import _bootstrap  # noqa: F401

import bus_monitor as bm
from _fixtures import make_step


class CheckpointFlowTest(unittest.TestCase):
    def setUp(self):
        bm.reset_daily_state()
        self.addCleanup(bm.reset_daily_state)

        self.step = make_step(1, "06:00:00", "07:00:00")
        bm.ALL_DISPATCHES = [self.step]
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=self.step)

        self.events = []
        self.announced = []
        self.checkpoint_calls = []
        self.dispatch_calls = []
        self.checkpoint_ok = True
        self.dispatch_ok = True

        originals = {
            "report_checkpoint": bm.report_checkpoint,
            "report_dispatch_checkpoint": bm.report_dispatch_checkpoint,
            "log_event": bm.log_event,
        }
        original_announce = bm.audio_announcer.announce

        def fake_report_checkpoint(ckpt_id, name, time_reported):
            self.checkpoint_calls.append((ckpt_id, name, time_reported))
            return self.checkpoint_ok

        def fake_report_dispatch(step, ckpt_id, time_reported):
            self.dispatch_calls.append((step, ckpt_id, time_reported))
            return self.dispatch_ok

        def fake_log_event(event_type, priority, message, payload=None):
            self.events.append({"event_type": event_type, "priority": priority,
                                "message": message, "payload": payload})
            return True

        def fake_announce(point_id, name, on_done=None):
            self.announced.append((point_id, name))

        bm.report_checkpoint = fake_report_checkpoint
        bm.report_dispatch_checkpoint = fake_report_dispatch
        bm.log_event = fake_log_event
        bm.audio_announcer.announce = fake_announce

        def restore():
            for key, value in originals.items():
                setattr(bm, key, value)
            bm.audio_announcer.announce = original_announce

        self.addCleanup(restore)

    # ── camino feliz ─────────────────────────────────────────────────────────

    def test_llegada_confirmada_emite_evento_y_audio(self):
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")

        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertIn(3701, bm.CONFIRMED_CHECKPOINTS)
        self.assertNotIn(3701, bm.IN_FLIGHT_CHECKPOINTS)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["event_type"], "checkpoint_arrival")
        self.assertEqual(self.announced, [(684, "Y DE CARIGÁN")])

    def test_payload_conserva_el_contrato(self):
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")
        payload = self.events[0]["payload"]
        esperadas = {"step", "checkpoint_id", "point_id", "point_name", "order",
                     "scheduled_time", "reported_time", "difference_seconds",
                     "arrival_status", "line", "reason"}
        self.assertEqual(set(payload), esperadas)
        self.assertEqual(payload["checkpoint_id"], 3701)
        self.assertEqual(payload["point_id"], 684)
        self.assertEqual(payload["arrival_status"], bm.ON_TIME)
        self.assertEqual(payload["reason"], "progreso normal")

    # ── invariante 1 y 2: fallo de la escritura indispensable ────────────────

    def test_persistencia_fallida_no_emite_evento_ni_audio(self):
        self.checkpoint_ok = False
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")

        self.assertEqual(self.events, [])
        self.assertEqual(self.announced, [])
        self.assertEqual(self.dispatch_calls, [])   # no se sigue con lo secundario

    def test_persistencia_fallida_libera_la_reserva(self):
        self.checkpoint_ok = False
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")

        self.assertNotIn(3701, bm.CONFIRMED_CHECKPOINTS)
        self.assertNotIn(3701, bm.IN_FLIGHT_CHECKPOINTS)   # no queda bloqueado

    def test_reintento_tras_fallo_transitorio_funciona(self):
        self.checkpoint_ok = False
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")
        self.assertEqual(self.events, [])

        self.checkpoint_ok = True
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:12:30")

        self.assertIn(3701, bm.CONFIRMED_CHECKPOINTS)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["payload"]["reported_time"], "06:12:30")

    # ── escritura secundaria ─────────────────────────────────────────────────

    def test_fallo_del_despacho_no_pierde_la_marcacion(self):
        """La marcación ya está a salvo: se confirma y se avisa igual."""
        self.dispatch_ok = False
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")

        self.assertIn(3701, bm.CONFIRMED_CHECKPOINTS)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.announced), 1)

    # ── invariante 3: sin duplicados ─────────────────────────────────────────

    def test_segunda_entrada_no_duplica(self):
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:09")

        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertEqual(len(self.events), 1)

    def test_entradas_concurrentes_reportan_una_sola_vez(self):
        lento = threading.Event()

        def report_lento(ckpt_id, name, time_reported):
            self.checkpoint_calls.append((ckpt_id, name, time_reported))
            lento.wait(0.5)      # mantiene la reserva en vuelo
            return True

        bm.report_checkpoint = report_lento

        hilos = [
            threading.Thread(target=bm.resolve_and_report_checkpoint,
                             args=(684, "Y DE CARIGÁN", "06:10:05"))
            for _ in range(8)
        ]
        for hilo in hilos:
            hilo.start()
        time.sleep(0.05)
        lento.set()
        for hilo in hilos:
            hilo.join(timeout=5)

        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.announced), 1)

    def test_reserva_es_test_and_set_atomico(self):
        ganadores = []
        barrera = threading.Barrier(16)

        def intentar():
            barrera.wait()
            if bm.reserve_checkpoint(999):
                ganadores.append(threading.current_thread().name)

        hilos = [threading.Thread(target=intentar) for _ in range(16)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(timeout=5)

        self.assertEqual(len(ganadores), 1)

    # ── invariante 4: estados distinguibles ──────────────────────────────────

    def test_estados_del_ciclo_de_vida(self):
        self.assertTrue(bm.reserve_checkpoint(555))
        self.assertIn(555, bm.IN_FLIGHT_CHECKPOINTS)
        self.assertNotIn(555, bm.CONFIRMED_CHECKPOINTS)
        self.assertIn(555, bm.taken_checkpoints())

        self.assertFalse(bm.reserve_checkpoint(555))   # reservado no es elegible

        bm.release_checkpoint(555)
        self.assertNotIn(555, bm.taken_checkpoints())
        self.assertTrue(bm.reserve_checkpoint(555))    # liberado vuelve a serlo

        bm.confirm_checkpoint(555)
        self.assertIn(555, bm.CONFIRMED_CHECKPOINTS)
        self.assertNotIn(555, bm.IN_FLIGHT_CHECKPOINTS)
        self.assertFalse(bm.reserve_checkpoint(555))   # confirmado queda cerrado

    def test_seleccion_ignora_checkpoints_en_vuelo(self):
        bm.reserve_checkpoint(3701)
        encontrado = bm.find_unreported_checkpoint(self.step, 684, bm.taken_checkpoints())
        self.assertIsNone(encontrado)

    # ── datos incompletos ────────────────────────────────────────────────────

    def test_checkpoint_sin_id_no_se_marca(self):
        step = make_step(2, "06:00:00", "07:00:00", checkpoints=[
            {"order": 0, "time_calculated": "06:10:00",
             "point": {"id": 690, "name": "SIN ID", "latitude": -4, "longitude": -79, "radius": 50}},
        ])
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=step)
        bm.resolve_and_report_checkpoint(690, "SIN ID", "06:10:05")

        self.assertEqual(self.checkpoint_calls, [])
        self.assertEqual(self.events, [])

    def test_step_sin_checkpoints_no_rompe(self):
        step = make_step(3, "06:00:00", "07:00:00")
        step["checkpoints"] = None
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=step)
        bm.resolve_and_report_checkpoint(684, "X", "06:10:05")
        self.assertEqual(self.events, [])

    def test_checkpoint_sin_hora_programada_emite_evento_sin_status(self):
        step = make_step(4, "06:00:00", "07:00:00", checkpoints=[
            {"id": 4001, "order": 0, "time_calculated": None,
             "point": {"id": 691, "name": "SIN HORA", "latitude": -4, "longitude": -79, "radius": 50}},
        ])
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=step)
        bm.resolve_and_report_checkpoint(691, "SIN HORA", "06:10:05")

        self.assertEqual(len(self.events), 1)
        payload = self.events[0]["payload"]
        self.assertIsNone(payload["arrival_status"])
        self.assertIsNone(payload["difference_seconds"])
        self.assertEqual(payload["point_name"], "SIN HORA")

    def test_step_sin_line_emite_line_nula(self):
        step = make_step(5, "06:00:00", "07:00:00", line=None)
        step["line"] = "A2"          # forma inesperada, no un objeto
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=step)
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")

        self.assertEqual(len(self.events), 1)
        self.assertIsNone(self.events[0]["payload"]["line"])

    def test_punto_sin_nombre_recibe_uno_presentable(self):
        step = make_step(6, "06:00:00", "07:00:00", checkpoints=[
            {"id": 6001, "order": 0, "time_calculated": "06:10:00",
             "point": {"id": 692, "latitude": -4, "longitude": -79, "radius": 50}},
        ])
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=step)
        bm.resolve_and_report_checkpoint(692, "", "06:10:05")

        self.assertEqual(self.events[0]["payload"]["point_name"], "PUNTO 692")

    # ── autorización temporal intacta ────────────────────────────────────────

    def test_sin_step_activo_no_se_marca(self):
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.BEFORE_FIRST_STEP, next_step=self.step)
        bm.resolve_and_report_checkpoint(684, "Y DE CARIGÁN", "06:10:05")

        self.assertEqual(self.checkpoint_calls, [])
        self.assertEqual(self.events, [])

    def test_seed_marca_como_confirmados_los_ya_reportados(self):
        step = make_step(7, checkpoints=[
            {"id": 7001, "order": 0, "time_reported": "06:10:00",
             "point": {"id": 700, "latitude": -4, "longitude": -79, "radius": 50}},
            {"id": 7002, "order": 1, "time_reported": "00:00:00",
             "point": {"id": 701, "latitude": -4, "longitude": -79, "radius": 50}},
            {"order": 2, "time_reported": "06:30:00",
             "point": {"id": 702, "latitude": -4, "longitude": -79, "radius": 50}},
        ])
        bm.seed_reported_checkpoints([step])

        self.assertIn(7001, bm.CONFIRMED_CHECKPOINTS)
        self.assertNotIn(7002, bm.CONFIRMED_CHECKPOINTS)

    def test_seed_tolera_dispatches_invalidos(self):
        for value in (None, "x", 42, [None, "y"], [{"checkpoints": None}]):
            with self.subTest(value=value):
                bm.seed_reported_checkpoints(value)   # no debe lanzar


if __name__ == "__main__":
    unittest.main()
