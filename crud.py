from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional
from models import Gps,CheckPoint,Passenger,Dispatch,Event,Vehicle
from schemas import GPSDataCreate,PassengerCreate,DispatchCreate,EventCreate,VehicleCreate
from datetime import datetime, timezone
from datetime import datetime,time

from zoneinfo import ZoneInfo
import logging

log = logging.getLogger("simtra")

ECUADOR_TZ = ZoneInfo("America/Guayaquil")

def create_gps_data(db: Session, data: GPSDataCreate):
    gps = Gps(**data.model_dump())
    db.add(gps)
    db.commit()
    db.refresh(gps)
    return gps


def get_all_gps(db: Session):
    return db.query(Gps).order_by(Gps.id.desc()).limit(100).all()


def get_last_position(db: Session):
    return db.query(Gps)\
        .order_by(Gps.created_at.desc())\
        .first()

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

def create_passenger(db: Session, data: PassengerCreate) -> Passenger:
    # Misma regla de "última posición" que get_last_position, para que un
    # pasajero y el mapa no se refieran a lecturas distintas.
    gps = db.query(Gps).order_by(Gps.created_at.desc()).first()

    if gps is None:
        # Se registra igual: perder el conteo de pasajeros sería peor que
        # guardarlo sin ubicación. Pero queda dicho en el log, porque (0, 0) es
        # una coordenada real y no debe confundirse con una posición medida.
        log.warning(
            "[PASSENGER] Todavía no hay ninguna lectura GPS: el pasajero se "
            "guarda con coordenadas (0, 0), que NO son una posición real"
        )

    passenger = Passenger(
        timestamp=datetime.now(ECUADOR_TZ),
        direction=data.direction,
        door=data.door,
        latitude=gps.latitude if gps else 0.0,
        longitude=gps.longitude if gps else 0.0,
    )
    db.add(passenger)
    db.commit()
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
