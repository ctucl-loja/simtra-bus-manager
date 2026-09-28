from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from sqlalchemy.orm.attributes import flag_modified
from types import SimpleNamespace
from typing import Optional
from models import Gps,GpsCurrent,CheckPoint,Passenger,Dispatch,Event,Vehicle
from schemas import GPSDataCreate,PassengerCreate,DispatchCreate,EventCreate,VehicleCreate
from datetime import datetime, timezone
from datetime import datetime,time

from zoneinfo import ZoneInfo
import logging
import threading

log = logging.getLogger("simtra")

ECUADOR_TZ = ZoneInfo("America/Guayaquil")
PASSENGER_CURRENT_GPS_MAX_AGE_SECONDS = 180


def gps_unix_seconds(value: datetime) -> int:
    """
    Instante Unix (segundos) de un timestamp recibido por POST /api/gps.

    Con zona horaria es exacto. Sin zona se interpreta como hora de pared de
    America/Guayaquil, que es la del equipo (el simulador envía
    `datetime.now()` sin zona). Se calcula UNA vez, al recibir el punto: lo que
    se guarda en `gps.timestamp` pierde la zona en SQLite y reinterpretarlo más
    tarde desplazaría 5 h cualquier punto que llegó en UTC.
    """
    aware = value if value.tzinfo else value.replace(tzinfo=ECUADOR_TZ)
    return int(aware.timestamp())


def _last_trace_point(db: Session) -> Optional[Gps]:
    """Último punto persistido en la traza (orden de inserción)."""
    return db.query(Gps).order_by(Gps.id.desc()).first()


# Serializa las escrituras de `gps` y `gps_current`: FastAPI atiende los
# endpoints síncronos en un pool de hilos y la fila única de la posición actual
# debe quedar enlazada al punto de traza de la misma lectura.
_GPS_WRITE_LOCK = threading.Lock()


def create_gps_data(db: Session, data: GPSDataCreate) -> Gps:
    """
    Registra una lectura GPS válida.

    Toda lectura se archiva en la traza pendiente de subida (`gps`,
    upload=False), aunque la velocidad sea 0 o repita la posición anterior, y
    actualiza la posición actual (`gps_current`).
    """
    with _GPS_WRITE_LOCK:
        try:
            gps = Gps(
                **data.model_dump(),
                upload=False,
                timestamp_unix=gps_unix_seconds(data.timestamp),
            )
            db.add(gps)
            db.flush()   # asigna el id para enlazarlo desde gps_current

            current = db.get(GpsCurrent, 1)
            if current is None:
                current = GpsCurrent(id=1)
                db.add(current)
            current.timestamp = data.timestamp
            current.latitude = data.latitude
            current.longitude = data.longitude
            current.speed = data.speed
            current.trace_id = gps.id
            current.created_at = datetime.now(timezone.utc)

            db.commit()
        except Exception:
            db.rollback()
            raise

    db.refresh(gps)
    return gps


def get_all_gps(db: Session):
    return db.query(Gps).order_by(Gps.id.desc()).limit(100).all()


def get_last_position(db: Session):
    """
    Posición actual: la última lectura válida recibida, esté o no en la traza.

    En una base anterior a `gps_current` (equipo recién actualizado, todavía
    sin lecturas nuevas) se cae al último punto de la traza.
    """
    current = db.get(GpsCurrent, 1)
    if current is not None:
        return {
            "id": current.trace_id,
            "latitude": current.latitude,
            "longitude": current.longitude,
            "speed": current.speed,
            "timestamp": current.timestamp,
            "created_at": current.created_at,
        }
    return _last_trace_point(db)


# Tope de la cola devuelta por ciclo: el loader procesa por tandas y una
# jornada sin red puede acumular decenas de miles de puntos.
MAX_PENDING_GPS = 500


