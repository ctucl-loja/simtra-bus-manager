from api import ApiService
import audio_announcer
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

# Reintento de consulta de despachos cuando el bus arranca sin ninguno (reserva,
# mantenimiento o backend caído). El watcher no duerme este tiempo: solo evita
# repreguntar antes de que transcurra.
NO_DISPATCH_RETRY_SECONDS = 60

# Omisiones toleradas antes de considerar la secuencia inconsistente. Solo se
# aplica al cierre tardío de un recorrido (ver resolve_checkpoint_candidate).
MAX_SKIPPED_CHECKPOINTS = 2

# Ventana de gracia para cerrar el último checkpoint del ÚLTIMO step del día.
# En BETWEEN_STEPS el cierre tardío queda acotado naturalmente por el
# start_schedule del siguiente step; después del último recorrido no existe ese
# límite, así que se acota aquí. NO es la vieja tolerancia de horario: no
# adelanta ningún step ni habilita marcaciones de un despacho que no comenzó.
CLOSING_GRACE_MINUTES = 10

# Tolerancia con la que se clasifica una llegada como "a tiempo" en el evento
# que ve el conductor. Es SOLO una clasificación informativa: no interviene en
# la selección de steps, la autorización de marcaje ni ninguna decisión horaria.
ON_TIME_TOLERANCE_SECONDS = 30

# ─────────────────────────────────────────────
# API - CLIENT
# ─────────────────────────────────────────────

simtra = ApiService(BACKEND_URL, BACKEND_USERNAME, BACKEND_PASSWORD)


# ─────────────────────────────────────────────
# CONTEXTO TEMPORAL
#
# Única fuente de verdad sobre en qué momento del día está el bus. Se calcula
# exclusivamente con la fecha y los horarios del despacho — nunca con el GPS:
#
#   HORARIO      → define el step autorizado a recibir marcaciones
#   GPS          → solo detecta la entrada a una geocerca
#   SECUENCIA    → decide si ese checkpoint es coherente
#   PERSISTENCIA → registra el evento
# ─────────────────────────────────────────────

BEFORE_FIRST_STEP = "BEFORE_FIRST_STEP"   # el primer recorrido del día aún no empieza
ACTIVE_STEP       = "ACTIVE_STEP"         # hay un recorrido dentro de su horario
BETWEEN_STEPS     = "BETWEEN_STEPS"       # uno terminó y el siguiente todavía no empieza
AFTER_LAST_STEP   = "AFTER_LAST_STEP"     # todos los recorridos del día terminaron


def step_number(step: Optional[dict]) -> Optional[int]:
    return step.get("step") if step else None


def schedule_seconds(value: str) -> int:
    """'HH:MM:SS' → segundos desde medianoche."""
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def now_seconds(now: datetime) -> int:
    """
    Hora del día de `now` en segundos. Se calcula con los campos del datetime en
    vez de strptime: el formato "%H:%M:%S" no tolera los microsegundos que
    arrastra datetime.now().
    """
    return now.hour * 3600 + now.minute * 60 + now.second


# Estados de puntualidad. Nombres técnicos estables: la traducción a
# "Adelantado / A tiempo / Atrasado" es responsabilidad de la UI.
EARLY   = "EARLY"
ON_TIME = "ON_TIME"
LATE    = "LATE"

ARRIVAL_LABELS = {EARLY: "ADELANTADO", ON_TIME: "A TIEMPO", LATE: "ATRASADO"}

HALF_DAY_SECONDS = 12 * 3600
DAY_SECONDS      = 24 * 3600


