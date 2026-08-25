from pydantic import BaseModel,Field
from datetime import datetime
from enum import Enum
from typing import Literal

class GPSDataCreate(BaseModel):
    # allow_inf_nan=False: NaN e infinito son float válidos para Python pero
    # veneno para el geofencing (toda comparación con NaN es False, así que el
    # bus dejaría de entrar a las geocercas sin un solo error en el log).
    # El rango descarta además coordenadas imposibles.
    latitude: float = Field(..., allow_inf_nan=False, ge=-90, le=90)
    longitude: float = Field(..., allow_inf_nan=False, ge=-180, le=180)
    # speed es informativa: se admite null (el receptor puede no reportarla),
    # pero no NaN/infinito.
    speed: float | None = Field(None, allow_inf_nan=False)
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


class VehicleCreate(BaseModel):
    register: int
    plate: str | None = None
    data: dict | None = None


class VehicleResponse(VehicleCreate):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
# INFORMACION DE RED DEL DISPOSITIVO
#
# Solo lectura e informativa: describe a que red esta conectada ESTA Raspberry.
# No incluye credenciales, MAC, gateway, DNS ni rutas (ver services/network_info.py).
# ─────────────────────────────────────────────

# connected   = al menos una conexion activa con IPv4
# disconnected = se pudo consultar el sistema y no hay conexiones
# unavailable = no se pudo obtener la informacion (herramienta ausente, timeout,
#               salida invalida, sistema no Linux, permisos)
NetworkStatus = Literal["connected", "disconnected", "unavailable"]

ConnectionType = Literal["wifi", "ethernet", "other"]


class NetworkConnection(BaseModel):
    type: ConnectionType
    interface: str | None = None
    # SSID; solo se completa para Wi-Fi. En cable siempre null.
    name: str | None = None
    ipv4: list[str] = []


class NetworkInfoResponse(BaseModel):
    status: NetworkStatus
    connections: list[NetworkConnection] = []