def get_pending_gps(db: Session, limit: int = 100):
    """Puntos de traza por subir, en orden de inserción (determinista)."""
    limit = max(1, min(int(limit), MAX_PENDING_GPS))
    return (
        db.query(Gps)
        .filter(Gps.upload == False, Gps.upload_error.is_(None))  # noqa: E712
        .order_by(Gps.id.asc())
        .limit(limit)
        .all()
    )


def upload_pending_gps(db: Session, id: int):
    gps = db.query(Gps).filter(Gps.id == id).first()
    if not gps:
        return None
    gps.upload = True
    db.commit()
    db.refresh(gps)
    return gps


def reject_pending_gps(db: Session, id: int, reason: str):
    """
    Saca de la cola un punto que no se subirá nunca, SIN marcarlo como subido.
    Queda en la base con su motivo para poder revisarlo.
    """
    gps = db.query(Gps).filter(Gps.id == id).first()
    if not gps:
        return None
    gps.upload_error = reason[:200]
    db.commit()
    db.refresh(gps)
    return gps

def create_checkpoint(db: Session, checkpoint_id: int, name: str, timestamp):
    existing = db.query(CheckPoint).filter(
        CheckPoint.checkpoint_id == checkpoint_id
    ).first()

    if existing:
        return existing  # ya existe uno pendiente, no se duplica

    checkpoint = CheckPoint(
        checkpoint_id=checkpoint_id,
        name=name,
        timestamp=timestamp,
    )
    db.add(checkpoint)
    db.commit()
    db.refresh(checkpoint)
    return checkpoint

def get_pending_checkpoints(db:Session):
    checkpoints = db.query(CheckPoint).filter(
        CheckPoint.upload == False
    ).all()
    return checkpoints

def upload_pending_checkpoints(db: Session, id: int):
    checkpoint = db.query(CheckPoint).filter(CheckPoint.id == id).first()

    if not checkpoint:
        return None

    checkpoint.upload = True  # o el campo que uses
    db.commit()
    db.refresh(checkpoint)

    return checkpoint

def _existing_passenger(db: Session, data: PassengerCreate) -> Optional[Passenger]:
    if data.event_id is None:
        return None
    passenger = db.query(Passenger).filter(Passenger.event_id == data.event_id).first()
    if passenger is not None:
        timestamp = data.timestamp
        if timestamp is not None:
            timestamp = (timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=ECUADOR_TZ))
            timestamp = timestamp.astimezone(ECUADOR_TZ).replace(tzinfo=None)
        if (passenger.direction != data.direction or passenger.door != data.door
                or (timestamp is not None and passenger.timestamp != timestamp)):
            raise HTTPException(409, "El ID ya pertenece a un evento diferente")
    return passenger


def _passenger_historical_gps(db: Session, timestamp: datetime) -> Optional[Gps]:
    # Dos consultas indexadas equivalen al mas cercano de todo el historial.
    # Las filas antiguas sin Unix no tienen zona de origen conocida.
    target = timestamp.timestamp()
    before = db.query(Gps).filter(Gps.timestamp_unix <= target).order_by(
        Gps.timestamp_unix.desc(), Gps.id.desc()).first()
    after = db.query(Gps).filter(Gps.timestamp_unix > target).order_by(
        Gps.timestamp_unix.asc(), Gps.id.asc()).first()
    candidates = [point for point in (before, after) if point is not None]
    return min(candidates, key=lambda point: abs(point.timestamp_unix - target), default=None)


