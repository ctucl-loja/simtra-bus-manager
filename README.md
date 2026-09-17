# SIMTRA Bus Manager

Microservicio de gestión de flotas de buses corriendo en Raspberry Pi. Compuesto por una API REST (FastAPI), un monitor de puntos de control y un loader de datos hacia el backend principal.

---

## Estructura de servicios

| Servicio | Descripción |
|---|---|
| `simtra-bus-manager` | API FastAPI — GPS, checkpoints y pasajeros |
| `simtra-bus-monitor` | Monitor de geofencing y puntos de control |
| `simtra-bus-loader` | Sincronización de datos recopilados al backend |

---

## Instalación

```bash
# Dependencias del sistema (reproductor de audio de los anuncios de voz)
sudo apt install mpg123

# Clonar el repositorio en la RPi
cd /home/admin/
git clone <repo-url> simtra-bus-manager
cd simtra-bus-manager

# Crear entorno virtual e instalar dependencias
python3 -m venv /home/admin/env
source /home/admin/env/bin/activate
pip install -r requirements.txt
```

---

## Ejecución en desarrollo

```bash
# API principal
uvicorn main:app --reload

# Monitor de puntos de control
python ./services/bus_monitor.py

# Loader de datos
python ./services/data_loader.py

# Simulación de movimiento GPS
python ./scripts/navigation_simulation.py
```

---

## Ejecución en producción

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

> En producción se gestiona con `systemd` — ver sección de servicios más abajo.

---

## Prueba rápida de la API

```bash
curl http://192.168.1.14:8000/api/gps/last_position
```

Documentación interactiva disponible en: `http://192.168.1.14:8000/docs`

---

## Rutas del proyecto

| Entorno | Ruta |
|---|---|
| Windows (desarrollo) | `C:\Users\ctucl\Documents\Python\simtra-bus-manager` |
| Raspberry Pi (producción) | `/home/admin/simtra-bus-manager/` |

---

## Tests

Suite de `unittest` (biblioteca estandar) sobre la logica critica: parsing de
horarios, contexto temporal, lecturas GPS, ciclo de vida de las marcaciones,
cliente HTTP remoto, deteccion de red, conexion Wi-Fi, energia, recarga del
itinerario y coordinacion con el monitor.

```bash
python3 -m unittest discover -s tests -t tests
```

No requiere variables de entorno ni red: `tests/_bootstrap.py` sustituye
`python-dotenv` y `gTTS` por stubs cuando no estan instalados, y el cliente
remoto, la deteccion de red, la conexion Wi-Fi y los comandos de energia se
ejercitan con dobles inyectados — **ningun test apaga, reinicia ni toca la red
de la maquina donde corre**.

### Dos niveles, y por que importa la diferencia

| Archivo | Que ejercita | Dependencias |
|---|---|---|
| Todos menos el siguiente | Funciones puras y servicios, con stubs | Ninguna |
| `tests/test_api_endpoints.py` | **Endpoints reales**: FastAPI + Pydantic + SQLAlchemy sobre SQLite temporal | `fastapi`, `sqlalchemy`, `httpx` |

Los tests unitarios **no validan los endpoints**: no importan `main.py` y no
ejercitan ni el enrutado, ni los `response_model`, ni la persistencia. Para eso
esta `test_api_endpoints.py`, que se **salta con un motivo visible** si faltan
las dependencias, para que la suite siga corriendo en una maquina pelada:

```bash
pip install fastapi sqlalchemy httpx
python3 -m unittest discover -s tests -t tests   # ya sin "skipped"
```

Cada test de integracion usa una base SQLite **temporal y propia** (nunca
`./app.db`) y un doble del backend remoto.

---

## Eventos locales (`/api/events`)

`/api/events` es el **canal local de eventos entre los procesos instalados en la
Raspberry Pi**. No sale a internet: los tres servicios escriben y leen contra el
FastAPI local. Los eventos quedan en SQLite y sirven además como bitácora de la
jornada.

| Evento | Lo emite | Lo consume |
|---|---|---|
| `checkpoint_arrival` | `bus_monitor.py` | `bus-display` (aviso de llegada) |
| `dispatch_refreshed` | `main.py`, tras una recarga manual | `bus_monitor.py` (adopta el itinerario nuevo) |

### `checkpoint_arrival`

Se emite **una sola vez por cada checkpoint realmente aceptado y persistido** por
`bus_monitor.py`. Una geocerca descartada —por horario, por pertenecer a un step
que no ha comenzado o por secuencia inconsistente— **no genera evento**, y por lo
tanto no genera aviso en la pantalla del conductor.

```text
GPS → geocerca → validación temporal → validación de secuencia
    → RESERVA → report_checkpoint (indispensable) → CONFIRMACIÓN
    → report_dispatch_checkpoint (secundaria) → checkpoint_arrival → audio
```

**El evento solo existe si la marcación se persistió.** El ciclo de vida de un
checkpoint tiene tres estados distintos:

