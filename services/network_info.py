"""
Información de red del propio dispositivo (la Raspberry Pi), de solo lectura.

Existe porque el navegador no puede consultar el SSID ni las interfaces del
sistema: la pantalla necesita mostrar a qué red está conectado EL EQUIPO, no la
laptop desde la que se abre la interfaz.

Alcance deliberadamente estrecho. Se exponen únicamente:

    tipo de conexión · nombre de interfaz · SSID (solo Wi-Fi) · direcciones IPv4

Nunca: contraseñas, claves, MAC, gateway, DNS, rutas, archivos de configuración
ni nada del router. Y no hay ninguna operación de escritura — este módulo no
conecta, desconecta ni modifica interfaces.

La ejecución de comandos está separada del parsing a propósito: las funciones
puras se prueban con salidas simuladas, sin depender de las interfaces reales de
la máquina donde corren los tests.
"""

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("simtra")

# Timeout corto: esto responde a una petición HTTP de la pantalla; es preferible
# devolver "unavailable" a dejar la vista colgada.
COMMAND_TIMEOUT_SECONDS = 3

# Los comandos del sistema no deben ejecutarse en cada petición: la pantalla
# refresca cada 30 s y podría haber varios clientes (kiosco + laptop).
CACHE_TTL_SECONDS = 15

STATUS_CONNECTED    = "connected"      # al menos una conexión activa con IPv4
STATUS_DISCONNECTED = "disconnected"   # se pudo consultar, no hay conexiones
STATUS_UNAVAILABLE  = "unavailable"    # no se pudo obtener la información

TYPE_WIFI     = "wifi"
TYPE_ETHERNET = "ethernet"
TYPE_OTHER    = "other"

# Nombres de interfaz aceptables antes de pasarlos como argumento a un comando.
# Los nombres vienen del propio sistema, nunca del usuario, pero se validan
# igual: es la última barrera antes de un subprocess.
INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9._@-]{1,15}$")


# ─────────────────────────────────────────────
# EJECUCIÓN DE COMANDOS
#
# Nada de esto lanza: cualquier fallo se traduce a None y el llamador decide.
# ─────────────────────────────────────────────

def run_command(args: list[str]) -> Optional[str]:
    """
    stdout del comando, o None si no se pudo ejecutar.

    Siempre lista de argumentos y shell=False: no se construyen líneas de
    comando por concatenación, así que no hay superficie de inyección.
    """
    try:
        result = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        # Herramienta no instalada, o sistema que no es Linux.
        log.debug(f"[NETWORK] '{args[0]}' no está disponible")
        return None
    except subprocess.TimeoutExpired:
        log.warning(f"[NETWORK] '{args[0]}' excedió {COMMAND_TIMEOUT_SECONDS} s")
        return None
    except (OSError, ValueError) as e:
        log.warning(f"[NETWORK] No se pudo ejecutar '{args[0]}': {e}")
        return None

    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:120]
        log.debug(f"[NETWORK] '{args[0]}' terminó con código {result.returncode}: {detail}")
        return None

    return result.stdout


def read_ip_addresses() -> Optional[str]:
    """Salida JSON de `ip -j address`: interfaces y direcciones en una llamada."""
    return run_command(["ip", "-j", "address"])


def read_nmcli_devices() -> Optional[str]:
    """
    Tipos de dispositivo y conexión activa en una sola llamada. Opcional:
    NetworkManager puede no estar instalado.
    """
    return run_command(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])


def read_ssid(interface: str) -> Optional[str]:
    """SSID vía iwgetid. Solo se invoca para interfaces Wi-Fi sin nombre conocido."""
    if not INTERFACE_PATTERN.match(interface or ""):
        return None
    output = run_command(["iwgetid", interface, "-r"])
    if output is None:
        return None
    ssid = output.strip()
    return ssid or None


def interface_is_wireless(interface: str) -> bool:
    """
    Detección de Wi-Fi sin subprocess ni nombres convencionales: el kernel
    expone /sys/class/net/<iface>/wireless solo para interfaces inalámbricas.
    Es el respaldo cuando nmcli no está disponible.
    """
    if not INTERFACE_PATTERN.match(interface or ""):
        return False
    try:
        return (Path("/sys/class/net") / interface / "wireless").is_dir()
    except OSError:
        return False


# ─────────────────────────────────────────────
# PARSING — funciones puras
# ─────────────────────────────────────────────

def valid_ipv4(value) -> bool:
    """IPv4 con cuatro octetos en rango. Se excluye loopback explícitamente."""
    if not isinstance(value, str):
        return False
    parts = value.strip().split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return False
    if not all(part.isdigit() and 0 <= octet <= 255 for part, octet in zip(parts, octets)):
        return False
    return octets[0] != 127   # 127.0.0.0/8 nunca se reporta


def parse_ip_addresses(raw) -> Optional[list[dict]]:
    """
    Salida de `ip -j address` → lista normalizada de interfaces, o None si la
    información no es utilizable (JSON inválido, forma inesperada, sin salida).

    None y [] significan cosas distintas: None es "no se pudo consultar"
    (unavailable) y [] es "se consultó y no hay nada" (disconnected).
    """
    if not raw or not str(raw).strip():
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("[NETWORK] La salida de 'ip -j address' no es JSON válido")
        return None

    if not isinstance(data, list):
        log.warning(f"[NETWORK] 'ip -j address' devolvió {type(data).__name__}, se esperaba lista")
        return None

    interfaces = []
    for entry in data:
        if not isinstance(entry, dict):
            continue

        name = entry.get("ifname")
        if not isinstance(name, str) or not name.strip():
            continue

        flags = entry.get("flags")
        flags = [f for f in flags if isinstance(f, str)] if isinstance(flags, list) else []
        link_type = entry.get("link_type") if isinstance(entry.get("link_type"), str) else ""

        # Loopback fuera, por flag o por link_type: no es una conexión de red.
        if "LOOPBACK" in flags or link_type == "loopback":
            continue

        # Solo interfaces levantadas.
        if "UP" not in flags and entry.get("operstate") != "UP":
            continue

        addr_info = entry.get("addr_info")
        addresses = []
        if isinstance(addr_info, list):
            for addr in addr_info:
                if not isinstance(addr, dict) or addr.get("family") != "inet":
                    continue
                local = addr.get("local")
                if valid_ipv4(local) and local not in addresses:
                    addresses.append(local.strip())

        interfaces.append({
            "interface": name.strip(),
            "link_type": link_type,
            "ipv4": addresses,
        })

    return interfaces


