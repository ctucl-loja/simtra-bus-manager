from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
import logging
import os
import models
import database
from database import engine, SessionLocal
from schemas import (
    GPSDataCreate, GPSDataResponse, CheckPointCreate, PassengerCreate, DispatchCreate,
    DispatchResponse, DispatchCheckpointUpdate, DispatchRefreshResponse, EventCreate,
    EventResponse, VehicleCreate, VehicleResponse, NetworkInfoResponse,
    PowerActionResponse, WifiConnectRequest, WifiConnectResponse,
)
import crud
from services import network_info, power, wifi, dispatch_refresh
from services.api import ApiService, FETCH_OK, FETCH_EMPTY, FETCH_AUTH_ERROR, FETCH_INVALID
from datetime import datetime

load_dotenv()

log = logging.getLogger("simtra")

ECUADOR_TZ = ZoneInfo("America/Guayaquil")
STATIC_DIR = Path(__file__).parent / "static"

# Identidad del bus y credenciales del backend remoto. Viven SOLO aquí (y en el
# monitor y el loader): ninguna llega jamás al frontend, que solo habla con esta
# API local y nunca con el backend remoto.
BUS_REGISTER = int(os.getenv("FAST_API_BUS_REGISTER") or 0)
BACKEND_URL = os.getenv("FAST_API_BACKEND_URL")
BACKEND_USERNAME = os.getenv("FAST_API_BACKEND_USERNAME")
BACKEND_PASSWORD = os.getenv("FAST_API_BACKEND_PASSWORD")

# Cliente del backend remoto, reutilizado entre peticiones para conservar el
# JWT: pedir un token nuevo en cada recarga sería una llamada de red extra en la
# red del bus. El token se renueva solo (ver services/api.py).
remote_api = ApiService(BACKEND_URL, BACKEND_USERNAME, BACKEND_PASSWORD)

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
# Columnas agregadas después de la primera puesta en marcha (hoy:
# dispatch.revision). En una RPi que ya lleva meses corriendo, create_all no
# toca una tabla existente y sin esto el servicio arrancaría para fallar en la
# primera consulta.
for column in database.ensure_schema(engine):
    log.warning("[SCHEMA] Columna agregada: %s", column)
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
    """
    Cache local del despacho del dia. Lo escribe bus_monitor cada vez que
    descarga los despachos del backend remoto.

    Dos detalles que importan (ver crud.save_dispatch):

      * las horas de llegada que ya estaban guardadas y el payload entrante no
        trae se CONSERVAN: el backend remoto todavia no conoce las marcaciones
        que data_loader no ha subido;
      * `base_revision` (opcional) descarta la escritura si la revision
        almacenada es mas nueva, para que una carga del monitor iniciada antes
        de una recarga manual no la deshaga al llegar tarde.
    """
    return crud.save_dispatch(db, data, base_revision=data.base_revision)

@app.get("/api/dispatch", response_model=Optional[DispatchResponse])
def read_dispatch(db: Session = Depends(get_db)):
    return crud.get_last_dispatch(db)

# Evento con el que la API local le avisa a simtra-bus-monitor que el
# itinerario cambio. Los tres procesos (FastAPI, monitor y loader) son unidades
# systemd separadas: no comparten memoria, asi que la coordinacion pasa por la
# base de datos. Importar el monitor desde aqui para tocar sus globals no haria
# absolutamente nada sobre el proceso real.
DISPATCH_REFRESHED_EVENT = "dispatch_refreshed"


def _empty_refresh(status: str, detail: str, date: str, dispatch) -> DispatchRefreshResponse:
    """Respuesta que NO cambia el itinerario: se devuelve el que ya habia."""
    return DispatchRefreshResponse(
        status=status,
        detail=detail,
        date=date,
        register=BUS_REGISTER,
        dispatch=dispatch,
        revision=dispatch.revision if dispatch else None,
    )