| Estado | Significado | Elegible |
|---|---|---|
| Reservado | un hilo se lo adjudicó y está persistiendo | no |
| Confirmado | `report_checkpoint` respondió OK | no, cerrado por el día |
| Liberado | la persistencia falló; la reserva se deshizo | sí, en la próxima entrada |

`report_checkpoint` es la escritura **indispensable**: alimenta la cola que
`data_loader` sube al backend. Si falla, se libera la reserva y no hay evento ni
audio — un fallo transitorio no puede costar la marcación del día ni anunciar al
conductor una llegada que no se guardó.

`report_dispatch_checkpoint` es **secundaria**: actualiza el despacho cacheado
que ve la pantalla. Si falla, la marcación ya está a salvo, así que se registra
el error y se continúa; revertir la confirmación arriesgaría un doble reporte.

| Campo | Valor |
|---|---|
| `event_type` | `checkpoint_arrival` |
| `priority` | `MEDIUM` |
| `message` | texto humano para logs, p. ej. `Llegada a Y DE CARIGÁN — A TIEMPO (-18 s)` |

El consumidor **no debe parsear `message`**: toda la información está en `payload`.

```json
{
  "step": 1,
  "checkpoint_id": 3701,
  "point_id": 684,
  "point_name": "Y DE CARIGÁN",
  "order": 1,
  "scheduled_time": "07:46:00",
  "reported_time": "07:45:42",
  "difference_seconds": -18,
  "arrival_status": "ON_TIME",
  "line": { "id": 17, "name": "A2", "number": 8,
            "start_route": "CARIGAN", "end_route": "CIUDAD VICTORIA" },
  "reason": "progreso normal"
}
```

`checkpoint_id` identifica la marcación (persistencia y sincronización);
`point_id` identifica el punto físico (y es la clave del cache de audio).

### Puntualidad

`calculate_arrival_status(scheduled_time, reported_time)` compara
`time_calculated` con `time_reported` y devuelve `{status, difference_seconds}`:

```text
difference_seconds > 0   → llegó después
difference_seconds < 0   → llegó antes

|difference| <= ON_TIME_TOLERANCE_SECONDS  → ON_TIME
         > +tolerancia                      → LATE
         < -tolerancia                      → EARLY
```

`ON_TIME_TOLERANCE_SECONDS` (por defecto **30 s**, en `bus_monitor.py`) es
**solo una clasificación informativa para el conductor**: no interviene en la
selección de steps ni en la autorización de marcajes. Los estados viajan con
nombres técnicos (`EARLY` / `ON_TIME` / `LATE`); traducirlos es tarea de la UI.

### Consulta incremental — `after_id`

```http
GET /api/events?event_type=checkpoint_arrival&after_id=125
```

Devuelve solo los eventos con `id > 125`, **ordenados de forma ascendente**, para
que un consumidor que hace polling los procese en el orden en que ocurrieron.

Sin `after_id` el endpoint mantiene su comportamiento original (más recientes
primero), igual que el resto de filtros (`priority`, `event_type`, `start_date`,
`end_date`, `limit`), que siguen funcionando sin cambios.

No existe marca de "leído" en la base: el consumidor recuerda localmente el
último id que procesó y los eventos nunca se modifican después de emitirse.

### `dispatch_refreshed`

Se emite cuando el conductor recarga el itinerario desde la pantalla y la
operación **cambió realmente** lo guardado (`updated` o `empty`). Un error de red
o un payload inválido **no emiten evento**: el monitor no debe despertar por una
recarga que no cambió nada.

```json
{ "date": "2026-09-17", "register": 1624, "revision": 4, "steps": 2, "checkpoints": 5 }
```

