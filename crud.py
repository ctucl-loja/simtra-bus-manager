from sqlalchemy.orm import Session
from models import Gps,CheckPoint,Passenger
from schemas import GPSDataCreate
from datetime import datetime, timezone
from datetime import datetime
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

def create_passenger(db: Session):
    gps = db.query(Gps)\
        .order_by(Gps.timestamp.desc())\
        .first()

    if not gps:
        return None  # o lanzar excepción

    passenger = Passenger(
        timestamp=datetime.now(),
        latitude=gps.latitude,
        longitude=gps.longitude
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



def upload_pending_passengers(db: Session, id: int):
    passenger = db.query(Passenger).filter(Passenger.id == id).first()
    if not passenger:
        return None
    passenger.upload = True  # o el campo que uses
    db.commit()
    db.refresh(passenger)
    return passenger
