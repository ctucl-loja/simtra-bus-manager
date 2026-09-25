"""
Cliente del backend remoto SIMTRA (Buslytics).

Es la ÚNICA salida a internet del sistema y habla EXCLUSIVAMENTE con las rutas
`/api/device-api/*`, autenticadas con la cabecera `X-API-Key` del equipo
(`FAST_API_DEVICE_API_KEY`). No hay login, ni JWT, ni fallback a las rutas
antiguas: si la clave falta o el backend la rechaza, el error se devuelve
explícito y el llamador decide (conservar colas, conservar itinerario).

Contrato confirmado en buslytics-backend
(src/modules/device-api/device-api.controller.ts, prefijo global `api`):

    GET   /api/device-api/dispatch/:register?date=YYYY-MM-DD
          200 {result: [...]} · 404 "No dispatch found…" = día sin despachos
    PATCH /api/device-api/dispatch/:register   {id, time_reported}
          200 registrada (o reintento con la MISMA hora) · 409 hora distinta
    POST  /api/device-api/gps/:register        {timestamp, latitude, longitude, speed?}
          201 registrado O descartado por las reglas de traza del backend
    POST  /api/device-api/passenger            {timestamp, latitude, longitude,
                                                register, direction?, door?}
          201 registrado

`DeviceApiKeyGuard` responde 401 ante clave ausente/inválida/desactivada y 403
si el registro de la petición no es el del bus asignado al equipo. El
`ValidationPipe` global usa `forbidNonWhitelisted`: un campo de más es un 400.

NO existe en device-api una ruta para la ficha del vehículo (ver get_vehicle).

Todas las llamadas llevan timeout: un socket colgado en la red del bus dejaría
el proceso vivo pero bloqueado, y systemd no lo reiniciaría. La API key nunca
se registra en los logs ni viaja en la URL.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger("simtra")

# Timeout de todas las solicitudes remotas (segundos). Generoso porque la red
# del bus es lenta, pero acotado: sin él una conexión colgada bloquea el hilo.
DEFAULT_TIMEOUT = 10

DEVICE_API_PREFIX = "/api/device-api"
API_KEY_HEADER = "X-API-Key"

# Mensaje con el que DispatchService.getByRegisterAndDate responde 404 cuando
# el bus no tiene despachos ese día. Cualquier otro 404 (ruta inexistente,
# backend desactualizado) NO es un día vacío.
NO_DISPATCH_MESSAGE_PREFIX = "No dispatch found"


# ─────────────────────────────────────────────
# RESULTADO EXPLÍCITO DE UNA LECTURA DE DESPACHOS
#
# Un día sin despachos debe vaciar el itinerario; un error NO debe tocarlo.
# ─────────────────────────────────────────────

FETCH_OK            = "ok"                 # respuesta válida con despachos
FETCH_EMPTY         = "empty"              # respuesta válida, el bus no trabaja hoy
FETCH_AUTH_ERROR    = "auth_error"         # API key rechazada (401/403)
FETCH_CONFIG_ERROR  = "config_error"       # falta URL o API key en el .env
FETCH_TRANSPORT     = "transport_error"    # red caída, timeout, 5xx, 404 de ruta…
FETCH_INVALID       = "invalid_response"   # respondió, pero con una forma inutilizable


@dataclass(frozen=True)
class DispatchFetch:
    """
    Resultado de pedir los despachos del día al backend remoto.

    `dispatches` solo tiene contenido con status FETCH_OK. En FETCH_EMPTY es una
    lista vacía QUE SÍ significa "hoy no hay despachos"; en los estados de error
    es vacía y NO significa nada sobre el día.
    """
    status: str
    dispatches: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """¿La consulta fue válida? (con o sin despachos)"""
        return self.status in (FETCH_OK, FETCH_EMPTY)


# ─────────────────────────────────────────────
# RESULTADO EXPLÍCITO DE UNA ESCRITURA
#
# data_loader necesita más que un booleano: un 400 (dato que el backend nunca
# aceptará) no se trata igual que un timeout (reintentar) ni que un 401 (parar
# la cola hasta que el operador arregle la clave).
# ─────────────────────────────────────────────

SEND_OK           = "ok"             # confirmado por el contrato (201/200)
SEND_CONFLICT     = "conflict"       # 409: el backend ya tiene OTRO valor; no se sobrescribe
SEND_REJECTED     = "rejected"       # 400/422: el dato nunca será aceptado tal cual
SEND_AUTH_ERROR   = "auth_error"     # 401/403: clave rechazada o bus no asignado
SEND_CONFIG_ERROR = "config_error"   # falta URL o API key: no se llegó a enviar
SEND_NOT_FOUND    = "not_found"      # 404: despacho o vehículo inexistente (o ruta ausente)
SEND_RETRY        = "retry"          # red, timeout, 429, 5xx: reintentar después


@dataclass(frozen=True)
class SendResult:
    status: str
    http_status: Optional[int] = None

    def __bool__(self) -> bool:
        return self.status == SEND_OK

    @property
    def ok(self) -> bool:
        return self.status == SEND_OK

    @property
    def stops_queue(self) -> bool:
        """
        ¿Hay que dejar de enviar en este ciclo?

        Con la clave rechazada, sin configuración o sin red, cada envío
        siguiente fallará igual: insistir solo alarga el ciclo (hasta
        DEFAULT_TIMEOUT por elemento) y retrasa las otras colas.
        """
        return self.status in (SEND_AUTH_ERROR, SEND_CONFIG_ERROR, SEND_RETRY)


class ApiService:
    """Cliente device-api. Nunca lanza: devuelve DispatchFetch / SendResult / None."""

    def __init__(self, api_url, device_api_key):
        self.api_url = (api_url or "").rstrip("/")
        self.device_api_key = (device_api_key or "").strip()
        self._session = requests.Session()
        self._vehicle_warning_logged = False

    # ─────────────────────────────────────────
    # INTERNO
    # ─────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            API_KEY_HEADER: self.device_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def configuration_error(self) -> Optional[str]:
        """Motivo por el que no se puede hablar con el backend, o None."""
        if not self.api_url:
            return "FAST_API_BACKEND_URL no está configurada"
        if not self.device_api_key:
            return "FAST_API_DEVICE_API_KEY no está configurada"
        return None

    @staticmethod
    def _json(response, description: str):
        """Cuerpo JSON de la respuesta, o None si no es JSON válido."""
        try:
            return response.json()
        except ValueError:
            body = (response.text or "")[:120]
            log.error(f"[API] {description}: respuesta no es JSON válido (HTTP {response.status_code}) {body!r}")
            return None

    def _request(self, method: str, path: str, description: str,
                 json=None, params=None) -> Optional[requests.Response]:
        """
        Solicitud autenticada con X-API-Key y timeout. Devuelve la respuesta o
        None si no se pudo completar (red). No reintenta: un 401 aquí significa
        clave ausente, inválida o desactivada, y repetir no lo arregla.
        """
        kwargs = {"json": json, "headers": self._headers(), "timeout": DEFAULT_TIMEOUT}
        if params:
            kwargs["params"] = params
        try:
            return self._session.request(method, f"{self.api_url}{path}", **kwargs)
        except requests.RequestException as e:
            # La clave va en una cabecera: el texto de la excepción (URL, causa)
            # no la contiene.
            log.error(f"[API] {description}: fallo de red ({type(e).__name__})")
            return None

    def _send(self, method: str, path: str, description: str, json,
              ok_statuses: tuple) -> SendResult:
        config_error = self.configuration_error()
        if config_error:
            log.error(f"[API] {description}: {config_error} — no se envía")
            return SendResult(SEND_CONFIG_ERROR)

        response = self._request(method, path, description, json=json)
        if response is None:
            return SendResult(SEND_RETRY)

        code = response.status_code
        if code in ok_statuses:
            return SendResult(SEND_OK, code)
        if code in (401, 403):
            log.error(f"[API] {description}: API key rechazada o bus no asignado (HTTP {code})")
            return SendResult(SEND_AUTH_ERROR, code)
        if code == 409:
            log.warning(f"[API] {description}: el backend ya tiene otro valor (HTTP 409)")
            return SendResult(SEND_CONFLICT, code)
        if code in (400, 422):
            log.error(f"[API] {description}: dato rechazado por el backend (HTTP {code})")
            return SendResult(SEND_REJECTED, code)
        if code == 404:
            log.error(f"[API] {description}: recurso no encontrado (HTTP 404)")
            return SendResult(SEND_NOT_FOUND, code)

        log.error(f"[API] {description}: HTTP {code} — se reintentará")
        return SendResult(SEND_RETRY, code)

    # ─────────────────────────────────────────
    # LECTURAS
    # ─────────────────────────────────────────

    def fetch_dispatch(self, register, date) -> DispatchFetch:
        """
        Despachos del día con resultado EXPLÍCITO (ver FETCH_*). Nunca lanza.

        `GET /api/device-api/dispatch/:register?date=` responde 404 con el
        mensaje "No dispatch found for vehicle …" cuando el bus no trabaja ese
        día: eso es FETCH_EMPTY. Cualquier otro 404 es de transporte.
        """
        description = f"GET {DEVICE_API_PREFIX}/dispatch/{register}"

        config_error = self.configuration_error()
        if config_error:
            log.error(f"[API] {description}: {config_error}")
            return DispatchFetch(FETCH_CONFIG_ERROR)

        response = self._request(
            "GET", f"{DEVICE_API_PREFIX}/dispatch/{register}", description,
            params={"date": date},
        )

        if response is None:
            return DispatchFetch(FETCH_TRANSPORT)

        if response.status_code in (401, 403):
            log.error(f"[API] {description}: API key rechazada o bus no asignado (HTTP {response.status_code})")
            return DispatchFetch(FETCH_AUTH_ERROR)

        if response.status_code == 404:
            data = self._json(response, description)
            message = data.get("message") if isinstance(data, dict) else None
            if isinstance(message, str) and message.startswith(NO_DISPATCH_MESSAGE_PREFIX):
                return DispatchFetch(FETCH_EMPTY)
            log.error(f"[API] {description}: HTTP 404 sin la forma de 'día sin despachos'")
            return DispatchFetch(FETCH_TRANSPORT)

        if response.status_code != 200:
            log.error(f"[API] {description}: HTTP {response.status_code}")
            return DispatchFetch(FETCH_TRANSPORT)

        data = self._json(response, description)
        if not isinstance(data, dict) or "result" not in data:
            log.error(f"[API] {description}: cuerpo sin 'result' utilizable")
            return DispatchFetch(FETCH_INVALID)

        result = data["result"]
        if not isinstance(result, list):
            log.error(
                f"[API] {description}: 'result' es {type(result).__name__}, se esperaba lista"
            )
            return DispatchFetch(FETCH_INVALID)

        return DispatchFetch(FETCH_OK if result else FETCH_EMPTY, result)

    def get_vehicle(self, register) -> Optional[dict]:
        """
        Ficha del vehículo: BLOQUEADA, siempre None y sin tocar la red.

        device-api no expone ninguna ruta de lectura del vehículo, y la antigua
        `GET /api/vehicle/register/:register` exige JWT de usuario, que este
        equipo ya no tiene. No se inventa una ruta: hasta que el backend
        publique `GET /api/device-api/vehicle/:register` (ver README →
        "Bloqueo: ficha del vehículo"), la ficha cacheada en la API local se
        conserva tal cual y no se actualiza.
        """
        if not self._vehicle_warning_logged:
            log.warning(
                f"[API] Ficha del vehículo {register}: device-api no expone una ruta "
                "de lectura del vehículo — se conserva la ficha local"
            )
            self._vehicle_warning_logged = True
        return None

    # ─────────────────────────────────────────
    # ESCRITURAS
    # ─────────────────────────────────────────

    def post_passenger(self, data) -> SendResult:
        """`POST /api/device-api/passenger`. OK solo con 201."""
        return self._send(
            "POST", f"{DEVICE_API_PREFIX}/passenger",
            f"POST {DEVICE_API_PREFIX}/passenger", data, ok_statuses=(201,),
        )

    def update_dispatch(self, data, register) -> SendResult:
        """
        `PATCH /api/device-api/dispatch/:register`. OK solo con 200, que el
        backend devuelve también al reintentar con la misma hora. 409 = el
        despacho ya tiene otra hora y no se sobrescribe (SEND_CONFLICT).
        """
        path = f"{DEVICE_API_PREFIX}/dispatch/{register}"
        return self._send("PATCH", path, f"PATCH {path}", data, ok_statuses=(200,))

    def post_gps(self, data, register) -> SendResult:
        """
        `POST /api/device-api/gps/:register`. OK solo con 201, que el backend
        devuelve tanto si archivó el punto como si lo descartó por sus reglas
        de traza (velocidad 0, < 5 m del anterior): en ambos casos el punto
        quedó procesado y no hay que reenviarlo.
        """
        path = f"{DEVICE_API_PREFIX}/gps/{register}"
        return self._send("POST", path, f"POST {path}", data, ok_statuses=(201,))
