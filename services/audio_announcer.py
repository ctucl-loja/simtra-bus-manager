"""
Anuncios de voz para puntos de control, generados con gTTS y cacheados en disco.

IDENTIDAD DEL CACHE — `point.id`, no `checkpoint.id`:

    checkpoint.id   → identidad del checkpoint dentro de un despacho
                      (persistencia, sincronización con el backend)
    checkpoint.point.id → identidad física del punto geográfico
                      (cache y reutilización de audio)

Un mismo punto físico aparece muchas veces al día (en varios steps y líneas) y
además cambia de checkpoint.id cada día, cuando el backend emite un despacho
nuevo. Cachear por checkpoint.id obligaba a regenerar todos los audios cada día;
cachear por point.id genera cada punto una sola vez y lo reutiliza siempre.

Por eso el mensaje NO incluye el siguiente punto: el punto que sigue depende de
la línea y del recorrido, así que un texto con "próximo punto de control X" no
sería reutilizable entre líneas y rompería la identidad del cache.

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
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from gtts import gTTS

log = logging.getLogger("simtra")

# Ruta ABSOLUTA derivada del propio módulo: no depende del working directory,
# así que es la misma se lance el proceso desde systemd, una terminal, un IDE,
# Windows o la Raspberry Pi.
AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_LANG = "es"
AUDIO_PLAYER = "mpg123"          # en la RPi: sudo apt install mpg123
AUDIO_PLAYER_ARGS = ["-q"]


def build_message(name: str) -> str:
    """
    Texto del anuncio. Depende ÚNICAMENTE del nombre del punto: es lo que hace
    que el mp3 sea reutilizable en cualquier línea, step o despacho.
    """
    return f"Punto de control {name}."


def _valid_request(point_id, name) -> Optional[tuple]:
    """
    Normaliza (point_id, name) o devuelve None.

    Se valida al ENCOLAR y no en el worker: así el error aparece en el log junto
    a la llegada que lo originó, y no como un fallo suelto de un hilo de audio.
    """
    if isinstance(point_id, bool) or not isinstance(point_id, int):
        log.error(f"[AUDIO] point_id inválido ({point_id!r}) — no se encola")
        return None
    if not isinstance(name, str) or not name.strip():
        log.error(f"[AUDIO] Nombre de punto inválido para point={point_id} ({name!r}) — no se encola")
        return None
    return point_id, name.strip()


def _paths_for(point_id: int) -> tuple[Path, Path]:
    """Rutas absolutas del mp3 y su metadata para un punto físico."""
    mp3_path = AUDIO_DIR / f"point_{point_id}.mp3"
    meta_path = AUDIO_DIR / f"point_{point_id}.json"
    return mp3_path, meta_path


# ─────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────

# Un lock por punto: evita que un prepare() y un announce() casi simultáneos
# generen dos veces el mismo audio (y hagan dos peticiones a gTTS).
_locks_mutex = threading.Lock()
_point_locks: dict[int, threading.Lock] = {}


def _lock_for(point_id: int) -> threading.Lock:
    with _locks_mutex:
        lock = _point_locks.get(point_id)
        if lock is None:
            lock = threading.Lock()
            _point_locks[point_id] = lock
        return lock


def _cache_hit(mp3_path: Path, meta_path: Path, text: str) -> bool:
    """
    True si el audio en disco sirve tal cual: mp3 presente y no vacío, y
    metadata con el mismo texto. Si el mp3 fue borrado pero quedó el json (o
    al revés), no es un hit y se regenera.
    """
    if not (mp3_path.is_file() and meta_path.is_file()):
        return False
    try:
        if mp3_path.stat().st_size == 0:
            return False
        cached = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False   # cache corrupta: se regenera
    return cached.get("text") == text


def _generate(point_id: int, name: str, text: str, mp3_path: Path, meta_path: Path) -> Optional[Path]:
    """
    Genera el mp3 con gTTS. Escribe primero en un temporal y luego renombra,
    para que un fallo a media escritura no deje un mp3 truncado que el cache
    considere válido. La metadata se escribe DESPUÉS del mp3 por la misma razón.
    """
    tmp_path = mp3_path.with_suffix(".mp3.tmp")
    try:
        gTTS(text=text, lang=AUDIO_LANG).save(str(tmp_path))
        os.replace(tmp_path, mp3_path)
        meta_path.write_text(
            json.dumps({"point_id": point_id, "name": name, "text": text}, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(f"[AUDIO] Generado: {mp3_path}")
        return mp3_path
    except Exception as e:
        log.error(f"[AUDIO] No se pudo generar el audio de point={point_id} ({mp3_path}): {e}")
        return None
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _ensure_cached(point_id: int, name: str) -> Optional[Path]:
    """
    Devuelve la ruta del mp3 del punto, reutilizando el cache siempre que sea
    posible. Solo se llama a gTTS cuando el archivo no existe o su texto cambió.
    """
    text = build_message(name)
    mp3_path, meta_path = _paths_for(point_id)

    # Camino rápido, sin lock: el caso habitual es que el audio ya exista.
    if _cache_hit(mp3_path, meta_path, text):
        log.debug(f"[AUDIO] Cache encontrado: {mp3_path.name}")
        return mp3_path

    with _lock_for(point_id):
        # Otro worker pudo generarlo mientras esperábamos el lock.
        if _cache_hit(mp3_path, meta_path, text):
            log.debug(f"[AUDIO] Cache encontrado: {mp3_path.name}")
            return mp3_path

        log.info(f"[AUDIO] Cache ausente para point={point_id} — generando")
        return _generate(point_id, name, text, mp3_path, meta_path)


# ─────────────────────────────────────────────
# REPRODUCCIÓN
# ─────────────────────────────────────────────

def _play_file(path: Path) -> bool:
    """
    Reproduce el mp3 distinguiendo con claridad las causas de fallo: reproductor
    ausente del PATH, archivo inexistente o error real del reproductor. Los
    errores siempre registran la ruta absoluta, para diagnosticar deployments.
    """
    player = shutil.which(AUDIO_PLAYER)
    if player is None:
        log.error(
            f"[AUDIO] Reproductor '{AUDIO_PLAYER}' no encontrado en PATH "
            f"— no se reproduce {path}"
        )
        return False

    if not path.is_file():
        log.error(f"[AUDIO] Archivo MP3 no encontrado: {path}")
        return False

    log.info(f"[AUDIO] Reproduciendo: {path}")
    try:
        subprocess.run(
            [player, *AUDIO_PLAYER_ARGS, str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except FileNotFoundError as e:
        # PATH cambió entre el which() y el run(), o el binario desapareció.
        log.error(f"[AUDIO] No se pudo ejecutar el reproductor '{player}': {e}")
    except subprocess.CalledProcessError as e:
        log.error(f"[AUDIO] '{AUDIO_PLAYER}' terminó con código {e.returncode} reproduciendo {path}")
    except Exception as e:
        log.error(f"[AUDIO] Error inesperado reproduciendo {path}: {e}")
    return False


# ─────────────────────────────────────────────
# COLAS + HILOS DEDICADOS
# ─────────────────────────────────────────────

_generate_queue: "queue.Queue[tuple[int, str]]" = queue.Queue()
_play_queue: "queue.Queue[tuple[int, str, Optional[Callable[[int], None]]]]" = queue.Queue()


def _generator_worker():
    while True:
        point_id, name = _generate_queue.get()
        try:
            _ensure_cached(point_id, name)
        except Exception as e:
            log.error(f"[AUDIO] Error generando audio para point={point_id}: {e}")
        finally:
            _generate_queue.task_done()


def _player_worker():
    while True:
        point_id, name, on_done = _play_queue.get()
        try:
            # Si el audio no estaba cacheado (o fue borrado), se genera aquí
            # antes de reproducir: nunca se lanza el reproductor a ciegas.
            path = _ensure_cached(point_id, name)
            if path is None:
                log.error(f"[AUDIO] Sin audio disponible para point={point_id} — no se reproduce")
            else:
                _play_file(path)
        except Exception as e:
            log.error(f"[AUDIO] Error reproduciendo point={point_id}: {e}")
        finally:
            if on_done:
                try:
                    on_done(point_id)
                except Exception as e:
                    log.error(f"[AUDIO] Error en callback on_done: {e}")
            _play_queue.task_done()


threading.Thread(target=_generator_worker, daemon=True, name="audio-generator").start()
threading.Thread(target=_player_worker, daemon=True, name="audio-player").start()


# ─────────────────────────────────────────────
# API PÚBLICA
#
# `point_id` es checkpoint["point"]["id"], NUNCA checkpoint["id"].
# ─────────────────────────────────────────────

def prepare(point_id: int, name: str):
    """Encola la generación (con cache) del audio de un punto. No bloquea."""
    request = _valid_request(point_id, name)
    if request is None:
        return
    _generate_queue.put(request)


def announce(
    point_id: int,
    name: str,
    on_done: Optional[Callable[[int], None]] = None,
):
    """
    Encola la reproducción inmediata del audio de un punto. No bloquea.
    Si el audio no estaba prefetch-eado, se genera dentro del hilo reproductor
    (nunca en el hilo de GPS). on_done(point_id) se invoca al terminar, desde
    el hilo reproductor.

    Una llegada sin punto identificable no se anuncia, pero eso NO invalida la
    marcación: el audio es la prioridad más baja de la cadena.
    """
    request = _valid_request(point_id, name)
    if request is None:
        return
    _play_queue.put((*request, on_done))
