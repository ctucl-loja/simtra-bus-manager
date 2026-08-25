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


def int_env(name: str, default: int) -> int:
    """
    Entero de una variable de entorno, tolerante a ausencia y a basura.

    Antes se hacía int(os.getenv(...)) directo: una variable ausente lanzaba
    TypeError y el servicio no arrancaba. En un bus es peor no arrancar que
    arrancar con el valor por defecto dejándolo dicho en el log.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        log.warning(f"[CONFIG] {name} ausente — se usa {default}")
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning(f"[CONFIG] {name}={raw!r} no es un entero — se usa {default}")
        return default
    if value <= 0:
        log.warning(f"[CONFIG] {name}={value} no es positivo — se usa {default}")
        return default
    return value


POLL_INTERVAL_SECONDS = int_env("FAST_API_POLL_INTERVAL_SECONDS", 2)
WATCHER_INTERVAL_SECONDS = int_env("FAST_API_WATCHER_INTERVAL_SECONDS", 10)  # el watcher evalúa si el turno cambió
LOCAL_BACKEND = os.getenv("FAST_API_LOCAL_BACKEND") or "http://127.0.0.1:8000"
BACKEND_URL = os.getenv("FAST_API_BACKEND_URL")
BACKEND_USERNAME = os.getenv("FAST_API_BACKEND_USERNAME")
BACKEND_PASSWORD = os.getenv("FAST_API_BACKEND_PASSWORD")
BUS_REGISTER = int_env("FAST_API_BUS_REGISTER", 0)

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
    return step.get("step") if isinstance(step, dict) else None


def schedule_seconds(value: str) -> int:
    """
    'HH:MM:SS' → segundos desde medianoche.

    Estricta a propósito: valida también el rango, para que un horario imposible
    ('25:00:00') no genere una ventana temporal capaz de autorizar marcaciones.
    Lanza ante cualquier entrada inválida; quien no pueda permitirse la excepción
    usa safe_schedule_seconds.
    """
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60):
        raise ValueError(f"horario fuera de rango: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def safe_schedule_seconds(value) -> Optional[int]:
    """
    schedule_seconds que devuelve None en vez de lanzar. None NO es 0: un
    horario ilegible no es medianoche, y confundirlos abriría ventanas de step
    que no existen.
    """
    try:
        return schedule_seconds(value)
    except (AttributeError, TypeError, ValueError):
        return None


def as_finite_float(value) -> Optional[float]:
    """
    float utilizable, o None. Descarta NaN e infinitos: en geometría envenenan
    toda comparación (NaN <= radio siempre es False) sin lanzar ningún error,
    así que un GPS corrupto apagaría el geofencing en silencio.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_int(value) -> Optional[int]:
    """int utilizable, o None. Los bool se rechazan: True no es un id."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    except (AttributeError, TypeError, ValueError):
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
            step = self.current_step or {}
            return (
                f"state={ACTIVE_STEP} step={step.get('step')} "
                f"horario={step.get('start_schedule')}→{step.get('end_schedule')}"
            )
        if self.state == BETWEEN_STEPS:
            return (
                f"state={BETWEEN_STEPS} previous={step_number(self.previous_step)} "
                f"next={step_number(self.next_step)} "
                f"(inicia {(self.next_step or {}).get('start_schedule')})"
            )
        if self.state == BEFORE_FIRST_STEP:
            if self.next_step is None:
                return f"state={BEFORE_FIRST_STEP} (sin despachos)"
            return (
                f"state={BEFORE_FIRST_STEP} next={step_number(self.next_step)} "
                f"(inicia {self.next_step.get('start_schedule')})"
            )
        return f"state={AFTER_LAST_STEP} previous={step_number(self.previous_step)}"


def usable_steps(dispatches) -> list[tuple]:
    """
    Steps que pueden definir una ventana temporal, como (step, inicio, fin) en
    segundos.

    Un step con horario ausente o ilegible se descarta con log en vez de tumbar
    el contexto entero: el resto de la jornada sigue siendo válida. Descartar es
    la opción segura — un step sin ventana confiable no debe autorizar marcajes.
    """
    if not isinstance(dispatches, list):
        log.error(
            f"[TEMPORAL] Despachos con forma inesperada ({type(dispatches).__name__}) — se ignoran"
        )
        return []

    usable = []
    for step in dispatches:
        if not isinstance(step, dict):
            log.warning(f"[TEMPORAL] Step con forma inesperada ({type(step).__name__}) — se ignora")
            continue

        start = safe_schedule_seconds(step.get("start_schedule"))
        end = safe_schedule_seconds(step.get("end_schedule"))
        if start is None or end is None:
            log.warning(
                f"[TEMPORAL] Step {step.get('step')} con horario inválido "
                f"(start={step.get('start_schedule')!r} end={step.get('end_schedule')!r}) — se ignora"
            )
            continue
        if end < start:
            # Un recorrido que cruza medianoche caería aquí. No se soporta hoy
            # (ver README): en vez de inventar una ventana que abarque dos días
            # —y con ella marcaciones fuera de hora— se descarta y se avisa.
            log.warning(
                f"[TEMPORAL] Step {step.get('step')} termina antes de empezar "
                f"({step.get('start_schedule')} → {step.get('end_schedule')}); "
                f"los recorridos que cruzan medianoche no están soportados — se ignora"
            )
            continue

        usable.append((step, start, end))

    return usable


def resolve_temporal_context(dispatches: list[dict], now: datetime) -> TemporalContext:
    """
    Resuelve el estado temporal del día a partir ÚNICAMENTE de los horarios.

    Fronteras estrictas: un step está activo si `start <= now <= end`, sin
    tolerancia. Un step que empieza en 4 minutos NO está activo.

    Sin despachos devuelve BEFORE_FIRST_STEP sin steps: un estado sin
    `current_step` ni `previous_step` no puede autorizar ninguna escritura.
    """
    scheduled = usable_steps(dispatches)
    if not scheduled:
        return TemporalContext(state=BEFORE_FIRST_STEP)

    # (step, inicio, fin) ya parseados y ordenados: se parsea una sola vez y
    # ningún horario ilegible llega hasta aquí.
    scheduled.sort(key=lambda item: item[1])
    steps = [item[0] for item in scheduled]
    current_sec = now_seconds(now)

    active_indexes = [
        i for i, (_, start, end) in enumerate(scheduled)
        if start <= current_sec <= end
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

    if current_sec < scheduled[0][1]:
        return TemporalContext(state=BEFORE_FIRST_STEP, next_step=steps[0])

    finished = [step for step, _, end in scheduled if end < current_sec]
    upcoming = [step for step, start, _ in scheduled if start > current_sec]

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

# Ciclo de vida de una marcación. Los dos conjuntos son deliberadamente
# distintos: confundirlos era lo que permitía perder marcaciones.
#
#   libre        → no está en ninguno de los dos: elegible
#   RESERVADO    → IN_FLIGHT_CHECKPOINTS: un hilo se lo adjudicó y está
#                  persistiendo; nadie más puede tomarlo, pero TODAVÍA no está
#                  garantizado
#   CONFIRMADO   → CONFIRMED_CHECKPOINTS: la escritura indispensable terminó
#                  bien; cerrado por el resto del día
#   fallido      → se quita de IN_FLIGHT y vuelve a estar libre, para que una
#                  próxima entrada a la geocerca pueda reintentarlo
CONFIRMED_CHECKPOINTS: set[int] = set()   # persistidos: no se vuelven a reportar
IN_FLIGHT_CHECKPOINTS: set[int] = set()   # reservados, persistencia en curso


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


def taken_checkpoints() -> frozenset:
    """
    Checkpoints no elegibles ahora mismo: confirmados + reservados.

    La selección debe mirar los DOS conjuntos. Si solo mirara los confirmados,
    dos entradas concurrentes elegirían el mismo checkpoint y la segunda
    fracasaría recién en reserve_checkpoint.
    """
    with _lock:
        return frozenset(CONFIRMED_CHECKPOINTS | IN_FLIGHT_CHECKPOINTS)


def reserve_checkpoint(checkpoint_id: int) -> bool:
    """
    Test-and-set atómico: True si este hilo se quedó con el derecho de reportar
    el checkpoint. Se reserva ANTES de persistir para que dos entradas de
    geocerca casi simultáneas no lo reporten dos veces.

    Reservar NO es confirmar: si la persistencia falla hay que llamar a
    release_checkpoint, o el checkpoint quedaría bloqueado todo el día sin
    haberse guardado nunca.
    """
    with _lock:
        if checkpoint_id in CONFIRMED_CHECKPOINTS or checkpoint_id in IN_FLIGHT_CHECKPOINTS:
            return False
        IN_FLIGHT_CHECKPOINTS.add(checkpoint_id)
        return True


def confirm_checkpoint(checkpoint_id: int):
    """La escritura indispensable terminó bien: el checkpoint queda cerrado."""
    with _lock:
        IN_FLIGHT_CHECKPOINTS.discard(checkpoint_id)
        CONFIRMED_CHECKPOINTS.add(checkpoint_id)


def release_checkpoint(checkpoint_id: int):
    """
    La persistencia falló: vuelve a estar libre para la próxima entrada a la
    geocerca. Un fallo transitorio (FastAPI reiniciándose, disco ocupado) no
    debe costar la marcación del día.
    """
    with _lock:
        IN_FLIGHT_CHECKPOINTS.discard(checkpoint_id)


def reset_daily_state():
    """Borra el rastro del día anterior: ningún step viejo debe quedar elegible."""
    global ALL_DISPATCHES, CURRENT_CONTEXT
    with _lock:
        ALL_DISPATCHES  = []
        CURRENT_CONTEXT = TemporalContext(state=BEFORE_FIRST_STEP)
        CONFIRMED_CHECKPOINTS.clear()
        IN_FLIGHT_CHECKPOINTS.clear()


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
    except Exception as e:
        # Barrera: el cliente ya no debería lanzar, pero esto corre en el hilo
        # watcher y una excepción aquí lo mataría para el resto del día.
        log.exception(f"Error consultando despachos: {e}")
        return False

    if not isinstance(dispatches, list):
        log.error(f"Despachos con forma inesperada ({type(dispatches).__name__}) — se ignoran")
        dispatches = []

    # Solo cuentan los steps con horario utilizable: uno sin ventana temporal no
    # puede autorizar marcaciones, así que tampoco debe hacer creer que el bus
    # tiene trabajo hoy.
    if not usable_steps(dispatches):
        with _lock:
            ALL_DISPATCHES = []
        log.warning("La API no devolvió despachos utilizables para hoy")
        return False

    with _lock:
        ALL_DISPATCHES = dispatches
    log.info(f"Despachos cargados: {len(dispatches)} turno(s)")

    # Cada paso se aísla: que falle el cache local o el vehículo no debe anular
    # unos despachos que ya están cargados en memoria y son válidos.
    for label, action in (
        ("seed de checkpoints", lambda: seed_reported_checkpoints(dispatches)),
        ("cache local del despacho", lambda: cache_dispatch_locally(dispatches, query_date)),
        ("sincronización del vehículo", sync_vehicle_info),
    ):
        try:
            action()
        except Exception as e:
            log.exception(f"Fallo en {label}: {e}")

    return True


def sync_vehicle_info():
    """
    Descarga la información del vehículo asociado al bus (vía services/api.py,
    nunca con requests directo al backend remoto) y la cachea en el backend
    local, igual que se hace con el despacho.
    """
    vehicle = simtra.get_vehicle(BUS_REGISTER)
    if not isinstance(vehicle, dict) or not vehicle:
        log.warning("La API no devolvió información utilizable del vehículo")
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
    seeded = []
    for step in dispatches if isinstance(dispatches, list) else []:
        for ckpt in step_checkpoints(step):
            reported = ckpt.get("time_reported") or "00:00:00"
            if reported == "00:00:00":
                continue
            cid = checkpoint_id(ckpt)
            if cid is None:
                log.warning(f"[SEED] Checkpoint sin id utilizable en step {step_number(step)} — se ignora")
                continue
            seeded.append(cid)

    with _lock:
        CONFIRMED_CHECKPOINTS.update(seeded)


# ─────────────────────────────────────────────
# VENTANA DE GEOCERCAS
#
# Define qué geocercas se OBSERVAN físicamente. No tiene relación con qué step
# está autorizado a escribir: que una geocerca esté cargada en el
# GeofenceMonitor no significa que su checkpoint pueda registrarse.
# ─────────────────────────────────────────────

def step_checkpoints(step) -> list[dict]:
    """
    Checkpoints utilizables de un step. Silenciosa a propósito: se invoca en
    rutas calientes (cada lectura GPS) y no puede convertirse en un generador
    de logs.
    """
    if not isinstance(step, dict):
        return []
    checkpoints = step.get("checkpoints")
    if not isinstance(checkpoints, list):
        return []
    return [c for c in checkpoints if isinstance(c, dict)]


def checkpoint_id(ckpt) -> Optional[int]:
    """Id de la marcación. Sin él no se puede reservar ni sincronizar: es
    indispensable, así que None significa 'checkpoint inutilizable'."""
    return as_int(ckpt.get("id")) if isinstance(ckpt, dict) else None


def checkpoint_order(ckpt) -> Optional[int]:
    return as_int(ckpt.get("order")) if isinstance(ckpt, dict) else None


def checkpoint_point(ckpt) -> dict:
    point = ckpt.get("point") if isinstance(ckpt, dict) else None
    return point if isinstance(point, dict) else {}


def checkpoint_point_id(ckpt) -> Optional[int]:
    return as_int(checkpoint_point(ckpt).get("id"))


def point_name(ckpt) -> str:
    """Nombre presentable del punto. Nunca vacío: se usa en logs y en el evento."""
    name = checkpoint_point(ckpt).get("name")
    if isinstance(name, str) and name.strip():
        return name
    pid = checkpoint_point_id(ckpt)
    return f"PUNTO {pid}" if pid is not None else "PUNTO DESCONOCIDO"


# Puntos ya reportados como inválidos: merge_geofences corre en cada tick del
# watcher, así que sin esto un solo punto corrupto llenaría el log del día.
_warned_invalid_points: set = set()


def geofence_from_point(point) -> Optional[dict]:
    """
    Geocerca normalizada a partir de un punto del despacho, o None si le falta
    algo indispensable para la geometría.

    Devuelve una COPIA validada: de aquí en adelante el monitor solo trabaja con
    números finitos, así que is_inside no puede comparar contra basura. Un radio
    nulo o negativo se rechaza — no se inventa un radio por defecto, porque eso
    produciría marcaciones en lugares donde no hay geocerca.
    """
    if not isinstance(point, dict):
        return None

    pid = as_int(point.get("id"))
    latitude = as_finite_float(point.get("latitude"))
    longitude = as_finite_float(point.get("longitude"))
    radius = as_finite_float(point.get("radius"))

    if pid is None or latitude is None or longitude is None or radius is None or radius <= 0:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None

    name = point.get("name")
    return {
        "id": pid,
        "name": str(name) if isinstance(name, str) and name.strip() else f"PUNTO {pid}",
        "latitude": latitude,
        "longitude": longitude,
        "radius": radius,
    }


def step_index(dispatches: list[dict], step: dict) -> Optional[int]:
    """Ubica la posición de un step dentro de la lista según su número de step."""
    if not isinstance(dispatches, list) or not isinstance(step, dict):
        return None
    target = step.get("step")
    if target is None:
        return None
    for i, s in enumerate(dispatches):
        if isinstance(s, dict) and s.get("step") == target:
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
        for ckpt in step_checkpoints(step):
            geofence = geofence_from_point(ckpt.get("point"))
            if geofence is None:
                signature = (step_number(step), str(ckpt.get("id")))
                if signature not in _warned_invalid_points:
                    _warned_invalid_points.add(signature)
                    log.warning(
                        f"[GEOFENCE] Punto inutilizable en step {step_number(step)} "
                        f"checkpoint {ckpt.get('id')} ({ckpt.get('point')!r}) — no se vigila"
                    )
                continue
            merged.setdefault(geofence["id"], geofence)
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

    def ordered(candidate_step, minimum: Optional[int] = None) -> list[dict]:
        """Checkpoints con order utilizable, ordenados. Los que no lo tienen no
        pueden ubicarse en el recorrido, así que no se pre-cargan."""
        usable = []
        for ckpt in step_checkpoints(candidate_step):
            order = checkpoint_order(ckpt)
            if order is None or (minimum is not None and order <= minimum):
                continue
            usable.append((order, ckpt))
        return [ckpt for _, ckpt in sorted(usable, key=lambda pair: pair[0])]

    result.extend(ordered(step, after_order))

    next_idx = idx + 1
    while len(result) < count and next_idx < len(dispatches):
        result.extend(ordered(dispatches[next_idx]))
        next_idx += 1

    return result[:count]


def prefetch_upcoming_audio(step: dict, after_order: int, count: int = 2):
    """
    Prepara con anticipación el audio de los siguientes `count` checkpoints
    después de after_order (cruzando de step si hace falta), para que estén
    listos antes de que el bus llegue físicamente. No bloquea.
    """
    for ckpt in get_upcoming_checkpoints(step, after_order, count):
        pid = checkpoint_point_id(ckpt)
        if pid is None:
            continue   # sin id de punto no hay identidad de cache posible
        audio_announcer.prepare(pid, point_name(ckpt))


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

        # Barrera de seguridad: este hilo es el único que hace avanzar el
        # contexto temporal. Si muere, el bus se queda congelado en el step de
        # la hora en que falló, así que ningún dato defectuoso puede tumbarlo.
        try:
            now          = datetime.now()
            current_date = now.strftime('%Y-%m-%d')

            # ── Nuevo día: recarga completa ──────────────────────────────────
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

            # ── Bus sin despachos: reintento espaciado, sin dormir el hilo ───
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

            # ── Curso normal: el reloj manda ─────────────────────────────────
            apply_context(resolve_temporal_context(dispatches, now), monitor_ref)

        except Exception as e:
            log.exception(f"[WATCHER] Error inesperado, se reintenta en el próximo ciclo: {e}")


# ─────────────────────────────────────────────
# CLIENTE GPS
# ─────────────────────────────────────────────

@dataclass
class GpsReading:
    latitude:  float
    longitude: float
    timestamp: str
    speed:     Optional[float]   # el receptor puede no reportar velocidad


def fetch_gps() -> Optional[GpsReading]:
    """Consulta la última posición GPS desde la API local."""
    try:
        resp = requests.get(f"{LOCAL_BACKEND}/api/gps/last_position", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Error consultando GPS: {e}")
        return None

    try:
        data = resp.json()
    except ValueError:
        log.error("Respuesta de la API GPS no es JSON válido")
        return None

    if not data:
        return None   # todavía no hay ninguna posición registrada
    if not isinstance(data, dict):
        log.error(f"Respuesta de la API GPS con forma inesperada ({type(data).__name__})")
        return None

    latitude  = as_finite_float(data.get("latitude"))
    longitude = as_finite_float(data.get("longitude"))

    # Sin coordenadas utilizables no hay geofencing posible. Se descarta la
    # lectura entera: inventar un 0.0 pondría el bus en el golfo de Guinea.
    if latitude is None or longitude is None:
        log.error(
            f"Lectura GPS sin coordenadas utilizables "
            f"(lat={data.get('latitude')!r} lon={data.get('longitude')!r}) — se descarta"
        )
        return None

    return GpsReading(
        latitude=latitude,
        longitude=longitude,
        timestamp=str(data.get("timestamp") or ""),
        # speed es informativa y puede venir nula: no se descarta la lectura por
        # ella, pero tampoco se convierte en 0.0, que significaría "detenido".
        speed=as_finite_float(data.get("speed")),
    )


# ─────────────────────────────────────────────
# PERSISTENCIA
# ─────────────────────────────────────────────

def report_checkpoint(ckpt_id: int, name: str, time_reported: str) -> bool:
    """
    Persiste la marcación en la API local (la cola que data_loader sube al
    backend remoto).

    ESCRITURA INDISPENSABLE: si falla, la llegada no quedó registrada en ningún
    lado y no puede darse por buena. Devuelve True solo si la API la confirmó.
    """
    try:
        payload = {
            "checkpoint_id": ckpt_id,
            "name": name,
            "timestamp": time_reported,
        }
        resp = requests.post(f"{LOCAL_BACKEND}/api/checkpoint", json=payload, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(
            f"[PERSIST] No se pudo guardar checkpoint id={ckpt_id} '{name}' @ {time_reported}: {e}"
        )
        return False

    log.debug(f"[PERSIST] Checkpoint guardado: id={ckpt_id} name={name}")
    return True


def report_dispatch_checkpoint(step: int, ckpt_id: int, time_reported: str) -> bool:
    """
    Actualiza time_reported dentro del despacho cacheado localmente: es lo que
    ve la pantalla del conductor.

    Escritura SECUNDARIA — su fallo degrada la visualización pero no pierde la
    marcación, que ya está en la cola de subida. Devuelve True si se confirmó.
    """
    try:
        payload = {
            "step": step,
            "checkpoint_id": ckpt_id,
            "time_reported": time_reported,
        }
        resp = requests.patch(f"{LOCAL_BACKEND}/api/dispatch/checkpoint", json=payload, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(
            f"[PERSIST] No se pudo actualizar el despacho local "
            f"(step={step} checkpoint_id={ckpt_id} @ {time_reported}): {e}"
        )
        return False

    # La API responde 200 con `null` cuando el checkpoint no existe en el
    # despacho cacheado. Sin esta comprobación, un no-op se leía como éxito.
    try:
        body = resp.json()
    except ValueError:
        log.error(f"[PERSIST] Respuesta no JSON al actualizar el despacho (checkpoint_id={ckpt_id})")
        return False

    if body is None:
        log.error(
            f"[PERSIST] El despacho cacheado no contiene step={step} "
            f"checkpoint_id={ckpt_id} — la pantalla no reflejará esta llegada"
        )
        return False

    log.debug(f"[PERSIST] Despacho actualizado: step={step} checkpoint_id={ckpt_id}")
    return True


def log_event(event_type: str, priority: str, message: str, payload: Optional[dict] = None) -> bool:
    """
    Registra un evento genérico en la API local (tabla `events`). Reutilizable
    para cualquier tipo de evento futuro: basta con llamar a esta función con
    un event_type/priority/payload distintos, sin tocar la capa de transporte.

    Devuelve True si el evento quedó registrado.
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
    except requests.RequestException as e:
        log.error(f"[EVENT] No se pudo registrar '{event_type}' ({message}): {e}")
        return False

    log.debug(f"[EVENT] Registrado: {event_type}")
    return True


