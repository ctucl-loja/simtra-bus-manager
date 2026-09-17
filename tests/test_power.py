"""
Apagado y REINICIO ordenados del dispositivo.

Ningún test apaga ni reinicia nada: `request_shutdown` y `request_reboot`
reciben el ejecutor y el planificador, así que se ejercita la lógica completa
con dobles. Lo que se comprueba:

  1. El comando NUNCA se arma con datos del cliente; sale de una constante o de
     SYSTEM_SHUTDOWN_COMMAND / SYSTEM_REBOOT_COMMAND.
  2. Apagado y reinicio son comandos DISTINTOS y no se confunden.
  3. Sin comando utilizable no se promete nada.
  4. La ejecución ocurre DESPUÉS de responder (planificada, no en línea).
  5. Dos peticiones seguidas no lanzan dos comandos, y las dos acciones se
     excluyen entre sí identificando cuál está pendiente.
  6. Un comando que falla —o una programación que falla— deja el equipo
     reintentable, nunca con un botón muerto.
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


class RebootCommandTest(unittest.TestCase):
    def setUp(self):
        self.original = os.environ.get("SYSTEM_REBOOT_COMMAND")
        self.addCleanup(self.restore)

    def restore(self):
        if self.original is None:
            os.environ.pop("SYSTEM_REBOOT_COMMAND", None)
        else:
            os.environ["SYSTEM_REBOOT_COMMAND"] = self.original

    def test_por_defecto_reinicia_no_apaga(self):
        """
        Forma exacta en la Raspberry Pi objetivo: `sudo -n /sbin/shutdown -r now`.
        `-r` (reinicio) y NUNCA `-h` (halt): confundirlos dejaría el bus apagado
        cuando el conductor pidió reiniciar.
        """
        os.environ.pop("SYSTEM_REBOOT_COMMAND", None)
        command = power.reboot_command()
        self.assertEqual(command, ["sudo", "-n", "/sbin/shutdown", "-r", "now"])
        self.assertIn("-r", command)
        self.assertNotIn("-h", command)

    def test_apagado_y_reinicio_son_comandos_distintos(self):
        os.environ.pop("SYSTEM_REBOOT_COMMAND", None)
        os.environ.pop("SYSTEM_SHUTDOWN_COMMAND", None)
        self.assertNotEqual(power.reboot_command(), power.shutdown_command())

    def test_se_puede_sustituir_por_entorno(self):
        os.environ["SYSTEM_REBOOT_COMMAND"] = "systemctl reboot"
        self.assertEqual(power.reboot_command(), ["systemctl", "reboot"])

    def test_valor_vacio_o_invalido_cae_al_por_defecto(self):
        por_defecto = ["sudo", "-n", "/sbin/shutdown", "-r", "now"]
        for value in ("", "   ", "'sin cerrar"):
            with self.subTest(value=value):
                os.environ["SYSTEM_REBOOT_COMMAND"] = value
                self.assertEqual(power.reboot_command(), por_defecto)

    def test_el_comando_no_pasa_por_una_shell(self):
        os.environ["SYSTEM_REBOOT_COMMAND"] = "/sbin/shutdown -r now"
        self.assertIsInstance(power.reboot_command(), list)


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

    # ── programación fallida ─────────────────────────────────────────────────

    def test_si_no_se_puede_programar_el_estado_se_libera(self):
        """
        Un planificador que revienta no puede dejar el botón bloqueado: si no se
        programó nada, no hay nada pendiente y el conductor debe poder
        reintentar.
        """
        def scheduler_roto(delay, action):
            raise RuntimeError("no se pudo crear el hilo")

        result = power.request_shutdown(runner=self.runner, scheduler=scheduler_roto)

        self.assertEqual(result.status, power.STATUS_UNAVAILABLE)
        self.assertFalse(power.is_scheduled())
        self.assertIsNone(power.pending_action())
        self.assertEqual(self.executed, [])

        # Y se puede reintentar de inmediato.
        self.assertEqual(
            power.request_shutdown(runner=self.runner, scheduler=self.scheduler).status,
            power.STATUS_SCHEDULED,
        )


class RequestRebootTest(unittest.TestCase):
    """
    Reinicio: mismo contrato que el apagado, más la exclusión mutua entre los
    dos. Nada se reinicia — el ejecutor es un doble.
    """

    def setUp(self):
        power.reset_state()
        self.addCleanup(power.reset_state)

        self.originals = {
            name: os.environ.get(name)
            for name in ("SYSTEM_SHUTDOWN_COMMAND", "SYSTEM_REBOOT_COMMAND")
        }
        # `true` existe en cualquier Linux y no hace nada: sirve como comando
        # "disponible" sin tocar el equipo.
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "true"
        os.environ["SYSTEM_REBOOT_COMMAND"] = "true"

        def restore():
            for name, value in self.originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)

        self.scheduled = []
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

    def test_programa_el_reinicio_y_responde_antes_de_ejecutarlo(self):
        result = power.request_reboot(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(result.status, power.STATUS_SCHEDULED)
        self.assertEqual(result.action, power.ACTION_REBOOT)
        self.assertEqual(result.pending_action, power.ACTION_REBOOT)
        self.assertEqual(result.scheduled_in_seconds, power.GRACE_SECONDS)
        # Responder NO es haber reiniciado: al devolver la respuesta el comando
        # todavía no se ejecutó.
        self.assertEqual(self.executed, [])
        self.assertEqual(len(self.scheduled), 1)

        self.fire_all()
        self.assertEqual(self.executed, [["true"]])

    def test_el_detalle_no_afirma_que_el_reinicio_termino(self):
        result = power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        self.assertIn("reiniciará", result.detail)
        self.assertNotIn("reinició", result.detail)

    def test_ejecuta_el_comando_de_reinicio_no_el_de_apagado(self):
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "/bin/echo"
        os.environ["SYSTEM_REBOOT_COMMAND"] = "true"
        power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        self.fire_all()
        self.assertEqual(self.executed[0], power.reboot_command())
        self.assertNotEqual(self.executed[0], power.shutdown_command())

    def test_sin_comando_disponible_no_promete_reinicio(self):
        os.environ["SYSTEM_REBOOT_COMMAND"] = "/no/existe/este/binario"
        result = power.request_reboot(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(result.status, power.STATUS_UNAVAILABLE)
        self.assertEqual(result.action, power.ACTION_REBOOT)
        self.assertIsNone(result.scheduled_in_seconds)
        self.assertEqual(self.scheduled, [])
        self.assertFalse(power.is_scheduled())

    def test_segunda_peticion_no_lanza_un_segundo_reinicio(self):
        primera = power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        segunda = power.request_reboot(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(primera.status, power.STATUS_SCHEDULED)
        self.assertEqual(segunda.status, power.STATUS_ALREADY_SCHEDULED)
        self.assertEqual(len(self.scheduled), 1)

    def test_comando_fallido_permite_reintentar(self):
        self.runner_ok = False
        power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        self.fire_all()

        self.assertFalse(power.is_scheduled())
        self.assertIsNone(power.pending_action())
        self.runner_ok = True
        self.assertEqual(
            power.request_reboot(runner=self.runner, scheduler=self.scheduler).status,
            power.STATUS_SCHEDULED,
        )


class PowerActionConflictTest(unittest.TestCase):
    """
    Apagado y reinicio NO pueden estar programados a la vez, y la respuesta debe
    decir cuál está realmente pendiente: si el conductor toca «Reiniciar»
    cuando ya hay un apagado en curso, la pantalla tiene que anunciar el
    apagado — si no, se queda esperando una pantalla que no va a volver.
    """

    def setUp(self):
        power.reset_state()
        self.addCleanup(power.reset_state)

        self.originals = {
            name: os.environ.get(name)
            for name in ("SYSTEM_SHUTDOWN_COMMAND", "SYSTEM_REBOOT_COMMAND")
        }
        os.environ["SYSTEM_SHUTDOWN_COMMAND"] = "true"
        os.environ["SYSTEM_REBOOT_COMMAND"] = "true"

        def restore():
            for name, value in self.originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)

        self.scheduled = []
        self.executed = []

    def scheduler(self, delay, action):
        self.scheduled.append((delay, action))

    def runner(self, command):
        self.executed.append(command)
        return True

    def test_reinicio_sobre_apagado_pendiente_identifica_el_apagado(self):
        power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        result = power.request_reboot(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(result.status, power.STATUS_ALREADY_SCHEDULED)
        self.assertEqual(result.action, power.ACTION_REBOOT)        # lo que se pidió
        self.assertEqual(result.pending_action, power.ACTION_SHUTDOWN)  # lo que pasará
        self.assertIn("apagado", result.detail.lower())
        self.assertNotIn("reinicio", result.detail.lower())

    def test_apagado_sobre_reinicio_pendiente_identifica_el_reinicio(self):
        power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        result = power.request_shutdown(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(result.status, power.STATUS_ALREADY_SCHEDULED)
        self.assertEqual(result.action, power.ACTION_SHUTDOWN)
        self.assertEqual(result.pending_action, power.ACTION_REBOOT)
        self.assertIn("reinicio", result.detail.lower())

    def test_nunca_se_programan_las_dos_acciones(self):
        power.request_shutdown(runner=self.runner, scheduler=self.scheduler)
        power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        power.request_reboot(runner=self.runner, scheduler=self.scheduler)
        power.request_shutdown(runner=self.runner, scheduler=self.scheduler)

        self.assertEqual(len(self.scheduled), 1)
        for _delay, action in self.scheduled:
            action()
        self.assertEqual(self.executed, [power.shutdown_command()])
        self.assertEqual(power.pending_action(), power.ACTION_SHUTDOWN)


if __name__ == "__main__":
    unittest.main()
