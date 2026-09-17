"""
Conexión / reconexión Wi-Fi de ESTE dispositivo (la Raspberry Pi).

Es la ÚNICA operación de escritura sobre la red del equipo. `network_info.py`
sigue siendo de solo lectura; este módulo existe aparte precisamente para que
esa frontera quede explícita.

Terminología — importante
─────────────────────────
En esta vista "usuario" NO es un usuario de sistema ni el usuario del backend
remoto SIMTRA: el formulario de la pantalla pide el NOMBRE DE LA RED (SSID) y
su clave. No hay soporte 802.1X / WPA-Enterprise (usuario + contraseña contra
un RADIUS): el equipo se conecta a redes WPA/WPA2-PSK o abiertas, que es lo que
hay en un patio de buses. Un SSID no es una identidad de usuario.

Manejo del secreto
──────────────────
  * La clave NUNCA aparece en la línea de comandos: se entrega a `nmcli --ask`
    por STDIN, así que no es visible en `ps`, ni en `/proc/<pid>/cmdline`, ni en
    los logs de auditoría del sistema.
  * La clave NUNCA se registra en el log ni se devuelve al cliente, ni siquiera
    dentro de un mensaje de error: los errores se traducen a mensajes fijos y la
    salida cruda de nmcli se depura antes de loguearse.
  * La clave NO se guarda en este proceso ni en la base de datos. Quien la
    conserva es NetworkManager, en su perfil de conexión
    (/etc/NetworkManager/system-connections/, modo 0600, root), que es su
    mecanismo normal.

Ejecución
─────────
  * Siempre lista de argumentos y `shell=False`: no se arma ninguna línea de
    comando por concatenación.
  * Todo comando lleva timeout.
  * El éxito NO se declara por que nmcli devuelva 0: se verifica que la
    conexión pedida quede realmente ACTIVA sobre un dispositivo Wi-Fi.

Requisitos reales de despliegue: ver README (sección «Wi-Fi»). Resumen: hace
falta NetworkManager con `nmcli` en el PATH, un adaptador Wi-Fi gestionado por
NM, y una regla de polkit que permita al usuario del servicio modificar
conexiones sin contraseña. Si algo de eso falta, el módulo responde un estado
controlado; nunca revienta ni promete una conexión que no ocurrió.
"""

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

# Doble forma de importar a propósito: main.py carga estos módulos como
# `services.wifi` (paquete de espacio de nombres) y la suite de tests los carga
# como `wifi` con services/ en sys.path, igual que bus_monitor.py hace con
# `from api import ApiService`. Ambas rutas deben funcionar.
try:
    from . import network_info
except ImportError:                     # pragma: no cover - depende del sys.path
    import network_info

log = logging.getLogger("simtra")

# ─────────────────────────────────────────────
# ESTADOS DE LA OPERACIÓN
# ─────────────────────────────────────────────

STATUS_CONNECTED        = "connected"          # verificado: la red pedida está activa
STATUS_INVALID_PASSWORD = "invalid_password"   # el punto de acceso rechazó el secreto
STATUS_NOT_FOUND        = "not_found"          # el SSID no está visible
STATUS_TIMEOUT          = "timeout"            # la asociación no terminó a tiempo
STATUS_UNAVAILABLE      = "unavailable"        # no hay nmcli / NetworkManager
STATUS_NO_ADAPTER       = "no_adapter"         # no hay adaptador Wi-Fi gestionado
STATUS_NOT_AUTHORIZED   = "not_authorized"     # faltan permisos (polkit / root)
STATUS_BUSY             = "busy"               # ya hay un intento en curso
STATUS_FAILED           = "failed"             # cualquier otro fallo de nmcli