Es el mecanismo de coordinación entre procesos descrito en
[Coordinación con simtra-bus-monitor](#coordinacion-con-simtra-bus-monitor). El
evento se publica **después** del commit: el monitor nunca ve una revisión que
todavía podría no existir.

---

## Informacion de red (`GET /api/system/network`)

Endpoint local, de **solo lectura**, que describe a que red esta conectada ESTA
Raspberry. Existe porque el navegador no puede consultar el SSID ni las
interfaces del sistema: lo consume la vista `/settings` (Configuracion) de
`bus-display`.

Cuando la pantalla se abre desde una laptop, la respuesta sigue describiendo la
Raspberry — es la maquina donde corre este servicio.

> **Nota.** Este endpoint sigue siendo de solo lectura, pero **ya no es cierto
> que toda operacion de red lo sea**: desde la vista de Configuracion el equipo
> puede conectarse a una red Wi-Fi (`POST /api/system/wifi/connect`, mas abajo).
> `services/network_info.py` no escribe nada; quien lo hace es
> `services/wifi.py`, deliberadamente en otro modulo para que la frontera quede
> explicita.

### Contrato

```json
{
  "status": "connected",
  "connections": [
    { "type": "wifi",     "interface": "wlan0", "name": "Nombre de la red", "ipv4": ["192.168.1.50"] },
    { "type": "ethernet", "interface": "eth0",  "name": null,               "ipv4": ["192.168.1.51"] }
  ]
}
```

| Campo | Valores |
|---|---|
| `status` | `connected` (al menos una conexion activa con IPv4) · `disconnected` (se consulto y no hay ninguna) · `unavailable` (no se pudo obtener la informacion) |
| `type` | `wifi` · `ethernet` · `other` |
| `interface` | nombre de interfaz, o `null` |
| `name` | SSID; **solo** en Wi-Fi. En cable siempre `null` |
| `ipv4` | lista de direcciones IPv4 validas (puede tener mas de una) |

Si Wi-Fi y Ethernet estan activas a la vez, se devuelven las dos.

### Como se detecta

| Dato | Fuente | Respaldo |
|---|---|---|
| Interfaces y direcciones | `ip -j address` (una sola llamada) | — |
| Tipo de conexion | `nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device` | `/sys/class/net/<iface>/wireless`, luego `link_type` |
| SSID | campo CONNECTION de `nmcli` | `iwgetid <iface> -r` |

**Nunca se decide por el nombre de la interfaz.** En equipos con nombres
predecibles (`eno2`, `wlo1`, `enp3s0`) esa heuristica falla, asi que se usan el
tipo que reporta NetworkManager y el flag inalambrico del kernel.

Se excluyen loopback, `127.0.0.0/8`, interfaces caidas y direcciones no validas.
Una interfaz activa **sin IPv4 no se reporta**: sin direccion no hay nada que
mostrar y anunciarla como conexion seria enganoso.

### Tolerancia a fallos

El endpoint **nunca responde 500**. Ante herramienta ausente, salida vacia, JSON
invalido, timeout, sistema que no sea Linux o permisos insuficientes devuelve
una respuesta estable con `unavailable` (o `disconnected` si se pudo consultar y
no hay conexiones). Todos los comandos se ejecutan con lista de argumentos,
`shell=False` y timeout corto; ninguna entrada del usuario participa en su
construccion.

La respuesta se cachea 15 s para no ejecutar comandos del sistema en cada
peticion. El lock del cache **no** se mantiene durante los subprocess.

### Que NO expone

Ni contrasenas de Wi-Fi, ni claves, ni JWT, ni archivos de configuracion, ni
rutas, ni DNS, ni gateway, ni MAC, ni nada del router Teltonika ni de otras
maquinas. **Este endpoint** no escribe: no conecta, desconecta ni modifica
interfaces ni servicios. La unica escritura sobre la red vive en
`POST /api/system/wifi/connect`, y tampoco devuelve nunca una clave.

---

## Energia del dispositivo: apagado y reinicio

Dos endpoints, pensados para la pantalla en modo kiosco: sin teclado ni
escritorio, la unica alternativa era cortarle la corriente al bus, y eso es lo
que termina corrompiendo la tarjeta SD. Los consume la vista `/settings`
(Configuracion) de `bus-display`, que pide **confirmacion explicita** antes de
llamar a cualquiera de los dos.

| Endpoint | Accion |
|---|---|
| `POST /api/system/shutdown` | Apaga el equipo |
| `POST /api/system/reboot` | Reinicia el equipo |

### Contrato

Ninguna de las dos peticiones **lleva cuerpo ni parametros**: el comando es una
constante del equipo (o una variable de entorno suya) y **el frontend nunca
puede proponerlo**. Lo unico que el cliente elige es la RUTA.

```json
{
  "status": "scheduled",
  "detail": "El dispositivo se reiniciara en unos segundos",
  "scheduled_in_seconds": 3.0,
  "action": "reboot",
  "pending_action": "reboot"
}
```

| Campo | Significado |
|---|---|
| `status` | `scheduled` \| `already_scheduled` \| `unavailable` |
| `action` | Lo que se pidio en ESTA peticion |
| `pending_action` | Lo que el equipo tiene realmente pendiente |
| `scheduled_in_seconds` | Solo viene con `scheduled` |

| `status` | Significado |
|---|---|
| `scheduled` | Accion programada; la orden se da en `scheduled_in_seconds` |
| `already_scheduled` | Ya habia una en curso; no se lanza un segundo comando |
| `unavailable` | El equipo no tiene un comando utilizable para esa accion |

**`scheduled` NO significa que el equipo ya se apago o reinicio.** Significa que
el sistema recibira la orden en unos segundos; el proceso que responde se va a
morir con el equipo y no puede confirmar nada mas.

### Exclusion mutua

Apagado y reinicio **no pueden estar programados a la vez**. Si el conductor
toca «Reiniciar» cuando ya hay un apagado en curso, la respuesta es
`already_scheduled` con `pending_action: "shutdown"`, y la pantalla anuncia el
apagado — anunciar un reinicio dejaria al conductor esperando una pantalla que
no va a volver.

### Por que hay un margen

La ejecucion se planifica unos segundos DESPUES de responder (`GRACE_SECONDS`,
3 s). Sin ese margen el sistema empieza a bajar mientras uvicorn escribe la
respuesta, y la pantalla muestra un error de red en vez del aviso.

Si la programacion o la ejecucion fallan (regla de sudo ausente, por ejemplo),
el estado se libera y el conductor puede reintentar: el boton no queda muerto
hasta el proximo arranque.

### Comandos exactos

| Accion | Comando por defecto | Variable de entorno |
|---|---|---|
| Apagado | `sudo -n /sbin/shutdown -h now` | `SYSTEM_SHUTDOWN_COMMAND` |
| Reinicio | `sudo -n /sbin/shutdown -r now` | `SYSTEM_REBOOT_COMMAND` |

El reinicio usa `shutdown -r now` y no `reboot now`: **`reboot` no acepta un
argumento `now`** y esa forma fallaria. `shutdown -r now` es el equivalente
correcto en Raspberry Pi OS (Bookworm, systemd). Alternativas validas para las
variables: `systemctl poweroff` y `systemctl reboot`.

Los comandos se ejecutan como **lista de argumentos con `shell=False`**: no se
arma ninguna linea de comando por concatenacion.

### Permisos minimos

`sudo -n` falla en vez de esperar una contrasena que nadie va a escribir, asi
que el usuario del servicio necesita reglas sin contrasena. **Se conceden los
dos comandos exactos, nunca `ALL`**: una regla generica permitiria ejecutar
cualquier cosa como root desde un proceso expuesto en la red del bus.

```bash
sudo tee /etc/sudoers.d/simtra-power > /dev/null <<'EOF'
admin ALL=(root) NOPASSWD: /sbin/shutdown -h now
admin ALL=(root) NOPASSWD: /sbin/shutdown -r now
EOF
sudo chmod 440 /etc/sudoers.d/simtra-power
sudo visudo -c        # valida la sintaxis antes de confiar en ella
```

Si se usan las variantes de systemd, las reglas son
`/usr/bin/systemctl poweroff` y `/usr/bin/systemctl reboot`.

Si el ejecutable no existe, el endpoint responde `unavailable` en vez de
prometer una accion que no va a ocurrir.

> El archivo `/etc/sudoers.d/simtra-shutdown` de la version anterior queda
> sustituido por este. Se puede borrar: `sudo rm /etc/sudoers.d/simtra-shutdown`.

### Solucion: el boton de reinicio falla con `a password is required`

Si al probar el boton aparece en el registro un mensaje como este:

```text
admin : a password is required ; PWD=/home/admin/simtra-bus-manager ; USER=root ; COMMAND=/sbin/shutdown -r now
```

el usuario `admin` no tiene autorizado ese comando sin contrasena. El backend
usa `sudo -n`, por lo que falla sin abrir una solicitud interactiva de clave.
Tener permiso para `/sbin/shutdown -h now` (apagado) **no autoriza**
`/sbin/shutdown -r now` (reinicio): los argumentos tambien deben coincidir.

Si ya instalaste las dos reglas de `simtra-power` indicadas arriba, no hace
falta duplicarlas. Para un equipo que solo tenia configurado el apagado,
agrega el permiso de reinicio con:

```bash
sudo visudo -f /etc/sudoers.d/simtra-reboot
```

Escribe esta linea y guarda el archivo:

```sudoers
admin ALL=(root) NOPASSWD: /sbin/shutdown -r now
```

Sustituye `admin` si el servicio se ejecuta con otro usuario. Si configuraste
`SYSTEM_REBOOT_COMMAND`, la regla debe coincidir con el ejecutable y los
argumentos que realmente utiliza ese comando.

Comprueba los permisos del archivo y la sintaxis:

```bash
sudo chmod 440 /etc/sudoers.d/simtra-reboot
sudo visudo -c
```

La validacion debe indicar que los archivos se analizaron correctamente.
El permiso se aplica sin reiniciar la Raspberry ni el backend. Despues puedes
volver a probar el boton con su confirmacion: **esa prueba reiniciara realmente
el equipo**. La validacion con `visudo -c` solo comprueba la sintaxis y no
ejecuta un reinicio.

### El archivo `simtra-power` aparece vacio al abrirlo

`/etc/sudoers.d/simtra-reboot` y `/etc/sudoers.d/simtra-power` son archivos
distintos. Si antes creaste solo `simtra-reboot`, abrir `simtra-power` con
`visudo` puede mostrar un archivo nuevo y vacio porque todavia no existe.
**Reiniciar la Raspberry no deberia borrar las reglas guardadas.**

Para dejar autorizados ambos botones en `simtra-power`, abre:

```bash
sudo visudo -f /etc/sudoers.d/simtra-power
```

Agrega las dos reglas (conserva cualquier otra regla necesaria que ya exista):

```sudoers
admin ALL=(root) NOPASSWD: /sbin/shutdown -h now
admin ALL=(root) NOPASSWD: /sbin/shutdown -r now
```

La primera autoriza **apagar** (`-h`) y la segunda **reiniciar** (`-r`). El
permiso de reinicio por si solo no autoriza el apagado. Si el servicio usa
otro usuario, sustituye `admin` por ese usuario.

Si `visudo` abre Nano, guarda con **Ctrl+O**, confirma el nombre con **Enter**
y sal con **Ctrl+X**. Despues comprueba el archivo:

```bash
sudo chmod 440 /etc/sudoers.d/simtra-power
sudo cat /etc/sudoers.d/simtra-power
sudo visudo -c
```

`cat` debe mostrar las dos reglas y `visudo -c` debe confirmar que la sintaxis
es correcta. Estos comandos no apagan ni reinician el equipo. Los permisos
se aplican inmediatamente, sin reiniciar la Raspberry ni el backend.
Si el archivo sigue vacio, verifica que guardaste los cambios en esa ruta
exacta y que el editor no mostro un error al guardar.

---

## Conexion Wi-Fi (`POST /api/system/wifi/connect`)

**Unica operacion que escribe sobre la red del equipo.** La consume el
formulario de la vista `/settings` de `bus-display`. Vive en `services/wifi.py`,
separado de `services/network_info.py`, que sigue siendo de solo lectura.

### Terminologia: «usuario» = nombre de red (SSID)

En la conversacion del proyecto se hablo de «usuario y clave» de la red. En esta
implementacion **«usuario» significa el NOMBRE DE LA RED WI-FI (SSID)**, y asi
esta etiquetado el campo en la pantalla: «Nombre de red (SSID)».

No tiene ninguna relacion con el usuario del backend remoto SIMTRA
(`FAST_API_BACKEND_USERNAME`), ni con un usuario del sistema. **No hay soporte
802.1X / WPA-Enterprise** (usuario + contrasena contra un RADIUS): el equipo se
conecta a redes WPA/WPA2-PSK o abiertas, que es lo que hay en un patio de buses.

### Contrato

```jsonc
// Peticion
{ "ssid": "SIMTRA-PATIO", "password": "clave-de-la-red" }   // password null o "" = red abierta,
                                                            // o reconectar con el perfil guardado
// Respuesta
{
  "status": "connected",
  "detail": "Conectado a la red",
  "ssid": "SIMTRA-PATIO",
  "network": { "status": "connected", "connections": [ /* igual que GET /api/system/network */ ] }
}
```

| `status` | Significado |
|---|---|
| `connected` | **Verificado**: la red pedida quedo activa |
| `invalid_password` | El punto de acceso rechazo el secreto |
| `not_found` | El SSID no esta visible |
| `timeout` | No se pudo confirmar a tiempo |
| `unavailable` | No hay NetworkManager/`nmcli` utilizable |
| `no_adapter` | No hay adaptador Wi-Fi gestionado |
| `not_authorized` | Faltan permisos (polkit) |
| `busy` | Ya hay un intento en curso |
| `failed` | Cualquier otro fallo |

`network` solo viene con `connected`. **Nunca responde 500**: la falta de
herramienta, de adaptador o de permisos son estados propios de la respuesta.

### Manejo de la clave

* Viaja a `nmcli` por **STDIN** (`nmcli --ask`), **nunca en la linea de
  comandos**: asi no es visible en `ps`, en `/proc/<pid>/cmdline` ni en la
  auditoria del sistema.
* **No se registra en ningun log** ni se devuelve al cliente, ni siquiera dentro
  de un mensaje de error: los errores se traducen a mensajes fijos y la salida
  cruda de `nmcli` se depura antes de loguearse.
* **No se guarda en ninguna tabla** de este proyecto. Quien la conserva es
  NetworkManager, en su perfil de conexion
  (`/etc/NetworkManager/system-connections/`, modo 0600, root) — su mecanismo
  normal.
* La validacion fina vive en el servicio y **sus mensajes no citan el valor
  recibido**. Por eso un parametro invalido devuelve 200 con `status: "failed"` y
  no un 422 de Pydantic, que incluiria la clave en el detalle del error.

### El exito se verifica, no se supone

Que `nmcli` devuelva 0 **no basta**: despues de conectar se comprueba con
`nmcli connection show --active` que la conexion pedida este realmente activa
sobre un dispositivo Wi-Fi. Solo entonces se responde `connected`, se invalida el
cache de 15 s de `network_info.py` (o la pantalla mostraria la IP anterior) y se
devuelve la red nueva.

Un `timeout` tambien pasa por esa verificacion: la asociacion puede terminar
justo despues, y darla por fallida seria igual de erroneo.

### Reconexion con clave nueva

Si se envia una clave y ya existe un perfil con ese nombre, **se borra primero**.
Sin eso NetworkManager reutiliza el secreto guardado, `--ask` no llega a
preguntar y la clave nueva se ignora en silencio. Con el campo de clave VACIO no
se borra nada: ahi el conductor esta pidiendo reconectar con lo que ya hay.

### Requisitos reales de despliegue

1. **NetworkManager con `nmcli` en el PATH.** Raspberry Pi OS Bookworm lo trae
   por defecto; en Bullseye o con `dhcpcd`/`wpa_supplicant` hay que instalarlo y
   activarlo:
   ```bash
   sudo apt install network-manager
   sudo systemctl enable --now NetworkManager
   ```
   Sin el, el endpoint responde `unavailable` y la pantalla lo dice.
2. **Adaptador Wi-Fi gestionado por NetworkManager.** Si aparece como
   `unmanaged`, el endpoint responde `no_adapter`.
3. **Permisos.** El usuario del servicio debe poder modificar conexiones sin
   contrasena. Lo habitual es anadirlo al grupo `netdev`; si la politica de
   polkit del equipo no lo permite, hace falta una regla explicita:
   ```bash
   sudo usermod -aG netdev admin

   sudo tee /etc/polkit-1/rules.d/50-simtra-wifi.rules > /dev/null <<'EOF'
   polkit.addRule(function(action, subject) {
     if (subject.user == "admin" &&
         (action.id == "org.freedesktop.NetworkManager.settings.modify.system" ||
          action.id == "org.freedesktop.NetworkManager.network-control")) {
       return polkit.Result.YES;
     }
   });
   EOF
   sudo systemctl restart polkit
   ```
   Sin permisos, el endpoint responde `not_authorized`.

### Aviso operativo

Cambiar de red **corta el acceso desde cualquier otro dispositivo de la red
anterior**. Si la pantalla se estaba viendo desde una laptop, esa peticion se
queda sin respuesta: eso NO significa que la conexion fallara, y la pantalla lo
dice asi en vez de reportar un fracaso.

---


---

## Recarga manual del itinerario (`POST /api/dispatch/refresh`)

Lo que hace el boton «Volver a cargar itinerario» de la Home. **No es repetir
`GET /api/dispatch`**: ese endpoint solo lee lo que ya esta cacheado en la RPi,
asi que si el monitor no ha recargado devuelve lo mismo una y otra vez.

### Flujo real

```
pantalla -> POST /api/dispatch/refresh (API local)
         -> backend remoto SIMTRA (services/api.py, con JWT)
         -> validacion de estructura
         -> fusion con las marcaciones locales pendientes
         -> persistencia en SQLite, en una transaccion
         -> respuesta con el despacho REALMENTE guardado
         -> evento `dispatch_refreshed` -> simtra-bus-monitor lo adopta
```

La peticion **no lleva cuerpo**: el registro sale de `FAST_API_BUS_REGISTER` y la
fecha del reloj del equipo en `America/Guayaquil`. **Ninguna credencial del
backend remoto llega jamas al frontend**: `FAST_API_BACKEND_URL`, `..._USERNAME`
y `..._PASSWORD` viven solo en el `.env` del equipo.

### Contrato

```json
{
  "status": "updated",
  "detail": "Itinerario actualizado: 2 recorrido(s)",
  "date": "2026-09-17",
  "register": 1624,
  "dispatch": { "id": 1, "date": "...", "register": 1624, "data": [ ... ], "revision": 4, "created_at": "..." },
  "preserved_reports": 1,
  "revision": 4
}
```

| `status` | Significado | ¿Cambia el itinerario? |
|---|---|---|
| `updated` | Itinerario nuevo descargado, validado y guardado | Si |
| `empty` | El backend respondio bien y el bus **no trabaja hoy** | Si, queda vacio |
| `auth_error` | El backend remoto rechazo las credenciales del equipo | **No** |
| `remote_error` | No se pudo hablar con el backend remoto | **No** |
| `invalid` | El backend respondio algo inutilizable | **No** |
| `save_error` | Se descargo bien pero no se pudo guardar | **No** |

En los cuatro estados de error se **conserva el itinerario anterior** y se
devuelve tal cual en `dispatch`: un fallo de red no puede dejar al conductor sin
recorrido. `preserved_reports` cuenta las marcaciones locales que se conservaron
al fusionar.

### Una lista con basura NO es un dia sin despachos

`services/dispatch_refresh.py` valida antes de reemplazar nada: cada recorrido
necesita numero, horario `HH:MM:SS` en rango y al menos un punto de control con
id y coordenadas utilizables. Una lista **vacia** es valida y produce `empty`;
una lista **con contenido roto** produce `invalid` y no borra nada. Confundirlas
vaciaria el itinerario del conductor a mitad de jornada por un error del backend.

Para distinguir los cuatro casos hizo falta un metodo nuevo en el cliente
remoto: `ApiService.fetch_dispatch()` devuelve un `DispatchFetch` con estado
explicito (`ok` / `empty` / `auth_error` / `transport_error` /
`invalid_response`). `get_dispatch()` se conserva sin cambios para
`bus_monitor.load_all_dispatches`, que solo necesita saber si hay trabajo hoy.

### Marcaciones locales pendientes

El bus pudo cruzar geocercas mientras la descarga estaba en curso, y esas
llegadas todavia no estan en el backend remoto. Al fusionar:

* Se conservan **solo las pendientes de subir** (filas de `checkpoint` con
  `upload = False`). Una que ya viajo al servidor es el servidor quien debe
  devolverla; resucitarla aqui desharia una correccion hecha en el backend.
* La identidad es **`(numero de step, id de checkpoint)`**, nunca la posicion en
  el array: el backend puede reordenar los recorridos o devolver uno menos, y una
  fusion por indice trasladaria una llegada a **otro recorrido**.
* Si el itinerario nuevo no tiene ese par exacto, la marcacion se **descarta y se
  registra en el log**. No se traslada a ninguna parte.
* Si el itinerario nuevo ya trae hora para ese checkpoint, gana el servidor.

### Concurrencia

La lectura del despacho anterior, la fusion y la escritura ocurren en **una sola
transaccion** (`crud.refresh_dispatch`), que relee la fila justo antes de
escribir. Asi una marcacion que entre por `PATCH /api/dispatch/checkpoint`
mientras la descarga estaba en curso ya esta presente y no se pierde. La ventana
se cierra a nivel de base de datos, no con un candado en memoria — que no
cruzaria entre procesos.

Ademas, `POST /api/dispatch` (el cache que escribe el monitor) **ya no reemplaza
`data` a secas**: conserva las horas de llegada que el payload entrante no trae.
Ese era un fallo real del upsert anterior — una recarga rutinaria del monitor
borraba de la pantalla marcaciones que el backend remoto aun no conocia.

---

## Coordinacion con simtra-bus-monitor

FastAPI, el monitor y el loader son **unidades systemd separadas**: procesos
distintos, sin memoria compartida. Importar `bus_monitor` desde `main.py` para
tocar sus variables globales **no afectaria al proceso real** — crearia una copia
del modulo dentro de uvicorn. La coordinacion pasa por la base de datos.

### Mecanismo: revision + evento

1. La tabla `dispatch` tiene una columna **`revision`** que se incrementa en cada
   escritura.
2. Tras una recarga exitosa, la API publica un evento `dispatch_refreshed` en
   `/api/events` con `{date, register, revision, steps, checkpoints}`.
3. El watcher del monitor consulta ese canal de forma incremental (`after_id`) en
   cada ciclo y, al ver una revision mas nueva, **lee el despacho ya GUARDADO**
   con `GET /api/dispatch` y lo adopta. No vuelve a bajarlo del backend remoto:
   asi monitor y pantalla no pueden discrepar.
4. Al adoptar, **sustituye** la ventana de geocercas observadas (anterior +
   actual + siguiente del itinerario nuevo) y recalcula el contexto temporal.

### Que NO se rompe al adoptar

| Garantia | Como |
|---|---|
| No se repiten avisos de llegada | No se llama a `reset_daily_state()`: `CONFIRMED_CHECKPOINTS` sobrevive, asi que un checkpoint ya reportado hoy no se vuelve a reportar |
| Estar dentro de una geocerca no se relee como entrada | `GeofenceMonitor.replace_geofences()` conserva el estado `_active` de las geocercas que siguen vigentes, en vez de crear un monitor nuevo |
| Las geocercas cambian aunque siga el mismo tramo | Se **reemplaza** la ventana, no se amplia: las paradas que ya no existen dejan de observarse |
| Una carga vieja del monitor no pisa la recarga | El monitor lee la revision ANTES de salir a la red y la declara como `base_revision` al cachear; si la almacenada es mas nueva, la API descarta esa escritura |

### Tiempo de adopcion

El monitor adopta la revision nueva en **hasta `FAST_API_WATCHER_INTERVAL_SECONDS`
(10 s por defecto)**. La pantalla, en cambio, se actualiza de inmediato: la
respuesta del endpoint ya trae el itinerario guardado.

Durante esa ventana, la pantalla muestra el itinerario nuevo y el monitor todavia
vigila las geocercas del anterior.

> **Si `simtra-bus-monitor` esta detenido, no adopta nada.** La recarga habra
> actualizado la pantalla y la base de datos, pero el geofencing seguira parado
> hasta que el servicio vuelva. Comprobar con
> `systemctl status simtra-bus-monitor` y buscar `[REFRESH]` en `bus_monitor.log`.

---

## Subsistema de audio

Los anuncios de voz de los puntos de control se generan con **gTTS** y se
reproducen con **mpg123**. Todo el trabajo ocurre en dos hilos daemon
(`audio-generator` y `audio-player`): `prepare()` y `announce()` solo encolan y
retornan de inmediato, así que el loop de GPS nunca se bloquea por audio.

### Identidad del cache

> Los archivos se cachean por `point.id`, **no** por `checkpoint.id`.

```text
checkpoint
│
├── checkpoint.id            → persistencia / dispatch / backend
│
└── checkpoint.point.id      → cache de audio
                                └── audio/point_{point.id}.mp3
```

Un mismo punto físico se reutiliza en múltiples líneas, steps y despachos, y
además recibe un `checkpoint.id` nuevo cada día. En un día real hay **158
checkpoints pero solo 27 puntos distintos**: cachear por punto genera 27 audios
una única vez en lugar de 158 cada día.

### Directorio

```text
audio/
├── point_689.mp3
├── point_689.json
├── point_690.mp3
└── point_690.json
```

`AUDIO_DIR` se deriva del propio módulo (`Path(__file__).resolve().parent.parent
/ "audio"`), por lo que es una **ruta absoluta independiente del working
directory**: es la misma se lance el proceso desde systemd, una terminal, un IDE,
Windows o la RPi. La carpeta se crea sola al importar el módulo.

El `.json` guarda `point_id`, `name` y `text`; sirve para detectar que el nombre
del punto cambió y regenerar. No se regenera por que el punto aparezca en otro
checkpoint, step o línea.

### Flujo

```text
bus_monitor detecta la llegada a un checkpoint
        ↓
obtiene checkpoint.point.id
        ↓
audio_announcer busca audio/point_{id}.mp3
        ↓
¿existe y el texto coincide?
   sí → reutiliza (sin tocar gTTS)
   no → genera con gTTS y lo guarda
        ↓
reproduce con: mpg123 -q /ruta/absoluta/audio/point_{id}.mp3
```

El texto es únicamente `"Punto de control {nombre}."`. No incluye el punto
siguiente a propósito: ese depende de la línea y del recorrido, así que un
mensaje con el próximo punto no sería reutilizable entre líneas y rompería la
identidad del cache.

Una generación por punto a la vez: un lock por `point_id` evita que un
`prepare()` y un `announce()` casi simultáneos disparen dos peticiones a gTTS.

### Dependencias

| Dependencia | Instalación | Nota |
|---|---|---|
| `gTTS` | `pip install -r requirements.txt` | requiere internet solo la primera vez que se genera cada punto |
| `mpg123` | `sudo apt install mpg123` | debe estar disponible en el `PATH` |

Si `mpg123` no está en el `PATH`, el log lo dice explícitamente y **no** se
confunde con un MP3 faltante:

```text
[AUDIO] Reproductor 'mpg123' no encontrado en PATH — no se reproduce /ruta/audio/point_689.mp3
[AUDIO] Archivo MP3 no encontrado: /ruta/audio/point_689.mp3
```

Los errores siempre registran la ruta absoluta completa para facilitar el
diagnóstico de deployments.

### Archivos antiguos

Los `checkpoint_*.mp3` / `checkpoint_*.json` generados por el esquema anterior
quedan sin uso. No hay migración automática; se pueden borrar cuando se quiera:

```bash
rm -f audio/checkpoint_*.mp3 audio/checkpoint_*.json
```

---

## Auditoría de bases de datos

Para copiar las bases de datos desde la RPi a la laptop:

```bash
scp admin@192.168.1.14:/home/admin/simtra-bus-manager/app.db .
scp admin@192.168.1.14:/home/admin/simtra-bus-manager/data_loader.db .
```

---

## Configuración de servicios systemd

### 1. API principal — `simtra-bus-manager`

```bash
sudo nano /etc/systemd/system/simtra-bus-manager.service
```

```ini
[Unit]
Description=Aplicacion Gestion de Buses
After=network.target

[Service]
User=admin
WorkingDirectory=/home/admin/simtra-bus-manager/
ExecStart=/home/admin/env/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

### 2. Monitor de puntos de control — `simtra-bus-monitor`

```bash
sudo nano /etc/systemd/system/simtra-bus-monitor.service
```

```ini
[Unit]
Description=Monitor de puntos de control
After=network.target

[Service]
User=admin
WorkingDirectory=/home/admin/simtra-bus-manager/services/
ExecStart=/home/admin/env/bin/python3 /home/admin/simtra-bus-manager/services/bus_monitor.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

### 3. Loader de datos — `simtra-bus-loader`

```bash
sudo nano /etc/systemd/system/simtra-bus-loader.service
```

```ini
[Unit]
Description=Subida de datos recopilados al backend
After=network.target

[Service]
User=admin
WorkingDirectory=/home/admin/simtra-bus-manager/services/
ExecStart=/home/admin/env/bin/python3 /home/admin/simtra-bus-manager/services/data_loader.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

### Activar todos los servicios

```bash
sudo systemctl daemon-reload

sudo systemctl enable simtra-bus-manager simtra-bus-monitor simtra-bus-loader
sudo systemctl start simtra-bus-manager simtra-bus-monitor simtra-bus-loader
```

---

## Monitoreo y logs

```bash
# Estado de los servicios
sudo systemctl status simtra-bus-manager.service
sudo systemctl status simtra-bus-monitor.service
sudo systemctl status simtra-bus-loader.service

# Logs en tiempo real
journalctl -u simtra-bus-manager -f
journalctl -u simtra-bus-monitor -f
journalctl -u simtra-bus-loader -f

# Reiniciar un servicio tras actualizar código
sudo systemctl restart simtra-bus-manager
```