def emit_arrival_event(step: dict, ckpt: dict, time_reported: str, reason: str) -> bool:
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
    name       = point_name(ckpt)
    # `line` viaja tal cual al frontend; si no es un objeto se envía null en vez
    # de una forma inesperada que la pantalla tendría que adivinar.
    line       = step.get("line") if isinstance(step.get("line"), dict) else None

    if arrival:
        message = (
            f"Llegada a {name} — {ARRIVAL_LABELS[status]} ({difference:+d} s)"
        )
    else:
        message = f"Llegada a {name} — sin hora programada válida"

    registered = log_event(
        event_type="checkpoint_arrival",
        priority="MEDIUM",
        message=message,
        payload={
            "step": step_number(step),
            "checkpoint_id": checkpoint_id(ckpt),
            "point_id": checkpoint_point_id(ckpt),
            "point_name": name,
            "order": checkpoint_order(ckpt),
            "scheduled_time": scheduled_time,
            "reported_time": time_reported,
            "difference_seconds": difference,
            "arrival_status": status,
            "line": line,
            "reason": reason,   # progreso normal / cierre tardío (auditoría)
        },
    )

    if registered:
        log.info(f"[ARRIVAL] {message}")
    else:
        # La marcación SÍ está persistida; lo que falló es el aviso a la
        # pantalla. Se deja constancia para que no parezca una llegada perdida.
        log.error(f"[ARRIVAL] Llegada confirmada pero sin evento para la pantalla: {message}")

    return registered


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
    for ckpt in step_checkpoints(step):
        cid = checkpoint_id(ckpt)
        if cid is None:
            continue   # sin id no se puede reservar ni sincronizar: se ignora
        if checkpoint_point_id(ckpt) == point_id and cid not in reported:
            return ckpt
    return None