def create_passenger(db: Session, data: PassengerCreate) -> Passenger:
    existing = _existing_passenger(db, data)
    if existing is not None:
        log.info("[PASSENGER] Evento %s ya registrado (id %s): no se duplica",
                 data.event_id, existing.id)
        return existing
    now = datetime.now(ECUADOR_TZ)
    timestamp = data.timestamp or now
    timestamp = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=ECUADOR_TZ)
    timestamp = timestamp.astimezone(ECUADOR_TZ)
    # Evento reciente (o sin timestamp): posición actual, igual que
    # get_last_position. Evento viejo (reenvío tras una caída): el punto de la
    # traza más cercano al instante real del cruce.
    historical = (now - timestamp).total_seconds() > PASSENGER_CURRENT_GPS_MAX_AGE_SECONDS
    last = _passenger_historical_gps(db, timestamp) if historical else get_last_position(db)
    gps = SimpleNamespace(**last) if isinstance(last, dict) else last

    if gps is None:
        # Se registra igual: perder el conteo de pasajeros sería peor que
        # guardarlo sin ubicación. Pero queda dicho en el log, porque (0, 0) es
        # una coordenada real y no debe confundirse con una posición medida.
        log.warning(
            "[PASSENGER] Evento %s (%s): no hay lectura GPS %s utilizable; se "
            "guarda con coordenadas (0, 0), que NO son una posición real",
            data.event_id, timestamp.isoformat(), "histórica" if historical else "actual",
        )
    elif historical:
        log.info("[PASSENGER] Evento %s de %s: se usa el GPS histórico más cercano (%s)",
                 data.event_id, timestamp.isoformat(), gps.timestamp)

    passenger = Passenger(
        event_id=data.event_id,
        timestamp=timestamp.replace(tzinfo=None),
        direction=data.direction,
        door=data.door,
        latitude=gps.latitude if gps else 0.0,
        longitude=gps.longitude if gps else 0.0,
    )
    db.add(passenger)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Otro worker pudo guardar el mismo evento entre la consulta y el INSERT.
        existing = _existing_passenger(db, data)
        if existing is not None:
            return existing
        raise
    db.refresh(passenger)
    return passenger

def get_pending_passengers(db:Session):
    passengers = db.query(Passenger).filter(
        Passenger.upload == False
    ).all()
    return passengers

def get_passengers_today(db: Session):
    """
    Retorna todos los pasajeros de hoy y el total.
    (La RPi ya está configurada con hora Ecuador)
    """
    # Hora de Ecuador explícita, igual que create_passenger: si el equipo se
    # configurara en otra zona, "hoy" seguiría significando lo mismo en ambos.
    today = datetime.now(ECUADOR_TZ).date()

    start_of_day = datetime.combine(today, time.min)
    end_of_day = datetime.combine(today, time.max)

    passengers = db.query(Passenger).filter(
        Passenger.timestamp >= start_of_day,
        Passenger.timestamp <= end_of_day
    ).order_by(Passenger.timestamp.desc()).all()   # ← Más recientes primero

    return {
        "total": len(passengers),   # ya está la lista: un COUNT extra sobra
        "passengers": passengers
    }



def upload_pending_passengers(db: Session, id: int):
    passenger = db.query(Passenger).filter(Passenger.id == id).first()
    if not passenger:
        return None
    passenger.upload = True  # o el campo que uses
    db.commit()
    db.refresh(passenger)
    return passenger


# Valor con el que el backend representa "todavía no llegó".
NOT_REPORTED = "00:00:00"


def _checkpoint_index(data) -> dict:
    """
    {(step, checkpoint_id): checkpoint} del despacho.

    Identidad ESTABLE: número de step + id de checkpoint. Nunca la posición en
    el array — el backend puede reordenar los steps o devolver uno menos, y
    entonces una comparación por índice mezclaría recorridos.
    """
    index = {}
    if not isinstance(data, list):
        return index
    for step in data:
        if not isinstance(step, dict) or not isinstance(step.get("step"), int):
            continue
        for ckpt in step.get("checkpoints") or []:
            if isinstance(ckpt, dict) and isinstance(ckpt.get("id"), int):
                index[(step["step"], ckpt["id"])] = ckpt
    return index