def calculate_arrival_status(scheduled_time: str, reported_time: str) -> Optional[dict]:
    """
    Compara la hora programada del checkpoint (`time_calculated`) con la hora
    real de llegada (`time_reported`).

        difference_seconds > 0  → llegó después  → LATE
        difference_seconds < 0  → llegó antes    → EARLY
        |difference| <= ON_TIME_TOLERANCE_SECONDS → ON_TIME

    Devuelve None si alguna de las horas no es parseable, para que el evento se
    emita igual (con los datos del punto) sin inventar una clasificación.
    """
    try:
        difference = schedule_seconds(reported_time) - schedule_seconds(scheduled_time)
    except (AttributeError, ValueError):
        log.warning(
            f"[ARRIVAL] Horas no parseables: programada={scheduled_time!r} "
            f"reportada={reported_time!r}"
        )
        return None

    # Cruce de medianoche: un recorrido que termina pasadas las 00:00 daría una
    # diferencia de casi un día entero; se normaliza al desfase más corto.
    if difference > HALF_DAY_SECONDS:
        difference -= DAY_SECONDS
    elif difference < -HALF_DAY_SECONDS:
        difference += DAY_SECONDS

    if difference > ON_TIME_TOLERANCE_SECONDS:
        status = LATE
    elif difference < -ON_TIME_TOLERANCE_SECONDS:
        status = EARLY
    else:
        status = ON_TIME

    return {"status": status, "difference_seconds": difference}


@dataclass(frozen=True)
class TemporalContext:
    """
    Fotografía inmutable del momento del día.

    `current_step` SOLO existe en ACTIVE_STEP: es el único step autorizado a
    recibir marcaciones normales. Un step que todavía no empieza vive en
    `next_step` y nunca en `current_step` — esa separación es la que impide que
    un despacho futuro reciba checkpoints por adelantado.
    """
    state:         str
    previous_step: Optional[dict] = None
    current_step:  Optional[dict] = None
    next_step:     Optional[dict] = None

    @property
    def key(self) -> tuple:
        """Identidad del contexto: sirve para loguear solo cuando algo cambió."""
        return (
            self.state,
            step_number(self.previous_step),
            step_number(self.current_step),
            step_number(self.next_step),
        )

    def describe(self) -> str:
        if self.state == ACTIVE_STEP:
            step = self.current_step
            return (
                f"state={ACTIVE_STEP} step={step['step']} "
                f"horario={step['start_schedule']}→{step['end_schedule']}"
            )
        if self.state == BETWEEN_STEPS:
            return (
                f"state={BETWEEN_STEPS} previous={step_number(self.previous_step)} "
                f"next={step_number(self.next_step)} "
                f"(inicia {self.next_step['start_schedule']})"
            )
        if self.state == BEFORE_FIRST_STEP:
            if self.next_step is None:
                return f"state={BEFORE_FIRST_STEP} (sin despachos)"
            return (
                f"state={BEFORE_FIRST_STEP} next={step_number(self.next_step)} "
                f"(inicia {self.next_step['start_schedule']})"
            )
        return f"state={AFTER_LAST_STEP} previous={step_number(self.previous_step)}"


def resolve_temporal_context(dispatches: list[dict], now: datetime) -> TemporalContext:
    """
    Resuelve el estado temporal del día a partir ÚNICAMENTE de los horarios.

    Fronteras estrictas: un step está activo si `start <= now <= end`, sin
    tolerancia. Un step que empieza en 4 minutos NO está activo.

    Sin despachos devuelve BEFORE_FIRST_STEP sin steps: un estado sin
    `current_step` ni `previous_step` no puede autorizar ninguna escritura.
    """
    if not dispatches:
        return TemporalContext(state=BEFORE_FIRST_STEP)

    steps = sorted(dispatches, key=lambda s: schedule_seconds(s["start_schedule"]))
    current_sec = now_seconds(now)

    active_indexes = [
        i for i, step in enumerate(steps)
        if schedule_seconds(step["start_schedule"]) <= current_sec <= schedule_seconds(step["end_schedule"])
    ]

    if active_indexes:
        if len(active_indexes) > 1:
            solapados = [step_number(steps[i]) for i in active_indexes]
            log.warning(f"[TEMPORAL] Horarios solapados: steps {solapados} — se usa el primero")
        index = active_indexes[0]
        return TemporalContext(
            state=ACTIVE_STEP,
            previous_step=steps[index - 1] if index > 0 else None,
            current_step=steps[index],
            next_step=steps[index + 1] if index + 1 < len(steps) else None,
        )

    if current_sec < schedule_seconds(steps[0]["start_schedule"]):
        return TemporalContext(state=BEFORE_FIRST_STEP, next_step=steps[0])

    finished = [s for s in steps if schedule_seconds(s["end_schedule"]) < current_sec]
    upcoming = [s for s in steps if schedule_seconds(s["start_schedule"]) > current_sec]

    if not upcoming:
        return TemporalContext(
            state=AFTER_LAST_STEP,
            previous_step=finished[-1] if finished else None,
        )

    return TemporalContext(
        state=BETWEEN_STEPS,
        previous_step=finished[-1] if finished else None,
        next_step=upcoming[0],
    )


