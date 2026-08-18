from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional
from models import Gps,CheckPoint,Passenger,Dispatch,Event,Vehicle
from schemas import GPSDataCreate,PassengerCreate,DispatchCreate,EventCreate,VehicleCreate
from datetime import datetime, timezone
from datetime import datetime,time

from zoneinfo import ZoneInfo

ECUADOR_TZ = ZoneInfo("America/Guayaquil")

def create_gps_data(db: Session, data: GPSDataCreate):
    gps = Gps(**data.dict())
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
    gps = db.query(Gps).order_by(Gps.timestamp.desc()).first()

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
    today = datetime.now().date()
    
    start_of_day = datetime.combine(today, time.min)
    end_of_day = datetime.combine(today, time.max)
    
    query = db.query(Passenger).filter(
        Passenger.timestamp >= start_of_day,
        Passenger.timestamp <= end_of_day
    ).order_by(Passenger.timestamp.desc())  # ← Más recientes primero
    
    passengers = query.all()
    total = query.count()
    
    return {
        "total": total,
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


def save_dispatch(db: Session, data: DispatchCreate):
    existing = db.query(Dispatch).filter(
        Dispatch.date == data.date,
        Dispatch.register == data.register
    ).first()

    if existing:
        existing.data = data.data
        db.commit()
        db.refresh(existing)
        return existing

    dispatch = Dispatch(date=data.date, register=data.register, data=data.data)
    db.add(dispatch)
    db.commit()
    db.refresh(dispatch)
    return dispatch


def get_last_dispatch(db: Session):
    return db.query(Dispatch).order_by(Dispatch.created_at.desc()).first()


def update_dispatch_checkpoint(db: Session, step: int, checkpoint_id: int, time_reported: str):
    """Actualiza time_reported del checkpoint checkpoint_id dentro del step indicado,
    siempre sobre el despacho más reciente almacenado localmente."""
    dispatch = get_last_dispatch(db)
    if not dispatch:
        return None

    for s in dispatch.data:
        if s.get("step") == step:
            for ckpt in s.get("checkpoints", []):
                if ckpt.get("id") == checkpoint_id:
                    ckpt["time_reported"] = time_reported
                    break
            break

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
