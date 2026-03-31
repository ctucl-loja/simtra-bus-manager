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