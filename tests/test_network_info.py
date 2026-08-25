"""
Deteccion de red del dispositivo.

Todo se ejercita con salidas simuladas: ningun test depende de las interfaces
reales de la maquina donde corre, ni ejecuta comandos del sistema.
"""

import json
import subprocess
import unittest

import _bootstrap  # noqa: F401

from services import network_info as ni


def ip_json(*interfaces) -> str:
    return json.dumps(list(interfaces))


def iface(name, ipv4=(), flags=("BROADCAST", "MULTICAST", "UP", "LOWER_UP"),
          link_type="ether", operstate="UP", family="inet"):
    return {
        "ifname": name,
        "flags": list(flags),
        "operstate": operstate,
        "link_type": link_type,
        "addr_info": [{"family": family, "local": ip, "prefixlen": 24} for ip in ipv4],
    }


LOOPBACK = {
    "ifname": "lo",
    "flags": ["LOOPBACK", "UP", "LOWER_UP"],
    "operstate": "UNKNOWN",
    "link_type": "loopback",
    "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}],
}

# Sin nmcli ni /sys: obliga a los caminos de respaldo.
NO_SSID = lambda _iface: None
NOT_WIRELESS = lambda _iface: False


class ValidIpv4Test(unittest.TestCase):
    def test_direcciones_validas(self):
        for value in ("192.168.1.50", "10.0.0.1", "0.0.0.0", "255.255.255.255"):
            self.assertTrue(ni.valid_ipv4(value), value)

    def test_direcciones_invalidas(self):
        casos = [
            "127.0.0.1", "127.1.1.1",          # loopback nunca se reporta
            "256.1.1.1", "192.168.1", "192.168.1.1.1", "192.168.1.-1",
            "192.168.1.a", "", "   ", "abc", None, 42, [], {},
            "192.168.01.1 extra", "fe80::1",
        ]
        for value in casos:
            self.assertFalse(ni.valid_ipv4(value), repr(value))


class ParseIpAddressesTest(unittest.TestCase):
    def test_wifi_con_ipv4(self):
        result = ni.parse_ip_addresses(ip_json(iface("wlan0", ["192.168.1.50"])))
        self.assertEqual(result, [{"interface": "wlan0", "link_type": "ether",
                                   "ipv4": ["192.168.1.50"]}])

    def test_excluye_loopback(self):
        result = ni.parse_ip_addresses(ip_json(LOOPBACK, iface("eth0", ["192.168.1.51"])))
        self.assertEqual([i["interface"] for i in result], ["eth0"])

    def test_excluye_127_aunque_este_en_otra_interfaz(self):
        result = ni.parse_ip_addresses(ip_json(iface("eth0", ["127.0.0.1", "192.168.1.51"])))
        self.assertEqual(result[0]["ipv4"], ["192.168.1.51"])

    def test_excluye_interfaces_caidas(self):
        caida = iface("eth0", ["192.168.1.51"], flags=["BROADCAST"], operstate="DOWN")
        self.assertEqual(ni.parse_ip_addresses(ip_json(caida)), [])

    def test_interfaz_activa_sin_ipv4_se_conserva_en_el_parseo(self):
        result = ni.parse_ip_addresses(ip_json(iface("eth0")))
        self.assertEqual(result[0]["ipv4"], [])

    def test_ignora_ipv6(self):
        entry = iface("wlan0", ["192.168.1.50"])
        entry["addr_info"].append({"family": "inet6", "local": "fe80::1", "prefixlen": 64})
        result = ni.parse_ip_addresses(ip_json(entry))
        self.assertEqual(result[0]["ipv4"], ["192.168.1.50"])

    def test_ipv4_invalida_se_descarta(self):
        result = ni.parse_ip_addresses(ip_json(iface("eth0", ["999.1.1.1", "192.168.1.51"])))
        self.assertEqual(result[0]["ipv4"], ["192.168.1.51"])

    def test_deduplica_direcciones(self):
        result = ni.parse_ip_addresses(ip_json(iface("eth0", ["192.168.1.51", "192.168.1.51"])))
        self.assertEqual(result[0]["ipv4"], ["192.168.1.51"])

    def test_salida_vacia_o_ausente_es_none(self):
        for raw in (None, "", "   "):
            self.assertIsNone(ni.parse_ip_addresses(raw))

    def test_json_invalido_es_none(self):
        for raw in ("no soy json", "{", "[1,2", "<html>error</html>"):
            self.assertIsNone(ni.parse_ip_addresses(raw))

    def test_json_valido_con_forma_inesperada_es_none(self):
        for raw in ('{"ifname": "eth0"}', '"cadena"', "42", "null"):
            self.assertIsNone(ni.parse_ip_addresses(raw))

    def test_lista_vacia_no_es_none(self):
        """[] es 'no hay interfaces', distinto de 'no se pudo consultar'."""
        self.assertEqual(ni.parse_ip_addresses("[]"), [])

    def test_entradas_basura_dentro_de_la_lista(self):
        raw = json.dumps([None, "x", 42, {"sin_ifname": True}, {"ifname": "  "},
                          iface("eth0", ["192.168.1.51"])])
        result = ni.parse_ip_addresses(raw)
        self.assertEqual([i["interface"] for i in result], ["eth0"])

    def test_campos_con_tipos_inesperados(self):
        entry = {"ifname": "eth0", "flags": "UP", "operstate": "UP",
                 "link_type": 5, "addr_info": "no soy lista"}
        result = ni.parse_ip_addresses(json.dumps([entry]))
        self.assertEqual(result, [{"interface": "eth0", "link_type": "", "ipv4": []}])


