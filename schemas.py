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
    # Control de concurrencia optimista, opcional. Quien lo envía declara sobre
    # qué revisión se basa su escritura; si la almacenada es más nueva, la
    # escritura se descarta en vez de pisarla. Lo usa bus_monitor.py para que
    # una carga suya que llegue tarde no deshaga una recarga manual.
    # Ausente = escritura incondicional (comportamiento histórico).
    base_revision: int | None = None


class DispatchResponse(BaseModel):
    date: str
    register: int
    data: list[dict]
    id: int
    # Revisión del despacho: se incrementa en cada escritura. Es el número con
    # el que el monitor detecta que la pantalla recargó el itinerario.
    revision: int = 0
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


# ─────────────────────────────────────────────
# ENERGIA DEL DISPOSITIVO
#
# Apagado ordenado de ESTA Raspberry (ver services/power.py). Una sola
# operacion, sin parametros: el cuerpo de la peticion no existe y el comando
# nunca se arma con datos del cliente.
# ─────────────────────────────────────────────

# scheduled         = apagado programado; el equipo se corta en unos segundos
# already_scheduled = ya habia uno en curso, no se lanza un segundo comando
# unavailable       = el equipo no tiene un comando de apagado utilizable
ShutdownStatus = Literal["scheduled", "already_scheduled", "unavailable"]


class ShutdownResponse(BaseModel):
    status: ShutdownStatus
    detail: str
    # Solo se completa cuando el apagado quedo realmente programado.
    scheduled_in_seconds: float | None = None


# ─────────────────────────────────────────────
# REINICIO / APAGADO (ver services/power.py)
#
# El frontend NUNCA propone un comando: ninguna de las dos peticiones tiene
# cuerpo. La acción queda determinada por la RUTA, y el comando concreto vive en
# el equipo (constante o SYSTEM_SHUTDOWN_COMMAND / SYSTEM_REBOOT_COMMAND).
# ─────────────────────────────────────────────

PowerAction = Literal["shutdown", "reboot"]


class PowerActionResponse(ShutdownResponse):
    """
    Respuesta de apagado y de reinicio.

    Extiende ShutdownResponse (misma forma histórica más dos campos) para que
    la pantalla pueda distinguir una acción de la otra:

      action         → lo que se pidió en ESTA petición
      pending_action → lo que el equipo tiene realmente pendiente

    Con status "already_scheduled" los dos pueden diferir: es el caso de tocar
    «Reiniciar» cuando ya había un apagado en curso, y la pantalla debe decir
    que se está apagando, no que se está reiniciando.

    "scheduled" significa que la orden se dará en unos segundos. NUNCA significa
    que el equipo ya se apagó o reinició.
    """
    action: PowerAction
    pending_action: PowerAction | None = None


# ─────────────────────────────────────────────
# CONEXION WI-FI (ver services/wifi.py)
#
# "Usuario" en esta pantalla significa NOMBRE DE RED (SSID). No hay soporte
# 802.1X / WPA-Enterprise, y el SSID no tiene relación alguna con el usuario del
# backend remoto SIMTRA.
#
# La clave viaja en el cuerpo de un POST a la API local (mismo dispositivo, red
# del bus), nunca en la URL ni en query string, y no se almacena en ninguna
# tabla: quien la conserva es NetworkManager, en su perfil protegido.
# ─────────────────────────────────────────────

class WifiConnectRequest(BaseModel):
    # Nombre de la red Wi-Fi. 32 octetos es el máximo de 802.11; la validación
    # fina (caracteres de control, prefijo '-') vive en services/wifi.py.
    ssid: str = Field(..., min_length=1, max_length=64)
    # Clave WPA/WPA2-PSK. Ausente o "" = red abierta, o reutilizar el perfil ya
    # guardado en el equipo. NUNCA se devuelve en ninguna respuesta.
    password: str | None = Field(None, max_length=63)

    class Config:
        # Un error de validación de Pydantic incluye el valor recibido. Con la
        # clave dentro del modelo, eso significaría devolverla al cliente dentro
        # del detalle del 422 y dejarla en los logs de uvicorn.
        json_schema_extra = {"example": {"ssid": "SIMTRA-PATIO", "password": "********"}}


# connected        = verificado: la red pedida quedó activa
# invalid_password = el punto de acceso rechazó el secreto
# not_found        = el SSID no está visible
# timeout          = no se pudo confirmar a tiempo
# unavailable      = no hay NetworkManager/nmcli utilizable
# no_adapter       = no hay adaptador Wi-Fi gestionado
# not_authorized   = faltan permisos (polkit)
# busy             = ya hay un intento en curso
# failed           = cualquier otro fallo
WifiStatus = Literal[
    "connected", "invalid_password", "not_found", "timeout",
    "unavailable", "no_adapter", "not_authorized", "busy", "failed",
]


class WifiConnectResponse(BaseModel):
    status: WifiStatus
    detail: str
    # Eco del SSID solicitado. La clave NO se devuelve nunca, ni enmascarada.
    ssid: str | None = None
    # Red del equipo recién consultada; solo se completa con status
    # "connected", para que la pantalla no tenga que hacer una segunda llamada.
    network: NetworkInfoResponse | None = None


# ─────────────────────────────────────────────
# RECARGA MANUAL DEL ITINERARIO
# (ver services/dispatch_refresh.py y POST /api/dispatch/refresh)
# ─────────────────────────────────────────────

# updated      = se descargó, validó y guardó un itinerario con despachos
# empty        = el backend respondió bien y el bus no trabaja hoy
# auth_error   = el backend remoto rechazó las credenciales del equipo
# remote_error = no se pudo hablar con el backend remoto
# invalid      = el backend respondió algo inutilizable
# save_error   = se descargó bien pero no se pudo guardar
#
# Solo "updated" y "empty" cambian el itinerario. En el resto se CONSERVA el
# anterior y `dispatch` trae lo que ya había (o null si nunca hubo nada).
DispatchRefreshStatus = Literal[
    "updated", "empty", "auth_error", "remote_error", "invalid", "save_error",
]


class DispatchRefreshResponse(BaseModel):
    status: DispatchRefreshStatus
    detail: str
    # Fecha (America/Guayaquil) y registro consultados.
    date: str
    register: int
    # El despacho REALMENTE almacenado tras la operación, con la misma forma que
    # GET /api/dispatch. Null solo si no hay ninguno para ese día.
    dispatch: DispatchResponse | None = None
    # Marcaciones locales pendientes que se conservaron al fusionar.
    preserved_reports: int = 0
    # Revisión que el monitor debe adoptar. Null cuando no hubo escritura.
    revision: int | None = None
