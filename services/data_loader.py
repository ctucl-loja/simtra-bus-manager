"""
simtra-bus-loader: sube al backend remoto (device-api) lo acumulado en la API
local — marcaciones de checkpoints, pasajeros y traza GPS.

Flujo de cada elemento:

    SQLite pendiente → API local (GET …/pending) → device-api con X-API-Key
    → respuesta confirmada por el contrato → PATCH local (upload = True)

Reglas:
  · Solo se marca como subido lo que device-api confirmó (201/200, o 409 en
    checkpoints: el backend ya tiene otra hora y no la sobrescribe).
  · Timeout, red caída, 5xx, API key ausente o rechazada → el elemento queda
    pendiente y el ciclo se corta: los siguientes fallarían igual.
  · Un punto GPS que el backend rechaza con 400 (o que localmente es
    inutilizable) sale de la cola con su motivo, sin marcarse como subido, para
    no bloquear a los siguientes.
  · Si el backend confirmó pero falló el PATCH local, el id se recuerda y en el
    ciclo siguiente se reintenta SOLO el marcado local, sin reenviar.
  · Orden: checkpoints, pasajeros y por último GPS, en tandas acotadas por
    cantidad y por tiempo, para que una cola GPS larga no retrase a los demás.

Importar este módulo no arranca el bucle (ver `run_forever`).
"""

import logging
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from api import (
    ApiService, SEND_OK, SEND_CONFLICT, SEND_REJECTED, SEND_NOT_FOUND,
)


# ---------------- CONFIG ----------------
load_dotenv()
LOCAL_BACKEND = os.getenv("FAST_API_LOCAL_BACKEND") or "http://127.0.0.1:8000"
BACKEND_URL = os.getenv("FAST_API_BACKEND_URL")
DEVICE_API_KEY = os.getenv("FAST_API_DEVICE_API_KEY", "")
BUS_REGISTER = int(os.getenv("FAST_API_BUS_REGISTER") or 0)
ECUADOR_TZ = ZoneInfo("America/Guayaquil")

# Timeout de cada llamada a la API local (mismo equipo).
LOCAL_TIMEOUT = 5
# Puntos GPS por ciclo y tiempo máximo dedicado a ellos. Con la red del bus a
# ~1 s por petición, 100 puntos caben en el presupuesto; con red lenta el
# presupuesto corta antes y el resto queda para el ciclo siguiente.
GPS_BATCH_SIZE = 100
GPS_CYCLE_BUDGET_SECONDS = 30

logger = logging.getLogger("data_loader")


# ─────────────────────────────────────────────
# CONVERSIONES
# ─────────────────────────────────────────────

def as_float(value):
    """float utilizable o None. Un registro sin coordenadas no se puede subir."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_local_datetime(value):
    """
    datetime CON zona a partir de lo que devuelve la API local, o None.

    SQLite no guarda la zona horaria: la API local devuelve fechas sin zona que
    son hora de pared de America/Guayaquil (así las escribe create_passenger).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ECUADOR_TZ)


