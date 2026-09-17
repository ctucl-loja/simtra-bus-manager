"""
Cliente del backend remoto SIMTRA.

Es la ÚNICA salida a internet del sistema. Todas las llamadas llevan timeout:
un socket colgado en la red del bus dejaría el proceso vivo pero bloqueado, y
systemd no lo reiniciaría.

Ante un 401 se renueva el token y se reintenta la operación UNA sola vez; el
reintento nunca vuelve a reintentar, así que no existe bucle de autenticación.
Ni la contraseña ni el token se registran nunca en los logs.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger("simtra")

# Timeout de todas las solicitudes remotas (segundos). Generoso porque la red
# del bus es lenta, pero acotado: sin él una conexión colgada bloquea el hilo.
DEFAULT_TIMEOUT = 10

# Códigos con los que el backend da por bueno un login. Es 201 porque
# AuthController lo fija así; se admite 200 por si esa ruta se normaliza.
LOGIN_OK_STATUSES = (200, 201)


# ─────────────────────────────────────────────
# RESULTADO EXPLÍCITO DE UNA LECTURA DE DESPACHOS
#
# `get_dispatch()` devuelve [] tanto cuando el bus no trabaja hoy como cuando la
# red falló. Para la pantalla eso es indistinguible y es justo la diferencia que
# necesita la recarga manual: un día sin despachos debe vaciar el itinerario,
# un error NO debe tocarlo.
#
# Se añade un método nuevo en vez de cambiar el existente: los consumidores
# actuales (bus_monitor.load_all_dispatches) siguen funcionando igual.
# ─────────────────────────────────────────────

FETCH_OK            = "ok"                 # respuesta válida con despachos
FETCH_EMPTY         = "empty"              # respuesta válida, el bus no trabaja hoy
FETCH_AUTH_ERROR    = "auth_error"         # credenciales rechazadas (401/403)
FETCH_TRANSPORT     = "transport_error"    # red caída, timeout, 5xx, 404…
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


class ApiService:
    """
    API pública sin cambios: get_jwt, get_dispatch, get_vehicle, post_passenger,
    update_dispatch. Lo que cambia es que ahora fallan de forma predecible —
    lista vacía, None o False— en vez de propagar excepciones o devolver
    respuestas a medio validar.
    """

    def __init__(self, api_url, username, password):
        self.user = username
        self.password = password
        self.api_url = (api_url or "").rstrip("/")
        # Token perezoso: pedirlo en el constructor hacía que un arranque sin
        # señal (lo normal en un bus que enciende) dejara el servicio sin token
        # y, con la red caída, bloqueara el import del módulo.
        self.jwt = ""
        self._session = requests.Session()

    # ─────────────────────────────────────────
    # INTERNO
    # ─────────────────────────────────────────

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.jwt}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _json(response, description: str):
        """Cuerpo JSON de la respuesta, o None si no es JSON válido."""
        try:
            return response.json()
        except ValueError:
            body = (response.text or "")[:120]
            log.error(f"[API] {description}: respuesta no es JSON válido (HTTP {response.status_code}) {body!r}")
            return None

    def _result(self, response, description: str, ok_statuses=(200,)):
        """
        Campo `result` de una respuesta correcta, o None. Valida la envoltura
        antes de devolver nada: el resto del sistema no debe recibir estructuras
        a medias.

        `ok_statuses` es parametrizable porque el login del backend responde 201
        (@HttpCode(201) en AuthController), no el 200 del resto de lecturas.
        """
        if response is None:
            return None
        if response.status_code not in ok_statuses:
            log.error(f"[API] {description}: HTTP {response.status_code}")
            return None

        data = self._json(response, description)
        if not isinstance(data, dict):
            log.error(f"[API] {description}: cuerpo inesperado ({type(data).__name__})")
            return None
        if "result" not in data:
            log.error(f"[API] {description}: respuesta sin campo 'result'")
            return None
        return data["result"]

    def _request(self, method: str, path: str, description: str,
                 json=None, retry_on_401: bool = True) -> Optional[requests.Response]:
        """
        Solicitud autenticada con timeout. Devuelve la respuesta o None si no se
        pudo completar. `retry_on_401=False` en el reintento corta cualquier
        posibilidad de recursión infinita.
        """
        if not self.api_url:
            log.error(f"[API] {description}: backend remoto sin URL configurada")
            return None

        if not self.jwt:
            self.get_jwt()

        try:
            response = self._session.request(
                method,
                f"{self.api_url}{path}",
                json=json,
                headers=self._auth_headers(),
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            log.error(f"[API] {description}: fallo de red ({e})")
            return None

        if response.status_code == 401 and retry_on_401:
            log.warning(f"[API] {description}: token no válido o expirado — se renueva y se reintenta una vez")
            if not self.get_jwt():
                # Se devuelve el 401 original en vez de None: para el llamador la
                # diferencia entre "no hubo red" y "las credenciales no sirven"
                # es la que separa un error transitorio de uno que exige tocar el
                # .env, y `fetch_dispatch` la necesita para reportarla.
                return response
            return self._request(method, path, description, json=json, retry_on_401=False)

        return response

    # ─────────────────────────────────────────
    # API PÚBLICA
    # ─────────────────────────────────────────

    def get_jwt(self) -> str:
        """
        Renueva el token y lo devuelve ('' si no se pudo). No reintenta: es el
        propio mecanismo de reintento de _request.
        """
        self.jwt = ""

        if not self.api_url or not self.user:
            log.error("[API] Login: falta URL o usuario del backend remoto")
            return ""

        try:
            response = self._session.post(
                f"{self.api_url}/api/auth/login",
                json={"email": self.user, "password": self.password},
                headers={"Content-Type": "application/json"},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            log.error(f"[API] Login: fallo de red ({e})")
            return ""

        # El backend emite el token con 201, no con 200: `POST /api/auth/login`
        # lleva un @HttpCode(201) explícito. Se aceptan ambos para no depender
        # de ese detalle si algún día se normaliza a 200.
        if response.status_code not in LOGIN_OK_STATUSES:
            # Nunca se registra usuario ni contraseña.
            log.error(f"[API] Login rechazado (HTTP {response.status_code})")
            return ""

        result = self._result(response, "Login", ok_statuses=LOGIN_OK_STATUSES)
        token = result.get("token") if isinstance(result, dict) else None

        if not isinstance(token, str) or not token:
            log.error("[API] Login: respuesta sin token utilizable")
            return ""

        self.jwt = token
        log.info("[API] Token renovado")
        return token

    def fetch_dispatch(self, register, date) -> DispatchFetch:
        """
        Despachos del día con resultado EXPLÍCITO.

        Distingue las cuatro situaciones que la pantalla necesita separar:

            FETCH_OK         → hay despachos utilizables
            FETCH_EMPTY      → el backend respondió bien y el bus no trabaja hoy
            FETCH_AUTH_ERROR → credenciales rechazadas (401/403 tras reintentar)
            FETCH_TRANSPORT  → no se pudo hablar con el backend (red, 5xx, 404…)
            FETCH_INVALID    → respondió, pero el cuerpo no es utilizable

        Nunca lanza. Ni el usuario ni la contraseña aparecen en el log.
        """
        description = f"GET /api/dispatch/{register}"
        response = self._request("GET", f"/api/dispatch/{register}?date={date}", description)

        if response is None:
            return DispatchFetch(FETCH_TRANSPORT)

        if response.status_code in (401, 403):
            log.error(f"[API] {description}: credenciales rechazadas (HTTP {response.status_code})")
            return DispatchFetch(FETCH_AUTH_ERROR)

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

    def get_dispatch(self, register, date) -> list:
        """
        Despachos del día. Siempre una lista (vacía si no se pudo obtener).

        Se conserva por compatibilidad con bus_monitor.load_all_dispatches, que
        solo necesita saber si hay trabajo hoy. Quien deba distinguir "sin
        despachos" de "falló la consulta" tiene que usar `fetch_dispatch`.
        """
        return self.fetch_dispatch(register, date).dispatches

    def get_vehicle(self, register) -> Optional[dict]:
        """Ficha del vehículo, o None si no se pudo obtener o no es un objeto."""
        description = f"GET /api/vehicle/register/{register}"
        response = self._request("GET", f"/api/vehicle/register/{register}", description)
        result = self._result(response, description)

        if result is None:
            return None
        if not isinstance(result, dict):
            log.error(f"[API] {description}: 'result' es {type(result).__name__}, se esperaba objeto — se ignora")
            return None
        return result

    def post_passenger(self, data) -> bool:
        """True solo si el backend confirmó la creación (201)."""
        description = "POST /api/passenger"
        response = self._request("POST", "/api/passenger", description, json=data)

        if response is None:
            return False
        if response.status_code != 201:
            log.error(f"[API] {description}: HTTP {response.status_code}")
            return False
        return True

    def update_dispatch(self, data) -> bool:
        """True solo si el backend confirmó la actualización (200)."""
        description = "PATCH /api/dispatch"
        response = self._request("PATCH", "/api/dispatch", description, json=data)

        if response is None:
            return False
        if response.status_code != 200:
            log.error(f"[API] {description}: HTTP {response.status_code}")
            return False
        return True