# Mensajes al conductor. Fijos: jamás se construyen con la salida de nmcli, que
# es donde podría colarse algo que no debe salir del equipo.
DETAILS = {
    STATUS_CONNECTED:        "Conectado a la red",
    STATUS_INVALID_PASSWORD: "La clave de la red no es correcta",
    STATUS_NOT_FOUND:        "No se encontró esa red Wi-Fi",
    STATUS_TIMEOUT:          "La conexión tardó demasiado y no se pudo confirmar",
    STATUS_UNAVAILABLE:      "Este equipo no permite cambiar la red Wi-Fi desde la pantalla",
    STATUS_NO_ADAPTER:       "El equipo no tiene un adaptador Wi-Fi disponible",
    STATUS_NOT_AUTHORIZED:   "El equipo no tiene permisos para cambiar la red Wi-Fi",
    STATUS_BUSY:             "Ya hay un intento de conexión en curso",
    STATUS_FAILED:           "No se pudo conectar a la red",
}

# Segundos que se le dan a nmcli para asociar y obtener IP. Por encima del
# timeout del proceso se corta igual: ver CONNECT_TIMEOUT_MARGIN.
DEFAULT_CONNECT_TIMEOUT = 25

# Margen entre el `--wait` de nmcli y el timeout del subprocess: si nmcli se
# cuelga por debajo de su propio wait, igual soltamos el hilo.
CONNECT_TIMEOUT_MARGIN = 10

# Comandos cortos (listar dispositivos, verificar conexión activa).
QUERY_TIMEOUT = 5

# ─────────────────────────────────────────────
# VALIDACIÓN DE PARÁMETROS
#
# Se valida ANTES de tocar el sistema y los mensajes de error no repiten el
# valor recibido: un mensaje que devuelve lo que el cliente mandó es la vía más
# fácil de filtrar un secreto por accidente.
# ─────────────────────────────────────────────

SSID_MAX_BYTES = 32          # 802.11: el SSID son 32 octetos como máximo
PSK_MIN_LENGTH = 8           # WPA/WPA2-PSK
PSK_MAX_LENGTH = 63          # 64 sería el PSK hexadecimal, que aquí no se acepta

# Caracteres de control: ni un SSID ni un PSK los llevan, y en un argumento o en
# stdin solo sirven para confundir al parser de nmcli.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class InvalidParameter(ValueError):
    """Parámetro inaceptable. Su mensaje es seguro de mostrar: no cita valores."""


def validate_ssid(ssid) -> str:
    if not isinstance(ssid, str):
        raise InvalidParameter("El nombre de red (SSID) es obligatorio")

    value = ssid.strip()
    if not value:
        raise InvalidParameter("El nombre de red (SSID) es obligatorio")
    if _CONTROL_CHARS.search(value):
        raise InvalidParameter("El nombre de red contiene caracteres no permitidos")
    if len(value.encode("utf-8")) > SSID_MAX_BYTES:
        raise InvalidParameter(f"El nombre de red no puede superar {SSID_MAX_BYTES} caracteres")
    # Un SSID que empieza por '-' sería interpretado por nmcli como una opción.
    # Son SSID legales en 802.11 pero inalcanzables por esta vía: se rechaza de
    # forma explícita en vez de construir un comando ambiguo.
    if value.startswith("-"):
        raise InvalidParameter("El nombre de red no puede empezar con '-'")
    return value


def validate_password(password) -> str:
    """
    '' significa red abierta (o reutilizar el perfil ya guardado). Cualquier
    otro valor debe ser un PSK WPA/WPA2 válido.

    El mensaje de error NUNCA incluye la clave ni parte de ella.
    """
    if password is None:
        return ""
    if not isinstance(password, str):
        raise InvalidParameter("La clave de red no es válida")
    if password == "":
        return ""
    if _CONTROL_CHARS.search(password):
        raise InvalidParameter("La clave de red contiene caracteres no permitidos")
    if not (PSK_MIN_LENGTH <= len(password) <= PSK_MAX_LENGTH):
        raise InvalidParameter(
            f"La clave de red debe tener entre {PSK_MIN_LENGTH} y {PSK_MAX_LENGTH} caracteres"
        )
    return password


# ─────────────────────────────────────────────
# EJECUCIÓN DE nmcli
# ─────────────────────────────────────────────

NMCLI = "nmcli"


@dataclass(frozen=True)
class CommandOutput:
    """Resultado crudo de un nmcli. `returncode` None = no se pudo ejecutar."""
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    missing: bool = False


def nmcli_available() -> bool:
    return shutil.which(NMCLI) is not None