class NmcliParsingTest(unittest.TestCase):
    def test_formato_terse(self):
        raw = ("wlan0:wifi:connected:MiRed\n"
               "eth0:ethernet:connected:netplan-eth0\n"
               "lo:loopback:connected (externally):lo\n")
        devices = ni.parse_nmcli_devices(raw)
        self.assertEqual(devices["wlan0"]["type"], "wifi")
        self.assertEqual(devices["wlan0"]["connection"], "MiRed")
        self.assertEqual(devices["eth0"]["type"], "ethernet")

    def test_ssid_con_dos_puntos_escapados(self):
        devices = ni.parse_nmcli_devices("wlan0:wifi:connected:Red\\:Rara\n")
        self.assertEqual(devices["wlan0"]["connection"], "Red:Rara")

    def test_ssid_con_espacios(self):
        devices = ni.parse_nmcli_devices("wlan0:wifi:connected:Xtrim Diaz3 5G\n")
        self.assertEqual(devices["wlan0"]["connection"], "Xtrim Diaz3 5G")

    def test_entradas_invalidas_devuelven_diccionario_vacio(self):
        for raw in (None, "", "   ", 42, [], {}):
            self.assertEqual(ni.parse_nmcli_devices(raw), {})

    def test_lineas_incompletas_se_ignoran(self):
        devices = ni.parse_nmcli_devices("solo-un-campo\n:sin-device\n\nwlan0:wifi\n")
        self.assertEqual(list(devices), ["wlan0"])
        self.assertEqual(devices["wlan0"]["connection"], "")


class ClassifyTest(unittest.TestCase):
    def test_nmcli_manda(self):
        self.assertEqual(ni.classify_connection("wifi", "ether", False), ni.TYPE_WIFI)
        self.assertEqual(ni.classify_connection("ethernet", "ether", True), ni.TYPE_ETHERNET)

    def test_wifi_p2p_no_es_wifi(self):
        self.assertEqual(ni.classify_connection("wifi-p2p", "ether", False), ni.TYPE_OTHER)

    def test_respaldo_por_flag_inalambrico(self):
        self.assertEqual(ni.classify_connection("", "ether", True), ni.TYPE_WIFI)

    def test_respaldo_por_link_type(self):
        self.assertEqual(ni.classify_connection("", "ether", False), ni.TYPE_ETHERNET)

    def test_desconocido_es_other(self):
        self.assertEqual(ni.classify_connection("", "ppp", False), ni.TYPE_OTHER)
        self.assertEqual(ni.classify_connection("bridge", "ether", False), ni.TYPE_OTHER)


