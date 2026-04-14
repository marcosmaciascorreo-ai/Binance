import os
import time
from dotenv import load_dotenv
from binance.client import Client

load_dotenv('config.env')
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'))
server_time = client.get_server_time()
client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)

OBJETIVO_PCT   = 0.4
VOLUMEN_MINIMO = 300000
TOP_N          = 25

print("=" * 65)
print("  ESCANEANDO BINANCE — solo mercados ABIERTOS")
print("=" * 65)

# Filtrar solo pares USDT con trading habilitado
info_exchange = client.get_exchange_info()
simbolos_activos = {
    s['symbol']
    for s in info_exchange['symbols']
    if s['symbol'].endswith('USDT')
    and s['status'] == 'TRADING'
    and s['isSpotTradingAllowed']
}

# Filtrar por volumen
tickers = client.get_ticker()
pares = [
    t for t in tickers
    if t['symbol'] in simbolos_activos
    and float(t['quoteVolume']) >= VOLUMEN_MINIMO
    and float(t['lastPrice']) > 0
]

print(f"  Mercados abiertos con volumen suficiente: {len(pares)}")
print(f"  Analizando movimientos de {OBJETIVO_PCT}%... espera 2-3 min")
print()

resultados = []

for i, ticker in enumerate(pares):
    symbol = ticker['symbol']
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=288)
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]

        movimientos = sum(
            1 for h, l in zip(highs, lows)
            if l > 0 and (h - l) / l * 100 >= OBJETIVO_PCT
        )

        rangos  = [(h - l) / l * 100 for h, l in zip(highs, lows) if l > 0]
        vol_avg = sum(rangos) / len(rangos) if rangos else 0
        cambio  = float(ticker['priceChangePercent'])
        volumen = float(ticker['quoteVolume'])

        # Excluir caidas extremas y simbolos con caracteres raros
        if cambio < -20:
            continue
        if not symbol.replace('USDT', '').isascii():
            continue

        resultados.append({
            'symbol': symbol,
            'movimientos': movimientos,
            'vol_avg': vol_avg,
            'cambio_24h': cambio,
            'volumen': volumen
        })

        if (i + 1) % 30 == 0:
            print(f"  Analizadas {i + 1}/{len(pares)} monedas...")

        time.sleep(0.1)

    except Exception:
        pass

resultados.sort(key=lambda x: x['movimientos'], reverse=True)
top = resultados[:TOP_N]

print()
print("=" * 65)
print(f"  TOP {TOP_N} MONEDAS — mercado abierto, sin caidas extremas")
print("=" * 65)
print(f"  {'#':<3} {'Moneda':<16} {'Mov':>4} {'Vol%':>6} {'24h':>7} {'Volumen':>14}")
print("-" * 65)
for i, r in enumerate(top, 1):
    print(f"  {i:<3} {r['symbol']:<16} {r['movimientos']:>4} {r['vol_avg']:>5.2f}% {r['cambio_24h']:>6.2f}% {r['volumen']:>14,.0f}")

symbols_str = ','.join([r['symbol'] for r in top])
print()
print("  Actualizando config.env automaticamente...")

# Leer y actualizar config.env
with open('config.env', 'r', encoding='utf-8') as f:
    config = f.read()

import re
config = re.sub(r'SYMBOLS=.*', f'SYMBOLS={symbols_str}', config)

with open('config.env', 'w', encoding='utf-8') as f:
    f.write(config)

print(f"  config.env actualizado con {len(top)} monedas!")
print()
input("Presiona Enter para salir y luego reinicia iniciar.bat...")
