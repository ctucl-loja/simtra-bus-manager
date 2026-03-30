import requests
import json
class ApiService():
    def __init__(self, api_url,username, password):
        self.user = username
        self.password = password
        self.api_url = api_url
        self.jwt = self.get_jwt()

    def get_jwt(self):
        url = f"{self.api_url}/api/auth/login"
        payload = json.dumps({
            "email": self.user,
            "password": self.password
        })
        headers = {
            'Content-Type': 'application/json'
        }
        try:
            response = requests.post(url, headers=headers, data=payload)
            if response.status_code == 200:
                data = response.json()
                return data['result']['token']
            else:
                print('Error al obtener token')
        except Exception as e:
            print(f"Excepción al obtener token: {e}")
        return ''
    

    def get_dispatch(self,register,date):
        url = f"{self.api_url}/api/dispatch/{register}?date={date}"
        headers = {
            'Authorization': f'Bearer {self.jwt}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                return response.json().get('result', [])
            elif response.status_code == 401:
                print("Token de autenticación no válido o expirado.")
                self.jwt = self.get_jwt()
            else:
                print(f"Error en la solicitud: {response.status_code}")
        except Exception as e:
            print(f"Error al obtener usuarios: {e}")
        return []
    
    def post_passenger(self,data):
        url = f"{self.api_url}/api/passenger"
        headers = {
            'Authorization': f'Bearer {self.jwt}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        try:
            response = requests.post(url, headers=headers,json=data)
            data = response.json()
            print(data)
            if response.status_code == 201:
                return True
            else:
                return False
        except Exception as e:
            print(f"Error al obtener usuarios: {e}")
        return False
    
    def update_dispatch(self,data):
        url = f"{self.api_url}/api/dispatch"
        headers = {
            'Authorization': f'Bearer {self.jwt}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        try:
            response = requests.patch(url, headers=headers,json=data)
            if response.status_code == 200:
                return True
            else:
                return False
        except Exception as e:
            print(f"Error al obtener usuarios: {e}")
        return False