# ─────────────────────────────────────────────
# ESTADO GLOBAL + LOCK
#
# El lock protege únicamente lecturas/escrituras rápidas en memoria. Nunca se
# mantiene tomado durante HTTP, audio ni ninguna otra I/O.
# ─────────────────────────────────────────────

_lock = threading.Lock()

ALL_DISPATCHES:  list[dict]      = []                                   # todos los despachos del día
CURRENT_CONTEXT: TemporalContext = TemporalContext(state=BEFORE_FIRST_STEP)
REPORTED_CHECKPOINTS: set[int]   = set()                                # ids de checkpoint ya reportados hoy


def get_dispatches() -> list[dict]:
    """
    Snapshot de los despachos. La lista se reemplaza entera, nunca se muta en
    sitio, así que devolver la referencia bajo lock es seguro.
    """
    with _lock:
        return ALL_DISPATCHES


def get_context() -> TemporalContext:
    with _lock:
        return CURRENT_CONTEXT


def get_reported_snapshot() -> frozenset:
    with _lock:
        return frozenset(REPORTED_CHECKPOINTS)


def reserve_checkpoint(checkpoint_id: int) -> bool:
    """
    Test-and-set atómico: devuelve True si este hilo se quedó con el derecho de
    reportar el checkpoint. Se reserva ANTES de la persistencia para que dos
    entradas de geocerca casi simultáneas no lo reporten dos veces.
    """
    with _lock:
        if checkpoint_id in REPORTED_CHECKPOINTS:
            return False
        REPORTED_CHECKPOINTS.add(checkpoint_id)
        return True


def reset_daily_state():
    """Borra el rastro del día anterior: ningún step viejo debe quedar elegible."""
    global ALL_DISPATCHES, CURRENT_CONTEXT
    with _lock:
        ALL_DISPATCHES  = []
        CURRENT_CONTEXT = TemporalContext(state=BEFORE_FIRST_STEP)
        REPORTED_CHECKPOINTS.clear()


# ─────────────────────────────────────────────
# CARGA DE DESPACHOS
# ─────────────────────────────────────────────

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
            with _lock:
                ALL_DISPATCHES = []
            log.warning("La API no devolvió despachos para hoy")
            return False
        with _lock:
            ALL_DISPATCHES = dispatches
        log.info(f"Despachos cargados: {len(dispatches)} turno(s)")
        seed_reported_checkpoints(dispatches)
        cache_dispatch_locally(dispatches, query_date)
        sync_vehicle_info()
        return True
    except Exception as e:
        log.error(f"Error consultando despachos: {e}")
        return False


def sync_vehicle_info():
    """
    Descarga la información del vehículo asociado al bus (vía services/api.py,
    nunca con requests directo al backend remoto) y la cachea en el backend
    local, igual que se hace con el despacho.
    """
    vehicle = simtra.get_vehicle(BUS_REGISTER)
    if not vehicle:
        log.warning("La API no devolvió información del vehículo")
        return
    cache_vehicle_locally(vehicle)


def cache_vehicle_locally(vehicle: dict):
    """Guarda la información del vehículo en el backend local (upsert por register)."""
    try:
        payload = {
            "register": BUS_REGISTER,
            "plate": vehicle.get("plate"),
            "data": vehicle,
        }
        resp = requests.post(f"{LOCAL_BACKEND}/api/vehicle", json=payload, timeout=5)
        resp.raise_for_status()
        log.debug("Información del vehículo cacheada en el backend local")
    except requests.RequestException as e:
        log.error(f"No se pudo cachear la información del vehículo: {e}")


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