def run_nmcli(args: list[str], timeout: int = QUERY_TIMEOUT,
              stdin_text: Optional[str] = None) -> CommandOutput:
    """
    Ejecuta `nmcli <args>`. Nunca lanza.

    `stdin_text` es el canal por el que viaja la clave: no se escribe en la
    línea de comandos, así que no aparece en `ps` ni en /proc.
    """
    try:
        result = subprocess.run(
            [NMCLI, *args],
            shell=False,
            input=stdin_text,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandOutput(returncode=None, stdout="", stderr="", missing=True)
    except subprocess.TimeoutExpired:
        return CommandOutput(returncode=None, stdout="", stderr="", timed_out=True)
    except (OSError, ValueError) as e:
        # El texto de la excepción puede arrastrar los argumentos; se depura.
        log.error("[WIFI] No se pudo ejecutar nmcli: %s", _scrub(str(e), stdin_text))
        return CommandOutput(returncode=None, stdout="", stderr="")

    return CommandOutput(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def _scrub(text: str, secret: Optional[str]) -> str:
    """
    Quita el secreto de un texto antes de loguearlo.

    Con el diseño actual la clave nunca debería llegar a stdout/stderr (viaja
    por stdin), pero esto es una red de seguridad barata: una versión futura de
    nmcli que la repita en un mensaje no debe terminar en bus_monitor.log.
    """
    if not text:
        return ""
    cleaned = text.strip()
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned[:200]


# ─────────────────────────────────────────────
# CLASIFICACIÓN DE ERRORES
# ─────────────────────────────────────────────

# Se compara sobre stderr en minúsculas. nmcli está traducido según el locale
# del equipo, así que se incluyen las variantes en español que emite con
# LANG=es_*; el fallback siempre es STATUS_FAILED, nunca un éxito.
_ERROR_PATTERNS = [
    (STATUS_INVALID_PASSWORD, (
        "secrets were required",
        "no secrets provided",
        "secrets not provided",
        "802-11-wireless-security.psk",
        "invalid password",
        "incorrect password",
        "se requerían secretos",
        "no se proporcionaron secretos",
    )),
    (STATUS_NOT_FOUND, (
        "no network with ssid",
        "ssid not found",
        "network not found",
        "no se encontró",
        "no existe la red",
    )),
    (STATUS_NOT_AUTHORIZED, (
        "not authorized",
        "insufficient privileges",
        "permission denied",
        "access denied",
        "no autorizado",
        "permiso denegado",
        "privilegios insuficientes",
    )),
    (STATUS_NO_ADAPTER, (
        "no wi-fi device found",
        "no wifi device found",
        "wi-fi is disabled",
        "no se encontró ningún dispositivo",
    )),
    (STATUS_TIMEOUT, (
        "timeout expired",
        "timeout",
        "tiempo de espera",
    )),
]


def classify_error(stderr: str) -> str:
    """stderr de nmcli → uno de los STATUS_*. Ante la duda, STATUS_FAILED."""
    haystack = (stderr or "").lower()
    for status, needles in _ERROR_PATTERNS:
        if any(needle in haystack for needle in needles):
            return status
    return STATUS_FAILED


# ─────────────────────────────────────────────
# CONSULTAS AUXILIARES (parsing puro + comandos)
# ─────────────────────────────────────────────

def parse_wifi_devices(raw: str) -> list[str]:
    """
    Salida de `nmcli -t -f DEVICE,TYPE,STATE device` → interfaces Wi-Fi que NO
    están `unmanaged` ni `unavailable`.

    Se reutiliza el splitter de network_info: nmcli escapa los ':' dentro de un
    valor y un split ingenuo partiría nombres en dos.
    """
    devices = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        fields = network_info.split_nmcli_fields(line)
        if len(fields) < 2 or not fields[0]:
            continue
        if fields[1].strip().lower() != "wifi":
            continue
        state = (fields[2] if len(fields) > 2 else "").strip().lower()
        if state in ("unmanaged", "unavailable"):
            continue
        devices.append(fields[0])
    return devices


def parse_active_wifi_connections(raw: str) -> list[tuple[str, str]]:
    """
    Salida de `nmcli -t -f NAME,TYPE,DEVICE connection show --active` →
    [(nombre, dispositivo)] de las conexiones inalámbricas activas.
    """
    active = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        fields = network_info.split_nmcli_fields(line)
        if len(fields) < 2:
            continue
        name, conn_type = fields[0], fields[1].strip().lower()
        # NM reporta el tipo como '802-11-wireless' en `connection show`.
        if "wireless" not in conn_type and conn_type != "wifi":
            continue
        device = fields[2] if len(fields) > 2 else ""
        active.append((name, device))
    return active


def wifi_devices() -> list[str]:
    output = run_nmcli(["-t", "-f", "DEVICE,TYPE,STATE", "device"])
    if output.returncode != 0:
        return []
    return parse_wifi_devices(output.stdout)


def active_wifi_connections() -> list[tuple[str, str]]:
    output = run_nmcli(["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"])
    if output.returncode != 0:
        return []
    return parse_active_wifi_connections(output.stdout)


def is_connected_to(ssid: str) -> bool:
    """
    ¿Está ACTIVA una conexión inalámbrica cuyo perfil se llama como el SSID?

    Es la comprobación que convierte "nmcli devolvió 0" en "la red pedida está
    funcionando". NetworkManager nombra el perfil con el SSID cuando se conecta
    con `device wifi connect`, que es siempre nuestro caso.
    """
    return any(name == ssid for name, _device in active_wifi_connections())


def forget_profile(ssid: str) -> None:
    """
    Borra el perfil guardado con ese nombre, si existe.

    Necesario para RE-conectar con una clave nueva: con un perfil viejo en
    disco, NetworkManager reutiliza el secreto almacenado y `--ask` no llega a
    preguntar, así que la clave que escribió el conductor se ignoraría en
    silencio y seguiría fallando con la vieja.

    Su fallo no es fatal: se sigue adelante y, si el secreto viejo era el
    problema, la conexión fallará con `invalid_password`, que es un resultado
    honesto.
    """
    output = run_nmcli(["connection", "delete", "id", ssid])
    if output.returncode not in (0, None):
        log.debug("[WIFI] No había perfil previo que borrar (o no se pudo borrar)")


# ─────────────────────────────────────────────
# API PÚBLICA
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class WifiResult:
    status: str
    detail: str
    ssid: Optional[str] = None
    # Información de red fresca; solo se completa cuando la conexión se
    # verificó, para que la pantalla no tenga que hacer una segunda consulta.
    network: Optional[dict] = None


# Un solo intento a la vez. Dos conexiones simultáneas sobre la misma tarjeta se
# pisan entre sí y dejan al equipo sin red.
_lock = threading.Lock()
_in_progress = False


def is_in_progress() -> bool:
    with _lock:
        return _in_progress


def reset_state():
    """Vuelve al estado inicial. Solo lo usan los tests."""
    global _in_progress
    with _lock:
        _in_progress = False


def _result(status: str, ssid: Optional[str] = None, network: Optional[dict] = None) -> WifiResult:
    return WifiResult(status=status, detail=DETAILS.get(status, DETAILS[STATUS_FAILED]),
                      ssid=ssid, network=network)


def connect(ssid: str, password: Optional[str] = None,
            timeout: int = DEFAULT_CONNECT_TIMEOUT,
            runner=None) -> WifiResult:
    """
    Conecta (o reconecta) el equipo a la red `ssid`.

    `runner` existe para los tests: misma firma que `run_nmcli`. En producción
    es None y se usa el real, así que la suite ejercita toda la lógica sin tocar
    la red de la máquina donde corre.

    Valida → comprueba herramienta y adaptador → (borra perfil previo si hay
    clave nueva) → conecta con el secreto por stdin → VERIFICA que la conexión
    quedó activa → invalida el caché de network_info.

    Nunca devuelve `connected` sin haber verificado.
    """
    global _in_progress

    ssid = validate_ssid(ssid)
    password = validate_password(password)

    execute = runner or run_nmcli

    if not nmcli_available() and runner is None:
        log.error("[WIFI] nmcli no está instalado: no se puede cambiar la red")
        return _result(STATUS_UNAVAILABLE, ssid)

    with _lock:
        if _in_progress:
            return _result(STATUS_BUSY, ssid)
        _in_progress = True

    try:
        return _connect_locked(ssid, password, timeout, execute)
    finally:
        with _lock:
            _in_progress = False


def _connect_locked(ssid: str, password: str, timeout: int, execute) -> WifiResult:
    # ── 1. ¿Hay adaptador Wi-Fi? ────────────────────────────────────────────
    devices_output = execute(["-t", "-f", "DEVICE,TYPE,STATE", "device"], QUERY_TIMEOUT, None)
    if devices_output.missing:
        return _result(STATUS_UNAVAILABLE, ssid)
    if devices_output.returncode != 0:
        # nmcli existe pero no responde: normalmente NetworkManager parado.
        log.error("[WIFI] nmcli no pudo listar dispositivos: %s",
                  _scrub(devices_output.stderr, password))
        return _result(STATUS_UNAVAILABLE, ssid)
    if not parse_wifi_devices(devices_output.stdout):
        log.error("[WIFI] No hay ningún dispositivo Wi-Fi gestionado por NetworkManager")
        return _result(STATUS_NO_ADAPTER, ssid)

    # ── 2. Perfil previo ────────────────────────────────────────────────────
    # Solo se borra cuando el conductor escribió una clave: si dejó el campo
    # vacío está pidiendo reconectar con lo que ya hay guardado, y borrar el
    # perfil destruiría justamente ese secreto.
    if password:
        delete = execute(["connection", "delete", "id", ssid], QUERY_TIMEOUT, None)
        if delete.returncode not in (0, None):
            log.debug("[WIFI] Sin perfil previo para esa red (o no se pudo borrar)")

    # ── 3. Conexión ─────────────────────────────────────────────────────────
    # `--wait` acota la espera dentro de nmcli; el timeout del subprocess lo
    # acota por fuera. `--ask` hace que el secreto se pida por STDIN en vez de
    # viajar en argv.
    args = ["--wait", str(timeout)]
    stdin_text = None
    if password:
        args.append("--ask")
        # nmcli pide "Password (…):" y lee una línea.
        stdin_text = password + "\n"
    args += ["device", "wifi", "connect", ssid]

    log.warning("[WIFI] Conexión solicitada desde la pantalla a la red indicada")
    output = execute(args, timeout + CONNECT_TIMEOUT_MARGIN, stdin_text)

    if output.missing:
        return _result(STATUS_UNAVAILABLE, ssid)

    if output.timed_out:
        log.error("[WIFI] nmcli excedió el tiempo de espera al conectar")
        # Aun con timeout puede haber terminado de asociar justo después: se
        # verifica antes de dar la operación por fallida.
        return _verify_or(STATUS_TIMEOUT, ssid, execute)

    if output.returncode != 0:
        status = classify_error(output.stderr)
        log.error("[WIFI] nmcli falló al conectar (%s): %s",
                  status, _scrub(output.stderr, password))
        return _verify_or(status, ssid, execute)

    # ── 4. Verificación ─────────────────────────────────────────────────────
    # Código 0 no basta: se comprueba que la conexión esté realmente activa.
    return _verify_or(STATUS_FAILED, ssid, execute)


def _verify_or(fallback_status: str, ssid: str, execute) -> WifiResult:
    """
    Éxito si la red pedida quedó activa; si no, `fallback_status`.

    Concentra aquí la verificación para que NINGÚN camino pueda responder
    `connected` sin haberla pasado.
    """
    active = execute(["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
                     QUERY_TIMEOUT, None)

    connected = (
        active.returncode == 0
        and any(name == ssid for name, _dev in parse_active_wifi_connections(active.stdout))
    )

    if not connected:
        return _result(fallback_status, ssid)

    # La red cambió: el caché de 15 s de network_info describiría la red
    # anterior y la pantalla mostraría la IP vieja.
    network_info.reset_cache()
    log.info("[WIFI] Conexión verificada: la red solicitada está activa")
    return _result(STATUS_CONNECTED, ssid, network=network_info.get_network_info())
