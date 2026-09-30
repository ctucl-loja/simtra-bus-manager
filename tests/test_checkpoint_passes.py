"""
Pasos por puntos de control con reloj y GPS controlados, de punta a punta en
el monitor: contexto temporal (como lo publica el watcher) → GeofenceMonitor →
resolución del checkpoint → persistencia local.

Cada escenario reproduce una situación de operación real: salida adelantada,
espera en la terminal compartida entre dos vueltas, recorrido atrasado, cruce
de una geocerca entre dos muestras GPS, lecturas fuera de orden, fallo
temporal de la API local, reinicio del monitor.

Nada toca red ni servicios reales: la persistencia, el evento y el audio son
dobles que solo registran las llamadas.
"""

import unittest
from datetime import datetime

import _bootstrap  # noqa: F401

import bus_monitor as bm
from _fixtures import make_checkpoint, make_point, make_step

# ── Geometría: puntos sobre un meridiano, ~1 km entre sí (0.009° de latitud) ──
BASE_LAT, LON = -4.0000, -79.2000
STEP_DEG = 0.009                 # ≈ 1 000 m
M_PER_DEG = 111_195              # metros por grado de latitud
RADIUS = 50


def lat_of(index, offset_m=0.0):
    """Latitud del punto `index` desplazada `offset_m` metros hacia el norte."""
    return BASE_LAT + index * STEP_DEG + offset_m / M_PER_DEG


def point(pid, index, name):
    return make_point(pid, name, lat_of(index), LON, RADIUS)


# Terminal T (índice 0) → SALIDA (1) → C (2) → D (3) → terminal U (4) → G (5)
T, SALIDA, C, D, U, G = 0, 1, 2, 3, 4, 5
POINTS = {
    T: point(101, T, "TERMINAL T"),
    SALIDA: point(102, SALIDA, "SALIDA T"),
    C: point(103, C, "PUNTO C"),
    D: point(104, D, "PUNTO D"),
    U: point(105, U, "TERMINAL U"),
    G: point(106, G, "PUNTO G"),
}


def ckpt(cid, index, order, time_calculated):
    return make_checkpoint(cid, POINTS[index]["id"], order, time_calculated,
                           point=dict(POINTS[index]))


def step_1():
    # Vuelta 1: T → SALIDA → C → D → U, de 07:00 a 07:30.
    return make_step(1, "07:00:00", "07:30:00", checkpoints=[
        ckpt(11, T, 0, "07:00:00"),
        ckpt(12, SALIDA, 1, "07:01:00"),
        ckpt(13, C, 2, "07:10:00"),
        ckpt(14, D, 3, "07:20:00"),
        ckpt(15, U, 4, "07:30:00"),
    ])


def step_2():
    # Vuelta 2: sale de U (el mismo punto donde terminó la 1) → G → D → C → T.
    return make_step(2, "07:40:00", "08:10:00", checkpoints=[
        ckpt(21, U, 0, "07:40:00"),
        ckpt(22, G, 1, "07:45:00"),
        ckpt(23, D, 2, "07:55:00"),
        ckpt(24, C, 3, "08:02:00"),
        ckpt(25, T, 4, "08:10:00"),
    ])