def seed_reported_checkpoints(dispatches: list[dict]):
    """
    Marca como ya reportados (en memoria) los checkpoints que el backend remoto
    ya trae con time_reported distinto de '00:00:00'. Evita reportarlos de nuevo
    si el proceso se reinicia a mitad del día.
    """
    with _lock:
        for step in dispatches:
            for ckpt in step["checkpoints"]:
                if ckpt.get("time_reported", "00:00:00") != "00:00:00":
                    REPORTED_CHECKPOINTS.add(ckpt["id"])


# ─────────────────────────────────────────────
# VENTANA DE GEOCERCAS
#
# Define qué geocercas se OBSERVAN físicamente. No tiene relación con qué step
# está autorizado a escribir: que una geocerca esté cargada en el
# GeofenceMonitor no significa que su checkpoint pueda registrarse.
# ─────────────────────────────────────────────

def step_index(dispatches: list[dict], step: dict) -> Optional[int]:
    """Ubica la posición de un step dentro de la lista según su número de step."""
    for i, s in enumerate(dispatches):
        if s.get("step") == step.get("step"):
            return i
    return None


def merge_geofences(*steps: Optional[dict]) -> list[dict]:
    """
    Une, sin duplicar por id de punto, las geocercas de los steps recibidos.

    Deduplicar por point.id es correcto a nivel FÍSICO: un mismo punto
    geográfico se vigila una sola vez. La identidad del checkpoint (step +
    checkpoint id + horario) se resuelve después, en resolve_checkpoint_candidate.
    """
    merged: dict[int, dict] = {}
    for step in steps:
        if not step:
            continue
        for ckpt in step["checkpoints"]:
            point = ckpt["point"]
            merged.setdefault(point["id"], point)
    return list(merged.values())


# ─────────────────────────────────────────────
# AUDIO
#
# El subsistema de audio se identifica por checkpoint["point"]["id"] (el punto
# físico), NUNCA por checkpoint["id"]:
#
#     checkpoint["id"]          → persistencia y sincronización con el backend
#     checkpoint["point"]["id"] → cache y reutilización del mp3
#
# Un mismo punto se repite en varios steps y líneas, así que su audio se genera
# una sola vez y se reutiliza (ver services/audio_announcer.py).
#
# El prefetch puede cruzar al siguiente step: preparar el audio de un checkpoint
# NO implica que ese checkpoint pueda registrarse. Son conceptos separados.
# ─────────────────────────────────────────────

def get_upcoming_checkpoints(step: dict, after_order: int, count: int) -> list[dict]:
    """
    Devuelve hasta `count` checkpoints posteriores a after_order dentro de step,
    continuando en los steps siguientes si hace falta. Uso exclusivo: audio.
    """
    result: list[dict] = []
    dispatches = get_dispatches()
    idx = step_index(dispatches, step)
    if idx is None:
        return result

    remaining = sorted(
        (c for c in step["checkpoints"] if c["order"] > after_order),
        key=lambda c: c["order"],
    )
    result.extend(remaining)

    next_idx = idx + 1
    while len(result) < count and next_idx < len(dispatches):
        result.extend(sorted(dispatches[next_idx]["checkpoints"], key=lambda c: c["order"]))
        next_idx += 1

    return result[:count]


def prefetch_upcoming_audio(step: dict, after_order: int, count: int = 2):
    """
    Prepara con anticipación el audio de los siguientes `count` checkpoints
    después de after_order (cruzando de step si hace falta), para que estén
    listos antes de que el bus llegue físicamente. No bloquea.
    """
    for ckpt in get_upcoming_checkpoints(step, after_order, count):
        audio_announcer.prepare(ckpt["point"]["id"], ckpt["point"]["name"])


# ─────────────────────────────────────────────
# APLICACIÓN DEL CONTEXTO
# ─────────────────────────────────────────────

