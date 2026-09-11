"""
Marcación EXCLUSIVAMENTE al ingresar a una geocerca.

test_checkpoint_flow.py ejercita resolve_and_report_checkpoint() a partir de
una entrada ya detectada. Aquí se entra un nivel antes: se alimenta
GeofenceMonitor.process() con lecturas GPS controladas y se comprueba qué
transición dispara una marcación y cuál no.

Invariante central: la marcación, el evento `checkpoint_arrival` y el audio
ocurren SOLO en la transición fuera → dentro. La salida (dentro → fuera) es
puramente diagnóstica: limpia el estado y deja un log, nada más.

No se usa red, GPS real ni servicios de producción: report_checkpoint,
report_dispatch_checkpoint, log_event y el anunciador de audio se sustituyen
por dobles que solo registran las llamadas.
"""

import unittest

import _bootstrap  # noqa: F401

import bus_monitor as bm
from _fixtures import make_checkpoint, make_point, make_step

# Punto físico de la geocerca de prueba y dos lecturas GPS: una dentro del
# radio (el centro mismo) y otra a varios kilómetros, claramente fuera.
CENTER_LAT, CENTER_LON = -4.0100, -79.2294
FAR_LAT, FAR_LON = -4.1000, -79.3300


def reading(lat, lon, timestamp="2026-09-11T06:10:05"):
    return bm.GpsReading(latitude=lat, longitude=lon, timestamp=timestamp, speed=0.0)


INSIDE = reading(CENTER_LAT, CENTER_LON)
OUTSIDE = reading(FAR_LAT, FAR_LON)