class FakeDatetime(datetime):
    """Reloj del monitor bajo control del test (hora de pared, sin zona)."""
    current = datetime(2026, 9, 30, 6, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def at(hhmmss):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(2026, 9, 30, h, m, s)


class PassScenarioTest(unittest.TestCase):
    """Base: dos vueltas cargadas, dobles de persistencia y reloj controlado."""

    def setUp(self):
        bm.reset_daily_state()
        self.addCleanup(bm.reset_daily_state)

        original_datetime = bm.datetime
        bm.datetime = FakeDatetime
        self.addCleanup(setattr, bm, "datetime", original_datetime)

        self.dispatches = [step_1(), step_2()]
        bm.ALL_DISPATCHES = self.dispatches

        self.persisted = []        # (checkpoint_id, time_reported)
        self.events = []
        self.announced = []
        self.persist_ok = True

        originals = {
            "report_checkpoint": bm.report_checkpoint,
            "report_dispatch_checkpoint": bm.report_dispatch_checkpoint,
            "log_event": bm.log_event,
        }
        original_announce = bm.audio_announcer.announce
        original_prepare = bm.audio_announcer.prepare

        def fake_report_checkpoint(ckpt_id, name, time_reported):
            if not self.persist_ok:
                return False
            self.persisted.append((ckpt_id, time_reported))
            return True

        bm.report_checkpoint = fake_report_checkpoint
        bm.report_dispatch_checkpoint = lambda step, cid, t: True
        bm.log_event = lambda event_type, priority, message, payload=None: (
            self.events.append(payload) or True)
        bm.audio_announcer.announce = lambda pid, name, on_done=None: self.announced.append(pid)
        bm.audio_announcer.prepare = lambda pid, name: None

        def restore():
            for key, value in originals.items():
                setattr(bm, key, value)
            bm.audio_announcer.announce = original_announce
            bm.audio_announcer.prepare = original_prepare

        self.addCleanup(restore)
        self.monitor_ref = [bm.GeofenceMonitor([])]

    # ── helpers ──────────────────────────────────────────────────────────
    def watcher(self, hhmmss):
        """Lo que hace el watcher en su ciclo: contexto por reloj + ventana."""
        FakeDatetime.current = at(hhmmss)
        bm.apply_context(bm.resolve_temporal_context(bm.get_dispatches(), at(hhmmss)),
                         self.monitor_ref)

    def gps(self, hhmmss, index, offset_m=0.0, *, watcher=True, timestamp=None):
        """Una lectura GPS a `offset_m` metros del punto `index`, a esa hora."""
        if watcher:
            self.watcher(hhmmss)
        FakeDatetime.current = at(hhmmss)
        reading = bm.GpsReading(latitude=lat_of(index, offset_m), longitude=LON,
                                timestamp=timestamp or f"2026-09-30T{hhmmss}", speed=30.0)
        self.monitor_ref[0].process(reading)

    def ids(self):
        return [cid for cid, _ in self.persisted]

    def time_of(self, cid):
        return dict(self.persisted).get(cid)


class InScheduleTest(PassScenarioTest):
    def test_paso_dentro_del_horario_marca_cada_punto_una_vez(self):
        self.gps("06:59:50", T, -300)
        self.gps("07:00:10", T)
        self.gps("07:00:40", T, 300)
        self.gps("07:01:05", SALIDA)
        self.gps("07:01:30", SALIDA, 300)
        self.gps("07:10:00", C)
        for _ in range(5):                      # detenido junto al punto
            self.gps("07:10:20", C, 10)
        self.gps("07:11:00", C, 300)

        self.assertEqual(self.ids(), [11, 12, 13])
        self.assertEqual(self.time_of(13), "07:10:00")


class EarlyDepartureTest(PassScenarioTest):
    def test_salida_adelantada_registra_inicio_y_salida(self):
        """El bus espera en T desde antes y arranca 2 min antes del horario."""
        self.gps("06:40:00", T, -400)
        self.gps("06:45:00", T)                 # llega a la terminal: aún no es su vuelta
        self.gps("06:57:55", T, 5)
        self.gps("06:58:05", T, 120)            # sale de T  (07:00 programado)
        self.gps("06:59:00", SALIDA)            # pasa SALIDA (07:01 programado)
        self.gps("06:59:30", SALIDA, 300)

        self.assertIn(11, self.ids(), "inicio (T) no registrado")
        self.assertIn(12, self.ids(), "salida (SALIDA) no registrada")
        # Inicio = momento en que el bus deja la terminal, no cuando llegó.
        self.assertEqual(self.time_of(11), "06:58:05")
        self.assertEqual(self.time_of(12), "06:59:00")

    def test_llegada_a_la_terminal_mucho_antes_no_marca_nada(self):
        """Llegar a la terminal 15 min antes no es un paso de la vuelta."""
        self.gps("06:40:00", T, -400)
        self.gps("06:45:00", T)
        self.gps("06:45:30", T, 10)
        self.assertEqual(self.ids(), [])

    def test_paso_por_un_punto_intermedio_del_proximo_recorrido_no_se_adelanta(self):
        """Adelanto sin respaldo de secuencia: pasar por D 20 min antes no es la vuelta 2."""
        self.gps("06:59:00", SALIDA)            # SALIDA sí (adelanto de 1 min)
        self.gps("07:20:00", D, -300)
        self.gps("07:20:10", D)                 # D pertenece a la vuelta 1 en curso
        self.assertIn(14, self.ids())
        self.assertNotIn(23, self.ids())        # nunca a la vuelta 2 (D en order 2)


class SharedTerminalTest(PassScenarioTest):
    def test_espera_en_terminal_compartida_registra_el_inicio_de_la_vuelta_siguiente(self):
        """
        Fin de la vuelta 1 en U e inicio de la vuelta 2 en U. El bus llega,
        espera y sale después del horario: el inicio de la 2 debe registrarse
        con la hora de salida real.
        """
        self.gps("07:10:00", C)
        self.gps("07:20:00", D)
        self.gps("07:27:50", U, -300)
        self.gps("07:28:00", U)                 # llega a U: fin de la vuelta 1
        for hhmmss in ("07:30:10", "07:35:00", "07:39:59", "07:40:00", "07:41:00", "07:41:25"):
            self.gps(hhmmss, U, 8)              # espera dentro de la geocerca
        self.gps("07:41:30", U, 150)            # sale de U, ya en la vuelta 2
        self.gps("07:45:00", G)

        self.assertEqual(self.time_of(15), "07:28:00")
        self.assertIn(21, self.ids(), "inicio de la vuelta 2 (U) no registrado")
        self.assertEqual(self.time_of(21), "07:41:30")
        self.assertIn(22, self.ids())
        self.assertEqual(self.ids().count(21), 1)


class LateTest(PassScenarioTest):
    def test_recorrido_atrasado_registra_los_puntos_pasados_despues_del_fin(self):
        """La vuelta 1 termina a las 07:30 pero el bus va 6 min tarde."""
        self.gps("07:00:05", T)
        self.gps("07:01:10", SALIDA)
        self.gps("07:16:00", C)
        self.gps("07:31:00", D, -300)
        self.gps("07:31:10", D)                 # después del end_schedule
        self.gps("07:36:00", U, -300)
        self.gps("07:36:10", U)

        self.assertIn(14, self.ids(), "punto intermedio atrasado descartado")
        self.assertIn(15, self.ids())
        self.assertEqual(self.time_of(14), "07:31:10")
        # Y no se asigna a la vuelta 2, que no ha empezado.
        self.assertNotIn(23, self.ids())

    def test_atraso_sin_secuencia_no_se_atribuye(self):
        """Sin haber recorrido la vuelta 1, pasar por U fuera de horario no la cierra."""
        self.gps("07:36:00", U, -300)
        self.gps("07:36:10", U)
        self.assertNotIn(15, self.ids())


class BetweenSamplesTest(PassScenarioTest):
    def test_cruce_entre_dos_muestras_se_registra_con_hora_interpolada(self):
        self.gps("07:09:58", C, -60)            # 60 m antes (radio 50: fuera)
        self.gps("07:10:02", C, 60)             # 60 m después (fuera)
        self.assertIn(13, self.ids(), "paso entre muestras no detectado")
        self.assertEqual(self.time_of(13), "07:10:00")

    def test_salto_largo_entre_muestras_no_se_interpola(self):
        """Con 5 min sin GPS no hay evidencia suficiente de cuándo pasó."""
        self.gps("07:05:00", C, -60)
        self.gps("07:10:00", C, 60)
        self.assertNotIn(13, self.ids())


class OutOfOrderTest(PassScenarioTest):
    def test_lectura_vieja_no_produce_una_salida_falsa(self):
        """
        El bus espera en T. Llega tarde una lectura de 06:50 (todavía lejos de
        la terminal): no debe leerse como una salida de T.
        """
        self.gps("06:57:00", T, 5, timestamp="2026-09-30T06:57:00")
        self.gps("06:57:02", T, -400, timestamp="2026-09-30T06:50:00")
        self.gps("06:57:04", T, 5, timestamp="2026-09-30T06:57:04")
        self.gps("07:00:35", T, 5, timestamp="2026-09-30T07:00:35")
        self.gps("07:00:40", T, 200, timestamp="2026-09-30T07:00:40")
        self.assertEqual(self.persisted, [(11, "07:00:40")])


class RepeatedPointTest(PassScenarioTest):
    def test_mismo_punto_en_otra_vuelta_se_registra_de_nuevo(self):
        self.gps("07:00:05", T)
        self.gps("07:00:40", T, 300)
        self.gps("07:10:00", C)
        self.gps("07:10:40", C, 300)
        self.gps("08:02:00", C, -300)
        self.gps("08:02:05", C)                 # C otra vez, ahora vuelta 2
        self.assertIn(13, self.ids())
        self.assertIn(24, self.ids())

    def test_omision_de_un_punto_no_bloquea_el_siguiente(self):
        self.gps("07:00:05", T)
        self.gps("07:00:40", T, 300)
        # SALIDA y C no se detectan
        self.gps("07:20:00", D)
        self.assertIn(14, self.ids())


class PersistenceRecoveryTest(PassScenarioTest):
    def test_fallo_temporal_de_la_api_local_se_recupera_con_la_hora_original(self):
        self.persist_ok = False
        self.gps("07:10:00", C)                 # detectado, persistencia falla
        self.gps("07:10:40", C, 300)            # ya salió de la geocerca
        self.persist_ok = True
        self.gps("07:11:00", C, 1200)
        self.assertEqual(self.persisted, [(13, "07:10:00")])


class RestartTest(PassScenarioTest):
    def test_monitor_nuevo_con_el_bus_dentro_de_la_terminal_registra_la_salida(self):
        self.watcher("06:58:00")
        self.monitor_ref[0] = bm.GeofenceMonitor([])   # reinicio del proceso
        self.gps("06:58:30", T, 5)                      # primera lectura: dentro
        self.gps("07:00:30", T, 5)
        self.gps("07:00:40", T, 200)                    # sale
        self.assertEqual(self.time_of(11), "07:00:40")

    def test_reinicio_no_repite_un_paso_local_aun_no_subido(self):
        """El paso de C ya está en la cola local; el backend todavía no lo tiene."""
        originals = (bm.read_local_dispatch, bm.fetch_pending_checkpoint_ids)
        self.addCleanup(lambda: (setattr(bm, "read_local_dispatch", originals[0]),
                                 setattr(bm, "fetch_pending_checkpoint_ids", originals[1])))
        bm.read_local_dispatch = lambda: None
        bm.fetch_pending_checkpoint_ids = lambda: [13]

        bm.seed_local_reports("2026-09-30")
        self.gps("07:12:00", C, -300)
        self.gps("07:12:05", C)
        self.assertNotIn(13, self.ids())

    def test_reinicio_toma_los_pasos_del_despacho_local(self):
        local = [step_1(), step_2()]
        local[0]["checkpoints"][2]["time_reported"] = "07:10:00"     # C ya marcado
        original = bm.read_local_dispatch
        self.addCleanup(setattr, bm, "read_local_dispatch", original)
        original_pending = bm.fetch_pending_checkpoint_ids
        self.addCleanup(setattr, bm, "fetch_pending_checkpoint_ids", original_pending)
        bm.read_local_dispatch = lambda: {"date": "2026-09-30", "register": bm.BUS_REGISTER,
                                          "data": local}
        bm.fetch_pending_checkpoint_ids = lambda: []

        bm.seed_local_reports("2026-09-30")
        self.assertIn(13, bm.CONFIRMED_CHECKPOINTS)


class LocalClockTest(unittest.TestCase):
    def test_hora_local_es_la_de_ecuador_aunque_el_sistema_este_en_utc(self):
        import os
        import time as time_module
        previous = os.environ.get("TZ")

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time_module.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "UTC"
        time_module.tzset()

        from datetime import timezone as tz
        from zoneinfo import ZoneInfo
        expected = datetime.now(tz.utc).astimezone(ZoneInfo("America/Guayaquil")).replace(tzinfo=None)
        delta = abs((bm.local_now() - expected).total_seconds())
        self.assertLess(delta, 2)
        # Y no es la hora del sistema (UTC), que está 5 h adelante.
        self.assertGreater(abs((datetime.now() - bm.local_now()).total_seconds()), 4 * 3600)


if __name__ == "__main__":
    unittest.main()