def apply_context(context: TemporalContext, monitor_ref: list) -> bool:
    """
    Publica el contexto temporal como estado vigente y amplía la ventana de
    geocercas observadas (anterior + actual + siguiente).

    No decide nada por sí misma: el contexto ya viene resuelto por el reloj.
    Devuelve True si el contexto cambió respecto del anterior.
    monitor_ref es [monitor] para poder mutar la referencia desde el watcher.
    """
    global CURRENT_CONTEXT

    with _lock:
        previous = CURRENT_CONTEXT
        changed  = previous.key != context.key
        CURRENT_CONTEXT = context
        monitor = monitor_ref[0]

    # Fuera del lock: tocar el monitor y encolar audio no debe bloquear al hilo GPS.
    monitor.add_geofences(
        merge_geofences(context.previous_step, context.current_step, context.next_step)
    )

    if not changed:
        return False

    log.info(f"[TEMPORAL] {context.describe()}")

    # Al entrar en un recorrido nuevo se precarga el audio de sus primeros
    # checkpoints: todavía no hubo ninguna reproducción que dispare la cadena.
    entered_new_step = (
        context.state == ACTIVE_STEP
        and step_number(previous.current_step) != step_number(context.current_step)
    )
    if entered_new_step:
        prefetch_upcoming_audio(context.current_step, -1, 2)

    return True


# ─────────────────────────────────────────────
# WATCHER THREAD
#
# Único responsable de hacer avanzar el contexto temporal. El GPS nunca cambia
# de step: si el bus está detenido, el contexto igual avanza cuando el reloj
# alcanza el start_schedule del siguiente recorrido.
# ─────────────────────────────────────────────

def schedule_watcher(monitor_ref: list, stop_event: threading.Event):
    """
    Hilo independiente que reevalúa periódicamente el contexto temporal:
      - Si cambió el día → recarga despachos y reinicia el estado.
      - Si el bus no tenía despachos → reintenta cada NO_DISPATCH_RETRY_SECONDS.
      - En cualquier otro caso → resuelve el contexto con la hora actual.
    """
    last_date  = datetime.now().strftime('%Y-%m-%d')
    last_retry = 0.0

    while not stop_event.is_set():
        time.sleep(WATCHER_INTERVAL_SECONDS)

        now          = datetime.now()
        current_date = now.strftime('%Y-%m-%d')

        # ── Nuevo día: recarga completa ──────────────────────────────────────
        if current_date != last_date:
            log.info(f"[WATCHER] Nuevo día detectado ({current_date}) — recargando despachos")
            last_date = current_date
            reset_daily_state()
            monitor_ref[0] = GeofenceMonitor([])
            if not load_all_dispatches():
                log.warning("[WATCHER] Bus sin despachos hoy")
            apply_context(resolve_temporal_context(get_dispatches(), datetime.now()), monitor_ref)
            continue

        dispatches = get_dispatches()

        # ── Bus sin despachos: reintento espaciado, sin dormir el hilo ───────
        if not dispatches:
            if time.monotonic() - last_retry >= NO_DISPATCH_RETRY_SECONDS:
                last_retry = time.monotonic()
                log.debug("[WATCHER] Sin despachos — reintentando consulta a la API")
                if load_all_dispatches():
                    log.info("[WATCHER] Despachos disponibles — resolviendo contexto temporal")
                    apply_context(
                        resolve_temporal_context(get_dispatches(), datetime.now()), monitor_ref
                    )
            continue

        # ── Curso normal: el reloj manda ─────────────────────────────────────
        apply_context(resolve_temporal_context(dispatches, now), monitor_ref)


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
# PERSISTENCIA
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
    """Actualiza time_reported del checkpoint dentro del despacho cacheado localmente."""
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