class GeofenceTransitionTest(unittest.TestCase):
    def setUp(self):
        bm.reset_daily_state()
        self.addCleanup(bm.reset_daily_state)

        # Recorrido con tres checkpoints; el primero (punto 684) es el que se
        # vigila con la geocerca de prueba.
        self.step = make_step(1, "06:00:00", "07:00:00")
        bm.ALL_DISPATCHES = [self.step]
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=self.step)

        self.events = []
        self.announced = []
        self.checkpoint_calls = []
        self.dispatch_calls = []
        self.checkpoint_ok = True

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
            return True

        def fake_log_event(event_type, priority, message, payload=None):
            self.events.append({"event_type": event_type, "payload": payload})
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

        self.geofence = bm.geofence_from_point(
            make_point(684, "Y DE CARIGÁN", CENTER_LAT, CENTER_LON, 50)
        )
        self.monitor = bm.GeofenceMonitor([self.geofence])

    def assert_nada_reportado(self):
        self.assertEqual(self.checkpoint_calls, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.announced, [])

    # ── 1. fuera → fuera ─────────────────────────────────────────────────────

    def test_fuera_a_fuera_no_marca(self):
        self.monitor.process(OUTSIDE)
        self.monitor.process(OUTSIDE)

        self.assert_nada_reportado()
        self.assertIsNone(self.monitor._active[684])
        self.assertEqual(self.monitor.history, [])

    # ── 2. fuera → dentro ────────────────────────────────────────────────────

    def test_entrada_marca_una_vez_con_hora_de_ingreso(self):
        self.monitor.process(OUTSIDE)
        self.monitor.process(INSIDE)

        self.assertEqual(len(self.checkpoint_calls), 1)
        ckpt_id, name, time_reported = self.checkpoint_calls[0]
        self.assertEqual(ckpt_id, 3701)
        self.assertEqual(name, "Y DE CARIGÁN")

        # La hora reportada es la del instante de ENTRADA: la que el monitor
        # acaba de sellar en el GeofenceEvent, no una posterior.
        entry = self.monitor.history[0]
        self.assertEqual(time_reported, entry.entry_time.strftime("%H:%M:%S"))

        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["event_type"], "checkpoint_arrival")
        self.assertEqual(self.events[0]["payload"]["reported_time"], time_reported)
        self.assertEqual(self.announced, [(684, "Y DE CARIGÁN")])
        self.assertIn(3701, bm.CONFIRMED_CHECKPOINTS)

    # ── 3. dentro → dentro ───────────────────────────────────────────────────

    def test_permanencia_dentro_no_repite(self):
        self.monitor.process(OUTSIDE)
        self.monitor.process(INSIDE)
        for _ in range(5):
            self.monitor.process(INSIDE)

        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.announced), 1)
        self.assertEqual(len(self.monitor.history), 1)

    # ── 4. dentro → fuera ────────────────────────────────────────────────────

    def test_salida_no_marca_ni_emite_llegada(self):
        """La salida limpia el estado y loguea; no toca el despacho ni avisa."""
        self.monitor.process(INSIDE)
        llamadas_tras_entrar = len(self.checkpoint_calls)
        despachos_tras_entrar = list(self.dispatch_calls)

        self.monitor.process(OUTSIDE)

        self.assertEqual(len(self.checkpoint_calls), llamadas_tras_entrar)
        self.assertEqual(self.dispatch_calls, despachos_tras_entrar)
        self.assertEqual(len(self.events), 1)      # solo el de la entrada
        self.assertEqual(len(self.announced), 1)
        self.assertIsNone(self.monitor._active[684])

    def test_salida_sin_entrada_previa_no_hace_nada(self):
        """Arranque fuera de la geocerca: la primera lectura externa es inerte."""
        self.monitor.process(OUTSIDE)
        self.assert_nada_reportado()

    # ── 5. salir y reingresar al mismo checkpoint ────────────────────────────

    def test_reingreso_al_mismo_checkpoint_no_duplica(self):
        self.monitor.process(INSIDE)
        self.monitor.process(OUTSIDE)
        self.monitor.process(INSIDE)

        # La segunda entrada sí es una transición nueva para la geometría…
        self.assertEqual(len(self.monitor.history), 2)
        # …pero el checkpoint ya está confirmado y no se reporta otra vez.
        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.announced), 1)

    # ── 6. mismo punto físico, otro checkpoint del recorrido ─────────────────

    def test_segundo_checkpoint_en_el_mismo_punto_fisico_si_se_marca(self):
        """
        Un recorrido de ida y vuelta pasa dos veces por el mismo punto. La
        deduplicación es por checkpoint id, no por point_id: el segundo paso
        debe poder marcarse.
        """
        point = make_point(684, "Y DE CARIGÁN", CENTER_LAT, CENTER_LON, 50)
        step = make_step(1, "06:00:00", "07:00:00", checkpoints=[
            make_checkpoint(3701, 684, 0, "06:10:00", point=point),
            make_checkpoint(3702, 685, 1, "06:30:00"),
            make_checkpoint(3703, 684, 2, "06:50:00", point=point),   # regreso
        ])
        bm.ALL_DISPATCHES = [step]
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.ACTIVE_STEP, current_step=step)
        monitor = bm.GeofenceMonitor([self.geofence])

        monitor.process(INSIDE)    # ida
        monitor.process(OUTSIDE)
        monitor.process(INSIDE)    # vuelta al mismo punto físico

        marcados = [call[0] for call in self.checkpoint_calls]
        self.assertEqual(marcados, [3701, 3703])
        self.assertEqual(len(self.events), 2)
        self.assertEqual(
            [e["payload"]["checkpoint_id"] for e in self.events], [3701, 3703]
        )

    def test_horario_y_secuencia_siguen_mandando_en_el_punto_repetido(self):
        """Reutilizar el punto no relaja las reglas: sin step activo, nada se marca."""
        bm.CURRENT_CONTEXT = bm.TemporalContext(state=bm.BEFORE_FIRST_STEP, next_step=self.step)
        self.monitor.process(INSIDE)

        self.assert_nada_reportado()
        # La geometría sí registró la entrada; la autorización temporal la negó.
        self.assertEqual(len(self.monitor.history), 1)

    # ── 7. persistencia indispensable fallida ────────────────────────────────

    def test_entrada_con_persistencia_fallida_no_emite_llegada_ni_audio(self):
        self.checkpoint_ok = False
        self.monitor.process(INSIDE)

        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.announced, [])
        self.assertEqual(self.dispatch_calls, [])
        # Reserva liberada: el checkpoint sigue siendo elegible.
        self.assertNotIn(3701, bm.CONFIRMED_CHECKPOINTS)
        self.assertNotIn(3701, bm.IN_FLIGHT_CHECKPOINTS)

    def test_reintento_en_la_siguiente_entrada_tras_fallo_de_persistencia(self):
        self.checkpoint_ok = False
        self.monitor.process(INSIDE)
        self.monitor.process(OUTSIDE)

        self.checkpoint_ok = True
        self.monitor.process(INSIDE)

        self.assertEqual(len(self.checkpoint_calls), 2)
        self.assertEqual(len(self.events), 1)
        self.assertIn(3701, bm.CONFIRMED_CHECKPOINTS)

    # ── arranque dentro de la geocerca ───────────────────────────────────────

    def test_arranque_dentro_de_la_geocerca_cuenta_como_entrada(self):
        """
        Comportamiento documentado: la PRIMERA lectura interna con estado
        inactivo se trata como entrada y pasa por la validación temporal.

        No se exige haber observado antes una lectura externa: hacerlo perdería
        la primera llegada válida cuando el monitor arranca (o se reinicia) con
        el bus ya dentro del punto de control.
        """
        self.monitor.process(INSIDE)   # sin ninguna lectura previa

        self.assertEqual(len(self.checkpoint_calls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertIn(3701, bm.CONFIRMED_CHECKPOINTS)

    # ── estado de geocercas al actualizar el recorrido ───────────────────────

    def test_add_geofences_conserva_el_estado_de_las_existentes(self):
        self.monitor.process(INSIDE)
        activo = self.monitor._active[684]
        self.assertIsNotNone(activo)

        nueva = bm.geofence_from_point(make_point(685, "SEGUNDO", -4.02, -79.23, 50))
        self.monitor.add_geofences([self.geofence, nueva])

        self.assertIs(self.monitor._active[684], activo)   # no se reinicia
        self.assertIsNone(self.monitor._active[685])
        self.assertEqual(len(self.monitor.geofences), 2)   # sin duplicar la existente

        # Y seguir dentro tras la actualización no vuelve a marcar.
        self.monitor.process(INSIDE)
        self.assertEqual(len(self.checkpoint_calls), 1)


if __name__ == "__main__":
    unittest.main()
