from sqlalchemy import Column, Integer, Float, DateTime,String,Boolean,JSON
from datetime import datetime, timezone
from database import Base

class Gps(Base):
    __tablename__ = "gps"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    speed = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CheckPoint(Base):
    __tablename__ = "checkpoint"
    id = Column(Integer, primary_key=True, index=True)
    checkpoint_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    timestamp = Column(String, nullable=False)   # DateTime, no String
    upload = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Dispatch(Base):
    __tablename__ = "dispatch"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, nullable=False, index=True)
    register = Column(Integer, nullable=False, index=True)
    data = Column(JSON, nullable=False)   # lista de steps/checkpoints tal como la entrega el backend
    # Se incrementa en CADA escritura del despacho. Dos usos, ambos entre
    # procesos distintos (FastAPI, monitor y loader son servicios systemd
    # separados, sin memoria compartida):
    #   · el monitor detecta que la pantalla recargó el itinerario y lo adopta;
    #   · una carga antigua del monitor que llegue tarde se rechaza en vez de
    #     pisar una recarga más nueva (ver crud.save_dispatch / base_revision).
    # Columna añadida después: database.ensure_schema() la agrega en equipos
    # que ya tenían la tabla creada.
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    priority = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    message = Column(String, nullable=False)
    payload = Column(JSON, nullable=True)   # datos especificos del event_type (geofence_id, checkpoint_id, step, etc.)


class Vehicle(Base):
    __tablename__ = "vehicle"
    id = Column(Integer, primary_key=True, index=True)
    register = Column(Integer, nullable=False, index=True)
    plate = Column(String, nullable=True, index=True)
    data = Column(JSON, nullable=True)   # informacion completa del vehiculo tal como la entrega el backend
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Passenger(Base):
    __tablename__ = "passenger"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=False), nullable=False)
    direction = Column(String, nullable=False,default='ENTRY') #0 para entrada ,1 para salida , se deja numerico por si hay mas casos
    door = Column(String,nullable=False,default='FRONT')
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    upload = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


