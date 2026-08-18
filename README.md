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

## Eventos locales (`/api/events`)

`/api/events` es el **canal local de eventos entre los procesos instalados en la
Raspberry Pi**. No sale a internet: `bus_monitor.py` escribe y `bus-display` lee,
ambos contra el FastAPI local. Los eventos quedan en SQLite y sirven además como
bitácora de la jornada.

### `checkpoint_arrival`

Se emite **una sola vez por cada checkpoint realmente aceptado y persistido** por
`bus_monitor.py`. Una geocerca descartada —por horario, por pertenecer a un step
que no ha comenzado o por secuencia inconsistente— **no genera evento**, y por lo
tanto no genera aviso en la pantalla del conductor.

```text
GPS → geocerca → validación temporal → validación de secuencia
    → checkpoint aceptado → report_checkpoint + report_dispatch_checkpoint
    → checkpoint_arrival
```

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