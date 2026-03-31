from api import ApiService
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv
import logging
import math
import time
import threading
import requests
import os

# ─────────────────────────────────────────────
# LOGGER
# ─────────────────────────────────────────────

def setup_logger() -> logging.Logger:
    """
    Configura el logger principal de SIMTRA.
    - Consola : nivel INFO  (solo mensajes relevantes)
    - Archivo  : nivel DEBUG (todo, incluyendo GPS tick a tick)
    El archivo se nombra simtra_YYYY-MM-DD.log para un archivo por día.
    """
    logger = logging.getLogger("simtra")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Handler consola ──────────────────────────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # ── Handler archivo (un .log por día) ────────────────────────────────────
    log_filename = f"simtra_{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


log = setup_logger()


# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

load_dotenv()
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS"))
WATCHER_INTERVAL_SECONDS = int(os.getenv("WATCHER_INTERVAL_SECONDS"))  # cada 10s el watcher evalúa si el turno cambió
LOCAL_BACKEND = os.getenv("LOCAL_BACKEND")
BACKEND_URL = os.getenv("BACKEND_URL")
BACKEND_USERNAME = os.getenv("BACKEND_USERNAME")
BACKEND_PASSWORD = os.getenv("BACKEND_PASSWORD")
BUS_REGISTER = int(os.getenv("BUS_REGISTER", 0))

# ─────────────────────────────────────────────
# API - CLIENT
# ─────────────────────────────────────────────

simtra = ApiService(BACKEND_URL, BACKEND_USERNAME, BACKEND_PASSWORD)

# ─────────────────────────────────────────────
# ESTADO GLOBAL + LOCK
# ─────────────────────────────────────────────

_lock = threading.Lock()

ALL_DISPATCHES: list[dict]     = []   # todos los despachos del día
CURRENT_STEP:   Optional[dict] = None
GEOFENCES:      list[dict]     = []
DISPATCHED:     bool           = False


# ─────────────────────────────────────────────
# LÓGICA DE DESPACHO
# ─────────────────────────────────────────────

def get_current_step(steps: list[dict]) -> Optional[dict]:
    """
    Evalúa la lista de despachos del día y devuelve:
      1. El step activo si la hora actual está dentro de su rango.
      2. El próximo step si estamos entre turnos o antes del primero.
      3. None si todos los turnos del día ya terminaron,
         o si la lista está vacía (bus sin despachos hoy).
    """
    if not steps:
        return None

    current_time = datetime.now().time()
    upcoming = []

    for step in steps:
        start = datetime.strptime(step['start_schedule'], "%H:%M:%S").time()
        end   = datetime.strptime(step['end_schedule'],   "%H:%M:%S").time()

        if start <= current_time <= end:
            return step  # turno activo → prioridad máxima

        if start > current_time:
            upcoming.append(step)

    if upcoming:
        return min(upcoming, key=lambda s: s['start_schedule'])

    return None  # todos los turnos del día ya terminaron


def is_step_active(step: dict) -> bool:
    """Retorna True si la hora actual está dentro del rango horario del step."""
    current_time = datetime.now().time()
    start = datetime.strptime(step['start_schedule'], "%H:%M:%S").time()
    end   = datetime.strptime(step['end_schedule'],   "%H:%M:%S").time()
    return start <= current_time <= end


def load_all_dispatches(date: Optional[str] = None) -> bool:
    """
    Carga todos los despachos del día desde la API de Simtra.
    Devuelve True si hay despachos, False si el bus no trabaja hoy
    (reserva, mantenimiento, o error de red).
    """
    global ALL_DISPATCHES

    query_date = date or datetime.now().strftime('%Y-%m-%d')
    log.info(f"Consultando despachos para bus={BUS_REGISTER} fecha={query_date}")

    try:
        dispatches = simtra.get_dispatch(BUS_REGISTER, query_date)
        if not dispatches:
            ALL_DISPATCHES = []
            log.warning("La API no devolvió despachos para hoy")
            return False
        ALL_DISPATCHES = dispatches
        log.info(f"Despachos cargados: {len(ALL_DISPATCHES)} turno(s)")
        return True
    except Exception as e:
        log.error(f"Error consultando despachos: {e}")
        return False


def apply_step(step: Optional[dict], monitor_ref: list):
    """
    Actualiza el estado global con el step recibido.
    Reinicializa el monitor si las geocercas cambiaron.
    monitor_ref es [monitor] para poder mutar la referencia desde el watcher.
    """
    global CURRENT_STEP, GEOFENCES, DISPATCHED

    with _lock:
        CURRENT_STEP = step

        if step is None:
            GEOFENCES  = []
            DISPATCHED = False
            log.info("Estado → SIN TURNO ACTIVO")
            return

        new_geofences = [ckpt['point'] for ckpt in step['checkpoints']]
        active        = is_step_active(step)

        # Reinicializa el monitor solo si las geocercas cambiaron
        if new_geofences != GEOFENCES:
            GEOFENCES      = new_geofences
            monitor_ref[0] = GeofenceMonitor(GEOFENCES)
            log.info(f"Monitor reiniciado — {len(GEOFENCES)} geocercas cargadas")

        DISPATCHED = active

        status = "ACTIVO" if active else f"EN ESPERA — inicia a las {step['start_schedule']}"
        log.info(
            f"Turno aplicado: {step.get('code', '?')}  "
            f"{step['start_schedule']} → {step['end_schedule']}  [{status}]"
        )