def unix_seconds(value):
    """
    Timestamp Unix en segundos (entero) para device-api, o None.

    Número → se usa tal cual (ya es Unix). Texto ISO → con zona es exacto; sin
    zona se interpreta como America/Guayaquil. Nunca se aplica más de una
    conversión.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) else None
    parsed = parse_local_datetime(value)
    return int(parsed.timestamp()) if parsed else None


def passenger_payload(p):
    """
    Cuerpo para `POST /api/device-api/passenger`, o None si al registro local
    le falta algo. El ValidationPipe remoto rechaza campos de más, así que solo
    se envían los del CreatePassengerDto.
    """
    if not isinstance(p, dict):
        return None

    latitude = as_float(p.get("latitude"))
    longitude = as_float(p.get("longitude"))
    timestamp = parse_local_datetime(p.get("timestamp"))

    if latitude is None or longitude is None or timestamp is None:
        return None

    payload = {
        "latitude": latitude,
        "longitude": longitude,
        "register": int(BUS_REGISTER),
        # Con desfase explícito: una fecha sin zona la interpretaría el backend
        # en SU zona horaria.
        "timestamp": timestamp.isoformat(),
    }
    for name in ("direction", "door"):
        if p.get(name) is not None:
            payload[name] = p.get(name)
    return payload


def gps_payload(point):
    """
    Cuerpo para `POST /api/device-api/gps/:register` (CreateGpsDto), o None si
    el registro local es inutilizable.

    `timestamp_unix` (calculado al recibir el punto) manda sobre `timestamp`,
    que llega sin zona desde SQLite. Solo las filas previas a la migración
    carecen de él.
    """
    if not isinstance(point, dict):
        return None

    latitude = as_float(point.get("latitude"))
    longitude = as_float(point.get("longitude"))
    timestamp = unix_seconds(point.get("timestamp_unix"))
    if timestamp is None:
        timestamp = unix_seconds(point.get("timestamp"))

    if latitude is None or longitude is None or timestamp is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    payload = {
        "timestamp": timestamp,
        "latitude": latitude,
        "longitude": longitude,
    }

    # Velocidad desconocida (null) se omite: el DTO remoto la declara opcional.
    speed = as_float(point.get("speed"))
    if speed is not None and speed >= 0:
        payload["speed"] = speed

    return payload


# ─────────────────────────────────────────────
# API - CLIENT
# ─────────────────────────────────────────────

simtra = ApiService(BACKEND_URL, DEVICE_API_KEY)

# (cola, id local) confirmados por el backend cuyo PATCH local falló. Se
# reintenta solo el marcado; reenviar crearía un pasajero duplicado.
# Vive en memoria: si el proceso se reinicia, el elemento se reenvía (GPS y
# checkpoints son idempotentes en el backend; ver README).
_sent_not_marked: set = set()


# ─────────────────────────────────────────────
# API LOCAL
# ─────────────────────────────────────────────

def _get_pending(path: str, label: str, params=None):
    try:
        resp = requests.get(f"{LOCAL_BACKEND}{path}", params=params, timeout=LOCAL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching {label}: {e}")
        return []
    except ValueError:
        logger.error(f"Respuesta de {path} no es JSON valido")
        return []

    if not isinstance(data, list):
        logger.error(f"{path} devolvio {type(data).__name__}, se esperaba lista")
        return []

    if data:
        logger.info(f"{label} pending: {len(data)}")
    return data


def get_pending_passengers():
    return _get_pending("/api/passenger/pending", "Passengers")


def get_pending_checkpoints():
    return _get_pending("/api/checkpoint/pending", "Checkpoints")


def get_pending_gps(limit=GPS_BATCH_SIZE):
    return _get_pending("/api/gps/pending", "GPS", params={"limit": limit})


def _patch_local(path: str, label: str, json=None, method="PATCH") -> bool:
    try:
        resp = requests.request(method, f"{LOCAL_BACKEND}{path}", json=json, timeout=LOCAL_TIMEOUT)
        if resp.status_code == 404:
            # La fila ya no existe: no queda nada que marcar.
            logger.warning(f"{label}: no existe en la API local")
            return True
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Error updating {label}: {e}")
        return False


def update_passenger_local_register(id):
    return _patch_local(f"/api/passenger/{id}", f"passenger {id}")


def update_checkpoint_local_register(id):
    return _patch_local(f"/api/checkpoint/{id}", f"checkpoint {id}")


def update_gps_local_register(id):
    return _patch_local(f"/api/gps/{id}", f"GPS point {id}")


def reject_gps_local_register(id, reason):
    return _patch_local(
        f"/api/gps/{id}/reject", f"GPS point {id} (reject)",
        json={"reason": reason}, method="POST",
    )


def _mark(kind: str, row_id) -> bool:
    marker = {
        "gps": update_gps_local_register,
        "passenger": update_passenger_local_register,
        "checkpoint": update_checkpoint_local_register,
    }[kind]
    if marker(row_id):
        _sent_not_marked.discard((kind, row_id))
        return True
    _sent_not_marked.add((kind, row_id))
    logger.warning(f"{kind} {row_id} sent but NOT updated locally — se reintentará el marcado")
    return False


def retry_local_marks() -> int:
    """Reintenta el marcado local de lo ya confirmado por el backend."""
    done = 0
    for kind, row_id in sorted(_sent_not_marked):
        if _mark(kind, row_id):
            done += 1
    return done


# ─────────────────────────────────────────────
# COLAS
# ─────────────────────────────────────────────

def sync_checkpoints():
    """Devuelve (procesados, cortar_ciclo)."""
    done = 0
    for c in get_pending_checkpoints():
        if not isinstance(c, dict):
            logger.error(f"Checkpoint local record con forma inesperada, se omite: {c!r}")
            continue

        row_id = c.get('id')
        remote_id = c.get('checkpoint_id')
        timestamp = c.get('timestamp')

        if row_id is None or remote_id is None or not timestamp:
            logger.error(f"Checkpoint local record incompleto, se omite: {c!r}")
            continue
        if ("checkpoint", row_id) in _sent_not_marked:
            continue

        try:
            formatted_data = {'id': int(remote_id), 'time_reported': timestamp}
        except (TypeError, ValueError):
            logger.error(f"Checkpoint {row_id} con checkpoint_id no numerico ({remote_id!r}), se omite")
            continue

        logger.info(f"Sending checkpoint {row_id}")
        result = simtra.update_dispatch(formatted_data, BUS_REGISTER)

        if result.status in (SEND_OK, SEND_CONFLICT):
            if result.status == SEND_CONFLICT:
                logger.warning(
                    f"Checkpoint {row_id}: el backend ya tiene otra hora para el despacho "
                    f"{remote_id}; se conserva la remota y se marca como sincronizado"
                )
            _mark("checkpoint", row_id)
            done += 1
        elif result.stops_queue:
            logger.warning(f"Failed to send checkpoint {row_id} ({result.status}) — se corta el ciclo")
            return done, True
        else:
            # 400/404: el despacho no existe o no es de este bus. No bloquea a
            # los demás; queda pendiente para revisión.
            logger.warning(f"Failed to send checkpoint {row_id} ({result.status})")
    return done, False


def sync_passengers():
    done = 0
    for p in get_pending_passengers():
        passenger_id = p.get('id') if isinstance(p, dict) else None
        formatted_data = passenger_payload(p)

        if passenger_id is None or formatted_data is None:
            logger.error(f"Passenger local record incompleto, se omite: {p!r}")
            continue
        if ("passenger", passenger_id) in _sent_not_marked:
            continue

        logger.info(f"Sending passenger {passenger_id}")
        result = simtra.post_passenger(formatted_data)

        if result.ok:
            _mark("passenger", passenger_id)
            done += 1
        elif result.stops_queue or result.status == SEND_NOT_FOUND:
            # 404 aquí = el registro del bus no existe en el backend: afecta a
            # todos los envíos, no solo a este.
            logger.warning(f"Failed to send passenger {passenger_id} ({result.status}) — se corta el ciclo")
            return done, True
        else:
            logger.warning(f"Failed to send passenger {passenger_id} ({result.status})")
    return done, False


def sync_gps(clock=time.monotonic):
    done = 0
    started = clock()

    for point in get_pending_gps(GPS_BATCH_SIZE):
        if clock() - started > GPS_CYCLE_BUDGET_SECONDS:
            logger.info("GPS: presupuesto de tiempo agotado, el resto queda para el próximo ciclo")
            break

        row_id = point.get("id") if isinstance(point, dict) else None
        if row_id is None:
            logger.error(f"GPS local record sin id, se omite: {point!r}")
            continue
        if ("gps", row_id) in _sent_not_marked:
            continue

        formatted_data = gps_payload(point)
        if formatted_data is None:
            logger.error(f"GPS point {row_id} inutilizable, sale de la cola: {point!r}")
            if reject_gps_local_register(row_id, "registro local inutilizable"):
                done += 1
            continue

        result = simtra.post_gps(formatted_data, BUS_REGISTER)

        if result.ok:
            _mark("gps", row_id)
            done += 1
        elif result.status in (SEND_REJECTED, SEND_CONFLICT):
            logger.error(f"GPS point {row_id} rechazado por el backend (HTTP {result.http_status}), sale de la cola")
            if reject_gps_local_register(row_id, f"rechazado por device-api (HTTP {result.http_status})"):
                done += 1
        else:
            # Red, 5xx, 401/403, 404 (bus inexistente) o falta de clave: el
            # siguiente punto fallaría igual. Queda pendiente, en orden.
            logger.warning(f"Failed to send GPS point {row_id} ({result.status}) — se corta el ciclo")
            return done, True
    return done, False


def sync_once() -> bool:
    """
    Una pasada por las tres colas. Devuelve True si procesó algo (el bucle
    duerme menos cuando acaba de trabajar).
    """
    config_error = simtra.configuration_error()
    if config_error:
        logger.error(f"Sincronización detenida: {config_error}. Configúrala en el .env y reinicia el servicio")
        return False
    if BUS_REGISTER <= 0:
        logger.error("Sincronización detenida: FAST_API_BUS_REGISTER no está configurado")
        return False

    done = retry_local_marks()

    for sync in (sync_checkpoints, sync_passengers, sync_gps):
        processed, halt = sync()
        done += processed
        if halt:
            break

    return done > 0


def run_forever():
    # Archivo + consola: con solo `filename=` los mensajes no llegaban a stdout,
    # y `journalctl -u simtra-bus-loader` salía vacío. Se configura aquí y no al
    # importar, para que importar el módulo (tests) no cree archivos.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler("data_loader.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logger.info("Sync service started")
    try:
        while True:
            try:
                worked = sync_once()
                time.sleep(1 if worked else 5)
            except Exception as e:
                logger.critical(f"🔥 Unexpected error in main loop: {e}")
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Sync service stopped by user (Ctrl+C)")


if __name__ == "__main__":
    run_forever()