def emit_arrival_event(step: dict, ckpt: dict, time_reported: str, reason: str):
    """
    Emite el evento `checkpoint_arrival`: el único canal por el que bus-display
    se entera de una llegada.

    Se llama SOLO desde resolve_and_report_checkpoint y SOLO después de que el
    checkpoint fue aceptado y persistido; una geocerca descartada por horario o
    por secuencia nunca produce evento, y por lo tanto nunca produce popup.

    El payload lleva los datos ya calculados (incluida la diferencia de tiempo)
    para que la pantalla solo presente: no recalcula puntualidad ni reimplementa
    ninguna regla de despacho.
    """
    scheduled_time = ckpt.get("time_calculated")
    arrival = calculate_arrival_status(scheduled_time, time_reported)

    status     = arrival["status"] if arrival else None
    difference = arrival["difference_seconds"] if arrival else None
    point      = ckpt["point"]

    if arrival:
        message = (
            f"Llegada a {point['name']} — {ARRIVAL_LABELS[status]} ({difference:+d} s)"
        )
    else:
        message = f"Llegada a {point['name']} — sin hora programada válida"

    log_event(
        event_type="checkpoint_arrival",
        priority="MEDIUM",
        message=message,
        payload={
            "step": step["step"],
            "checkpoint_id": ckpt["id"],
            "point_id": point["id"],
            "point_name": point["name"],
            "order": ckpt["order"],
            "scheduled_time": scheduled_time,
            "reported_time": time_reported,
            "difference_seconds": difference,
            "arrival_status": status,
            "line": step.get("line"),
            "reason": reason,   # progreso normal / cierre tardío (auditoría)
        },
    )
    log.info(f"[ARRIVAL] {message}")


# ─────────────────────────────────────────────
# ELEGIBILIDAD DE CHECKPOINTS
#
# Función pura: recibe el contexto temporal ya resuelto, el punto en el que
# entró el bus y los checkpoints ya reportados; decide si existe un candidato
# válido. No hace I/O ni toca estado global.
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class CheckpointCandidate:
    step:       dict
    checkpoint: dict
    reason:     str


def find_unreported_checkpoint(step: Optional[dict], point_id: int, reported) -> Optional[dict]:
    """Busca, dentro de un step, el checkpoint del punto point_id que aún no fue reportado."""
    if not step:
        return None
    for ckpt in step["checkpoints"]:
        if ckpt["point"]["id"] == point_id and ckpt["id"] not in reported:
            return ckpt
    return None


def step_has_point(step: Optional[dict], point_id: int) -> bool:
    """True si alguna geocerca del step corresponde a ese punto físico."""
    if not step:
        return False
    return any(ckpt["point"]["id"] == point_id for ckpt in step["checkpoints"])


def is_last_checkpoint(step: dict, ckpt: dict) -> bool:
    return ckpt["order"] == max(c["order"] for c in step["checkpoints"])


def count_skipped(step: dict, target_ckpt: dict, reported) -> int:
    """Checkpoints anteriores (order menor) del mismo step que siguen sin reportar."""
    return sum(
        1 for c in step["checkpoints"]
        if c["order"] < target_ckpt["order"] and c["id"] not in reported
    )


def has_consistent_sequence(step: dict, target_ckpt: dict, reported) -> bool:
    """
    Evalúa si el recorrido ya hecho respalda marcar target_ckpt.

    Hasta MAX_SKIPPED_CHECKPOINTS omisiones se consideran inconsistencias
    normales del GPS y no impiden marcar; más que eso indica que el bus no
    venía realmente recorriendo este step y que la entrada a la geocerca es
    un cruce de paso, no una llegada legítima.
    """
    return count_skipped(step, target_ckpt, reported) <= MAX_SKIPPED_CHECKPOINTS


def within_closing_grace(step: dict, now: datetime) -> bool:
    """Ventana de gracia posterior al end_schedule para el cierre del último recorrido."""
    end = schedule_seconds(step["end_schedule"])
    return now_seconds(now) - end <= CLOSING_GRACE_MINUTES * 60


def log_not_started(context: TemporalContext, point_id: int):
    """Deja constancia cuando la geocerca pertenece a un step que aún no empieza."""
    if step_has_point(context.next_step, point_id):
        log.info(
            f"[GEOFENCE] point={point_id} ignorado: pertenece al step "
            f"{step_number(context.next_step)} pero todavía no inicia "
            f"({context.next_step['start_schedule']})"
        )


