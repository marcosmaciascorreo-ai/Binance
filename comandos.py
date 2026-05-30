"""
comandos.py — Procesa comandos de Telegram para el bot de trading.
Lee mensajes entrantes via webhook y ejecuta acciones.

Comandos soportados:
  /status — estado completo del bot
  /balance — capital actual
  /sharpe — sharpe ratio
  /liquidar — vende todo y limpia estado
  /rebalance — ajusta capital al balance real de Binance
  /top — muestra mejores monedas segun historial
"""
import os, sys, json, csv, time
from datetime import datetime

# Importar funciones del bot
sys.path.insert(0, os.path.dirname(__file__))
from estado_rapido import status
from dotenv import load_dotenv
from binance.client import Client

load_dotenv('config.env')
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'))
server_time = client.get_server_time()
client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)

def cmd_status():
    return status()

def cmd_balance():
    try:
        usdt = float(client.get_asset_balance(asset='USDT')['free'])
        try:
            with open('ganancias.json') as f:
                g = json.load(f)
            capital = g.get('capital', 0)
        except:
            capital = usdt
        return (f'💰 BALANCE\n{"-"*25}\n'
                f'Binance real: ${usdt:.4f} USDT\n'
                f'Capital bot: ${capital:.4f} USDT\n'
                f'Diferencia: ${usdt - capital:+.4f}')
    except Exception as e:
        return f'Error obteniendo balance: {e}'

def cmd_sharpe():
    try:
        with open('riesgo.json') as f:
            r = json.load(f)
        hist = r.get('historial_rendimientos', [])
        if len(hist) < 10:
            return f'No hay suficientes datos. Solo {len(hist)} ciclos registrados.'
        promedio = sum(hist) / len(hist)
        # Sharpe aproximado
        import statistics
        std = statistics.stdev(hist) if len(hist) > 1 else 0.0001
        tasa_libre = 0.0137 / 100
        sharpe = (promedio - tasa_libre) / std if std else 0
        ultimos_10 = sum(hist[-10:]) / 10
        wins = sum(1 for h in hist if h > 0)
        win_rate = wins / len(hist) * 100
        return (f'📈 SHARPE RATIO\n{"-"*25}\n'
                f'Sharpe: {sharpe:.2f}\n'
                f'Rend. promedio: {promedio:+.3f}%\n'
                f'Rend. ult.10: {ultimos_10:+.3f}%\n'
                f'Win rate: {win_rate:.1f}%\n'
                f'Ciclos: {len(hist)}\n'
                f'{"> 1.0 = BUENO" if sharpe > 1 else "< 1.0 = REGULAR"}')
    except Exception as e:
        return f'Error: {e}'

def cmd_top():
    try:
        if not os.path.exists('trades_log.csv'):
            return 'No hay historial de trades.'
        stats = {}
        with open('trades_log.csv', 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = row['par']
                pct = float(row.get('ganancia_pct', 0))
                if sym not in stats:
                    stats[sym] = {'trades': 0, 'wins': 0, 'total': 0.0}
                stats[sym]['trades'] += 1
                stats[sym]['total'] += float(row.get('ganancia_neta_usdt', 0))
                if pct > 0:
                    stats[sym]['wins'] += 1
        top = sorted(stats.items(), key=lambda x: x[1]['total'], reverse=True)[:5]
        if not top:
            return 'Sin datos suficientes.'
        msg = '🏆 TOP MONEDAS\n'
        msg += '-' * 25 + '\n'
        for sym, s in top:
            wr = s['wins'] / s['trades'] * 100
            msg += f'{sym:<12} {s["total"]:+.4f} USDT  ({s["trades"]}t, {wr:.0f}% WR)\n'
        return msg.strip()
    except Exception as e:
        return f'Error: {e}'

def cmd_liquidar():
    return '⚠️ Para liquidar ejecuta: python liquidar.py manualmente desde el servidor.'

def cmd_rebalance():
    try:
        usdt = float(client.get_asset_balance(asset='USDT')['free'])
        if usdt > 0.5:
            with open('ganancias.json', 'r') as f:
                g = json.load(f)
            g['capital'] = round(usdt, 4)
            with open('ganancias.json', 'w') as f:
                json.dump(g, f)
            return f'✅ Capital rebalanceado a ${usdt:.4f} USDT'
        return 'Balance muy bajo para rebalancear.'
    except Exception as e:
        return f'Error: {e}'

COMANDOS = {
    '/status': cmd_status,
    '/balance': cmd_balance,
    '/sharpe': cmd_sharpe,
    '/top': cmd_top,
    '/liquidar': cmd_liquidar,
    '/rebalance': cmd_rebalance,
    '/help': lambda: (
        'COMANDOS DISPONIBLES\n'
        '/status — estado completo\n'
        '/balance — capital en Binance\n'
        '/sharpe — Sharpe ratio actual\n'
        '/top — mejores monedas (historial)\n'
        '/rebalance — ajusta capital al real\n'
        '/liquidar — instrucciones para liquidar\n'
        '/help — este mensaje'
    ),
}

def procesar(comando):
    cmd = comando.strip().lower()
    if cmd in COMANDOS:
        return COMANDOS[cmd]()
    # Buscar coincidencia parcial
    for key, fn in COMANDOS.items():
        if cmd.startswith(key):
            return fn()
    return f'Comando no reconocido: {comando}\nUsa /help para ver los disponibles.'

if __name__ == '__main__':
    if len(sys.argv) > 1:
        print(procesar(sys.argv[1]))
    else:
        print(procesar('/status'))