def _carry_over_reports(previous, incoming) -> int:
    """
    Copia al despacho entrante las horas de llegada que el anterior ya tenía y
    el nuevo no. MUTA `incoming`. Devuelve cuántas conservó.

    Motivo: `save_dispatch` es un upsert que reemplazaba `existing.data` entero.
    El monitor lo llama cada vez que recarga los despachos del backend remoto, y
    el backend todavía no conoce las llegadas que data_loader no ha subido: sin
    esto, una recarga rutinaria borraba de la pantalla marcaciones reales.
    """
    if not isinstance(incoming, list):
        return 0

    old_index = _checkpoint_index(previous)
    if not old_index:
        return 0

    carried = 0
    for key, ckpt in _checkpoint_index(incoming).items():
        current = ckpt.get("time_reported")
        if isinstance(current, str) and current not in ("", NOT_REPORTED):
            continue   # el entrante ya trae hora: manda el dato nuevo
        earlier = old_index.get(key, {}).get("time_reported")
        if isinstance(earlier, str) and earlier not in ("", NOT_REPORTED):
            ckpt["time_reported"] = earlier
            carried += 1

    if carried:
        log.info(f"[DISPATCH] {carried} hora(s) de llegada conservadas del despacho anterior")
    return carried


def save_dispatch(db: Session, data: DispatchCreate, base_revision: Optional[int] = None):
    """
    Upsert del despacho del día (clave: fecha + registro).

    Dos garantías que antes no existían:

      · Las horas de llegada locales que el payload entrante no trae se
        CONSERVAN (ver _carry_over_reports). Reemplazar `data` a secas perdía
        las marcaciones todavía no subidas al backend remoto.

      · `base_revision` es un control de concurrencia optimista: si se indica y
        la revisión almacenada es MAYOR, la escritura se descarta y se devuelve
        lo que hay. Es lo que impide que una carga del monitor iniciada antes de
        una recarga manual llegue tarde y pise el itinerario nuevo.
    """
    existing = db.query(Dispatch).filter(
        Dispatch.date == data.date,
        Dispatch.register == data.register
    ).first()

    if existing:
        if base_revision is not None and (existing.revision or 0) > base_revision:
            log.warning(
                f"[DISPATCH] Escritura descartada: se basaba en la revisión "
                f"{base_revision} y la almacenada es {existing.revision}"
            )
            return existing

        incoming = data.data
        _carry_over_reports(existing.data, incoming)
        existing.data = incoming
        existing.revision = (existing.revision or 0) + 1
        flag_modified(existing, "data")
        db.commit()
        db.refresh(existing)
        return existing

    dispatch = Dispatch(date=data.date, register=data.register, data=data.data, revision=1)
    db.add(dispatch)
    db.commit()
    db.refresh(dispatch)
    return dispatch


def get_dispatch_for(db: Session, date: str, register: int):
    """Despacho de una fecha y un registro concretos, o None."""
    return db.query(Dispatch).filter(
        Dispatch.date == date,
        Dispatch.register == register,
    ).first()


def pending_checkpoint_ids(db: Session) -> set:
    """
    checkpoint_id de las marcaciones que todavía NO se subieron al backend
    remoto. Es el conjunto que la recarga manual debe preservar.
    """
    rows = db.query(CheckPoint.checkpoint_id).filter(CheckPoint.upload == False).all()  # noqa: E712
    return {row[0] for row in rows if isinstance(row[0], int)}


def refresh_dispatch(db: Session, date: str, register: int, remote_data: list,
                     merge) -> tuple:
    """
    Reemplaza el itinerario del día con el recién descargado, en UNA transacción.

    `merge(previous_data, pending_ids, remote_data)` es la fusión (vive en
    services/dispatch_refresh.py, sin base de datos). Se invoca AQUÍ dentro,
    después de releer la fila: así una marcación que llegue por
    `PATCH /api/dispatch/checkpoint` mientras la descarga estaba en curso ya
    está en `previous_data` y no se pierde. La ventana de carrera se cierra a
    nivel de base de datos, no con un candado en memoria que no cruzaría entre
    procesos.

    Devuelve (dispatch, merge_report).
    """
    existing = get_dispatch_for(db, date, register)
    previous = existing.data if existing else None
    pending = pending_checkpoint_ids(db)

    report = merge(previous, pending, remote_data)

    try:
        if existing:
            existing.data = remote_data
            existing.revision = (existing.revision or 0) + 1
            flag_modified(existing, "data")
            dispatch = existing
        else:
            dispatch = Dispatch(date=date, register=register, data=remote_data, revision=1)
            db.add(dispatch)
        db.commit()
    except Exception:
        # Sin rollback, la sesión queda inutilizable y el itinerario anterior
        # podría quedar a medio reemplazar.
        db.rollback()
        raise

    db.refresh(dispatch)
    return dispatch, report


