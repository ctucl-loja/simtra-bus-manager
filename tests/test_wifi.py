"""
Conexión Wi-Fi del dispositivo.

NINGÚN test toca la red de la máquina donde corre: `wifi.connect` recibe el
ejecutor (`runner`, misma firma que `run_nmcli`), así que se ejercita la lógica
completa con salidas de nmcli simuladas. No se ejecuta un solo nmcli real.

Lo que se comprueba:

  1. Validación de SSID y clave, con mensajes que NO citan el valor recibido.
  2. La clave viaja por STDIN, nunca en la línea de comandos.
  3. `connected` solo se responde tras VERIFICAR que la conexión quedó activa;
     que nmcli devuelva 0 no basta.
  4. Clave incorrecta, red inexistente, timeout, herramienta ausente, sin
     adaptador y permisos insuficientes producen estados propios, no un 500.
  5. La clave no aparece en NINGUNA respuesta ni en NINGÚN log.
  6. Dos intentos simultáneos no se pisan.
"""

import logging
import unittest

import _bootstrap  # noqa: F401

import network_info
import wifi


# ─────────────────────────────────────────────
# DOBLE DE nmcli
# ─────────────────────────────────────────────

def output(returncode=0, stdout="", stderr="", timed_out=False, missing=False):
    return wifi.CommandOutput(
        returncode=returncode, stdout=stdout, stderr=stderr,
        timed_out=timed_out, missing=missing,
    )


DEVICES_WITH_WIFI = "wlan0:wifi:connected:CASA\neth0:ethernet:unavailable:\n"
DEVICES_WITHOUT_WIFI = "eth0:ethernet:connected:Cable\n"


def active_connections(*names):
    return "".join(f"{name}:802-11-wireless:wlan0\n" for name in names)


class FakeNmcli:
    """
    Registra cada invocación y responde según la operación.

    `calls` guarda (args, timeout, stdin) tal cual, que es lo que permite
    afirmar dónde NO está la clave.
    """

    def __init__(self, devices=DEVICES_WITH_WIFI, connect=None, active=()):
        self.devices = devices
        self.connect_result = connect if connect is not None else output(0)
        self.active_after = active
        self.calls = []

    def __call__(self, args, timeout=None, stdin_text=None):
        self.calls.append((list(args), timeout, stdin_text))

        if args[:1] == ["-t"] and "device" in args and "show" not in args:
            return output(0, self.devices)
        if args[:2] == ["connection", "delete"]:
            return output(0)
        if "--active" in args:
            return output(0, active_connections(*self.active_after))
        if "connect" in args:
            return self.connect_result
        return output(0)

    # ── ayudas de aserción ───────────────────────────────────────────────────

    @property
    def connect_call(self):
        for args, timeout, stdin in self.calls:
            if "connect" in args:
                return args, timeout, stdin
        return None, None, None

    def deleted_profiles(self):
        return [args[-1] for args, _t, _s in self.calls if args[:2] == ["connection", "delete"]]

    def every_argument(self):
        return [arg for args, _t, _s in self.calls for arg in args]


class ValidationTest(unittest.TestCase):
    def test_ssid_obligatorio(self):
        for value in (None, "", "   ", 123):
            with self.subTest(value=value):
                with self.assertRaises(wifi.InvalidParameter):
                    wifi.validate_ssid(value)

    def test_ssid_con_caracteres_de_control(self):
        with self.assertRaises(wifi.InvalidParameter):
            wifi.validate_ssid("mi\x00red")

    def test_ssid_demasiado_largo(self):
        """32 OCTETOS, no 32 caracteres: 'ñ' ocupa dos."""
        self.assertEqual(wifi.validate_ssid("a" * 32), "a" * 32)
        with self.assertRaises(wifi.InvalidParameter):
            wifi.validate_ssid("a" * 33)
        with self.assertRaises(wifi.InvalidParameter):
            wifi.validate_ssid("ñ" * 17)   # 34 octetos

    def test_ssid_que_empieza_por_guion_se_rechaza(self):
        """Sería interpretado como una opción por nmcli: se rechaza explícitamente."""
        with self.assertRaises(wifi.InvalidParameter):
            wifi.validate_ssid("-red")

    def test_ssid_se_recorta(self):
        self.assertEqual(wifi.validate_ssid("  MI RED  "), "MI RED")

    def test_clave_vacia_es_valida(self):
        """Red abierta, o reconectar con el perfil ya guardado."""
        self.assertEqual(wifi.validate_password(""), "")
        self.assertEqual(wifi.validate_password(None), "")

    def test_clave_fuera_de_rango_wpa(self):
        for value in ("corta", "a" * 64):
            with self.subTest(length=len(value)):
                with self.assertRaises(wifi.InvalidParameter):
                    wifi.validate_password(value)

    def test_clave_no_se_recorta(self):
        """Un espacio al final puede ser parte legítima del PSK."""
        self.assertEqual(wifi.validate_password("clave123 "), "clave123 ")

    def test_los_mensajes_de_error_nunca_citan_la_clave(self):
        secreto = "MiClaveSuperSecreta"
        for value in (secreto[:4], secreto + "x" * 60, secreto + "\x01"):
            with self.subTest(value=len(value)):
                try:
                    wifi.validate_password(value)
                except wifi.InvalidParameter as e:
                    self.assertNotIn(secreto, str(e))
                    self.assertNotIn(value, str(e))


