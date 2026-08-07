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
    log_filename = f"bus_monitor.log"
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
POLL_INTERVAL_SECONDS = int(os.getenv("FAST_API_POLL_INTERVAL_SECONDS"))
WATCHER_INTERVAL_SECONDS = int(os.getenv("FAST_API_WATCHER_INTERVAL_SECONDS"))  # cada 10s el watcher evalúa si el turno cambió
LOCAL_BACKEND = os.getenv("FAST_API_LOCAL_BACKEND")
BACKEND_URL = os.getenv("FAST_API_BACKEND_URL")
BACKEND_USERNAME = os.getenv("FAST_API_BACKEND_USERNAME")
BACKEND_PASSWORD = os.getenv("FAST_API_BACKEND_PASSWORD")
BUS_REGISTER = int(os.getenv("FAST_API_BUS_REGISTER", 0))

# ─────────────────────────────────────────────
# API - CLIENT
# ─────────────────────────────────────────────

simtra = ApiService(BACKEND_URL, BACKEND_USERNAME, BACKEND_PASSWORD)

# ─────────────────────────────────────────────
# ESTADO GLOBAL + LOCK
# ─────────────────────────────────────────────

_lock = threading.Lock()

ALL_DISPATCHES: list[dict]      = []   # todos los despachos del día
CURRENT_INDEX:  Optional[int]   = None   # posición del step activo dentro de ALL_DISPATCHES
CURRENT_STEP:   Optional[dict]  = None   # alias de ALL_DISPATCHES[CURRENT_INDEX]
GEOFENCES:      list[dict]      = []
DISPATCHED:     bool            = False
REPORTED_CHECKPOINTS: set[int]  = set()  # ids de checkpoint ya reportados hoy (evita doble reporte)


# ─────────────────────────────────────────────
# LÓGICA DE DESPACHO
# ─────────────────────────────────────────────

from datetime import datetime, timedelta

def get_current_step(steps: list[dict]) -> Optional[dict]:
    """
    Evalúa la lista de despachos con tolerancia de ±5 minutos.
    """
    if not steps:
        return None

    # Se formatea explícitamente sin microsegundos antes de re-parsear;
    # str(datetime.now().time()) a veces incluye microsegundos (".111463")
    # y eso rompe strptime con el formato "%H:%M:%S".
    current_dt = datetime.strptime(datetime.now().strftime("%H:%M:%S"), "%H:%M:%S")
    TOLERANCE_MINUTES = 5
    tolerance = timedelta(minutes=TOLERANCE_MINUTES)

    upcoming = []

    for step in steps:
        start = datetime.strptime(step['start_schedule'], "%H:%M:%S")
        end   = datetime.strptime(step['end_schedule'],   "%H:%M:%S")

        # Aplicar tolerancia: ±5 minutos
        start_with_buffer = start - tolerance
        end_with_buffer   = end + tolerance

        if start_with_buffer <= current_dt <= end_with_buffer:
            return step

        if start_with_buffer > current_dt:
            upcoming.append(step)

    if upcoming:
        return min(upcoming, key=lambda s: s['start_schedule'])

    return None


def is_step_active(step: dict) -> bool:
    """
    Retorna True si está dentro del rango ±5 minutos.
    """
    TOLERANCE_MINUTES = 5
    tolerance = timedelta(minutes=TOLERANCE_MINUTES)

    start = datetime.strptime(step['start_schedule'], "%H:%M:%S")
    end   = datetime.strptime(step['end_schedule'],   "%H:%M:%S")
    # Se formatea explícitamente sin microsegundos antes de re-parsear.
    current_dt = datetime.strptime(datetime.now().strftime("%H:%M:%S"), "%H:%M:%S")

    start_with_buffer = start - tolerance
    end_with_buffer   = end + tolerance

    return start_with_buffer <= current_dt <= end_with_buffer


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
        seed_reported_checkpoints(ALL_DISPATCHES)
        cache_dispatch_locally(ALL_DISPATCHES, query_date)
        return True
    except Exception as e:
        log.error(f"Error consultando despachos: {e}")
        return False


def seed_reported_checkpoints(dispatches: list[dict]):
    """
    Marca como ya reportados (en memoria) los checkpoints que el backend remoto
    ya trae con time_reported distinto de '00:00:00'. Evita reportarlos de nuevo
    si el proceso se reinicia a mitad del día.
    """
    for step in dispatches:
        for ckpt in step["checkpoints"]:
            if ckpt.get("time_reported", "00:00:00") != "00:00:00":
                REPORTED_CHECKPOINTS.add(ckpt["id"])


def step_index(step: dict) -> Optional[int]:
    """Ubica la posición de un step dentro de ALL_DISPATCHES según su número de step."""
    for i, s in enumerate(ALL_DISPATCHES):
        if s.get("step") == step.get("step"):
            return i
    return None


