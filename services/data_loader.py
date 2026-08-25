import requests
import time
import logging
import math
import os
from api import ApiService
from dotenv import load_dotenv


# ---------------- CONFIG ----------------
load_dotenv()
LOCAL_BACKEND = os.getenv("FAST_API_LOCAL_BACKEND")
BACKEND_URL = os.getenv("FAST_API_BACKEND_URL")
BACKEND_USERNAME = os.getenv("FAST_API_BACKEND_USERNAME")
BACKEND_PASSWORD = os.getenv("FAST_API_BACKEND_PASSWORD")
BUS_REGISTER = int(os.getenv("FAST_API_BUS_REGISTER", 0))


# ─────────────────────────────────────────────
# lOGGER
# ─────────────────────────────────────────────
# Archivo + consola: con solo `filename=` los mensajes no llegaban a stdout, y
# `journalctl -u simtra-bus-loader` (lo que documenta el README) salía vacío.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("data_loader.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("data_loader")


def as_float(value):
    """float utilizable o None. Un registro sin coordenadas no se puede subir."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def passenger_payload(p):
    """
    Cuerpo para el backend remoto, o None si al registro local le falta algo.

    Se descarta el registro defectuoso y se sigue con los demás: un solo dato
    corrupto no debe bloquear la cola de subida del día entero.
    """
    if not isinstance(p, dict):
        return None

    latitude = as_float(p.get("latitude"))
    longitude = as_float(p.get("longitude"))
    timestamp = p.get("timestamp")

    if latitude is None or longitude is None or not timestamp:
        return None

    return {
        "latitude": latitude,
        "longitude": longitude,
        "register": int(BUS_REGISTER),
        "timestamp": timestamp,
        "direction": p.get("direction"),
        "door": p.get("door"),
    }

# ─────────────────────────────────────────────
# API - CLIENT
# ─────────────────────────────────────────────

simtra = ApiService(BACKEND_URL, BACKEND_USERNAME, BACKEND_PASSWORD)


# ─────────────────────────────────────────────
# FUNCTIONS
# ─────────────────────────────────────────────
def get_pending_passengers():
    try:
        resp = requests.get(f"{LOCAL_BACKEND}/api/passenger/pending", timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching passengers: {e}")
        return []
    except ValueError:
        logger.error("Respuesta de /api/passenger/pending no es JSON valido")
        return []

    if not isinstance(data, list):
        logger.error(f"/api/passenger/pending devolvio {type(data).__name__}, se esperaba lista")
        return []

    logger.info(f"Passengers pending: {len(data)}")
    return data


def get_pending_checkpoints():
    try:
        resp = requests.get(f"{LOCAL_BACKEND}/api/checkpoint/pending", timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching checkpoints: {e}")
        return []
    except ValueError:
        logger.error("Respuesta de /api/checkpoint/pending no es JSON valido")
        return []

    if not isinstance(data, list):
        logger.error(f"/api/checkpoint/pending devolvio {type(data).__name__}, se esperaba lista")
        return []

    logger.info(f"Checkpoints pending: {len(data)}")
    return data


def update_passenger_local_register(id):
    try:
        resp = requests.patch(f"{LOCAL_BACKEND}/api/passenger/{id}", timeout=5)
        resp.raise_for_status()
        logger.info(f"Passenger {id} marked as uploaded")
        return True
    except requests.RequestException as e:
        logger.error(f"Error updating passenger {id}: {e}")
        return False


def update_checkpoint_local_register(id):
    try:
        resp = requests.patch(f"{LOCAL_BACKEND}/api/checkpoint/{id}", timeout=5)
        resp.raise_for_status()
        logger.info(f"Checkpoint {id} marked as uploaded")
        return True
    except requests.RequestException as e:
        logger.error(f"Error updating checkpoint {id}: {e}")
        return False


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
logger.info("Sync service started")

try:
    while True:
        try:
            pending_passengers = get_pending_passengers()
            pending_checkpoints = get_pending_checkpoints()

            # -------- PASSENGERS --------
            for p in pending_passengers:
                passenger_id = p.get('id') if isinstance(p, dict) else None
                formated_data = passenger_payload(p)

                if passenger_id is None or formated_data is None:
                    logger.error(f"Passenger local record incompleto, se omite: {p!r}")
                    continue

                logger.info(f"Sending passenger {passenger_id}")

                if simtra.post_passenger(formated_data):
                    if not update_passenger_local_register(passenger_id):
                        logger.warning(f"Passenger {passenger_id} sent but NOT updated locally")
                else:
                    logger.warning(f"Failed to send passenger {passenger_id}")

            # -------- CHECKPOINTS --------
            for c in pending_checkpoints:
                if not isinstance(c, dict):
                    logger.error(f"Checkpoint local record con forma inesperada, se omite: {c!r}")
                    continue

                row_id = c.get('id')
                remote_id = c.get('checkpoint_id')
                timestamp = c.get('timestamp')

                if row_id is None or remote_id is None or not timestamp:
                    logger.error(f"Checkpoint local record incompleto, se omite: {c!r}")
                    continue

                try:
                    formated_data = {'id': int(remote_id), 'time_reported': timestamp}
                except (TypeError, ValueError):
                    logger.error(f"Checkpoint {row_id} con checkpoint_id no numerico ({remote_id!r}), se omite")
                    continue

                logger.info(f"Sending checkpoint {row_id}")

                if simtra.update_dispatch(formated_data):
                    if not update_checkpoint_local_register(row_id):
                        logger.warning(f"Checkpoint {row_id} sent but NOT updated locally")
                else:
                    logger.warning(f"Failed to send checkpoint {row_id}")

            # -------- SLEEP INTELIGENTE --------
            if not pending_passengers and not pending_checkpoints:
                time.sleep(5)
            else:
                time.sleep(1)

        except Exception as e:
            logger.critical(f"🔥 Unexpected error in main loop: {e}")
            time.sleep(5)

except KeyboardInterrupt:
    logger.info("Sync service stopped by user (Ctrl+C)")