"""
Anuncios de voz para checkpoints, generados con gTTS y cacheados en disco.

Este módulo es intencionalmente independiente de la lógica de despacho/steps:
solo sabe generar texto -> mp3 (con cache) y reproducirlo. La decisión de QUÉ
anunciar y CUÁNDO prefetch-ear el siguiente vive en bus_monitor.py.

Todo el trabajo pesado (red hacia gTTS, reproducción con subprocess) ocurre en
dos hilos daemon dedicados, cada uno consumiendo su propia queue.Queue. Las
funciones públicas (`announce`, `prepare`) solo encolan y retornan de inmediato,
por lo que nunca bloquean al hilo principal de bus_monitor.py.
"""

import json
import logging
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from gtts import gTTS

log = logging.getLogger("simtra")

AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

AUDIO_LANG = "es"
AUDIO_PLAYER_CMD = ["mpg123", "-q"]  # requiere `mpg123` instalado en el sistema


def build_message(current_name: str, next_name: Optional[str]) -> str:
    """Construye el texto del anuncio. Sin next_name, adapta el mensaje sin romper."""
    if next_name:
        return f"Punto de control {current_name}, próximo punto de control {next_name}."
    return f"Punto de control {current_name}."


def _paths_for(checkpoint_id: int) -> tuple[Path, Path]:
    mp3_path = AUDIO_DIR / f"checkpoint_{checkpoint_id}.mp3"
    meta_path = AUDIO_DIR / f"checkpoint_{checkpoint_id}.json"
    return mp3_path, meta_path


def _ensure_cached(checkpoint_id: int, current_name: str, next_name: Optional[str]) -> Optional[Path]:
    """
    Devuelve la ruta del mp3 ya generado, reutilizando el cache si el texto no
    cambió. Genera con gTTS solo si hace falta (cache ausente o desactualizado).
    """
    text = build_message(current_name, next_name)
    mp3_path, meta_path = _paths_for(checkpoint_id)

    if mp3_path.exists() and meta_path.exists():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8"))
            if cached.get("text") == text:
                return mp3_path
        except (OSError, json.JSONDecodeError):
            pass  # cache corrupta: se regenera abajo

    try:
        gTTS(text=text, lang=AUDIO_LANG).save(str(mp3_path))
        meta_path.write_text(json.dumps({"text": text}), encoding="utf-8")
        log.debug(f"[AUDIO] Generado {mp3_path.name}: \"{text}\"")
        return mp3_path
    except Exception as e:
        log.error(f"[AUDIO] No se pudo generar audio para checkpoint {checkpoint_id}: {e}")
        return None


def _play_file(path: Path):
    try:
        subprocess.run(
            AUDIO_PLAYER_CMD + [str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.error(f"[AUDIO] No se pudo reproducir {path.name}: {e}")


# ─────────────────────────────────────────────
# COLAS + HILOS DEDICADOS
# ─────────────────────────────────────────────

_generate_queue: "queue.Queue[tuple[int, str, Optional[str]]]" = queue.Queue()
_play_queue: "queue.Queue[tuple[int, str, Optional[str], Optional[Callable[[int], None]]]]" = queue.Queue()


def _generator_worker():
    while True:
        checkpoint_id, current_name, next_name = _generate_queue.get()
        try:
            _ensure_cached(checkpoint_id, current_name, next_name)
        except Exception as e:
            log.error(f"[AUDIO] Error generando audio para checkpoint {checkpoint_id}: {e}")
        finally:
            _generate_queue.task_done()


def _player_worker():
    while True:
        checkpoint_id, current_name, next_name, on_done = _play_queue.get()
        try:
            path = _ensure_cached(checkpoint_id, current_name, next_name)
            if path:
                _play_file(path)
        except Exception as e:
            log.error(f"[AUDIO] Error reproduciendo checkpoint {checkpoint_id}: {e}")
        finally:
            if on_done:
                try:
                    on_done(checkpoint_id)
                except Exception as e:
                    log.error(f"[AUDIO] Error en callback on_done: {e}")
            _play_queue.task_done()


threading.Thread(target=_generator_worker, daemon=True, name="audio-generator").start()
threading.Thread(target=_player_worker, daemon=True, name="audio-player").start()


# ─────────────────────────────────────────────
# API PÚBLICA
# ─────────────────────────────────────────────

def prepare(checkpoint_id: int, current_name: str, next_name: Optional[str]):
    """Encola la generación (con cache) del audio de un checkpoint. No bloquea."""
    _generate_queue.put((checkpoint_id, current_name, next_name))


def announce(
    checkpoint_id: int,
    current_name: str,
    next_name: Optional[str],
    on_done: Optional[Callable[[int], None]] = None,
):
    """
    Encola la reproducción inmediata del audio de un checkpoint. No bloquea.
    Si el audio no estaba prefetch-eado, se genera dentro del hilo reproductor
    (nunca en el hilo de GPS). on_done(checkpoint_id) se invoca al terminar,
    desde el hilo reproductor.
    """
    _play_queue.put((checkpoint_id, current_name, next_name, on_done))