class BuildNetworkInfoTest(unittest.TestCase):
    def build(self, ip_raw, nmcli_raw=None, ssid=NO_SSID, wireless=NOT_WIRELESS):
        return ni.build_network_info(ip_raw, nmcli_raw, ssid_lookup=ssid, wireless_lookup=wireless)

    def test_wifi_con_ssid_e_ipv4(self):
        info = self.build(ip_json(iface("wlan0", ["192.168.1.50"])), "wlan0:wifi:connected:MiRed\n")
        self.assertEqual(info["status"], ni.STATUS_CONNECTED)
        self.assertEqual(info["connections"], [{
            "type": "wifi", "interface": "wlan0", "name": "MiRed", "ipv4": ["192.168.1.50"],
        }])

    def test_ethernet_activa(self):
        info = self.build(ip_json(iface("eth0", ["192.168.1.51"])), "eth0:ethernet:connected:cable\n")
        self.assertEqual(info["connections"], [{
            "type": "ethernet", "interface": "eth0", "name": None, "ipv4": ["192.168.1.51"],
        }])

    def test_ambas_simultaneas(self):
        info = self.build(
            ip_json(LOOPBACK, iface("wlan0", ["192.168.1.50"]), iface("eth0", ["192.168.1.51"])),
            "wlan0:wifi:connected:MiRed\neth0:ethernet:connected:cable\n",
        )
        self.assertEqual(info["status"], ni.STATUS_CONNECTED)
        self.assertEqual([(c["type"], c["interface"]) for c in info["connections"]],
                         [("wifi", "wlan0"), ("ethernet", "eth0")])

    def test_interfaz_activa_sin_ipv4_no_se_reporta(self):
        info = self.build(ip_json(iface("eth0")), "eth0:ethernet:connected:cable\n")
        self.assertEqual(info["status"], ni.STATUS_DISCONNECTED)
        self.assertEqual(info["connections"], [])

    def test_solo_loopback_es_disconnected(self):
        info = self.build(ip_json(LOOPBACK))
        self.assertEqual(info["status"], ni.STATUS_DISCONNECTED)

    def test_sin_interfaces_es_disconnected(self):
        self.assertEqual(self.build("[]")["status"], ni.STATUS_DISCONNECTED)

    def test_json_invalido_es_unavailable(self):
        for raw in ("no json", "", None, "{"):
            info = self.build(raw)
            self.assertEqual(info["status"], ni.STATUS_UNAVAILABLE)
            self.assertEqual(info["connections"], [])

    def test_sin_nmcli_usa_el_flag_inalambrico(self):
        info = self.build(ip_json(iface("wlp2s0", ["192.168.1.50"])),
                          nmcli_raw=None, wireless=lambda i: i == "wlp2s0")
        self.assertEqual(info["connections"][0]["type"], "wifi")

    def test_sin_nmcli_ni_flag_es_ethernet_por_link_type(self):
        info = self.build(ip_json(iface("enp3s0", ["192.168.1.51"])))
        self.assertEqual(info["connections"][0]["type"], "ethernet")

    def test_nombres_no_convencionales(self):
        """eno2/wlo1: nombres reales que rompen cualquier heuristica por nombre."""
        info = self.build(
            ip_json(iface("wlo1", ["192.168.1.16"]), iface("eno2", ["192.168.1.34"])),
            "wlo1:wifi:connected:Red Casa\neno2:ethernet:connected:netplan-eno2\n",
        )
        self.assertEqual([(c["type"], c["interface"], c["name"]) for c in info["connections"]],
                         [("wifi", "wlo1", "Red Casa"), ("ethernet", "eno2", None)])

    def test_ssid_no_disponible_es_none(self):
        info = self.build(ip_json(iface("wlan0", ["192.168.1.50"])),
                          nmcli_raw=None, wireless=lambda _i: True, ssid=NO_SSID)
        self.assertEqual(info["connections"][0]["type"], "wifi")
        self.assertIsNone(info["connections"][0]["name"])

    def test_ssid_por_iwgetid_cuando_nmcli_no_lo_da(self):
        info = self.build(ip_json(iface("wlan0", ["192.168.1.50"])),
                          nmcli_raw="wlan0:wifi:connected:\n",
                          ssid=lambda i: "DesdeIwgetid" if i == "wlan0" else None)
        self.assertEqual(info["connections"][0]["name"], "DesdeIwgetid")

    def test_ethernet_nunca_expone_el_perfil_de_nmcli(self):
        info = self.build(ip_json(iface("eth0", ["192.168.1.51"])),
                          "eth0:ethernet:connected:PerfilPrivado\n")
        self.assertIsNone(info["connections"][0]["name"])

    def test_tipo_desconocido_es_other(self):
        info = self.build(ip_json(iface("tun0", ["10.8.0.2"], link_type="none")))
        self.assertEqual(info["connections"][0]["type"], "other")

    def test_multiples_ipv4_en_una_interfaz(self):
        info = self.build(ip_json(iface("eth0", ["192.168.1.51", "10.0.0.5"])))
        self.assertEqual(info["connections"][0]["ipv4"], ["192.168.1.51", "10.0.0.5"])

    def test_la_respuesta_no_filtra_datos_sensibles(self):
        info = self.build(ip_json(iface("wlan0", ["192.168.1.50"])), "wlan0:wifi:connected:MiRed\n")
        permitidas = {"type", "interface", "name", "ipv4"}
        for connection in info["connections"]:
            self.assertEqual(set(connection), permitidas)
        self.assertEqual(set(info), {"status", "connections"})