def resolve_late_closing_candidate(
    context: TemporalContext, point_id: int, reported
) -> Optional[CheckpointCandidate]:
    """
    Excepción de cierre tardío: el bus puede terminar físicamente el recorrido
    unos minutos después de que su horario acabó.

    SOLO el último checkpoint del step anterior es evaluable, y solo si la
    secuencia recorrida lo respalda. Un checkpoint intermedio del step anterior
    nunca se marca fuera de horario.
    """
    step = context.previous_step
    if step is None:
        return None

    ckpt = find_unreported_checkpoint(step, point_id, reported)
    if ckpt is None:
        return None

    if not is_last_checkpoint(step, ckpt):
        log.info(
            f"[TRANSITION] checkpoint intermedio rechazado step={step_number(step)} "
            f"checkpoint={ckpt['id']}: fuera de horario solo se cierra el último punto"
        )
        return None

    skipped = count_skipped(step, ckpt, reported)
    if skipped > MAX_SKIPPED_CHECKPOINTS:
        log.info(
            f"[TRANSITION] checkpoint final rechazado step={step_number(step)} "
            f"checkpoint={ckpt['id']} skipped={skipped}"
        )
        return None

    log.info(
        f"[TRANSITION] checkpoint final aceptado step={step_number(step)} "
        f"checkpoint={ckpt['id']} skipped={skipped}"
    )
    return CheckpointCandidate(step=step, checkpoint=ckpt, reason="cierre tardío")


def resolve_checkpoint_candidate(
    context: TemporalContext, point_id: int, reported, now: datetime
) -> Optional[CheckpointCandidate]:
    """
    Decide qué checkpoint —si alguno— corresponde a la entrada a una geocerca.

        ACTIVE_STEP    → solo checkpoints del step vigente
        BETWEEN_STEPS  → solo el último checkpoint del step anterior (cierre tardío)
        AFTER_LAST_STEP→ igual, acotado por CLOSING_GRACE_MINUTES
        BEFORE_FIRST_STEP → nada

    Nunca se busca en `next_step`: un despacho que no ha comenzado no recibe
    marcaciones, aunque el bus haya entrado físicamente en su geocerca.
    """
    if context.state == ACTIVE_STEP:
        ckpt = find_unreported_checkpoint(context.current_step, point_id, reported)
        if ckpt is not None:
            return CheckpointCandidate(
                step=context.current_step, checkpoint=ckpt, reason="progreso normal"
            )
        log_not_started(context, point_id)
        return None

    if context.state == BETWEEN_STEPS:
        # El siguiente step nunca es elegible antes de su start_schedule; el
        # límite superior de esta ventana es justamente ese horario.
        log_not_started(context, point_id)
        return resolve_late_closing_candidate(context, point_id, reported)

    if context.state == AFTER_LAST_STEP:
        if context.previous_step and not within_closing_grace(context.previous_step, now):
            return None
        return resolve_late_closing_candidate(context, point_id, reported)

    return None


# ─────────────────────────────────────────────
# REPORTE DE CHECKPOINT
# ─────────────────────────────────────────────