def split_nmcli_fields(line: str) -> list[str]:
    """
    Divide una línea de `nmcli -t` respetando los dos puntos escapados: nmcli
    emite `\\:` dentro de un valor, así que un split ingenuo partiría en dos un
    SSID que contenga ':'.
    """
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def parse_nmcli_devices(raw) -> dict:
    """
    Salida de `nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device` →
    {interfaz: {"type": ..., "state": ..., "connection": ...}}.

    Devuelve {} ante cualquier problema: nmcli es una fuente opcional y su
    ausencia no puede impedir que se reporten las interfaces.
    """
    if not raw or not isinstance(raw, str):
        return {}

    devices = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = split_nmcli_fields(line)
        if len(fields) < 2 or not fields[0]:
            continue
        devices[fields[0]] = {
            "type": fields[1] or "",
            "state": fields[2] if len(fields) > 2 else "",
            "connection": fields[3] if len(fields) > 3 else "",
        }
    return devices


def classify_connection(nmcli_type: str, link_type: str, is_wireless: bool) -> str:
    """
    Tipo de conexión. El orden importa:

      1. nmcli, cuando está disponible (única fuente que distingue con certeza);
      2. el flag inalámbrico del kernel;
      3. link_type.

    Nunca se decide por el nombre de la interfaz: en equipos con nombres
    predecibles (eno2, wlo1, enp3s0) esa heurística falla.
    """
    normalized = (nmcli_type or "").strip().lower()
    if normalized == "wifi":
        return TYPE_WIFI
    if normalized == "ethernet":
        return TYPE_ETHERNET

    if is_wireless:
        return TYPE_WIFI
    if not normalized and link_type == "ether":
        return TYPE_ETHERNET

    return TYPE_OTHER


def build_network_info(
    ip_raw,
    nmcli_raw=None,
    ssid_lookup: Optional[Callable[[str], Optional[str]]] = None,
    wireless_lookup: Optional[Callable[[str], bool]] = None,
) -> dict:
    """
    Compone la respuesta del endpoint a partir de salidas crudas.

    Función pura salvo por las dos búsquedas inyectadas, que en los tests se
    sustituyen por funciones simuladas. Ese es justamente el motivo de que la
    ejecución de comandos viva en otras funciones.

    Una interfaz activa SIN IPv4 no se reporta: sin dirección no hay nada que
    mostrar al conductor, y anunciarla como conexión sería engañoso.
    """
    interfaces = parse_ip_addresses(ip_raw)
    if interfaces is None:
        return {"status": STATUS_UNAVAILABLE, "connections": []}

    devices = parse_nmcli_devices(nmcli_raw)
    ssid_lookup = ssid_lookup or read_ssid
    wireless_lookup = wireless_lookup or interface_is_wireless

    connections = []
    for entry in interfaces:
        if not entry["ipv4"]:
            continue

        name = entry["interface"]
        device = devices.get(name, {})
        connection_type = classify_connection(
            device.get("type", ""), entry["link_type"], wireless_lookup(name)
        )

        # `name` es el SSID y solo aplica a Wi-Fi. Para cable se deja en null a
        # propósito: el perfil de NetworkManager no es información de red útil
        # y no tiene por qué salir de la máquina.
        ssid = None
        if connection_type == TYPE_WIFI:
            ssid = (device.get("connection") or "").strip() or None
            if ssid is None:
                ssid = ssid_lookup(name)

        connections.append({
            "type": connection_type,
            "interface": name,
            "name": ssid,
            "ipv4": entry["ipv4"],
        })

    status = STATUS_CONNECTED if connections else STATUS_DISCONNECTED
    return {"status": status, "connections": connections}


# ─────────────────────────────────────────────
# API PÚBLICA (con caché)
# ─────────────────────────────────────────────

_cache_lock = threading.Lock()
_cached: Optional[dict] = None
_cached_at: float = 0.0


def collect_network_info() -> dict:
    """Consulta el sistema y compone la respuesta. Sin caché."""
    return build_network_info(read_ip_addresses(), read_nmcli_devices())


def get_network_info() -> dict:
    """
    Respuesta cacheada durante CACHE_TTL_SECONDS.

    El lock protege solo la lectura/escritura del caché: los comandos se
    ejecutan FUERA de él. Si dos peticiones coinciden justo al expirar, ambas
    consultan el sistema —son de solo lectura e idempotentes— y eso es
    preferible a serializar las peticiones detrás de un subprocess.
    """
    global _cached, _cached_at

    now = time.monotonic()
    with _cache_lock:
        if _cached is not None and (now - _cached_at) < CACHE_TTL_SECONDS:
            return _cached

    info = collect_network_info()

    with _cache_lock:
        _cached = info
        _cached_at = time.monotonic()

    return info


def reset_cache():
    """Invalida el caché. Uso: tests."""
    global _cached, _cached_at
    with _cache_lock:
        _cached = None
        _cached_at = 0.0
