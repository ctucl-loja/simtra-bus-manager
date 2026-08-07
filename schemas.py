from pydantic import BaseModel,Field
from datetime import datetime
from enum import Enum

class GPSDataCreate(BaseModel):
    latitude: float
    longitude: float
    speed: float | None = None
    timestamp: datetime

class GPSDataResponse(GPSDataCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CheckPointCreate(BaseModel):
    checkpoint_id: int
    name: str
    timestamp: str


class PassengerCreate(BaseModel):
    direction: str
    door:str


class PassengerResponse(BaseModel):
    id: int
    timestamp: datetime
    direction: str
    door:str
    latitude: float
    longitude: float
    upload: bool
    created_at: datetime

    class Config:
        from_attributes = True


class DispatchCreate(BaseModel):
    date: str
    register: int
    data: list[dict]


class DispatchResponse(DispatchCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class DispatchCheckpointUpdate(BaseModel):
    step: int
    checkpoint_id: int
    time_reported: str


class EventPriority(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EventCreate(BaseModel):
    event_type: str
    priority: EventPriority
    message: str
    payload: dict | None = None


class EventResponse(EventCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True