def get_checkpoint_id(point_id: int) -> Optional[int]:
    """Busca el id del checkpoint asociado al point_id dentro del turno activo."""
    if not CURRENT_STEP:
        return None
    for ckpt in CURRENT_STEP["checkpoints"]:
        if ckpt["point"]["id"] == point_id:
            return ckpt["id"]
    return None


# ─────────────────────────────────────────────
# WATCHER THREAD
# ─────────────────────────────────────────────

def schedule_watcher(monitor_ref: list, stop_event: threading.Event):
    """
    Hilo independiente que evalúa periódicamente:
      - Si el turno actual terminó → busca el siguiente en ALL_DISPATCHES.
      - Si un turno en espera ya comenzó → activa DISPATCHED.
      - Si es un nuevo día → recarga todos los despachos desde la API.
    """
    global DISPATCHED
    last_date = datetime.now().strftime('%Y-%m-%d')

    while not stop_event.is_set():
        time.sleep(WATCHER_INTERVAL_SECONDS)

        current_date = datetime.now().strftime('%Y-%m-%d')

        # ── Nuevo día: recarga completa ──────────────────────────────────────
        if current_date != last_date:
            log.info(f"[WATCHER] Nuevo día detectado ({current_date}) — recargando despachos")
            last_date      = current_date
            has_dispatches = load_all_dispatches()
            if not has_dispatches:
                log.warning("[WATCHER] Bus sin despachos hoy")
                apply_step(None, monitor_ref)
            else:
                apply_step(get_current_step(ALL_DISPATCHES), monitor_ref)
            continue

        # ── Bus sin trabajo hoy: nada que evaluar ───────────────────────────
        if not ALL_DISPATCHES:
            time.sleep(60)
            log.debug("[WATCHER] Sin despachos — reintentando consulta a la API")
            has_dispatches = load_all_dispatches()
            if has_dispatches:
                log.info("[WATCHER] Nuevos despachos detectados — aplicando step inicial")
                apply_step(get_current_step(ALL_DISPATCHES), monitor_ref)
            continue

        new_step = get_current_step(ALL_DISPATCHES)

        with _lock:
            current    = CURRENT_STEP
            dispatched = DISPATCHED

        # ── Todos los turnos terminaron ──────────────────────────────────────
        if new_step is None:
            if current is not None:
                log.info("[WATCHER] Todos los turnos del día han finalizado")
                apply_step(None, monitor_ref)
            continue

        step_changed = (current is None) or (new_step.get('step') != current.get('step'))
        active_now   = is_step_active(new_step)

        # ── El step cambió (nuevo turno o primer turno del día) ──────────────
        if step_changed:
            log.info(f"[WATCHER] Cambio de step detectado → {new_step.get('code', '?')}")
            apply_step(new_step, monitor_ref)

        # ── Mismo step: acaba de entrar en horario (EN ESPERA → ACTIVO) ─────
        elif not dispatched and active_now:
            with _lock:
                DISPATCHED = True
            log.info(f"[WATCHER]  Turno iniciado — {new_step['start_schedule']}")

        # ── Mismo step: acaba de salir del horario (buscar siguiente) ────────
        elif dispatched and not active_now:
            log.info(f"[WATCHER] Turno finalizado — {new_step['end_schedule']}")
            next_step = get_current_step(ALL_DISPATCHES)
            apply_step(next_step, monitor_ref)


# ─────────────────────────────────────────────
# CLIENTE GPS
# ─────────────────────────────────────────────

@dataclass
class GpsReading:
    latitude:  float
    longitude: float
    timestamp: str
    speed:     float


def fetch_gps() -> Optional[GpsReading]:
    """Consulta la última posición GPS desde la API local."""
    try:
        resp = requests.get(f"{LOCAL_BACKEND}/api/gps/last_position", timeout=5)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return None

        return GpsReading(
            latitude=float(data["latitude"]),
            longitude=float(data["longitude"]),
            timestamp=str(data["timestamp"]),
            speed=float(data["speed"]),
        )
    except requests.RequestException as e:
        log.error(f"Error consultando GPS: {e}")
        return None
    except (KeyError, ValueError) as e:
        log.error(f"Respuesta inesperada de la API GPS: {e}")
        return None


# ─────────────────────────────────────────────
# REPORTE DE CHECKPOINT
# ─────────────────────────────────────────────

def report_checkpoint(checkpoint_id: int, name: str, time_reported: str):
    """Envía el evento de llegada a un checkpoint a la API local."""
    try:
        payload = {
            "checkpoint_id": checkpoint_id,
            "name": name,
            "timestamp": time_reported,
        }
        resp = requests.post(f"{LOCAL_BACKEND}/api/checkpoint", json=payload, timeout=5)
        resp.raise_for_status()
        log.debug(f"Checkpoint reportado OK: id={checkpoint_id} name={name}")
    except requests.RequestException as e:
        log.error(f"No se pudo guardar checkpoint '{name}': {e}")