class ParsingTest(unittest.TestCase):
    def test_dispositivos_wifi(self):
        self.assertEqual(wifi.parse_wifi_devices(DEVICES_WITH_WIFI), ["wlan0"])
        self.assertEqual(wifi.parse_wifi_devices(DEVICES_WITHOUT_WIFI), [])

    def test_dispositivo_no_gestionado_no_cuenta(self):
        self.assertEqual(wifi.parse_wifi_devices("wlan0:wifi:unmanaged:\n"), [])
        self.assertEqual(wifi.parse_wifi_devices("wlan0:wifi:unavailable:\n"), [])

    def test_conexiones_activas_solo_inalambricas(self):
        raw = "CASA:802-11-wireless:wlan0\nCable:802-3-ethernet:eth0\n"
        self.assertEqual(wifi.parse_active_wifi_connections(raw), [("CASA", "wlan0")])

    def test_ssid_con_dos_puntos_escapados(self):
        """nmcli emite '\\:' dentro de un valor; un split ingenuo partiría el SSID."""
        raw = "RED\\:INVITADOS:802-11-wireless:wlan0\n"
        self.assertEqual(wifi.parse_active_wifi_connections(raw), [("RED:INVITADOS", "wlan0")])

    def test_clasificacion_de_errores(self):
        casos = {
            "Error: Secrets were required, but not provided.": wifi.STATUS_INVALID_PASSWORD,
            "Error: No network with SSID 'X' found.": wifi.STATUS_NOT_FOUND,
            "Error: Not authorized to control networking.": wifi.STATUS_NOT_AUTHORIZED,
            "Error: No Wi-Fi device found.": wifi.STATUS_NO_ADAPTER,
            "Error: Timeout expired (30 seconds)": wifi.STATUS_TIMEOUT,
            "Algo raro que nadie previó": wifi.STATUS_FAILED,
            "": wifi.STATUS_FAILED,
        }
        for stderr, expected in casos.items():
            with self.subTest(stderr=stderr[:30]):
                self.assertEqual(wifi.classify_error(stderr), expected)