@app.post("/api/dispatch/refresh", response_model=DispatchRefreshResponse)
def refresh_dispatch(db: Session = Depends(get_db)):
    """
    Vuelve a descargar el itinerario del dia del backend remoto SIMTRA y lo
    guarda localmente. Es lo que hace el boton «Volver a cargar itinerario».

    Flujo real:
        pantalla -> POST local -> backend remoto -> validacion -> persistencia
        -> respuesta con el despacho guardado -> evento para el monitor

    Sin cuerpo: el registro sale de FAST_API_BUS_REGISTER y la fecha del reloj
    en America/Guayaquil. Las credenciales del backend remoto viven en el .env
    del equipo y NUNCA llegan al frontend.

    Contrato de la respuesta (DispatchRefreshResponse):

        status "updated"      -> itinerario nuevo guardado
               "empty"        -> el backend respondio bien y el bus NO trabaja
                                 hoy; el itinerario queda vacio a proposito
               "auth_error"   -> credenciales rechazadas por el backend remoto
               "remote_error" -> no se pudo hablar con el backend remoto
               "invalid"      -> respondio algo inutilizable
               "save_error"   -> se descargo bien pero no se pudo guardar

    Solo "updated" y "empty" tocan el itinerario. En los cuatro estados de error
    se CONSERVA el anterior y se devuelve tal cual en `dispatch`: un fallo de
    red no puede dejar al conductor sin recorrido.

    Una lista con contenido invalido NO equivale a un dia sin despachos: se
    responde "invalid" y no se borra nada (services/dispatch_refresh.py).

    Concurrencia: la fusion y la escritura ocurren en una sola transaccion que
    relee la fila, asi que una marcacion que entre por
    PATCH /api/dispatch/checkpoint durante la descarga no se pierde. Las
    marcaciones locales aun no subidas se conservan por identidad
    (step, checkpoint_id), nunca por posicion en el array.
    """
    date = datetime.now(ECUADOR_TZ).strftime("%Y-%m-%d")
    current = crud.get_dispatch_for(db, date, BUS_REGISTER)

    if not BACKEND_URL:
        return _empty_refresh(
            "remote_error",
            "Este equipo no tiene configurado el servidor de SIMTRA",
            date, current,
        )

    fetched = remote_api.fetch_dispatch(BUS_REGISTER, date)

    if fetched.status == FETCH_AUTH_ERROR:
        return _empty_refresh(
            "auth_error",
            "El servidor de SIMTRA rechazó las credenciales de este equipo",
            date, current,
        )
    if fetched.status == FETCH_INVALID:
        return _empty_refresh(
            "invalid",
            "El servidor devolvió un itinerario con un formato inesperado",
            date, current,
        )
    if fetched.status not in (FETCH_OK, FETCH_EMPTY):
        return _empty_refresh(
            "remote_error",
            "No se pudo contactar con el servidor de SIMTRA",
            date, current,
        )

    # Validacion ANTES de reemplazar nada. Una lista con basura dentro se
    # rechaza: seria peor mostrar un itinerario roto que no actualizarlo.
    validation = dispatch_refresh.validate_dispatches(fetched.dispatches)
    if not validation:
        log.error("[REFRESH] Itinerario remoto rechazado: %s", validation.reason)
        return _empty_refresh("invalid", validation.reason, date, current)

    remote_data = fetched.dispatches

    def merge(previous, pending_ids, incoming):
        return dispatch_refresh.merge_pending_reports(
            incoming, dispatch_refresh.local_reports(previous, pending_ids)
        )

    try:
        dispatch, report = crud.refresh_dispatch(db, date, BUS_REGISTER, remote_data, merge)
    except Exception:
        log.exception("[REFRESH] No se pudo guardar el itinerario descargado")
        return _empty_refresh(
            "save_error",
            "Se descargó el itinerario pero no se pudo guardar en el equipo",
            date, current,
        )

    # El evento va DESPUES del commit: el monitor no debe enterarse de una
    # revision que todavia podria no existir. Su fallo no invalida la recarga,
    # solo retrasa la adopcion del monitor hasta su proximo ciclo.
    try:
        crud.create_event(db, EventCreate(
            event_type=DISPATCH_REFRESHED_EVENT,
            priority="HIGH",
            message="Itinerario recargado desde la pantalla",
            payload={
                "date": date,
                "register": BUS_REGISTER,
                "revision": dispatch.revision,
                "steps": len(remote_data),
                "checkpoints": dispatch_refresh.count_checkpoints(remote_data),
            },
        ))
    except Exception:
        log.exception("[REFRESH] No se pudo publicar el evento para el monitor")

    empty = not remote_data
    return DispatchRefreshResponse(
        status="empty" if empty else "updated",
        detail=(
            "El servidor no tiene despachos para este bus hoy"
            if empty else
            f"Itinerario actualizado: {len(remote_data)} recorrido(s)"
        ),
        date=date,
        register=BUS_REGISTER,
        dispatch=dispatch,
        preserved_reports=len(report.preserved),
        revision=dispatch.revision,
    )


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


