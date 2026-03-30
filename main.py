from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from typing import Optional

import models
from database import engine, SessionLocal
from schemas import GPSDataCreate, GPSDataResponse, CheckPointCreate, PassengerCreate
import crud


models.Base.metadata.create_all(bind=engine)
app = FastAPI(title="SIMTRA TRACKING API")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

#endpoints gps

@app.post("/api/gps", response_model=GPSDataResponse)
def create_gps(data: GPSDataCreate, db: Session = Depends(get_db)):
    return crud.create_gps_data(db, data)


@app.get("/api/gps", response_model=list[GPSDataResponse])
def read_gps(db: Session = Depends(get_db)):
    return crud.get_all_gps(db)


@app.get("/api/gps/last_position", response_model=Optional[GPSDataResponse])
def read_last_position(db: Session = Depends(get_db)):   # nombre corregido
    return crud.get_last_position(db)


#endpoints checkpoints

@app.post("/api/checkpoint")
def save_checkpoint(data: CheckPointCreate, db: Session = Depends(get_db)):
    return crud.create_checkpoint(db, data.checkpoint_id, data.name, data.timestamp)

@app.patch("/api/checkpoint/{id}")
def update_status_checkpoint(id: int, db: Session = Depends(get_db)):
    return crud.upload_pending_checkpoints(db, id=id)

@app.get("/api/checkpoint/pending")
def get_pending_checkpoint(db: Session = Depends(get_db)):
    return crud.get_pending_checkpoints(db)



#endpoints passengers

@app.post("/api/passenger")
def save_passenger(db: Session = Depends(get_db)):
    return crud.create_passenger(db)

@app.patch("/api/passenger/{id}")
def update_status_passenger(id: int, db: Session = Depends(get_db)):
    return crud.upload_pending_passengers(db, id=id)

@app.get("/api/passenger/pending")
def get_pending_passenger(db: Session = Depends(get_db)):
    return crud.get_pending_passengers(db)