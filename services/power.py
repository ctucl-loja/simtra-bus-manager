"""
Apagado ordenado de ESTE dispositivo (la Raspberry Pi).

La pantalla del bus corre en Chromium en modo kiosco, sin teclado ni acceso al
escritorio: el conductor no tiene forma de apagar el equipo salvo cortarle la
corriente, que es justo lo que corrompe la tarjeta SD. Este módulo es esa
salida ordenada.

Alcance deliberadamente estrecho:

  * una sola operación, sin argumentos: apagar;
  * el comando NO se construye con nada que venga del cliente — es una
    constante o el valor de una variable de entorno del propio equipo;
  * no hay reinicio, ni cancelación, ni apagado programado a una hora;
  * si el comando no está disponible se responde `unavailable` y no se
    promete un apagado que no va a ocurrir.

La ejecución está separada de la decisión a propósito: `request_shutdown`
recibe el ejecutor y el planificador, así que los tests ejercitan toda la
lógica sin apagar la máquina donde corren.
"""

import logging
import os
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("simtra")

# Margen entre la respuesta HTTP y el corte real. Sin él, el sistema empieza a
# bajar mientras uvicorn todavía está escribiendo la respuesta y la pantalla se
# queda con un error de red en vez de con el aviso de apagado.
GRACE_SECONDS = 3.0

# El apagado lo ejecuta el sistema, no Python. `sudo -n` (non-interactive)
# falla en vez de quedarse esperando una contraseña que nadie va a escribir:
# la RPi necesita una regla NOPASSWD para el usuario del servicio.
DEFAULT_SHUTDOWN_COMMAND = "sudo -n /sbin/shutdown -h now"

# Estados de la respuesta. `scheduled` es lo único que promete un apagado.
STATUS_SCHEDULED = "scheduled"            # comando aceptado y programado
STATUS_ALREADY_SCHEDULED = "already_scheduled"   # ya había uno en curso
STATUS_UNAVAILABLE = "unavailable"        # no hay comando de apagado utilizable

# Un apagado ya programado no se repite: dos toques seguidos en la pantalla no
# deben lanzar dos `shutdown`.
_lock = threading.Lock()
_scheduled = False


@dataclass(frozen=True)
class ShutdownResult:
    status: str
    detail: str
    # Segundos hasta el corte, solo cuando realmente se programó.
    scheduled_in_seconds: Optional[float] = None


def reset_state():
    """Vuelve al estado inicial. Solo lo usan los tests."""
    global _scheduled
    with _lock:
        _scheduled = False


def is_scheduled() -> bool:
    with _lock:
        return _scheduled


def shutdown_command() -> list[str]:
    """
    Comando de apagado como lista de argumentos.

    Se puede sustituir con SYSTEM_SHUTDOWN_COMMAND (por ejemplo
    `systemctl poweroff` en equipos sin /sbin/shutdown). Una variable vacía o
    con comillas mal cerradas cae al valor por defecto: es preferible el
    comando conocido a no tener ninguno.
    """
    raw = os.getenv("SYSTEM_SHUTDOWN_COMMAND", "").strip()
    if not raw:
        return shlex.split(DEFAULT_SHUTDOWN_COMMAND)
    try:
        parsed = shlex.split(raw)
    except ValueError:
        log.error(
            "SYSTEM_SHUTDOWN_COMMAND mal formada (%r) — se usa el comando por defecto", raw
        )
        return shlex.split(DEFAULT_SHUTDOWN_COMMAND)
    if not parsed:
        return shlex.split(DEFAULT_SHUTDOWN_COMMAND)
    return parsed


def command_is_available(command: list[str]) -> bool:
    """
    ¿Existe el ejecutable? Es una comprobación previa barata que evita
    prometerle a la pantalla un apagado que va a fallar en silencio tres
    segundos después.

    No verifica permisos de sudo: eso solo se sabe ejecutándolo.
    """
    return bool(command) and shutil.which(command[0]) is not None


def run_shutdown(command: list[str]) -> bool:
    """Ejecuta el apagado. Nunca lanza: el hilo que la llama no tiene a quién avisar."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.error("[POWER] No se pudo ejecutar el apagado (%s): %s", " ".join(command), e)
        return False

    if result.returncode != 0:
        log.error(
            "[POWER] El comando de apagado falló (código %s): %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False

    log.info("[POWER] Comando de apagado aceptado por el sistema")
    return True


def _default_scheduler(delay: float, action: Callable[[], None]) -> None:
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()


def request_shutdown(
    runner: Optional[Callable[[list[str]], bool]] = None,
    scheduler: Optional[Callable[[float, Callable[[], None]], None]] = None,
    delay: float = GRACE_SECONDS,
) -> ShutdownResult:
    """
    Programa el apagado del equipo y responde de inmediato.

    El corte ocurre `delay` segundos después para que la respuesta HTTP llegue
    a la pantalla; el conductor ve el aviso de "apagando" y no un error de red.

    Idempotente: mientras haya un apagado programado, las peticiones siguientes
    responden `already_scheduled` sin lanzar un segundo comando.
    """
    global _scheduled

    command = shutdown_command()
    if not command_is_available(command):
        log.error("[POWER] Sin comando de apagado utilizable: %s", " ".join(command) or "(vacío)")
        return ShutdownResult(
            status=STATUS_UNAVAILABLE,
            detail="El equipo no tiene un comando de apagado disponible",
        )

    with _lock:
        if _scheduled:
            return ShutdownResult(
                status=STATUS_ALREADY_SCHEDULED,
                detail="El apagado ya estaba en curso",
            )
        _scheduled = True

    execute = runner or run_shutdown
    schedule = scheduler or _default_scheduler

    def fire():
        # Un fallo aquí (sudo sin regla NOPASSWD, por ejemplo) libera el
        # cerrojo: el equipo sigue encendido, así que el conductor debe poder
        # reintentar en vez de quedarse con un botón muerto hasta el reinicio.
        if not execute(command):
            reset_state()

    log.warning("[POWER] Apagado solicitado desde la pantalla — en %.0f s", delay)
    schedule(delay, fire)

    return ShutdownResult(
        status=STATUS_SCHEDULED,
        detail="El dispositivo se apagará en unos segundos",
        scheduled_in_seconds=delay,
    )
