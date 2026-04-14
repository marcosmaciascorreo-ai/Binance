"""
liquidar.py — Vende TODAS las monedas abiertas y limpia el estado.
Usa esto antes de reiniciar el bot desde cero.
"""
import os, json, time
from dotenv import load_dotenv
from binance.client import Client

load_dotenv('config.env')
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'))
server_time = client.get_server_time()
client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)

def get_step_size(symbol):
    info = client.get_symbol_info(symbol)
    for f in info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            step = float(f['stepSize'])
            dec  = len(f['stepSize'].rstrip('0').split('.')[-1])
            return step, dec
    return 1.0, 0

def round_qty(qty, step, dec):
    return round((qty // step) * step, dec)

print("\n=== LIQUIDACION TOTAL ===\n")

vendidos = 0
balances = client.get_account()['balances']
for b in balances:
    asset = b['asset']
    libre = float(b['free'])
    if asset in ('USDT', 'BNB') or libre <= 0:
        continue
    symbol = asset + 'USDT'
    try:
        info = client.get_symbol_info(symbol)
        if not info:
            print(f"  {asset}: par {symbol} no existe")
            continue
        precio = float(client.get_symbol_ticker(symbol=symbol)['price'])
        valor  = libre * precio
        if valor < 0.50:
            print(f"  {asset}: dust ignorado (${valor:.4f})")
            continue
        step, dec = get_step_size(symbol)
        qty = round_qty(libre, step, dec)
        if qty <= 0:
            continue
        order     = client.order_market_sell(symbol=symbol, quantity=qty)
        filled    = float(order['executedQty'])
        avg_price = float(order['cummulativeQuoteQty']) / filled
        recibido  = filled * avg_price
        print(f"  VENDIDO: {filled} {asset} a ${avg_price:.8f} = ${recibido:.4f} USDT")
        vendidos += 1
    except Exception as e:
        print(f"  ERROR vendiendo {asset}: {e}")

if vendidos == 0:
    print("  No habia monedas abiertas.")

# Limpiar estado
for f in ['estado.json']:
    if os.path.exists(f):
        os.remove(f)
        print(f"\n  Estado limpiado: {f}")

# Mostrar USDT disponible
usdt = float(client.get_asset_balance(asset='USDT')['free'])
print(f"\n  USDT disponible ahora: ${usdt:.4f}")
print("\n=== Listo para reiniciar el bot ===\n")
