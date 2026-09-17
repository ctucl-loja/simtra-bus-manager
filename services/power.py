"""
Energía de ESTE dispositivo (la Raspberry Pi): apagado y reinicio ordenados.

La pantalla del bus corre en Chromium en modo kiosco, sin teclado ni acceso al
escritorio: el conductor no tiene forma de apagar ni de reiniciar el equipo
salvo cortarle la corriente, que es justo lo que corrompe la tarjeta SD. Este
módulo es esa salida ordenada.

Alcance deliberadamente estrecho:

  * dos operaciones, ambas sin argumentos: apagar y reiniciar;
  * el comando NO se construye con nada que venga del cliente — es una
    constante o el valor de una variable de entorno del propio equipo. El
    frontend jamás puede proponer un comando;
  * no hay cancelación ni programación a una hora determinada;
  * apagado y reinicio se excluyen entre sí: mientras uno está pendiente el
    otro responde `already_scheduled` diciendo CUÁL está pendiente, para que la
    pantalla no confunda un reinicio con un apagado;
  * si el comando no está disponible se responde `unavailable` y no se promete
    una acción que no va a ocurrir;
  * programar NO es haber terminado: `scheduled` significa "el sistema recibirá
    la orden en unos segundos", nunca "el equipo ya se reinició".

La ejecución está separada de la decisión a propósito: `request_shutdown` y
`request_reboot` reciben el ejecutor y el planificador, así que los tests
ejercitan toda la lógica sin apagar ni reiniciar la máquina donde corren.
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

# Reinicio inmediato. Forma exacta en la Raspberry Pi objetivo (Raspberry Pi OS
# Bookworm, systemd): `/sbin/shutdown -r now`, que es el equivalente correcto de
# "reboot now" — `reboot` no acepta un argumento `now`, así que escribirlo así
# fallaría. Sustituible por SYSTEM_REBOOT_COMMAND (p. ej. `systemctl reboot`).
DEFAULT_REBOOT_COMMAND = "sudo -n /sbin/shutdown -r now"

# Acciones posibles. Son también el valor del campo `action` de la respuesta.
ACTION_SHUTDOWN = "shutdown"
ACTION_REBOOT = "reboot"

# Estados de la respuesta. `scheduled` es lo único que promete un apagado.
STATUS_SCHEDULED = "scheduled"            # comando aceptado y programado
STATUS_ALREADY_SCHEDULED = "already_scheduled"   # ya había uno en curso
STATUS_UNAVAILABLE = "unavailable"        # no hay comando de apagado utilizable

# Una acción de energía ya programada no se repite ni convive con la otra: dos
# toques seguidos en la pantalla no deben lanzar dos `shutdown`, y un reinicio
# no debe colarse encima de un apagado en curso. Por eso el estado no es un
# booleano sino CUÁL acción está pendiente.
_lock = threading.Lock()
_pending: Optional[str] = None


@dataclass(frozen=True)
class ShutdownResult:
    status: str
    detail: str
    # Segundos hasta la ejecución, solo cuando realmente se programó.
    scheduled_in_seconds: Optional[float] = None
    # Acción que se pidió: 'shutdown' | 'reboot'.
    action: str = ACTION_SHUTDOWN
    # Acción realmente pendiente en el equipo. Con `already_scheduled` puede
    # ser DISTINTA de `action`: es lo que permite a la pantalla decir "ya hay un
    # apagado en curso" cuando el conductor tocó «Reiniciar».
    pending_action: Optional[str] = None


def reset_state():
    """Vuelve al estado inicial. Solo lo usan los tests."""
    global _pending
    with _lock:
        _pending = None


def is_scheduled() -> bool:
    """¿Hay alguna acción de energía pendiente? (apagado o reinicio)."""
    with _lock:
        return _pending is not None


def pending_action() -> Optional[str]:
    """'shutdown' | 'reboot' | None."""
    with _lock:
        return _pending


def _command_from_env(variable: str, default: str) -> list[str]:
    """
    Comando del equipo como lista de argumentos.

    El valor sale SIEMPRE del propio equipo: una constante o una variable de
    entorno. Nunca del cliente — ni el frontend ni el cuerpo de la petición
    intervienen aquí, y por eso ninguna de estas funciones recibe parámetros.

    Una variable vacía o con comillas mal cerradas cae al valor por defecto: es
    preferible el comando conocido a no tener ninguno.
    """
    raw = os.getenv(variable, "").strip()
    if not raw:
        return shlex.split(default)
    try:
        parsed = shlex.split(raw)
    except ValueError:
        log.error("%s mal formada (%r) — se usa el comando por defecto", variable, raw)
        return shlex.split(default)
    if not parsed:
        return shlex.split(default)
    return parsed


def shutdown_command() -> list[str]:
    """
    Comando de apagado. Sustituible con SYSTEM_SHUTDOWN_COMMAND (por ejemplo
    `systemctl poweroff` en equipos sin /sbin/shutdown).
    """
    return _command_from_env("SYSTEM_SHUTDOWN_COMMAND", DEFAULT_SHUTDOWN_COMMAND)


def reboot_command() -> list[str]:
    """
    Comando de reinicio. Sustituible con SYSTEM_REBOOT_COMMAND (por ejemplo
    `systemctl reboot`). El frontend NUNCA lo proporciona.
    """
    return _command_from_env("SYSTEM_REBOOT_COMMAND", DEFAULT_REBOOT_COMMAND)


def command_for(action: str) -> list[str]:
    return reboot_command() if action == ACTION_REBOOT else shutdown_command()


def command_is_available(command: list[str]) -> bool:
    """
    ¿Existe el ejecutable? Es una comprobación previa barata que evita
    prometerle a la pantalla un apagado que va a fallar en silencio tres
    segundos después.

    No verifica permisos de sudo: eso solo se sabe ejecutándolo.
    """
    return bool(command) and shutil.which(command[0]) is not None


def run_shutdown(command: list[str]) -> bool:
    """
    Ejecuta el comando de energía (apagado o reinicio). Nunca lanza: el hilo que
    la llama no tiene a quién avisar.

    Conserva el nombre histórico porque es el ejecutor por defecto que los tests
    ya sustituyen; sirve igual para las dos acciones, que solo se diferencian en
    la lista de argumentos.
    """
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.error("[POWER] No se pudo ejecutar el comando (%s): %s", " ".join(command), e)
        return False

    if result.returncode != 0:
        log.error(
            "[POWER] El comando de energía falló (código %s): %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False

    log.info("[POWER] Comando de energía aceptado por el sistema")
    return True


def _default_scheduler(delay: float, action: Callable[[], None]) -> None:
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()


# Textos por acción. Fijos: la pantalla los muestra tal cual y ninguno promete
# que la acción ya haya terminado.
_ACTION_TEXTS = {
    ACTION_SHUTDOWN: {
        "scheduled": "El dispositivo se apagará en unos segundos",
        "pending": "El apagado ya estaba en curso",
        "unavailable": "El equipo no tiene un comando de apagado disponible",
        "log": "Apagado",
    },
    ACTION_REBOOT: {
        "scheduled": "El dispositivo se reiniciará en unos segundos",
        "pending": "El reinicio ya estaba en curso",
        "unavailable": "El equipo no tiene un comando de reinicio disponible",
        "log": "Reinicio",
    },
}


def _request_power_action(
    action: str,
    runner: Optional[Callable[[list[str]], bool]] = None,
    scheduler: Optional[Callable[[float, Callable[[], None]], None]] = None,
    delay: float = GRACE_SECONDS,
) -> ShutdownResult:
    """
    Programa una acción de energía y responde de inmediato.

    La ejecución ocurre `delay` segundos DESPUÉS para que la respuesta HTTP
    llegue a la pantalla: el conductor ve el aviso y no un error de red.

    Exclusión mutua real: mientras haya CUALQUIER acción pendiente —la misma u
    otra— se responde `already_scheduled` con `pending_action` indicando cuál
    es, y no se lanza un segundo comando. Nunca se programan a la vez un
    apagado y un reinicio.

    Si la programación o la ejecución fallan, el estado se libera: el equipo
    sigue encendido y el conductor debe poder reintentar, no quedarse con un
    botón muerto.
    """
    global _pending

    texts = _ACTION_TEXTS[action]
    command = command_for(action)

    if not command_is_available(command):
        log.error("[POWER] Sin comando utilizable para %s: %s", action, " ".join(command) or "(vacío)")
        return ShutdownResult(
            status=STATUS_UNAVAILABLE,
            detail=texts["unavailable"],
            action=action,
        )

    with _lock:
        if _pending is not None:
            busy = _pending
            return ShutdownResult(
                status=STATUS_ALREADY_SCHEDULED,
                detail=_ACTION_TEXTS[busy]["pending"],
                action=action,
                pending_action=busy,
            )
        _pending = action

    execute = runner or run_shutdown
    schedule = scheduler or _default_scheduler

    def fire():
        # Un fallo aquí (sudo sin regla NOPASSWD, por ejemplo) libera el
        # estado: el equipo sigue encendido, así que el conductor debe poder
        # reintentar en vez de quedarse con un botón muerto hasta el reinicio.
        if not execute(command):
            reset_state()

    log.warning("[POWER] %s solicitado desde la pantalla — en %.0f s", texts["log"], delay)

    try:
        schedule(delay, fire)
    except Exception:
        # Si ni siquiera se pudo programar, no hay nada pendiente: liberar el
        # estado es obligatorio o el botón quedaría bloqueado para siempre.
        reset_state()
        log.exception("[POWER] No se pudo programar %s", action)
        return ShutdownResult(
            status=STATUS_UNAVAILABLE,
            detail=texts["unavailable"],
            action=action,
        )

    return ShutdownResult(
        status=STATUS_SCHEDULED,
        detail=texts["scheduled"],
        scheduled_in_seconds=delay,
        action=action,
        pending_action=action,
    )


def request_shutdown(
    runner: Optional[Callable[[list[str]], bool]] = None,
    scheduler: Optional[Callable[[float, Callable[[], None]], None]] = None,
    delay: float = GRACE_SECONDS,
) -> ShutdownResult:
    """Programa el APAGADO del equipo. Ver _request_power_action."""
    return _request_power_action(ACTION_SHUTDOWN, runner, scheduler, delay)


def request_reboot(
    runner: Optional[Callable[[list[str]], bool]] = None,
    scheduler: Optional[Callable[[float, Callable[[], None]], None]] = None,
    delay: float = GRACE_SECONDS,
) -> ShutdownResult:
    """
    Programa el REINICIO del equipo. Ver _request_power_action.

    `scheduled` significa que la orden se dará en unos segundos, no que el
    equipo ya haya reiniciado: eso no hay forma de confirmarlo desde el propio
    proceso que se va a morir con él.
    """
    return _request_power_action(ACTION_REBOOT, runner, scheduler, delay)