def step_has_point(step: Optional[dict], point_id: int) -> bool:
    """True si alguna geocerca del step corresponde a ese punto físico."""
    return any(checkpoint_point_id(ckpt) == point_id for ckpt in step_checkpoints(step))


def is_last_checkpoint(step: dict, ckpt: dict) -> bool:
    orders = [o for o in (checkpoint_order(c) for c in step_checkpoints(step)) if o is not None]
    order = checkpoint_order(ckpt)
    return bool(orders) and order is not None and order == max(orders)


def count_skipped(step: dict, target_ckpt: dict, reported) -> int:
    """Checkpoints anteriores (order menor) del mismo step que siguen sin reportar."""
    target_order = checkpoint_order(target_ckpt)
    if target_order is None:
        return 0

    skipped = 0
    for c in step_checkpoints(step):
        order = checkpoint_order(c)
        cid = checkpoint_id(c)
        if order is None or cid is None:
            continue
        if order < target_order and cid not in reported:
            skipped += 1
    return skipped


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
    """
    Ventana de gracia posterior al end_schedule para el cierre del último
    recorrido. Con un horario ilegible se responde False: sin fin conocido no
    hay ventana que respetar, y negarla es lo seguro.
    """
    end = safe_schedule_seconds((step or {}).get("end_schedule"))
    if end is None:
        log.warning(
            f"[TRANSITION] Step {step_number(step)} sin end_schedule utilizable — sin ventana de cierre"
        )
        return False
    return now_seconds(now) - end <= CLOSING_GRACE_MINUTES * 60


