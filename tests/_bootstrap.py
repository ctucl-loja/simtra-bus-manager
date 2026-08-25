"""
Arranque común de los tests.

El equipo de desarrollo puede no tener instaladas todas las dependencias de
requirements.txt. Las que NO participan de la lógica bajo prueba se sustituyen
por stubs mínimos para poder ejercitar bus_monitor.py sin red y sin gTTS:

    python-dotenv → load_dotenv() no-op
    gTTS          → clase inerte; ningún test genera audio

Si la dependencia real está instalada no se toca nada. Los tests que necesitan
fastapi/sqlalchemy/pydantic (main.py, crud.py, schemas.py) NO están aquí: sin
esas librerías no son ejecutables, y se documenta como limitación.
"""

import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "services"

for path in (str(SERVICES), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _install_stub(name: str, build) -> None:
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = build()


def _dotenv_stub() -> types.ModuleType:
    module = types.ModuleType("dotenv")
    module.load_dotenv = lambda *args, **kwargs: False
    return module


def _gtts_stub() -> types.ModuleType:
    module = types.ModuleType("gtts")

    class gTTS:                     # noqa: N801  (nombre impuesto por la librería real)
        def __init__(self, *args, **kwargs):
            raise RuntimeError("stub de gTTS: los tests no generan audio")

    module.gTTS = gTTS
    return module


_install_stub("dotenv", _dotenv_stub)
_install_stub("gtts", _gtts_stub)

# Los módulos loguean mucho a propósito; en los tests solo estorba.
logging.disable(logging.CRITICAL)
