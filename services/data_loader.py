import requests
import time
import logging
import os
from api import ApiService
from dotenv import load_dotenv


# ---------------- CONFIG ----------------
load_dotenv()
LOCAL_BACKEND = os.getenv("LOCAL_BACKEND")
BACKEND_URL = os.getenv("BACKEND_URL")
BACKEND_USERNAME = os.getenv("BACKEND_USERNAME")
BACKEND_PASSWORD = os.getenv("BACKEND_PASSWORD")
BUS_REGISTER = int(os.getenv("BUS_REGISTER", 0))


# ─────────────────────────────────────────────
# lOGGER
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    filename=f"data_loader.log"
)

logger = logging.getLogger("data_loader")

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
        logger.info(f"Passengers pending: {len(data)}")
        return data
    except requests.RequestException as e:
        logger.error(f"Error fetching passengers: {e}")
        return []


def get_pending_checkpoints():
    try:
        resp = requests.get(f"{LOCAL_BACKEND}/api/checkpoint/pending", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Checkpoints pending: {len(data)}")
        return data
    except requests.RequestException as e:
        logger.error(f"Error fetching checkpoints: {e}")
        return []


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

while True:
    try:
        pending_passengers = get_pending_passengers()
        pending_checkpoints = get_pending_checkpoints()

        # -------- PASSENGERS --------
        for p in pending_passengers:
            formated_data = {
                'latitude': float(p['latitude']),
                'longitude': float(p['longitude']),
                'register': int(BUS_REGISTER),
                'timestamp': p['timestamp'],
            }
            print(formated_data)

            logger.info(f"Sending passenger {p['id']}")

            if simtra.post_passenger(formated_data):
                if not update_passenger_local_register(p['id']):
                    logger.warning(f"Passenger {p['id']} sent but NOT updated locally")
            else:
                logger.warning(f"Failed to send passenger {p['id']}")

        # -------- CHECKPOINTS --------
        for c in pending_checkpoints:
            formated_data = {
                'id': int(c['checkpoint_id']),
                'time_reported': c['timestamp'],
            }
       

            logger.info(f"Sending checkpoint {c['id']}")

            if simtra.update_dispatch(formated_data):
                if not update_checkpoint_local_register(c['id']):
                    logger.warning(f"Checkpoint {c['id']} sent but NOT updated locally")
            else:
                logger.warning(f"Failed to send checkpoint {c['id']}")

        # -------- SLEEP INTELIGENTE --------
        if not pending_passengers and not pending_checkpoints:
            time.sleep(5)
        else:
            time.sleep(1)

    except Exception as e:
        logger.critical(f"🔥 Unexpected error in main loop: {e}")
        time.sleep(5)