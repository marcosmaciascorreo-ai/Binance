import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv('config.env')
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'))
server_time = client.get_server_time()
import time
client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)

precio_actual  = float(client.get_symbol_ticker(symbol='PEPEUSDT')['price'])
precio_compra  = 0.00000341
qty            = 2941176.0
objetivo_venta = precio_compra * 1.015

valor_actual   = precio_actual * qty
valor_compra   = precio_compra * qty
diferencia     = valor_actual - valor_compra
pct            = (precio_actual - precio_compra) / precio_compra * 100

print(f"Precio compra:   ${precio_compra:.8f}")
print(f"Precio actual:   ${precio_actual:.8f}")
print(f"Objetivo venta:  ${objetivo_venta:.8f}")
print(f"Movimiento:      {pct:+.2f}%")
print(f"")
print(f"Valor comprado:  ${valor_compra:.4f} USDT")
print(f"Valor actual:    ${valor_actual:.4f} USDT")
print(f"Diferencia:      ${diferencia:+.4f} USDT")
print(f"")
if precio_actual >= objetivo_venta:
    print("LISTO PARA VENDER ✓")
elif diferencia < 0:
    print(f"En perdida de ${abs(diferencia):.4f} USDT — esperar o vender y reiniciar")
else:
    print(f"En ganancia pero aun no llega al objetivo")

input("\nPresiona Enter para salir...")
