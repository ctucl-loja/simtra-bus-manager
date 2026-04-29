from sqlalchemy import Column, Integer, Float, DateTime,String,Boolean
from datetime import datetime, timezone
from datetime import datetime
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


