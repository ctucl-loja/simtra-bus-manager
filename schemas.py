from pydantic import BaseModel,Field
from datetime import datetime

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