def get_last_dispatch(db: Session):
    return db.query(Dispatch).order_by(Dispatch.created_at.desc()).first()


def update_dispatch_checkpoint(db: Session, step: int, checkpoint_id: int, time_reported: str):
    """Actualiza time_reported del checkpoint checkpoint_id dentro del step indicado,
    siempre sobre el despacho más reciente almacenado localmente."""
    dispatch = get_last_dispatch(db)
    if not dispatch:
        return None

    if not isinstance(dispatch.data, list):
        log.error(
            f"[DISPATCH] El despacho cacheado no es una lista "
            f"({type(dispatch.data).__name__}) — no se actualiza"
        )
        return None

    updated = False
    for s in dispatch.data:
        if not isinstance(s, dict) or s.get("step") != step:
            continue
        checkpoints = s.get("checkpoints")
        if not isinstance(checkpoints, list):
            break
        for ckpt in checkpoints:
            if isinstance(ckpt, dict) and ckpt.get("id") == checkpoint_id:
                ckpt["time_reported"] = time_reported
                updated = True
                break
        break

    if not updated:
        # None => el llamador sabe que la marcación NO quedó reflejada. Antes se
        # devolvía el despacho igual, así que un checkpoint inexistente parecía
        # una actualización exitosa.
        log.warning(
            f"[DISPATCH] step={step} checkpoint_id={checkpoint_id} no existe en el "
            f"despacho cacheado — no se actualiza nada"
        )
        return None

    flag_modified(dispatch, "data")
    db.commit()
    db.refresh(dispatch)
    return dispatch


def create_event(db: Session, data: EventCreate) -> Event:
    event = Event(
        event_type=data.event_type,
        priority=data.priority.value,
        message=data.message,
        payload=data.payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_events(
    db: Session,
    priority: Optional[str] = None,
    event_type: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    after_id: Optional[int] = None,
):
    """
    Eventos más recientes primero. Los filtros son opcionales.

    Con `after_id` el endpoint se vuelve incremental: devuelve solo los eventos
    posteriores a ese id y en orden ASCENDENTE, para que un consumidor que hace
    polling (bus-display) los procese en el mismo orden en que ocurrieron. Sin
    `after_id` se conserva el comportamiento histórico (más recientes primero).
    """
    query = db.query(Event)
    if priority:
        query = query.filter(Event.priority == priority)
    if event_type:
        query = query.filter(Event.event_type == event_type)
    if start_date:
        query = query.filter(Event.created_at >= start_date)
    if end_date:
        query = query.filter(Event.created_at <= end_date)

    if after_id is not None:
        return query.filter(Event.id > after_id).order_by(Event.id.asc()).limit(limit).all()

    return query.order_by(Event.created_at.desc()).limit(limit).all()


def save_vehicle(db: Session, data: VehicleCreate) -> Vehicle:
    """Registra o actualiza (upsert por register) la informacion del vehiculo."""
    existing = db.query(Vehicle).filter(Vehicle.register == data.register).first()

    if existing:
        existing.plate = data.plate
        existing.data = data.data
        db.commit()
        db.refresh(existing)
        return existing

    vehicle = Vehicle(register=data.register, plate=data.plate, data=data.data)
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle


def get_last_vehicle(db: Session):
    return db.query(Vehicle).order_by(Vehicle.updated_at.desc()).first()