def get_step_context(index: Optional[int]) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Devuelve (step_anterior, step_actual, step_siguiente) según el índice en ALL_DISPATCHES."""
    if index is None or not (0 <= index < len(ALL_DISPATCHES)):
        return None, None, None
    prev_step = ALL_DISPATCHES[index - 1] if index > 0 else None
    next_step = ALL_DISPATCHES[index + 1] if index + 1 < len(ALL_DISPATCHES) else None
    return prev_step, ALL_DISPATCHES[index], next_step


def merge_geofences(*steps: Optional[dict]) -> list[dict]:
    """Une, sin duplicar por id de punto, las geocercas de los steps recibidos."""
    merged: dict[int, dict] = {}
    for step in steps:
        if not step:
            continue
        for ckpt in step["checkpoints"]:
            point = ckpt["point"]
            merged.setdefault(point["id"], point)
    return list(merged.values())


def cache_dispatch_locally(dispatches: list[dict], date: str):
    """
    Guarda los despachos del día en el backend local (API - CLIENT) para que
    otros servicios los consulten sin repetir la llamada al backend remoto.
    """
    try:
        payload = {
            "date": date,
            "register": BUS_REGISTER,
            "data": dispatches,
        }
        resp = requests.post(f"{LOCAL_BACKEND}/api/dispatch", json=payload, timeout=5)
        resp.raise_for_status()
        log.debug("Despachos cacheados en el backend local")
    except requests.RequestException as e:
        log.error(f"No se pudo cachear los despachos localmente: {e}")


def apply_step(step: Optional[dict], monitor_ref: list):
    """
    Sincroniza el estado global con el step recibido y amplía la ventana de
    geocercas monitoreadas (anterior + actual + siguiente) de forma aditiva,
    sin perder el estado de geocercas ya en seguimiento.
    Nunca retrocede un CURRENT_INDEX que un avance por GPS ya superó (Regla 5:
    el horario nunca deshace un avance real).
    monitor_ref es [monitor] para poder mutar la referencia desde el watcher.
    """
    global CURRENT_INDEX, CURRENT_STEP, GEOFENCES, DISPATCHED

    with _lock:
        if step is None:
            CURRENT_INDEX = None
            CURRENT_STEP  = None
            GEOFENCES     = []
            DISPATCHED    = False
            log.info("Estado → SIN TURNO ACTIVO")
            return

        target_index = step_index(step)
        if target_index is None:
            log.warning("No se pudo ubicar el step recibido dentro de ALL_DISPATCHES")
            return

        if CURRENT_INDEX is not None and target_index < CURRENT_INDEX:
            log.debug("Se ignora: el horario sugiere retroceder a un step ya superado")
            return

        is_new_step   = target_index != CURRENT_INDEX
        CURRENT_INDEX = target_index
        CURRENT_STEP  = step

        prev_step, current_step, next_step = get_step_context(CURRENT_INDEX)
        monitor_ref[0].add_geofences(merge_geofences(prev_step, current_step, next_step))
        GEOFENCES = monitor_ref[0].geofences

        active     = is_step_active(step)
        DISPATCHED = active if is_new_step else (DISPATCHED or active)

        status = "ACTIVO" if DISPATCHED else f"EN ESPERA — inicia a las {step['start_schedule']}"
        log.info(
            f"Turno aplicado: {step.get('code', '?')}  "
            f"{step['start_schedule']} → {step['end_schedule']}  [{status}]"
        )


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
    global DISPATCHED, CURRENT_INDEX
    last_date = datetime.now().strftime('%Y-%m-%d')

    while not stop_event.is_set():
        time.sleep(WATCHER_INTERVAL_SECONDS)

        current_date = datetime.now().strftime('%Y-%m-%d')

        # ── Nuevo día: recarga completa ──────────────────────────────────────
        if current_date != last_date:
            log.info(f"[WATCHER] Nuevo día detectado ({current_date}) — recargando despachos")
            last_date = current_date
            with _lock:
                CURRENT_INDEX = None
                REPORTED_CHECKPOINTS.clear()
            monitor_ref[0]  = GeofenceMonitor([])
            has_dispatches  = load_all_dispatches()
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


def report_dispatch_checkpoint(step: int, checkpoint_id: int, time_reported: str):
    """Actualiza time_reported del checkpoint dentro del despacho activo cacheado localmente."""
    try:
        payload = {
            "step": step,
            "checkpoint_id": checkpoint_id,
            "time_reported": time_reported,
        }
        resp = requests.patch(f"{LOCAL_BACKEND}/api/dispatch/checkpoint", json=payload, timeout=5)
        resp.raise_for_status()
        log.debug(f"Despacho actualizado OK: step={step} checkpoint_id={checkpoint_id}")
    except requests.RequestException as e:
        log.error(f"No se pudo actualizar el despacho para checkpoint_id={checkpoint_id}: {e}")


# ─────────────────────────────────────────────
# REGISTRO DE EVENTOS
# ─────────────────────────────────────────────

def log_event(event_type: str, priority: str, message: str, payload: Optional[dict] = None):
    """
    Registra un evento genérico en la API local (tabla `events`). Reutilizable
    para cualquier tipo de evento futuro: basta con llamar a esta función con
    un event_type/priority/payload distintos, sin tocar la capa de transporte.
    """
    try:
        body = {
            "event_type": event_type,
            "priority": priority,
            "message": message,
            "payload": payload,
        }
        resp = requests.post(f"{LOCAL_BACKEND}/api/events", json=body, timeout=5)
        resp.raise_for_status()
        log.debug(f"Evento registrado OK: {event_type}")
    except requests.RequestException as e:
        log.error(f"No se pudo registrar el evento '{event_type}': {e}")


def _find_unreported_checkpoint(step: Optional[dict], point_id: int) -> Optional[dict]:
    """Busca, dentro de un step, el checkpoint del punto point_id que aún no fue reportado."""
    if not step:
        return None
    for ckpt in step["checkpoints"]:
        if ckpt["point"]["id"] == point_id and ckpt["id"] not in REPORTED_CHECKPOINTS:
            return ckpt
    return None


def resolve_and_report_checkpoint(monitor: "GeofenceMonitor", point_id: int, name: str, time_reported: str):
    """
    Decide a qué checkpoint corresponde una entrada de geocerca (usando el
    contexto step anterior/actual/siguiente) y la reporta. Prioridad:

      1) Checkpoint intermedio (no el último) del step actual, sin reportar
         → progreso normal dentro del step.
      2) Checkpoint sin reportar del step siguiente → avanza de step. Cubre
         tanto la geocerca compartida entre el último checkpoint pendiente
         del step actual y el primero del siguiente (gana el avance, el
         pendiente del step actual queda sin marcar a propósito) como una
         llegada tardía a la primera geocerca del siguiente step.
      3) Checkpoint del step actual (incluida su última geocerca cuando NO es
         compartida con el siguiente) → cierre normal, sin avanzar.
      4) Sin candidato sin reportar (incluye coincidir solo con el step
         anterior, ya superado) → se ignora, no se recupera.
    """
    prev_step, current_step, next_step = get_step_context(CURRENT_INDEX)

    current_ckpt = _find_unreported_checkpoint(current_step, point_id)
    is_last_of_current = (
        current_ckpt is not None
        and current_ckpt["order"] == max(c["order"] for c in current_step["checkpoints"])
    )

    advance = False
    if current_ckpt is not None and not is_last_of_current:
        target_step, target_ckpt = current_step, current_ckpt
    else:
        next_ckpt = _find_unreported_checkpoint(next_step, point_id)
        if next_ckpt is not None:
            target_step, target_ckpt, advance = next_step, next_ckpt, True
        elif current_ckpt is not None:
            target_step, target_ckpt = current_step, current_ckpt
        else:
            log.debug(f"Geocerca punto={point_id} sin checkpoint pendiente en el contexto actual — se ignora")
            return

    report_checkpoint(target_ckpt["id"], name, time_reported)
    report_dispatch_checkpoint(target_step["step"], target_ckpt["id"], time_reported)
    REPORTED_CHECKPOINTS.add(target_ckpt["id"])

    log_event(
        event_type="geofence_entry",
        priority="HIGH",
        message=f"Entrada a geocerca '{name}' — checkpoint {target_ckpt['id']} (step {target_step['step']})",
        payload={
            "geofence_id": point_id,
            "geofence_name": name,
            "step": target_step["step"],
            "checkpoint_id": target_ckpt["id"],
            "time_reported": time_reported,
        },
    )

    if advance:
        log.info(f"[TRIP] Avance de step por geocerca → {target_step.get('code', '?')} (step {target_step['step']})")
        apply_step(target_step, [monitor])


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
        self.geofences = list(geofences)
        self._active: dict[int, Optional[GeofenceEvent]] = {
            g["id"]: None for g in geofences
        }
        self.history: list[GeofenceEvent] = []

    def add_geofences(self, geofences: list[dict]):
        """Agrega geocercas nuevas sin tocar el estado (_active/history) de las existentes."""
        for geo in geofences:
            gid = geo["id"]
            if gid not in self._active:
                self._active[gid] = None
                self.geofences.append(geo)

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

                time_reported = now.strftime('%H:%M:%S')
                log.info(f" ENTRADA  [{gid}] {geo['name']}  @ {time_reported}")
                resolve_and_report_checkpoint(self, gid, geo["name"], time_reported)

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
    # Se monitorea el GPS mientras exista un step vigente (activo o en espera),
    # sin importar el horario — una llegada anticipada o tardía nunca debe
    # ignorarse solo por estar fuera de la ventana de schedule (Regla 5).
    try:
        while True:
            with _lock:
                current_step = CURRENT_STEP
                monitor      = monitor_ref[0]

            if current_step is None:
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