def resolve_and_report_checkpoint(point_id: int, name: str, time_reported: str):
    """
    Orquesta una entrada de geocerca:

        snapshot coherente → candidato → reserva → persistencia → audio

    No cambia de step: el avance del recorrido lo hace exclusivamente el
    watcher a partir del reloj.
    """
    now = datetime.now()

    # Snapshot coherente del estado compartido; el resto del trabajo (HTTP,
    # audio) ocurre fuera del lock.
    with _lock:
        context  = CURRENT_CONTEXT
        reported = frozenset(REPORTED_CHECKPOINTS)

    candidate = resolve_checkpoint_candidate(context, point_id, reported, now)
    if candidate is None:
        log.debug(
            f"Geocerca punto={point_id} sin checkpoint autorizado "
            f"(state={context.state}) — se ignora"
        )
        return

    target_step = candidate.step
    target_ckpt = candidate.checkpoint

    if not reserve_checkpoint(target_ckpt["id"]):
        log.debug(f"Checkpoint {target_ckpt['id']} ya reportado por otra entrada — se ignora")
        return

    report_checkpoint(target_ckpt["id"], name, time_reported)
    report_dispatch_checkpoint(target_step["step"], target_ckpt["id"], time_reported)

    log.info(
        f"[TRIP] Checkpoint {target_ckpt['id']} marcado en step "
        f"{step_number(target_step)} ({candidate.reason}) @ {time_reported}"
    )

    emit_arrival_event(target_step, target_ckpt, time_reported, candidate.reason)

    # Anuncio de voz: prioridad más baja, nunca bloquea el hilo de GPS (solo
    # encola). Reproduce lo que ya estaba prefetch-eado; al terminar, prepara
    # los próximos 2 checkpoints del recorrido.
    audio_announcer.announce(
        target_ckpt["point"]["id"],
        target_ckpt["point"]["name"],
        on_done=lambda _pid: prefetch_upcoming_audio(target_step, target_ckpt["order"], 2),
    )


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
    """
    Detecta entradas y salidas de geocercas. Solo sabe de geometría: qué
    checkpoint corresponde a una entrada —y si puede registrarse— lo decide
    resolve_and_report_checkpoint con el contexto temporal.
    """

    def __init__(self, geofences: list[dict]):
        self._mutex = threading.Lock()   # el watcher agrega geocercas mientras el hilo GPS itera
        self.geofences = list(geofences)
        self._active: dict[int, Optional[GeofenceEvent]] = {
            g["id"]: None for g in geofences
        }
        self.history: list[GeofenceEvent] = []

    def add_geofences(self, geofences: list[dict]):
        """Agrega geocercas nuevas sin tocar el estado (_active/history) de las existentes."""
        with self._mutex:
            for geo in geofences:
                gid = geo["id"]
                if gid not in self._active:
                    self._active[gid] = None
                    self.geofences.append(geo)

    def process(self, reading: GpsReading):
        now = datetime.now()

        with self._mutex:
            geofences = list(self.geofences)   # snapshot: el watcher puede estar agregando

        for geo in geofences:
            gid    = geo["id"]
            inside = is_inside(reading, geo)
            active = self._active[gid]

            if inside and active is None:
                event = GeofenceEvent(geofence_id=gid, geofence_name=geo["name"], entry_time=now)
                self._active[gid] = event
                self.history.append(event)

                time_reported = now.strftime('%H:%M:%S')
                log.info(f" ENTRADA  [{gid}] {geo['name']}  @ {time_reported}")
                resolve_and_report_checkpoint(gid, geo["name"], time_reported)

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

    # 2. Resuelve el contexto temporal con la hora actual. Un reinicio a media
    #    jornada retoma el recorrido que corresponde a esa hora, nunca el primero.
    monitor_ref = [GeofenceMonitor([])]
    context = resolve_temporal_context(get_dispatches(), datetime.now())
    apply_context(context, monitor_ref)

    # 3. Cabecera informativa
    log.info(f"Bus               : {BUS_REGISTER}")

    if not has_dispatches:
        log.warning("Estado            : SIN DESPACHOS HOY (reserva o mantenimiento)")
    else:
        log.info(f"Estado            : {context.describe()}")
        log.info(f"Geocercas cargadas: {len(monitor_ref[0].geofences)}")
        log.info(f"Despachos hoy     : {len(get_dispatches())}")

    log.info(f"Polling GPS       : cada {POLL_INTERVAL_SECONDS}s")
    log.info(f"Watcher           : cada {WATCHER_INTERVAL_SECONDS}s")
    log.info(f"Log archivo       : bus_monitor.log")
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
    # Se monitorea el GPS siempre que haya despachos cargados: la detección de
    # geocercas es física y continua. Qué puede registrarse lo decide el
    # contexto temporal en cada entrada, no este loop.
    try:
        while True:
            if not get_dispatches():
                time.sleep(1)
                continue

            reading = fetch_gps()

            if reading is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            monitor_ref[0].process(reading)
            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("Detenido por el usuario (Ctrl+C)")

    finally:
        stop_event.set()
        monitor_ref[0].print_summary()
        log.info("Programa finalizado")


if __name__ == "__main__":
    main()
