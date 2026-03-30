import requests
import random
from datetime import datetime

GPS_API_URL = "http://localhost:8000/api/gps"

def update_gps_position(coordenadas: str):
    try:
        # 1. Procesar coordenadas
        partes = coordenadas.split(",")
        if len(partes) != 2:
            raise ValueError("Formato incorrecto. Usa: latitud, longitud")
            
        latitud = float(partes[0].strip())
        longitud = float(partes[1].strip())
        
        # 2. Preparar datos
        # Convertimos el timestamp a string ISO porque JSON no acepta objetos datetime directamente

        random_speed = round(random.uniform(22, 70), 2)
        
        gps_body_data = {
            "latitude": latitud,
            "longitude": longitud,
            "speed": random_speed,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        }
        print(gps_body_data)
        
        # 3. Enviar a la API
        # Usamos el argumento 'json=' para que requests convierta el dict y ponga los headers correctos
        resp = requests.post(GPS_API_URL, json=gps_body_data, timeout=5)
        resp.raise_for_status()
        
        print(f"✅ [EXITO] Enviado: {latitud}, {longitud} a {random_speed} km/h")
        return resp.json()

    except requests.RequestException as e:
        print(f"❌ [ERROR GPS] No se pudo conectar: {e}")
    except ValueError as e:
        print(f"⚠️ [ERROR FORMATO] {e}")
    except Exception as e:
        print(f"❗ [ERROR INESPERADO] {e}")
    return None

# Ciclo principal
print("Simulador de GPS iniciado. (Presiona Ctrl+C para salir)")
while True:
    gps_data = input("\nPegue coordenadas de Google Maps (lat, lon): ")
    if gps_data.lower() in ['salir', 'exit', 'q']:
        break
    update_gps_position(gps_data)