# ─────────────────────────────────────────────
# GEOMETRÍA
# ─────────────────────────────────────────────

EARTH_RADIUS_M = 6_371_000

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def is_inside(reading: GpsReading, geofence: dict) -> bool:
    dist = haversine_distance(
        reading.latitude, reading.longitude,
        geofence["latitude"], geofence["longitude"]
    )
    return dist <= geofence["radius"]


# ─────────────────────────────────────────────
# MONITOR DE GEOCERCAS
# ─────────────────────────────────────────────

@dataclass
class GeofenceEvent:
    geofence_id:   int
    geofence_name: str
    entry_time:    datetime

    def __str__(self):
        return (
            f"[{self.geofence_id:>4}] {self.geofence_name:<30} "
            f"entrada={self.entry_time.strftime('%H:%M:%S')}"
        )


class GeofenceMonitor:
    def __init__(self, geofences: list[dict]):
        self.geofences = geofences
        self._active: dict[int, Optional[GeofenceEvent]] = {
            g["id"]: None for g in geofences
        }
        self.history: list[GeofenceEvent] = []

    def process(self, reading: GpsReading):
        now = datetime.now()
        for geo in self.geofences:
            gid    = geo["id"]
            inside = is_inside(reading, geo)
            active = self._active[gid]

            if inside and active is None:
                event = GeofenceEvent(geofence_id=gid, geofence_name=geo["name"], entry_time=now)
                self._active[gid] = event
                self.history.append(event)

                checkpoint_id = get_checkpoint_id(gid)
                if checkpoint_id:
                    log.info(f" ENTRADA  [{gid}] {geo['name']}  @ {now.strftime('%H:%M:%S')}")
                    report_checkpoint(checkpoint_id, geo["name"], now.strftime('%H:%M:%S'))
                else:
                    log.warning(f"ENTRADA [{gid}] {geo['name']} — checkpoint_id no encontrado")

            elif not inside and active is not None:
                self._active[gid] = None
                log.info(f"🚪 SALIDA   [{gid}] {geo['name']}")

    def print_summary(self):
        separator = "═" * 75
        log.info(separator)
        log.info("RESUMEN DE GEOCERCAS VISITADAS")
        log.info(separator)
        if not self.history:
            log.info("Sin eventos registrados.")
        for ev in self.history:
            log.info(str(ev))
        log.info(separator)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    log.info("=" * 65)
    log.info("SIMTRA — Monitor de Geocercas arrancando")
    log.info("=" * 65)

    # 1. Carga todos los despachos del día
    has_dispatches = load_all_dispatches()

    # 2. Determina el step inicial y arranca el monitor
    initial_step = get_current_step(ALL_DISPATCHES) if has_dispatches else None
    monitor_ref  = [GeofenceMonitor([])]
    apply_step(initial_step, monitor_ref)

    # 3. Cabecera informativa
    log.info(f"Bus               : {BUS_REGISTER}")

    if not has_dispatches:
        log.warning("Estado            : SIN DESPACHOS HOY (reserva o mantenimiento)")
    elif CURRENT_STEP:
        status = "ACTIVO" if DISPATCHED else f"EN ESPERA — inicia a las {CURRENT_STEP['start_schedule']}"
        log.info(f"Turno actual      : {CURRENT_STEP['start_schedule']} → {CURRENT_STEP['end_schedule']}")
        log.info(f"Estado            : {status}")
        log.info(f"Geocercas cargadas: {len(GEOFENCES)}")
        log.info(f"Despachos hoy     : {len(ALL_DISPATCHES)}")
    else:
        log.info("Estado            : Todos los turnos del día han finalizado")

    log.info(f"Polling GPS       : cada {POLL_INTERVAL_SECONDS}s")
    log.info(f"Watcher           : cada {WATCHER_INTERVAL_SECONDS}s")
    log.info(f"Log archivo       : simtra_{datetime.now().strftime('%Y-%m-%d')}.log")
    log.info("=" * 65)

    # 4. Inicia el watcher en hilo demonio
    stop_event = threading.Event()
    watcher    = threading.Thread(
        target=schedule_watcher,
        args=(monitor_ref, stop_event),
        daemon=True,
        name="schedule-watcher",
    )
    watcher.start()
    log.debug("Watcher thread iniciado")

    # 5. Loop principal de tracking
    try:
        while True:
            with _lock:
                dispatched = DISPATCHED
                monitor    = monitor_ref[0]

            if not dispatched:
                time.sleep(0.5)
                continue

            reading = fetch_gps()

            if reading is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            monitor.process(reading)
            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("Detenido por el usuario (Ctrl+C)")

    finally:
        stop_event.set()
        monitor_ref[0].print_summary()
        log.info("Programa finalizado")


if __name__ == "__main__":
    main()