@app.post("/api/system/shutdown", response_model=PowerActionResponse)
def shutdown_device():
    """
    Apaga ESTE dispositivo (la Raspberry) de forma ordenada.

    La pantalla corre en Chromium en modo kiosco, sin teclado ni escritorio: sin
    este endpoint la unica forma de apagar el equipo es cortarle la corriente, y
    eso es lo que termina corrompiendo la tarjeta SD.

    No recibe cuerpo ni parametros: el comando de apagado es una constante del
    equipo (o SYSTEM_SHUTDOWN_COMMAND), nunca algo que llegue del cliente. El
    corte ocurre unos segundos DESPUES de responder, para que la pantalla
    alcance a mostrar el aviso en vez de un error de red.

    La confirmacion del conductor se resuelve en la pantalla: llegar aqui ya
    significa que acepto el dialogo.

    Apagado y reinicio se excluyen entre si: si ya hay un REINICIO en curso se
    responde `already_scheduled` con `pending_action: "reboot"`, para que la
    pantalla anuncie lo que realmente va a pasar y no un apagado.
    """
    return _power_response(power.request_shutdown())


@app.post("/api/system/reboot", response_model=PowerActionResponse)
def reboot_device():
    """
    Reinicia ESTE dispositivo (la Raspberry) de forma ordenada.

    Mismo contrato que el apagado: sin cuerpo y sin parametros. El comando es
    una constante del equipo (o SYSTEM_REBOOT_COMMAND) y NUNCA puede
    proporcionarlo el frontend; la accion la determina esta ruta.

    En la Raspberry Pi objetivo el comando es `sudo -n /sbin/shutdown -r now`,
    el equivalente correcto de "reboot now" (`reboot` no acepta un argumento
    `now`). La orden se da unos segundos DESPUES de responder, para que la
    pantalla alcance a mostrar el aviso en vez de un error de red.

    Apagado y reinicio se excluyen: si ya hay uno de los dos en curso se
    responde `already_scheduled` y `pending_action` dice CUAL, para que la
    pantalla no confunda una accion con la otra.

    `scheduled` significa que el sistema recibira la orden. NO significa que el
    equipo ya se haya reiniciado: eso no puede confirmarlo el proceso que se va
    a morir con el.
    """
    return _power_response(power.request_reboot())


def _power_response(result) -> PowerActionResponse:
    return PowerActionResponse(
        status=result.status,
        detail=result.detail,
        scheduled_in_seconds=result.scheduled_in_seconds,
        action=result.action,
        pending_action=result.pending_action,
    )


@app.post("/api/system/wifi/connect", response_model=WifiConnectResponse)
def connect_wifi(data: WifiConnectRequest):
    """
    Conecta (o reconecta) ESTE dispositivo a una red Wi-Fi.

    Es la UNICA operacion que escribe sobre la red del equipo;
    GET /api/system/network sigue siendo de solo lectura.

    Contrato
    --------
    Entrada : {"ssid": "<nombre de red>", "password": "<clave o null>"}
              "usuario" en la pantalla significa NOMBRE DE RED (SSID). No hay
              soporte 802.1X/WPA-Enterprise y el SSID no tiene ninguna relacion
              con el usuario del backend remoto SIMTRA.
              password ausente o "" = red abierta, o reutilizar el perfil ya
              guardado en el equipo.
    Salida  : {"status", "detail", "ssid", "network"}
              status connected | invalid_password | not_found | timeout |
                     unavailable | no_adapter | not_authorized | busy | failed
              `network` solo viene con "connected".

    Garantias
    ---------
    * La clave viaja a nmcli por STDIN, nunca en la linea de comandos: no es
      visible en `ps` ni en /proc.
    * La clave no se guarda en ninguna tabla ni se registra en ningun log, y no
      vuelve al cliente en ninguna respuesta, ni siquiera en un mensaje de
      error. La conserva NetworkManager en su perfil protegido.
    * "connected" solo se responde tras VERIFICAR que la conexion quedo activa;
      que nmcli devuelva 0 no basta.
    * Nunca responde 500: falta de nmcli, de adaptador o de permisos son
      estados propios de la respuesta.

    Aviso: cambiar de red puede cortar el acceso desde otro dispositivo de la
    red anterior. Que la pantalla remota pierda la respuesta NO significa que la
    conexion fallara; significa que ya no esta en la misma red.
    """
    try:
        result = wifi.connect(data.ssid, data.password)
    except wifi.InvalidParameter as e:
        # Mensaje propio, que por construccion no cita el valor recibido: un
        # 422 de Pydantic devolveria la clave dentro del detalle del error.
        return WifiConnectResponse(status="failed", detail=str(e), ssid=None)
    except Exception:
        log.exception("[WIFI] Fallo inesperado al conectar")
        return WifiConnectResponse(status="failed", detail=wifi.DETAILS[wifi.STATUS_FAILED])

    return WifiConnectResponse(
        status=result.status,
        detail=result.detail,
        ssid=result.ssid,
        network=result.network,
    )


#herramienta de prueba: inyector manual de GPS

@app.get("/gps-tool")
def gps_tool_page():
    return FileResponse(STATIC_DIR / "gps_injector.html")