class ConnectTest(unittest.TestCase):
    SSID = "SIMTRA-PATIO"
    SECRET = "clave-secreta-123"

    def setUp(self):
        wifi.reset_state()
        self.addCleanup(wifi.reset_state)
        network_info.reset_cache()
        self.addCleanup(network_info.reset_cache)

    # ── camino feliz ─────────────────────────────────────────────────────────

    def test_conexion_verificada(self):
        nmcli = FakeNmcli(active=(self.SSID,))
        result = wifi.connect(self.SSID, self.SECRET, runner=nmcli)

        self.assertEqual(result.status, wifi.STATUS_CONNECTED)
        self.assertEqual(result.ssid, self.SSID)

    def test_la_clave_viaja_por_stdin_nunca_en_argv(self):
        """
        Es la garantía central: en la línea de comandos la clave sería visible en
        `ps`, en /proc/<pid>/cmdline y en la auditoría del sistema.
        """
        nmcli = FakeNmcli(active=(self.SSID,))
        wifi.connect(self.SSID, self.SECRET, runner=nmcli)

        args, _timeout, stdin = nmcli.connect_call
        self.assertIsNotNone(args)
        self.assertNotIn(self.SECRET, args)
        self.assertIn("--ask", args)
        self.assertEqual(stdin, self.SECRET + "\n")

        # Y en NINGÚN argumento de NINGUNA de las llamadas.
        for argument in nmcli.every_argument():
            self.assertNotIn(self.SECRET, argument)

    def test_sin_clave_no_se_pide_secreto_ni_se_borra_el_perfil(self):
        """
        Campo vacío = reconectar con lo guardado. Borrar el perfil destruiría
        justamente el secreto que se quiere reutilizar.
        """
        nmcli = FakeNmcli(active=(self.SSID,))
        result = wifi.connect(self.SSID, "", runner=nmcli)

        args, _timeout, stdin = nmcli.connect_call
        self.assertEqual(result.status, wifi.STATUS_CONNECTED)
        self.assertNotIn("--ask", args)
        self.assertIsNone(stdin)
        self.assertEqual(nmcli.deleted_profiles(), [])

    def test_con_clave_se_borra_el_perfil_previo(self):
        """
        Sin esto, NetworkManager reutiliza el secreto guardado y la clave nueva
        que escribió el conductor se ignora en silencio.
        """
        nmcli = FakeNmcli(active=(self.SSID,))
        wifi.connect(self.SSID, self.SECRET, runner=nmcli)
        self.assertEqual(nmcli.deleted_profiles(), [self.SSID])

    def test_el_comando_lleva_timeout(self):
        nmcli = FakeNmcli(active=(self.SSID,))
        wifi.connect(self.SSID, self.SECRET, timeout=12, runner=nmcli)
        _args, timeout, _stdin = nmcli.connect_call
        self.assertGreaterEqual(timeout, 12)
        self.assertLess(timeout, 12 + 60)

    def test_conexion_exitosa_invalida_el_cache_de_red(self):
        """
        Sin invalidar, los 15 s de caché describirían la red ANTERIOR y la
        pantalla mostraría la IP vieja como si fuera la nueva.
        """
        network_info._cached = {"status": "connected", "connections": ["viejo"]}
        network_info._cached_at = 1e12   # caché "fresquísimo"

        nmcli = FakeNmcli(active=(self.SSID,))
        wifi.connect(self.SSID, self.SECRET, runner=nmcli)

        self.assertNotEqual(network_info._cached, {"status": "connected", "connections": ["viejo"]})

    # ── el código 0 NO basta ─────────────────────────────────────────────────

    def test_codigo_cero_sin_conexion_activa_no_es_exito(self):
        """
        nmcli puede devolver 0 y dejar la conexión sin activar. Responder
        `connected` ahí sería mentirle al conductor.
        """
        nmcli = FakeNmcli(connect=output(0), active=())
        result = wifi.connect(self.SSID, self.SECRET, runner=nmcli)
        self.assertEqual(result.status, wifi.STATUS_FAILED)

    def test_otra_red_activa_no_cuenta_como_exito(self):
        nmcli = FakeNmcli(connect=output(0), active=("OTRA-RED",))
        result = wifi.connect(self.SSID, self.SECRET, runner=nmcli)
        self.assertEqual(result.status, wifi.STATUS_FAILED)

    # ── fallos controlados ───────────────────────────────────────────────────

    def test_clave_incorrecta(self):
        nmcli = FakeNmcli(
            connect=output(4, stderr="Error: Secrets were required, but not provided."),
            active=(),
        )
        result = wifi.connect(self.SSID, self.SECRET, runner=nmcli)
        self.assertEqual(result.status, wifi.STATUS_INVALID_PASSWORD)
        self.assertNotIn(self.SECRET, result.detail)

    def test_red_inexistente(self):
        nmcli = FakeNmcli(
            connect=output(10, stderr="Error: No network with SSID 'X' found."), active=())
        self.assertEqual(
            wifi.connect(self.SSID, self.SECRET, runner=nmcli).status, wifi.STATUS_NOT_FOUND)

    def test_timeout(self):
        nmcli = FakeNmcli(connect=output(None, timed_out=True), active=())
        self.assertEqual(
            wifi.connect(self.SSID, self.SECRET, runner=nmcli).status, wifi.STATUS_TIMEOUT)

    def test_timeout_pero_la_conexion_llego_a_establecerse(self):
        """
        La asociación puede terminar justo después del timeout. Se verifica antes
        de darla por fallida, en vez de asumir el peor caso.
        """
        nmcli = FakeNmcli(connect=output(None, timed_out=True), active=(self.SSID,))
        self.assertEqual(
            wifi.connect(self.SSID, self.SECRET, runner=nmcli).status, wifi.STATUS_CONNECTED)

    def test_permisos_insuficientes(self):
        nmcli = FakeNmcli(
            connect=output(2, stderr="Error: Not authorized to control networking."), active=())
        self.assertEqual(
            wifi.connect(self.SSID, self.SECRET, runner=nmcli).status, wifi.STATUS_NOT_AUTHORIZED)

    def test_herramienta_ausente(self):
        """nmcli no instalado: estado propio, nunca una excepción."""
        nmcli = FakeNmcli()
        nmcli.__call__ = None   # no se usa: se corta en la primera llamada

        class SinNmcli(FakeNmcli):
            def __call__(self, args, timeout=None, stdin_text=None):
                self.calls.append((list(args), timeout, stdin_text))
                return output(None, missing=True)

        result = wifi.connect(self.SSID, self.SECRET, runner=SinNmcli())
        self.assertEqual(result.status, wifi.STATUS_UNAVAILABLE)

    def test_sin_adaptador_wifi(self):
        nmcli = FakeNmcli(devices=DEVICES_WITHOUT_WIFI)
        result = wifi.connect(self.SSID, self.SECRET, runner=nmcli)

        self.assertEqual(result.status, wifi.STATUS_NO_ADAPTER)
        # No se intentó conectar: no hay con qué.
        self.assertIsNone(nmcli.connect_call[0])

    def test_networkmanager_parado(self):
        """nmcli existe pero no puede listar dispositivos."""
        class NmDown(FakeNmcli):
            def __call__(self, args, timeout=None, stdin_text=None):
                self.calls.append((list(args), timeout, stdin_text))
                return output(8, stderr="Error: NetworkManager is not running.")

        self.assertEqual(
            wifi.connect(self.SSID, self.SECRET, runner=NmDown()).status, wifi.STATUS_UNAVAILABLE)

    # ── concurrencia ─────────────────────────────────────────────────────────

    def test_un_intento_a_la_vez(self):
        """
        Dos conexiones simultáneas sobre la misma tarjeta se pisan y dejan al
        equipo sin red. La segunda responde `busy` en vez de lanzarse.
        """
        resultados = []

        class Reentrante(FakeNmcli):
            def __call__(self, args, timeout=None, stdin_text=None):
                if "connect" in args and not resultados:
                    resultados.append(wifi.connect("OTRA-RED", "otra-clave-123", runner=self))
                return super().__call__(args, timeout, stdin_text)

        wifi.connect(self.SSID, self.SECRET, runner=Reentrante(active=(self.SSID,)))
        self.assertEqual(resultados[0].status, wifi.STATUS_BUSY)

    def test_el_estado_se_libera_siempre(self):
        nmcli = FakeNmcli(connect=output(1, stderr="boom"), active=())
        wifi.connect(self.SSID, self.SECRET, runner=nmcli)
        self.assertFalse(wifi.is_in_progress())

    # ── el secreto no sale por ningún lado ───────────────────────────────────

    def test_ninguna_respuesta_contiene_la_clave(self):
        casos = [
            FakeNmcli(active=(self.SSID,)),
            FakeNmcli(connect=output(4, stderr="Secrets were required"), active=()),
            FakeNmcli(connect=output(None, timed_out=True), active=()),
            FakeNmcli(devices=DEVICES_WITHOUT_WIFI),
        ]
        for nmcli in casos:
            with self.subTest(nmcli=nmcli.connect_result.returncode):
                wifi.reset_state()
                result = wifi.connect(self.SSID, self.SECRET, runner=nmcli)
                self.assertNotIn(self.SECRET, result.detail)
                self.assertNotIn(self.SECRET, str(result))

    def test_la_clave_no_aparece_en_los_logs(self):
        """
        Los tests corren con logging deshabilitado; aquí se reactiva a propósito
        para capturar TODO lo que el módulo escribiría en producción.
        """
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)

        # Peor caso: una versión de nmcli que repitiera la clave en su stderr.
        nmcli = FakeNmcli(
            connect=output(4, stderr=f"Error with psk '{self.SECRET}'"), active=())

        with self.assertLogs("simtra", level="DEBUG") as captured:
            wifi.connect(self.SSID, self.SECRET, runner=nmcli)

        registro = "\n".join(captured.output)
        self.assertNotIn(self.SECRET, registro)
        self.assertIn("***", registro)   # se depuró, no se omitió el error


if __name__ == "__main__":
    unittest.main()
