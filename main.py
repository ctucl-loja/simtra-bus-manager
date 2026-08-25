from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
import os
import models
from database import engine, SessionLocal
from schemas import GPSDataCreate, GPSDataResponse, CheckPointCreate, PassengerCreate, DispatchCreate, DispatchResponse, DispatchCheckpointUpdate, EventCreate, EventResponse, VehicleCreate, VehicleResponse, NetworkInfoResponse
import crud
from services import network_info
from datetime import datetime

load_dotenv()

ECUADOR_TZ = ZoneInfo("America/Guayaquil")
STATIC_DIR = Path(__file__).parent / "static"

# Origenes permitidos para la pantalla del bus (bus-display), que corre en el
# mismo dispositivo pero en otro puerto -> es cross-origin para el navegador.
# Se configura con FAST_API_CORS_ORIGINS (lista separada por comas); "*" abre
# a cualquier origen, aceptable porque el equipo esta aislado en el bus.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("FAST_API_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

models.Base.metadata.create_all(bind=engine)
app = FastAPI(title="SIMTRA TRACKING API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,   # la API local no usa cookies ni auth
    allow_methods=["*"],
    allow_headers=["*"],
)


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

@app.post("/api/passenger", response_model=PassengerCreate, status_code=201)
def save_passenger(data: PassengerCreate, db: Session = Depends(get_db)):
    return crud.create_passenger(db, data)

@app.get("/api/passenger/today")
def get_passengers_today(db: Session = Depends(get_db)):
    """Obtener pasajeros de hoy con total"""
    result = crud.get_passengers_today(db)
    return {
        "date": datetime.now(ECUADOR_TZ).date(),
        **result
    }


@app.patch("/api/passenger/{id}")
def update_status_passenger(id: int, db: Session = Depends(get_db)):
    return crud.upload_pending_passengers(db, id=id)

@app.get("/api/passenger/pending")
def get_pending_passenger(db: Session = Depends(get_db)):
    return crud.get_pending_passengers(db)


#endpoints dispatch

@app.post("/api/dispatch", response_model=DispatchResponse)
def save_dispatch(data: DispatchCreate, db: Session = Depends(get_db)):
    return crud.save_dispatch(db, data)

@app.get("/api/dispatch", response_model=Optional[DispatchResponse])
def read_dispatch(db: Session = Depends(get_db)):
    return crud.get_last_dispatch(db)

@app.patch("/api/dispatch/checkpoint", response_model=Optional[DispatchResponse])
def update_dispatch_checkpoint(data: DispatchCheckpointUpdate, db: Session = Depends(get_db)):
    return crud.update_dispatch_checkpoint(db, data.step, data.checkpoint_id, data.time_reported)


#endpoints events

@app.post("/api/events", response_model=EventResponse, status_code=201)
def create_event(data: EventCreate, db: Session = Depends(get_db)):
    return crud.create_event(db, data)

@app.get("/api/events", response_model=list[EventResponse])
def read_events(
    priority: Optional[str] = None,
    event_type: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    after_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    Canal local de eventos entre los procesos de la RPi.

    Con `after_id` la respuesta es incremental (solo eventos con id mayor) y
    ordenada de forma ascendente; sin él se mantiene el comportamiento actual.
    """
    return crud.get_events(db, priority, event_type, start_date, end_date, limit, after_id)


#endpoints vehicle

@app.post("/api/vehicle", response_model=VehicleResponse)
def save_vehicle(data: VehicleCreate, db: Session = Depends(get_db)):
    return crud.save_vehicle(db, data)

@app.get("/api/vehicle", response_model=Optional[VehicleResponse])
def read_vehicle(db: Session = Depends(get_db)):
    return crud.get_last_vehicle(db)


#endpoints sistema

@app.get("/api/system/network", response_model=NetworkInfoResponse)
def read_network_info():
    """
    Informacion de red de ESTE dispositivo (la Raspberry), para la vista /info
    de bus-display: el navegador no puede consultar el SSID ni las interfaces
    del sistema por su cuenta.

    Estrictamente de solo lectura e informativa. Expone unicamente tipo de
    conexion, interfaz, SSID y direcciones IPv4 — nunca credenciales, MAC,
    gateway, DNS ni rutas. No existe ninguna operacion que modifique la red.

    Nunca responde 500: si la informacion no se puede obtener (herramienta
    ausente, timeout, salida invalida, sistema no Linux) devuelve
    status="unavailable" con la lista vacia.
    """
    return network_info.get_network_info()


#herramienta de prueba: inyector manual de GPS

@app.get("/gps-tool")
def gps_tool_page():
    return FileResponse(STATIC_DIR / "gps_injector.html")