class RunCommandTest(unittest.TestCase):
    """La capa de ejecucion nunca lanza: cualquier fallo se traduce a None."""

    def setUp(self):
        self._real_run = ni.subprocess.run
        self.addCleanup(lambda: setattr(ni.subprocess, "run", self._real_run))

    def patch(self, side_effect=None, returncode=0, stdout="", stderr=""):
        def fake_run(args, **kwargs):
            self.kwargs = kwargs
            self.args = args
            if side_effect:
                raise side_effect
            return subprocess.CompletedProcess(args, returncode, stdout, stderr)
        ni.subprocess.run = fake_run

    def test_comando_ausente(self):
        self.patch(side_effect=FileNotFoundError("no existe"))
        self.assertIsNone(ni.run_command(["ip", "-j", "address"]))

    def test_timeout(self):
        self.patch(side_effect=subprocess.TimeoutExpired(cmd="ip", timeout=3))
        self.assertIsNone(ni.run_command(["ip", "-j", "address"]))

    def test_permisos_u_otro_oserror(self):
        self.patch(side_effect=PermissionError("denegado"))
        self.assertIsNone(ni.run_command(["ip"]))

    def test_codigo_de_salida_distinto_de_cero(self):
        self.patch(returncode=1, stderr="error")
        self.assertIsNone(ni.run_command(["ip"]))

    def test_salida_correcta(self):
        self.patch(stdout="[]")
        self.assertEqual(ni.run_command(["ip"]), "[]")

    def test_nunca_usa_shell_y_siempre_lleva_timeout(self):
        self.patch(stdout="ok")
        ni.run_command(["ip", "-j", "address"])
        self.assertIs(self.kwargs["shell"], False)
        self.assertEqual(self.kwargs["timeout"], ni.COMMAND_TIMEOUT_SECONDS)
        self.assertIsInstance(self.args, list)

    def test_sistema_sin_ip_devuelve_unavailable(self):
        """Equivale a ejecutar fuera de Linux: ninguna herramienta disponible."""
        self.patch(side_effect=FileNotFoundError())
        ni.reset_cache()
        self.addCleanup(ni.reset_cache)
        info = ni.collect_network_info()
        self.assertEqual(info["status"], ni.STATUS_UNAVAILABLE)
        self.assertEqual(info["connections"], [])

    def test_interfaz_con_nombre_sospechoso_no_llega_al_comando(self):
        self.patch(stdout="ssid")
        for name in ("", "a b", "eth0; rm -rf /", "../../etc", "x" * 40, None):
            self.assertIsNone(ni.read_ssid(name), repr(name))
        self.assertFalse(ni.interface_is_wireless("eth0; rm -rf /"))


class CacheTest(unittest.TestCase):
    def setUp(self):
        ni.reset_cache()
        self.addCleanup(ni.reset_cache)
        self._real = ni.collect_network_info
        self.addCleanup(lambda: setattr(ni, "collect_network_info", self._real))

    def test_no_ejecuta_comandos_en_cada_peticion(self):
        llamadas = []
        ni.collect_network_info = lambda: (llamadas.append(1),
                                           {"status": "connected", "connections": []})[1]
        for _ in range(5):
            ni.get_network_info()
        self.assertEqual(len(llamadas), 1)

    def test_reset_invalida_el_cache(self):
        llamadas = []
        ni.collect_network_info = lambda: (llamadas.append(1),
                                           {"status": "connected", "connections": []})[1]
        ni.get_network_info()
        ni.reset_cache()
        ni.get_network_info()
        self.assertEqual(len(llamadas), 2)

    def test_es_thread_safe(self):
        import threading
        ni.collect_network_info = lambda: {"status": "connected", "connections": []}
        resultados = []
        hilos = [threading.Thread(target=lambda: resultados.append(ni.get_network_info()))
                 for _ in range(20)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=5)
        self.assertEqual(len(resultados), 20)
        self.assertTrue(all(r["status"] == "connected" for r in resultados))


if __name__ == "__main__":
    unittest.main()
