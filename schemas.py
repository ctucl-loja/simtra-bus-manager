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
    direction: int = Field(0, ge=0)  # 0 entrada, 1 salida


class PassengerResponse(BaseModel):
    id: int
    timestamp: datetime
    direction: int
    latitude: float
    longitude: float
    upload: bool
    created_at: datetime

    class Config:
        from_attributes = True

