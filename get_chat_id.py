import requests

TOKEN = "8712108662:AAHu8CqL7gVa8-leDuElgx1AiGZQcg2gOhQ"

url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
response = requests.get(url).json()

if not response['result']:
    print("No se encontraron mensajes.")
    print("Haz esto:")
    print("1. Abre Telegram")
    print("2. Busca tu bot (el que creaste con BotFather)")
    print("3. Escríbele cualquier mensaje, ejemplo: 'hola'")
    print("4. Vuelve a ejecutar este script")
else:
    for msg in response['result']:
        chat = msg['message']['chat']
        print(f"Chat ID encontrado: {chat['id']}")
        print(f"Nombre: {chat.get('first_name', '')} {chat.get('last_name', '')}")
        break

input("\nPresiona Enter para salir...")
