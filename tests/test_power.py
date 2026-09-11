"""
Apagado ordenado del dispositivo.

Ningún test apaga nada: `request_shutdown` recibe el ejecutor y el planificador,
así que se ejercita la lógica completa con dobles. Lo que se comprueba:

  1. El comando NUNCA se arma con datos del cliente; sale de una constante o
     de SYSTEM_SHUTDOWN_COMMAND.
  2. Sin comando utilizable no se promete un apagado.
  3. El corte ocurre DESPUÉS de responder (planificado, no en línea).
  4. Dos peticiones seguidas no lanzan dos apagados.
  5. Un comando que falla deja el equipo reintentable.
"""

import os
import unittest

import _bootstrap  # noqa: F401

import power


class ShutdownCommandTest(unittest.TestCase):
    def setUp(self):
        self.original = os.environ.get("SYSTEM_SHUTDOWN_COMMAND")
        self.addCleanup(self.restore)

    def restore(self):
        if self.original is None:
            os.environ.pop("SYSTEM_SHUTDOWN_COMMAND", None)
        else:
            os.environ["SYSTEM_SHUTDOWN_COMMAND"] = self.original

    def set_command(self, value):
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = value

    def test_por_defecto_apaga_no_reinicia(self):
        os.environ.pop("SYSTEM_SHUTDOWN_COMMAND", None)
        command = power.shutdown_command()
        self.assertEqual(command, ["sudo", "-n", "/sbin/shutdown", "-h", "now"])
        self.assertNotIn("-r", command)   # nunca un reinicio

    def test_se_puede_sustituir_por_entorno(self):
        self.set_command("systemctl poweroff")
        self.assertEqual(power.shutdown_command(), ["systemctl", "poweroff"])

    def test_valor_vacio_o_invalido_cae_al_por_defecto(self):
        por_defecto = ["sudo", "-n", "/sbin/shutdown", "-h", "now"]
        for value in ("", "   ", "'sin cerrar"):
            with self.subTest(value=value):
                self.set_command(value)
                self.assertEqual(power.shutdown_command(), por_defecto)

    def test_el_comando_no_pasa_por_una_shell(self):
        """Lista de argumentos, no una cadena: no hay interpretación de shell."""
        self.set_command("/sbin/shutdown -h now")
        self.assertIsInstance(power.shutdown_command(), list)

    def test_comando_inexistente_no_esta_disponible(self):
        self.assertFalse(power.command_is_available(["/no/existe/este/binario"]))
        self.assertFalse(power.command_is_available([]))


class RequestShutdownTest(unittest.TestCase):
    def setUp(self):
        power.reset_state()
        self.addCleanup(power.reset_state)

        self.original = os.environ.get("SYSTEM_SHUTDOWN_COMMAND")
        # `true` existe en cualquier Linux y no hace nada: sirve como comando
        # "disponible" sin tocar el equipo.
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "true"

        def restore():
            if self.original is None:
                os.environ.pop("SYSTEM_SHUTDOWN_COMMAND", None)
            else:
                os.environ["SYSTEM_SHUTDOWN_COMMAND"] = self.original

        self.addCleanup(restore)

        self.scheduled = []    # (delay, acción) — nada se ejecuta solo
        self.executed = []
        self.runner_ok = True

    def scheduler(self, delay, action):
        self.scheduled.append((delay, action))

    def runner(self, command):
        self.executed.append(command)
        return self.runner_ok

    def fire_all(self):
        for _delay, action in list(self.scheduled):
            action()

    # ── camino feliz ─────────────────────────────────────────────────────────

    def test_programa_el_apagado_y_responde_antes_de_ejecutarlo(self):
        result = power.request_shutdown(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(result.status, power.STATUS_SCHEDULED)
        self.assertEqual(result.scheduled_in_seconds, power.GRACE_SECONDS)
        self.assertGreater(power.GRACE_SECONDS, 0)
        # Al responder todavía NO se ejecutó: la pantalla alcanza a recibir el aviso.
        self.assertEqual(self.executed, [])
        self.assertEqual(len(self.scheduled), 1)

        self.fire_all()
        self.assertEqual(self.executed, [["true"]])

    def test_el_comando_ejecutado_es_el_del_equipo(self):
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "true"
        power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        self.fire_all()
        self.assertEqual(self.executed[0], power.shutdown_command())

    # ── sin comando utilizable ───────────────────────────────────────────────

    def test_sin_comando_disponible_no_promete_apagado(self):
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "/no/existe/este/binario"
        result = power.request_shutdown(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(result.status, power.STATUS_UNAVAILABLE)
        self.assertIsNone(result.scheduled_in_seconds)
        self.assertEqual(self.scheduled, [])
        self.assertEqual(self.executed, [])
        self.assertFalse(power.is_scheduled())   # no queda bloqueado

    # ── idempotencia ─────────────────────────────────────────────────────────

    def test_segunda_peticion_no_lanza_un_segundo_apagado(self):
        primera = power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        segunda = power.request_shutdown(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(primera.status, power.STATUS_SCHEDULED)
        self.assertEqual(segunda.status, power.STATUS_ALREADY_SCHEDULED)
        self.assertIsNone(segunda.scheduled_in_seconds)
        self.assertEqual(len(self.scheduled), 1)

        self.fire_all()
        self.assertEqual(len(self.executed), 1)

    # ── fallo del comando ────────────────────────────────────────────────────

    def test_comando_fallido_permite_reintentar(self):
        self.runner_ok = False
        power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        self.fire_all()

        # El equipo sigue encendido: el botón no puede quedar muerto.
        self.assertFalse(power.is_scheduled())
        self.runner_ok = True
        result = power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        self.assertEqual(result.status, power.STATUS_SCHEDULED)

    def test_apagado_exitoso_mantiene_el_estado_programado(self):
        power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        self.fire_all()
        self.assertTrue(power.is_scheduled())

    def test_run_shutdown_no_lanza_con_un_binario_inexistente(self):
        self.assertFalse(power.run_shutdown(["/no/existe/este/binario"]))


if __name__ == "__main__":
    unittest.main()
