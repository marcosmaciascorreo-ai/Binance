"""
estado_rapido.py — Muestra resumen rapido del estado del bot de trading.
Usalo para responder comandos como /status, /sharpe, /balance
"""
import os, json, csv
from datetime import datetime, timedelta

GANANCIAS_FILE = 'ganancias.json'
RIESGO_FILE = 'riesgo.json'
TRADES_CSV = 'trades_log.csv'
REPORTE_FILE = 'reporte_diario.json'
STATE_FILE = 'estado.json'

def leer(file, default=None):
    if not os.path.exists(file):
        return default
    try:
        with open(file) as f:
            return json.load(f)
    except:
        return default

def status():
    ganancias = leer(GANANCIAS_FILE, {})
    riesgo = leer(RIESGO_FILE, {})
    estado = leer(STATE_FILE, {})
    reporte = leer(REPORTE_FILE, {})

    capital = ganancias.get('capital', 0)
    total_usdt = ganancias.get('total_usdt', 0)
    hoy = datetime.now().strftime('%Y-%m-%d')

    # Estadisticas de trades
    trades_hoy = 0
    trades_totales = 0
    ganancia_hoy = 0.0
    perdida_hoy = 0.0

    if os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades_totales += 1
                ts = row.get('timestamp', '')
                if ts.startswith(hoy):
                    trades_hoy += 1
                    pct = float(row.get('ganancia_pct', 0))
                    if pct > 0:
                        ganancia_hoy += float(row.get('ganancia_neta_usdt', 0))
                    else:
                        perdida_hoy += float(row.get('ganancia_neta_usdt', 0))

    cb = riesgo.get('circuit_breaker', 0)
    cb_razon = riesgo.get('cb_razon', '')
    perdidas_cons = riesgo.get('perdidas_consecutivas', 0)
    hist = riesgo.get('historial_rendimientos', [])
    rend_promedio = sum(hist[-10:]) / max(len(hist[-10:]), 1) if hist else 0

    linea = '-' * 30
    msg = (
        f'📊 ESTADO DEL BOT\n{linea}\n'
        f'Capital: ${capital:.4f} USDT\n'
        f'Reservado: ${total_usdt:.4f} USDT\n'
        f'Total: ${capital + total_usdt:.4f} USDT\n'
        f'Ciclos totales: {trades_totales}\n'
        f'Trades hoy: {trades_hoy}\n'
        f'{linea}\n'
    )

    if estado:
        moneda = estado.get('symbol', 'N/A')
        eestado = estado.get('estado', 'N/A')
        msg += f'Posicion: {moneda} ({eestado})\n'

    if cb:
        msg += f'⚠️ Circuit Breaker Nivel {cb}: {cb_razon}\n'

    msg += (
        f'Perdidas consec: {perdidas_cons}\n'
        f'Rend. prom (10): {rend_promedio:+.3f}%\n'
        f'Historial: {len(hist)} ciclos\n'
        f'{linea}\n'
        f'Hoy: +${ganancia_hoy:.4f} / ${perdida_hoy:.4f}\n'
        f'Neto hoy: ${ganancia_hoy + perdida_hoy:+.4f}\n'
        f'Actualizado: {datetime.now().strftime("%H:%M")}'
    )

    # Guardar para que el bot lo lea
    with open('ultimo_status.json', 'w') as f:
        json.dump({'mensaje': msg, 'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f)

    return msg

if __name__ == '__main__':
    print(status())