def log_not_started(context: TemporalContext, point_id: int):
    """Deja constancia cuando la geocerca pertenece a un step que aún no empieza."""
    if step_has_point(context.next_step, point_id):
        log.info(
            f"[GEOFENCE] point={point_id} ignorado: pertenece al step "
            f"{step_number(context.next_step)} pero todavía no inicia "
            f"({(context.next_step or {}).get('start_schedule')})"
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

        snapshot → candidato → RESERVA → escritura indispensable
                 → CONFIRMACIÓN → despacho (secundaria) → evento → audio

    Invariantes que sostiene esta función:

      1. No se emite `checkpoint_arrival` si la escritura indispensable falló.
      2. Un fallo transitorio LIBERA la reserva: el checkpoint vuelve a ser
         elegible en la próxima entrada en vez de quedar bloqueado todo el día.
      3. La reserva es un test-and-set atómico, así que dos entradas
         concurrentes no pueden reportar el mismo checkpoint.
      4. El audio solo anuncia una llegada ya confirmada.
      5. El lock nunca se mantiene durante HTTP ni audio.

    No cambia de step: el avance del recorrido lo hace exclusivamente el
    watcher a partir del reloj.
    """
    now = datetime.now()

    # Snapshot coherente del estado compartido; el resto del trabajo (HTTP,
    # audio) ocurre fuera del lock.
    with _lock:
        context = CURRENT_CONTEXT
        taken   = frozenset(CONFIRMED_CHECKPOINTS | IN_FLIGHT_CHECKPOINTS)

    candidate = resolve_checkpoint_candidate(context, point_id, taken, now)
    if candidate is None:
        log.debug(
            f"Geocerca punto={point_id} sin checkpoint autorizado "
            f"(state={context.state}) — se ignora"
        )
        return

    target_step = candidate.step
    target_ckpt = candidate.checkpoint
    ckpt_id     = checkpoint_id(target_ckpt)

    if ckpt_id is None:
        log.error(
            f"[TRIP] Candidato sin id utilizable (step={step_number(target_step)} "
            f"punto={point_id}) — no se puede reservar ni sincronizar, se ignora"
        )
        return

    # ── RESERVA ───────────────────────────────────────────────────────────
    if not reserve_checkpoint(ckpt_id):
        log.debug(f"Checkpoint {ckpt_id} ya reservado o confirmado por otra entrada — se ignora")
        return

    # ── ESCRITURA INDISPENSABLE ───────────────────────────────────────────
    # Es la que convierte la llegada en un dato real (y la que data_loader sube
    # al backend). Si falla, nada de lo que sigue debe ocurrir.
    if not report_checkpoint(ckpt_id, name, time_reported):
        release_checkpoint(ckpt_id)
        log.error(
            f"[TRIP] Marcación NO registrada: checkpoint {ckpt_id} "
            f"(step={step_number(target_step)} punto={point_id} '{name}' @ {time_reported}) "
            f"— reserva liberada, se reintentará en la próxima entrada a la geocerca"
        )
        return

    # ── CONFIRMACIÓN ──────────────────────────────────────────────────────
    confirm_checkpoint(ckpt_id)

    log.info(
        f"[TRIP] Checkpoint {ckpt_id} marcado en step "
        f"{step_number(target_step)} ({candidate.reason}) @ {time_reported}"
    )

    # ── ESCRITURA SECUNDARIA ──────────────────────────────────────────────
    # Actualiza el despacho que ve la pantalla. Su fallo NO revierte la
    # confirmación: la marcación ya está a salvo, y liberar la reserva aquí
    # arriesgaría un segundo reporte de la misma llegada.
    step_no = as_int(step_number(target_step))
    if step_no is None:
        log.error(
            f"[TRIP] Step sin número utilizable: no se actualiza el despacho local "
            f"del checkpoint {ckpt_id}"
        )
    elif not report_dispatch_checkpoint(step_no, ckpt_id, time_reported):
        log.error(
            f"[TRIP] Checkpoint {ckpt_id} guardado, pero el despacho local no se "
            f"actualizó: la pantalla seguirá mostrando 'Sin reportar' hasta la próxima recarga"
        )

    emit_arrival_event(target_step, target_ckpt, time_reported, candidate.reason)

    # Anuncio de voz sobre una llegada ya confirmada: prioridad más baja, nunca
    # bloquea el hilo de GPS (solo encola). Al terminar prepara los próximos 2.
    announce_order = checkpoint_order(target_ckpt)
    audio_announcer.announce(
        checkpoint_point_id(target_ckpt),
        point_name(target_ckpt),
        on_done=lambda _pid: prefetch_upcoming_audio(
            target_step, announce_order if announce_order is not None else -1, 2
        ),
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
            active = self._active.get(gid)

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
            # Barrera de seguridad del loop de tracking: una lectura corrupta o
            # un despacho con una forma inesperada no pueden dejar al bus sin
            # geofencing el resto del día. Antes, un speed nulo bastaba para
            # matar el proceso.
            try:
                if not get_dispatches():
                    time.sleep(1)
                    continue

                reading = fetch_gps()

                if reading is None:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                monitor_ref[0].process(reading)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.exception(f"[LOOP] Error procesando la lectura GPS: {e}")

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("Detenido por el usuario (Ctrl+C)")

    finally:
        stop_event.set()
        monitor_ref[0].print_summary()
        log.info("Programa finalizado")


if __name__ == "__main__":
    main()
