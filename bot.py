import os
import time
import json
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ── Configuracion ─────────────────────────────────────────────────────────────
load_dotenv('config.env')

API_KEY        = os.getenv('BINANCE_API_KEY')
API_SECRET     = os.getenv('BINANCE_SECRET_KEY')
TG_TOKEN       = os.getenv('TELEGRAM_TOKEN')
TG_CHAT_ID     = os.getenv('TELEGRAM_CHAT_ID')
TRADE_USDT     = float(os.getenv('TRADE_AMOUNT_USDT', '10'))
PROFIT_PCT     = float(os.getenv('PROFIT_PCT', '0.4'))
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '10'))
SYMBOLS        = [s.strip() for s in os.getenv('SYMBOLS', 'PEPEUSDT').split(',')]
STATE_FILE     = 'estado.json'
GANANCIAS_FILE = 'ganancias.json'
BLACKLIST_FILE = 'blacklist.json'
RIESGO_FILE    = 'riesgo.json'
TRAILING_PCT        = 0.15  # trailing: vende si baja 0.15% desde el maximo
TRAILING_ACTIVACION = 0.5   # trailing activa desde el mismo objetivo (PROFIT_PCT)
MIN_VALOR_VENTA     = 0.50  # ignorar balances menores a $0.50 USDT (dust)

# ── Circuit Breaker ───────────────────────────────────────────────────────────
CB_PERDIDAS_CONSECUTIVAS = 3      # Nivel 1: 3 perdidas seguidas → pausa 15 min
CB_DRAWDOWN_6H           = 2.0    # Nivel 2: -2% en 6h → pausa 2h + diagnostico
CB_DRAWDOWN_DIARIO       = 5.0    # Nivel 3: -5% diario → parada total
CB_SLIPPAGE_MULT         = 2.0    # Nivel 1: slippage real > 2x estimado → pausa

# ── Nuevos parametros de precision ────────────────────────────────────────────
TIME_STOP_HORAS       = 6     # Horas max en posicion antes de forzar venta
INTERVALO_MIN_TRADES  = 120   # Segundos minimos entre operaciones completadas
OB_IMBALANCE_MIN      = 0.38  # Ratio minimo de presion compradora (bajo=vendedores dominan)
TRADES_CSV            = 'trades_log.csv'

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('operaciones.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ── Cliente Binance ───────────────────────────────────────────────────────────
client = Client(API_KEY, API_SECRET)
server_time = client.get_server_time()
client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)
log.info(f"Reloj sincronizado con Binance: offset {client.timestamp_offset}ms")

# ── Telegram ──────────────────────────────────────────────────────────────────
def telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TG_CHAT_ID, 'text': mensaje}, timeout=5)
    except Exception:
        pass

# ── Sistema de Riesgo ────────────────────────────────────────────────────────
def cargar_riesgo():
    if not os.path.exists(RIESGO_FILE):
        return {
            'perdidas_consecutivas': 0,
            'capital_inicio_dia': None,
            'capital_inicio_6h': None,
            'ts_inicio_6h': None,
            'historial_rendimientos': [],   # lista de % por ciclo (ultimos 50)
            'slippage_estimado': 0.002,     # 0.2% inicial (fees)
            'circuit_breaker': 0,           # 0=libre, 1=pausado, 2=extendido, 3=total
            'cb_hasta': None,               # timestamp hasta cuando pausa
            'cb_razon': ''
        }
    with open(RIESGO_FILE, 'r') as f:
        return json.load(f)

def guardar_riesgo(data):
    with open(RIESGO_FILE, 'w') as f:
        json.dump(data, f)

def verificar_circuit_breaker(riesgo, capital_actual):
    """Evalua si se debe activar algun nivel de circuit breaker."""
    ahora = datetime.now()

    # Verificar si el CB activo ya expiró
    if riesgo['circuit_breaker'] in (1, 2) and riesgo['cb_hasta']:
        cb_hasta = datetime.strptime(riesgo['cb_hasta'], '%Y-%m-%d %H:%M:%S')
        if ahora > cb_hasta:
            log.info(f"Circuit Breaker expirado — reanudando operacion")
            telegram(f"✅ Circuit Breaker liberado\nReanudando operaciones normales")
            riesgo['circuit_breaker'] = 0
            riesgo['cb_hasta'] = None
            riesgo['cb_razon'] = ''
            riesgo['perdidas_consecutivas'] = 0
            guardar_riesgo(riesgo)
            return riesgo, 0

    if riesgo['circuit_breaker'] == 3:
        return riesgo, 3  # Nivel 3 nunca se auto-libera

    # Inicializar capital inicio dia
    hoy = ahora.strftime('%Y-%m-%d')
    if not riesgo['capital_inicio_dia'] or not riesgo.get('fecha_inicio_dia') == hoy:
        riesgo['capital_inicio_dia'] = capital_actual
        riesgo['fecha_inicio_dia'] = hoy

    # Inicializar ventana 6h
    if not riesgo['ts_inicio_6h']:
        riesgo['capital_inicio_6h'] = capital_actual
        riesgo['ts_inicio_6h'] = ahora.strftime('%Y-%m-%d %H:%M:%S')
    else:
        ts_6h = datetime.strptime(riesgo['ts_inicio_6h'], '%Y-%m-%d %H:%M:%S')
        if (ahora - ts_6h).seconds > 21600:  # reset cada 6h
            riesgo['capital_inicio_6h'] = capital_actual
            riesgo['ts_inicio_6h'] = ahora.strftime('%Y-%m-%d %H:%M:%S')

    # Drawdown diario
    drawdown_diario = (capital_actual - riesgo['capital_inicio_dia']) / riesgo['capital_inicio_dia'] * 100
    # Drawdown 6h
    drawdown_6h = (capital_actual - riesgo['capital_inicio_6h']) / riesgo['capital_inicio_6h'] * 100

    # ── NIVEL 3: Drawdown diario > 5% ────────────────────────────────────────
    if drawdown_diario <= -CB_DRAWDOWN_DIARIO:
        if riesgo['circuit_breaker'] != 3:
            riesgo['circuit_breaker'] = 3
            riesgo['cb_razon'] = f"Drawdown diario {drawdown_diario:.2f}%"
            guardar_riesgo(riesgo)
            msg = (f"🚨 CIRCUIT BREAKER NIVEL 3\n"
                   f"Drawdown diario: {drawdown_diario:.2f}%\n"
                   f"BOT DETENIDO — requiere revision manual\n"
                   f"Revisa operaciones.log y reinicia manualmente")
            log.critical(f"[CB-3] {riesgo['cb_razon']}")
            telegram(msg)
        return riesgo, 3

    # ── NIVEL 2: Drawdown 6h > 2% ────────────────────────────────────────────
    if drawdown_6h <= -CB_DRAWDOWN_6H and riesgo['circuit_breaker'] < 2:
        hasta = (ahora + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        riesgo['circuit_breaker'] = 2
        riesgo['cb_hasta'] = hasta
        riesgo['cb_razon'] = f"Drawdown 6h {drawdown_6h:.2f}%"
        guardar_riesgo(riesgo)
        msg = (f"⛔ CIRCUIT BREAKER NIVEL 2\n"
               f"Drawdown 6h: {drawdown_6h:.2f}%\n"
               f"Pausa 2 horas hasta {hasta}\n"
               f"Diagnostico: revisa las ultimas operaciones")
        log.warning(f"[CB-2] {riesgo['cb_razon']}")
        telegram(msg)
        return riesgo, 2

    # ── NIVEL 1: Perdidas consecutivas ───────────────────────────────────────
    if riesgo['perdidas_consecutivas'] >= CB_PERDIDAS_CONSECUTIVAS and riesgo['circuit_breaker'] < 1:
        hasta = (ahora + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        riesgo['circuit_breaker'] = 1
        riesgo['cb_hasta'] = hasta
        riesgo['cb_razon'] = f"{riesgo['perdidas_consecutivas']} perdidas consecutivas"
        guardar_riesgo(riesgo)
        msg = (f"⚠️ CIRCUIT BREAKER NIVEL 1\n"
               f"{riesgo['perdidas_consecutivas']} perdidas consecutivas\n"
               f"Pausa 15 minutos hasta {hasta}")
        log.warning(f"[CB-1] {riesgo['cb_razon']}")
        telegram(msg)
        return riesgo, 1

    guardar_riesgo(riesgo)
    return riesgo, riesgo['circuit_breaker']

def registrar_resultado_ciclo(riesgo, ganancia_pct, slippage_real):
    """Registra resultado de ciclo y actualiza metricas de riesgo."""
    # Perdidas consecutivas
    if ganancia_pct < 0:
        riesgo['perdidas_consecutivas'] += 1
    else:
        riesgo['perdidas_consecutivas'] = 0

    # Historial de rendimientos (max 50 ciclos)
    riesgo['historial_rendimientos'].append(ganancia_pct)
    if len(riesgo['historial_rendimientos']) > 50:
        riesgo['historial_rendimientos'].pop(0)

    # Actualizar slippage estimado (promedio movil)
    if slippage_real:
        riesgo['slippage_estimado'] = round(
            riesgo['slippage_estimado'] * 0.8 + slippage_real * 0.2, 6
        )

    guardar_riesgo(riesgo)
    return riesgo

MIN_CICLOS_SHARPE = 50  # Sharpe no actua hasta tener suficiente historial

def calcular_sharpe(riesgo):
    """Calcula Sharpe Ratio con los ultimos ciclos registrados."""
    rendimientos = riesgo['historial_rendimientos']
    if len(rendimientos) < MIN_CICLOS_SHARPE:
        return None
    import statistics
    promedio = sum(rendimientos) / len(rendimientos)
    tasa_libre = 0.0137 / 100  # 5% anual → diario → por ciclo es minimo
    std = statistics.stdev(rendimientos) if len(rendimientos) > 1 else 0.0001
    if std == 0:
        return None
    return (promedio - tasa_libre) / std

def ajustar_capital_por_sharpe(riesgo, ganancias_data):
    """Ajusta el capital operando segun Sharpe de los ultimos ciclos.
    Requiere MIN_CICLOS_SHARPE completados para evitar espirales con capital pequeño."""
    ciclos_totales = len(riesgo['historial_rendimientos'])
    if ciclos_totales < MIN_CICLOS_SHARPE:
        log.info(f"[SHARPE] Esperando {MIN_CICLOS_SHARPE} ciclos ({ciclos_totales}/{MIN_CICLOS_SHARPE}) — capital sin ajuste")
        return ganancias_data, None

    sharpe = calcular_sharpe(riesgo)
    if sharpe is None:
        return ganancias_data, None

    capital_base = TRADE_USDT
    capital_actual = ganancias_data['capital']

    if sharpe > 2.0:
        nuevo = min(capital_actual * 1.10, capital_base * 1.30)  # max +30%
        accion = f"Sharpe {sharpe:.2f} > 2.0 — capital +10%"
    elif sharpe >= 1.0:
        nuevo = capital_actual
        accion = None
    elif sharpe >= 0.5:
        nuevo = capital_actual * 0.85
        accion = f"Sharpe {sharpe:.2f} bajo — capital -15%"
    elif sharpe >= 0:
        nuevo = capital_actual * 0.70
        accion = f"Sharpe {sharpe:.2f} muy bajo — capital -30%"
    else:
        nuevo = capital_actual * 0.70
        accion = f"Sharpe {sharpe:.2f} NEGATIVO — capital -30% + diagnostico"
        telegram(f"⚠️ Sharpe negativo ({sharpe:.2f})\nRevisa las operaciones recientes\nCapital reducido al 70%")

    nuevo = max(round(nuevo, 4), 5.0)  # minimo $5 USDT para operar
    if accion:
        ganancias_data['capital'] = nuevo
        guardar_ganancias(ganancias_data)
        log.info(f"[SHARPE] {accion} | Capital: ${capital_actual:.2f} → ${nuevo:.2f}")

    return ganancias_data, sharpe

def regimen_mercado(btc_cambio_1h, btc_cambio_15m):
    """
    Clasifica el regimen de mercado actual.
    CALMA / ESTRES / CRISIS / OPORTUNIDAD
    """
    if abs(btc_cambio_15m) > 0.3:
        return "CRISIS", 0.0    # Movimiento BTC > 0.3% en 15min
    if btc_cambio_1h < -2.5 or btc_cambio_15m < -1.0:
        return "ESTRES", 0.6    # Mercado bajista fuerte → operar al 60%
    if btc_cambio_1h > 3.0:
        return "OPORTUNIDAD", 0.4  # Subida fuerte → posible reversal, 40%
    return "CALMA", 1.0            # Condiciones normales → 100%

# ── Ganancias ─────────────────────────────────────────────────────────────────
def cargar_ganancias():
    if not os.path.exists(GANANCIAS_FILE):
        return {'total_usdt': 0.0, 'capital': TRADE_USDT, 'ultima_reinversion': ''}
    with open(GANANCIAS_FILE, 'r') as f:
        data = json.load(f)
    if 'capital' not in data:
        data['capital'] = TRADE_USDT
    if 'ultima_reinversion' not in data:
        data['ultima_reinversion'] = ''
    return data

def guardar_ganancias(data):
    data['actualizado'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(GANANCIAS_FILE, 'w') as f:
        json.dump(data, f)

def reinvertir_ganancias(data):
    """Cada lunes reinvierte el 50% de las ganancias al capital."""
    hoy = datetime.now()
    if hoy.weekday() != 0:  # Solo lunes
        return data
    ultima = data.get('ultima_reinversion', '')
    if ultima == hoy.strftime('%Y-%m-%d'):
        return data  # Ya se hizo hoy

    ganancias = data['total_usdt']
    if ganancias <= 0:
        return data

    reinversion = round(ganancias * 0.5, 4)
    data['capital'] = round(data['capital'] + reinversion, 4)
    data['total_usdt'] = round(ganancias - reinversion, 4)
    data['ultima_reinversion'] = hoy.strftime('%Y-%m-%d')

    log.info(f"[REINVERSION] +${reinversion} USDT al capital | Nuevo capital: ${data['capital']} USDT")
    telegram(
        f"📈 REINVERSION SEMANAL\n"
        f"50% de ganancias reinvertido\n"
        f"+${reinversion} USDT al capital\n"
        f"Nuevo capital operando: ${data['capital']} USDT\n"
        f"Ganancias reservadas: ${data['total_usdt']} USDT"
    )
    guardar_ganancias(data)
    return data

# ── Reporte diario ───────────────────────────────────────────────────────────
REPORTE_FILE = 'reporte_diario.json'

def cargar_reporte_dia():
    hoy = datetime.now().strftime('%Y-%m-%d')
    if not os.path.exists(REPORTE_FILE):
        return {'fecha': hoy, 'ganancias': [], 'perdidas': [], 'ultimo_reporte': ''}
    with open(REPORTE_FILE, 'r') as f:
        data = json.load(f)
    if data.get('fecha') != hoy:
        # Nuevo dia — resetear
        return {'fecha': hoy, 'ganancias': [], 'perdidas': [], 'ultimo_reporte': ''}
    return data

def guardar_reporte_dia(data):
    with open(REPORTE_FILE, 'w') as f:
        json.dump(data, f)

def registrar_en_reporte(ganancia_usdt, symbol):
    data = cargar_reporte_dia()
    if ganancia_usdt >= 0:
        data['ganancias'].append({'usdt': round(ganancia_usdt, 6), 'symbol': symbol})
    else:
        data['perdidas'].append({'usdt': round(ganancia_usdt, 6), 'symbol': symbol})
    guardar_reporte_dia(data)

def enviar_reporte_diario():
    """Envia reporte a Telegram una vez al dia a las 10pm."""
    ahora = datetime.now()
    if ahora.hour != 22:
        return
    data = cargar_reporte_dia()
    if data.get('ultimo_reporte') == ahora.strftime('%Y-%m-%d'):
        return  # Ya se envio hoy

    total_ganancias = sum(g['usdt'] for g in data['ganancias'])
    total_perdidas  = sum(p['usdt'] for p in data['perdidas'])
    neto            = total_ganancias + total_perdidas
    ciclos_ganados  = len(data['ganancias'])
    ciclos_perdidos = len(data['perdidas'])
    total_ciclos    = ciclos_ganados + ciclos_perdidos
    win_rate        = (ciclos_ganados / total_ciclos * 100) if total_ciclos > 0 else 0
    emoji           = "✅" if neto >= 0 else "❌"

    msg = (
        f"📊 REPORTE DIARIO — {ahora.strftime('%d/%m/%Y')}\n"
        f"{'─'*30}\n"
        f"Ciclos totales:  {total_ciclos}\n"
        f"✅ Ganados:      {ciclos_ganados}  (+${total_ganancias:.4f} USDT)\n"
        f"❌ Perdidos:     {ciclos_perdidos}  (${total_perdidas:.4f} USDT)\n"
        f"{'─'*30}\n"
        f"{emoji} NETO DEL DIA: ${neto:+.4f} USDT\n"
        f"Win Rate: {win_rate:.1f}%\n"
    )
    log.info(f"[REPORTE] Neto dia: ${neto:+.4f} | Win rate: {win_rate:.1f}%")
    telegram(msg)
    data['ultimo_reporte'] = ahora.strftime('%Y-%m-%d')
    guardar_reporte_dia(data)

# ── Auto-actualizacion semanal de monedas ────────────────────────────────────
SYMBOLS_UPDATE_FILE   = 'ultimo_update_monedas.json'
_update_sesion_fecha  = None  # bandera en memoria — evita spam aunque el archivo no exista

def actualizar_monedas_automatico():
    """Cada domingo re-escanea Binance y actualiza la lista de monedas. Solo una vez."""
    global _update_sesion_fecha
    ahora = datetime.now()
    if ahora.weekday() != 6:  # Solo domingo
        return

    hoy = ahora.strftime('%Y-%m-%d')

    # Bandera en memoria: ya se ejecuto hoy en esta sesion
    if _update_sesion_fecha == hoy:
        return

    # Bandera en archivo (persiste entre reinicios del mismo dia)
    if os.path.exists(SYMBOLS_UPDATE_FILE):
        with open(SYMBOLS_UPDATE_FILE, 'r') as f:
            data = json.load(f)
        if data.get('fecha') == hoy:
            _update_sesion_fecha = hoy  # sincronizar bandera en memoria
            return

    # Marcar inmediatamente para no repetir aunque falle
    _update_sesion_fecha = hoy

    log.info("[AUTO-UPDATE] Actualizando lista de monedas...")
    telegram("🔄 Actualizacion semanal de monedas iniciada...")

    try:
        OBJETIVO_PCT   = 0.4
        VOLUMEN_MINIMO = 300000
        TOP_N          = 25

        info_exchange   = client.get_exchange_info()
        simbolos_activos = {
            s['symbol'] for s in info_exchange['symbols']
            if s['symbol'].endswith('USDT')
            and s['status'] == 'TRADING'
            and s['isSpotTradingAllowed']
        }

        tickers = client.get_ticker()
        pares   = [
            t for t in tickers
            if t['symbol'] in simbolos_activos
            and float(t['quoteVolume']) >= VOLUMEN_MINIMO
            and float(t['lastPrice']) > 0
            and t['symbol'].replace('USDT', '').isascii()
            and float(t['priceChangePercent']) > -20
        ]

        resultados = []
        for ticker in pares:
            symbol = ticker['symbol']
            try:
                klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=288)
                highs  = [float(k[2]) for k in klines]
                lows   = [float(k[3]) for k in klines]
                movs   = sum(1 for h, l in zip(highs, lows) if l > 0 and (h - l) / l * 100 >= OBJETIVO_PCT)
                resultados.append((movs, symbol))
                time.sleep(0.1)
            except Exception:
                pass

        resultados.sort(reverse=True)
        top_symbols = [s for _, s in resultados[:TOP_N]]

        if top_symbols:
            global SYMBOLS
            SYMBOLS = top_symbols
            symbols_str = ','.join(top_symbols)

            with open('config.env', 'r', encoding='utf-8') as f:
                config = f.read()
            import re
            config = re.sub(r'SYMBOLS=.*', f'SYMBOLS={symbols_str}', config)
            with open('config.env', 'w', encoding='utf-8') as f:
                f.write(config)

            with open(SYMBOLS_UPDATE_FILE, 'w') as f:
                json.dump({'fecha': ahora.strftime('%Y-%m-%d'), 'symbols': top_symbols}, f)

            log.info(f"[AUTO-UPDATE] {len(top_symbols)} monedas actualizadas")
            telegram(f"✅ Monedas actualizadas ({len(top_symbols)})\nTop 3: {', '.join(top_symbols[:3])}")

    except Exception as e:
        log.error(f"[AUTO-UPDATE] Error: {e}")

# ── Blacklist dinamica ────────────────────────────────────────────────────────
def cargar_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        return {}
    with open(BLACKLIST_FILE, 'r') as f:
        return json.load(f)

def guardar_blacklist(bl):
    with open(BLACKLIST_FILE, 'w') as f:
        json.dump(bl, f)

def agregar_blacklist(symbol):
    bl = cargar_blacklist()
    expira = (datetime.now() + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    bl[symbol] = expira
    guardar_blacklist(bl)
    log.warning(f"[BLACKLIST] {symbol} bloqueada por 24h hasta {expira}")

def limpiar_blacklist_expirada():
    bl = cargar_blacklist()
    ahora = datetime.now()
    activa = {s: exp for s, exp in bl.items() if datetime.strptime(exp, '%Y-%m-%d %H:%M:%S') > ahora}
    guardar_blacklist(activa)
    return activa


# ── Persistencia de estado ────────────────────────────────────────────────────
def guardar_estado(data):
    with open(STATE_FILE, 'w') as f:
        json.dump(data, f)

def cargar_estado():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return None

def borrar_estado():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

# ── Indicadores tecnicos ──────────────────────────────────────────────────────
def get_rsi(symbol, periodo=14):
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=periodo + 5)
        closes = pd.Series([float(k[4]) for k in klines])
        delta  = closes.diff()
        gain   = delta.clip(lower=0).rolling(periodo).mean()
        loss   = (-delta.clip(upper=0)).rolling(periodo).mean()
        rs     = gain / loss
        rsi    = 100 - (100 / (1 + rs))
        return round(rsi.iloc[-1], 2)
    except Exception:
        return 50.0


def evaluar_moneda(symbol):
    """
    Scoring multi-indicador. Menor score = mejor oportunidad de compra.

    Indicadores:
      RSI          — sobrevendido = mejor entrada
      Bollinger    — precio cerca del band inferior = rebote probable
      Volumen      — volumen reciente vs promedio = momentum entrando
      Momentum     — ultimas velas subiendo tras caida = rebote confirmado
      Liquidez     — volumen total 24h = menos slippage al vender
    """
    try:
        klines  = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=50)
        closes  = pd.Series([float(k[4]) for k in klines])
        volumes = pd.Series([float(k[5]) for k in klines])
        precio  = closes.iloc[-1]

        # ── RSI ──────────────────────────────────────────────────────────────
        delta = closes.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        rsi   = round((100 - (100 / (1 + rs))).iloc[-1], 2)

        # ── EMA ──────────────────────────────────────────────────────────────
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]

        # ── Bollinger Bands (20 periodos, 2 desviaciones) ─────────────────────
        sma20   = closes.rolling(20).mean().iloc[-1]
        std20   = closes.rolling(20).std().iloc[-1]
        bb_inf  = sma20 - 2 * std20
        bb_sup  = sma20 + 2 * std20
        bb_rango = bb_sup - bb_inf if bb_sup != bb_inf else 1
        bb_pos  = (precio - bb_inf) / bb_rango  # 0=band inf, 1=band sup

        # ── Volumen creciente ─────────────────────────────────────────────────
        vol_promedio  = volumes.iloc[:-5].mean()
        vol_reciente  = volumes.iloc[-5:].mean()
        vol_ratio     = vol_reciente / vol_promedio if vol_promedio > 0 else 1

        # ── Momentum (rebote: caida seguida de subida) ────────────────────────
        cambio_3      = (closes.iloc[-1] - closes.iloc[-4]) / closes.iloc[-4] * 100
        ultima_vela   = closes.iloc[-1] - closes.iloc[-2]
        rebote        = ultima_vela > 0 and cambio_3 < 0  # subiendo tras caida

        # ── Scoring (menor = mejor) ───────────────────────────────────────────
        score = rsi                           # base: RSI

        if precio < ema20:
            score -= 8                        # bonus: debajo de EMA

        if bb_pos < 0.2:
            score -= 12                       # bonus fuerte: cerca de Bollinger inferior
        elif bb_pos < 0.35:
            score -= 6                        # bonus moderado

        if vol_ratio > 1.5:
            score -= 8                        # bonus: volumen creciente (momentum)
        elif vol_ratio > 1.2:
            score -= 4

        if rebote:
            score -= 6                        # bonus: iniciando rebote

        return score, rsi, precio, bb_pos, vol_ratio, rebote

    except Exception:
        precio = float(client.get_symbol_ticker(symbol=symbol)['price'])
        return 50.0, 50.0, precio, 0.5, 1.0, False

def calcular_objetivo_dinamico(symbol):
    """
    Calcula el objetivo de ganancia segun la volatilidad real de la moneda.
    Si la moneda se mueve poco → objetivo conservador.
    Si se mueve mucho → objetivo mayor para no vender en el primer rebote.
    Rango: min 0.8%, max 2.5%
    """
    try:
        klines   = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=48)
        rangos   = [(float(k[2]) - float(k[3])) / float(k[3]) * 100 for k in klines if float(k[3]) > 0]
        avg_rango = sum(rangos) / len(rangos) if rangos else 1.0
        # El objetivo es la mitad del movimiento promedio, ajustado por fees
        objetivo = max(0.8, min(2.5, avg_rango * 0.6))
        return round(objetivo, 2)
    except Exception:
        return PROFIT_PCT

def imbalance_orderbook(symbol, capital_operando):
    """
    Analiza el desbalance del order book.
    Retorna (ratio, presion, liquidez_ok)
    ratio < OB_IMBALANCE_MIN = vendedores dominan = no comprar
    """
    try:
        ob       = client.get_order_book(symbol=symbol, limit=5)
        bids     = ob['bids']  # [[precio, cantidad], ...]
        asks     = ob['asks']
        p_compra = sum(float(p) * float(q) for p, q in bids)
        p_venta  = sum(float(p) * float(q) for p, q in asks)
        total    = p_compra + p_venta
        ratio    = p_compra / total if total > 0 else 0.5

        if ratio < OB_IMBALANCE_MIN:
            presion = "VENDEDORA"
        elif ratio > 0.62:
            presion = "COMPRADORA"
        else:
            presion = "NEUTRA"

        # Liquidez: top-3 asks debe tener al menos 2x nuestra orden
        precio_ask  = float(asks[0][0])
        mi_orden    = capital_operando / precio_ask if precio_ask > 0 else 0
        liq_asks    = sum(float(q) for _, q in asks[:3])
        liquidez_ok = liq_asks >= mi_orden * 2

        return ratio, presion, liquidez_ok
    except Exception:
        return 0.5, "NEUTRA", True

def validar_pre_trade(symbol, capital_operando, riesgo, ultimo_trade_ts):
    """
    Checklist pre-trade. Retorna (ok, razon_rechazo).
    TODOS los checks deben pasar para operar.
    """
    ahora = datetime.now()

    # 1. Capital operativo suficiente
    if capital_operando < 5.0:
        return False, f"Capital insuficiente: ${capital_operando:.2f} (min $5)"

    # 2. Intervalo minimo entre trades
    if ultimo_trade_ts:
        segs = (ahora - ultimo_trade_ts).total_seconds()
        if segs < INTERVALO_MIN_TRADES:
            return False, f"Intervalo minimo: espera {int(INTERVALO_MIN_TRADES - segs)}s mas"

    # 3. Circuit breaker no activo
    if riesgo['circuit_breaker'] > 0:
        return False, f"Circuit Breaker nivel {riesgo['circuit_breaker']} activo"

    # 4. Perdidas consecutivas bajo limite
    if riesgo['perdidas_consecutivas'] >= CB_PERDIDAS_CONSECUTIVAS:
        return False, f"Perdidas consecutivas: {riesgo['perdidas_consecutivas']}/{CB_PERDIDAS_CONSECUTIVAS}"

    # 5. Order book: liquidez y presion compradora
    ratio, presion, liquidez_ok = imbalance_orderbook(symbol, capital_operando)
    if presion == "VENDEDORA":
        return False, f"Order book dominado por vendedores (ratio={ratio:.2f})"
    if not liquidez_ok:
        return False, f"Liquidez insuficiente en order book"

    return True, ""

def historial_por_moneda():
    """
    Lee trades_log.csv y calcula el rendimiento real de cada moneda.
    Ignora cierres por time-stop (duracion > 5h) ya que ese mecanismo fue eliminado.
    Retorna dict: {symbol: {'wins': int, 'stop_losses': int, 'score_adj': float}}
    score_adj negativo = moneda mejor de lo esperado (baja su score = mas prioridad)
    score_adj positivo = moneda problematica (sube su score = menos prioridad)
    """
    if not os.path.exists(TRADES_CSV):
        return {}
    try:
        import csv
        stats = {}
        with open(TRADES_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym      = row['par']
                pct      = float(row.get('ganancia_pct', 0))
                razon    = row.get('razon_cierre', '')
                dur_seg  = int(row.get('duracion_seg', 0))
                # Ignorar ventas que fueron por time-stop del sistema anterior (>5h)
                if dur_seg >= 18000 and razon != 'STOP_LOSS':
                    continue
                if sym not in stats:
                    stats[sym] = {'wins': 0, 'losses': 0, 'stop_losses': 0}
                if razon == 'STOP_LOSS':
                    stats[sym]['stop_losses'] += 1
                    stats[sym]['losses'] += 1
                elif pct >= 0.3:
                    stats[sym]['wins'] += 1
                elif pct < -0.5:
                    stats[sym]['losses'] += 1

        # Calcular ajuste de score
        resultado = {}
        for sym, s in stats.items():
            total = s['wins'] + s['losses']
            if total < 2:
                resultado[sym] = {'wins': s['wins'], 'stop_losses': s['stop_losses'], 'score_adj': 0}
                continue
            win_rate   = s['wins'] / total
            stop_lrate = s['stop_losses'] / total
            adj = 0
            if stop_lrate >= 0.5:
                adj += 25    # muchos stop loss: evitar fuertemente
            elif s['stop_losses'] >= 1:
                adj += 10    # al menos un stop loss: penalizar
            if win_rate >= 0.70 and s['wins'] >= 2:
                adj -= 10    # buen historial: preferir
            elif win_rate >= 0.50 and s['wins'] >= 2:
                adj -= 5
            resultado[sym] = {'wins': s['wins'], 'stop_losses': s['stop_losses'], 'score_adj': adj}
        return resultado
    except Exception as e:
        log.warning(f"[HISTORIAL] Error leyendo CSV: {e}")
        return {}

def registrar_csv(symbol, precio_compra, precio_venta, qty, ganancia_neta,
                  ganancia_pct, slippage, capital_post, duracion_seg, razon_cierre):
    """Guarda cada operacion en trades_log.csv para analisis posterior."""
    import csv
    existe = os.path.exists(TRADES_CSV)
    try:
        with open(TRADES_CSV, 'a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if not existe:
                w.writerow(['timestamp', 'par', 'precio_compra', 'precio_venta', 'cantidad',
                            'ganancia_neta_usdt', 'ganancia_pct', 'slippage_pct',
                            'capital_post', 'duracion_seg', 'razon_cierre'])
            w.writerow([
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                symbol,
                round(precio_compra, 8), round(precio_venta, 8), qty,
                round(ganancia_neta, 6), round(ganancia_pct, 4),
                round(slippage * 100, 4), round(capital_post, 4),
                int(duracion_seg), razon_cierre
            ])
    except Exception as e:
        log.warning(f"[CSV] No se pudo guardar: {e}")

def estado_btc():
    """
    Revisa tendencia BTC en 1h y 15min con velas reales.
    Retorna (seguro, cambio_1h, tendencia)
    """
    try:
        # Velas de 1h — ultimas 2 para cambio real de 1h
        klines_1h  = client.get_klines(symbol='BTCUSDT', interval=Client.KLINE_INTERVAL_1HOUR, limit=2)
        closes_1h  = [float(k[4]) for k in klines_1h]
        cambio_1h  = (closes_1h[-1] - closes_1h[0]) / closes_1h[0] * 100

        # Velas de 15min — ultimas 2 para cambio real de 15min
        klines_15m = client.get_klines(symbol='BTCUSDT', interval=Client.KLINE_INTERVAL_15MINUTE, limit=2)
        closes_15m = [float(k[4]) for k in klines_15m]
        cambio_15m = (closes_15m[-1] - closes_15m[0]) / closes_15m[0] * 100

        if cambio_1h < -2.5 or cambio_15m < -1.0:
            return False, cambio_1h, "BAJISTA FUERTE ⚠"
        elif cambio_1h < -1.0:
            return True, cambio_1h, "BAJISTA LEVE"
        elif cambio_1h > 1.0:
            return True, cambio_1h, "ALCISTA ✓"
        else:
            return True, cambio_1h, "NEUTRAL"
    except Exception:
        return True, 0.0, "DESCONOCIDO"

COOLDOWN_GANADOR_MIN = 20  # minutos de espera antes de re-entrar a una moneda que acaba de ganar

def elegir_mejor_moneda(cooldown_ganadores=None):
    log.info("Analizando monedas con scoring avanzado + historial...")

    # Termometro BTC
    btc_seguro, btc_cambio, btc_tendencia = estado_btc()
    log.info(f"  BTC 1h: {btc_cambio:+.2f}% — {btc_tendencia}")

    if not btc_seguro:
        log.info(f"  BTC bajista — analizando monedas de todos modos (solo CRISIS bloquea)")

    blacklist  = limpiar_blacklist_expirada()
    historial  = historial_por_moneda()
    cooldowns  = cooldown_ganadores or {}
    ahora      = datetime.now()
    resultados = []

    for symbol in SYMBOLS:
        if symbol in blacklist:
            log.info(f"  {symbol:<16} BLACKLISTED")
            continue

        # Cooldown: moneda que acabo de ganar — esperar antes de re-entrar
        if symbol in cooldowns:
            mins_rest = (cooldowns[symbol] - ahora).total_seconds() / 60
            if mins_rest > 0:
                log.info(f"  {symbol:<16} COOLDOWN ({mins_rest:.0f} min restantes)")
                continue

        try:
            score, rsi, precio, bb_pos, vol_ratio, rebote = evaluar_moneda(symbol)

            # Ajuste por historial real de la moneda
            hist = historial.get(symbol, {})
            adj  = hist.get('score_adj', 0)
            wins = hist.get('wins', 0)
            sls  = hist.get('stop_losses', 0)
            score_final = score + adj

            rebote_txt = "REBOTE✓" if rebote else ""
            hist_txt   = f"Hist:{wins}G/{sls}SL" if wins + sls > 0 else "Hist:nuevo"
            adj_txt    = f"({adj:+.0f})" if adj != 0 else ""
            log.info(f"  {symbol:<16} RSI={rsi:<6} Score={score_final:>6.1f}{adj_txt:<5}  BB:{bb_pos:.2f}  Vol:{vol_ratio:.1f}x  {hist_txt}  {rebote_txt}")
            resultados.append((score_final, symbol, rsi, precio))
        except Exception as e:
            log.warning(f"  {symbol} error: {e}")

    if not resultados:
        symbol = SYMBOLS[0]
        precio = float(client.get_symbol_ticker(symbol=symbol)['price'])
        return symbol, 50.0, precio

    resultados.sort(key=lambda x: x[0])
    mejor = resultados[0]
    log.info(f"Seleccionada: {mejor[1]} (Score final={mejor[0]:.1f})")
    return mejor[1], mejor[2], mejor[3]

# ── Limpieza automatica al iniciar ───────────────────────────────────────────
def limpiar_monedas_sueltas():
    try:
        balances = client.get_account()['balances']
        for b in balances:
            asset  = b['asset']
            libre  = float(b['free'])
            if asset == 'USDT' or libre <= 0:
                continue
            symbol = asset + 'USDT'
            try:
                precio_coin = float(client.get_symbol_ticker(symbol=symbol)['price'])
                valor_usdt  = libre * precio_coin
                if valor_usdt < MIN_VALOR_VENTA:
                    log.info(f"Dust ignorado: {libre} {asset} (${valor_usdt:.4f} USDT)")
                    continue
                step, dec = get_step_size(symbol)
                qty       = round_qty(libre, step, dec)
                if qty <= 0:
                    continue
                log.info(f"Moneda suelta detectada: {qty} {asset} — vendiendo...")
                order     = client.order_market_sell(symbol=symbol, quantity=qty)
                filled    = float(order['executedQty'])
                avg_price = float(order['cummulativeQuoteQty']) / filled
                log.info(f"Vendido {filled} {asset} a ${avg_price:.8f} | Recibido: ${filled * avg_price:.4f} USDT")
            except Exception as e:
                log.warning(f"No se pudo vender {asset}: {e}")
    except Exception as e:
        log.warning(f"Error al limpiar monedas: {e}")
    borrar_estado()

# ── Utilidades Binance ────────────────────────────────────────────────────────
def get_price(symbol):
    return float(client.get_symbol_ticker(symbol=symbol)['price'])

def get_step_size(symbol):
    info = client.get_symbol_info(symbol)
    for f in info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            step     = float(f['stepSize'])
            decimals = len(f['stepSize'].rstrip('0').split('.')[-1])
            return step, decimals
    return 1.0, 0

def round_qty(qty, step, decimals):
    return round((qty // step) * step, decimals)


def get_balance_coin(symbol):
    asset = symbol.replace('USDT', '')
    return float(client.get_asset_balance(asset=asset)['free'])

# ── Operaciones ───────────────────────────────────────────────────────────────
def ejecutar_compra(symbol, capital):
    price     = get_price(symbol)
    step, dec = get_step_size(symbol)
    qty       = round_qty(capital / price, step, dec)
    order     = client.order_market_buy(symbol=symbol, quantity=qty)
    filled    = float(order['executedQty'])
    avg_price = float(order['cummulativeQuoteQty']) / filled
    log.info(f"[COMPRA] {filled} {symbol[:-4]} a ${avg_price:.8f} | Total: ${filled * avg_price:.4f} USDT")
    return avg_price, filled

def ejecutar_venta(symbol):
    balance_real = get_balance_coin(symbol)
    step, dec    = get_step_size(symbol)
    # Vender TODO el balance real (incluye remanentes de ciclos anteriores)
    qty_real     = round_qty(balance_real, step, dec)
    if qty_real <= 0:
        log.warning(f"Sin balance de {symbol} para vender, limpiando estado.")
        borrar_estado()
        return None
    order     = client.order_market_sell(symbol=symbol, quantity=qty_real)
    filled    = float(order['executedQty'])
    avg_price = float(order['cummulativeQuoteQty']) / filled
    log.info(f"[VENTA]  {filled} {symbol[:-4]} a ${avg_price:.8f} | Total: ${filled * avg_price:.4f} USDT")
    return avg_price

# ── Dashboard ─────────────────────────────────────────────────────────────────
def print_dashboard(estado, symbol, precio_actual, precio_compra, objetivo_venta, precio_maximo, qty, ciclos, ganancias_data, rsi_actual, btc_info=None):
    os.system('cls')
    pct   = ((precio_actual - precio_compra) / precio_compra * 100) if precio_compra else 0
    barra = "▲" if pct >= 0 else "▼"
    capital_actual = ganancias_data['capital']
    ganancias_usdt = ganancias_data['total_usdt']

    print("╔══════════════════════════════════════════════════╗")
    print(f"║   BOT INTELIGENTE MULTI-MONEDA                   ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Moneda activa:  {symbol:<32} ║")
    print(f"║  Estado:         {estado:<32} ║")
    print(f"║  Precio actual:  ${precio_actual:<31.8f} ║")
    if precio_compra:
        print(f"║  Precio compra:  ${precio_compra:<31.8f} ║")
        print(f"║  Objetivo venta: ${objetivo_venta:<31.8f} ║")
        if precio_maximo:
            trailing = precio_maximo * (1 - TRAILING_PCT / 100)
            print(f"║  Trailing stop:  ${trailing:<31.8f} ║")
        print(f"║  Movimiento:     {barra} {abs(pct):.2f}%{'':<25} ║")
        if rsi_actual:
            zona = "SOBREVENDIDO ✓" if rsi_actual < 35 else ("SOBRECOMPRADO" if rsi_actual > 65 else "NEUTRAL")
            print(f"║  RSI:            {rsi_actual:<6} {zona:<26} ║")
    if qty:
        print(f"║  Cantidad:       {qty:<32} ║")
    print("╠══════════════════════════════════════════════════╣")
    if btc_info:
        btc_cambio, btc_tendencia = btc_info
        print(f"║  BTC 1h:  {btc_cambio:+.2f}%  {btc_tendencia:<33} ║")
    print(f"║  Ciclos completados: {ciclos:<28} ║")
    print(f"║  Ganancias reservadas: ${ganancias_usdt:<25.4f} ║")
    print(f"║  Capital operando:     ${capital_actual:<25.2f} ║")
    print(f"║  Hora: {datetime.now().strftime('%H:%M:%S'):<41} ║")
    print("╚══════════════════════════════════════════════════╝")
    print("  Presiona Ctrl+C para pausar el bot")

# ── Loop principal ────────────────────────────────────────────────────────────
def run():
    log.info("=" * 55)
    log.info(f"Bot iniciado | Objetivo: +{PROFIT_PCT}% | Trailing: -{TRAILING_PCT}% | Sin Stop Loss")
    log.info(f"Monedas: {', '.join(SYMBOLS)}")
    log.info("=" * 55)

    ganancias_data = cargar_ganancias()

    # Usar capital REAL de Binance, no el guardado en archivo
    try:
        capital_real = float(client.get_asset_balance(asset='USDT')['free'])
        if capital_real > 0.5:
            ganancias_data['capital'] = round(capital_real, 4)
            guardar_ganancias(ganancias_data)
            log.info(f"Capital real de Binance: ${capital_real:.4f} USDT")
    except Exception as e:
        log.warning(f"No se pudo obtener capital real: {e}")

    telegram(f"🤖 Bot iniciado\nCapital real: ${ganancias_data['capital']:.4f} USDT\nMonedas: {len(SYMBOLS)}\nObjetivo: +{PROFIT_PCT}% | Sin Stop Loss | Trailing: -{TRAILING_PCT}%\n\nAnalizando mercado...")

    ciclos = 0
    limpiar_monedas_sueltas()

    estado_previo = cargar_estado()
    if estado_previo:
        symbol         = estado_previo['symbol']
        estado         = estado_previo['estado']
        precio_compra  = estado_previo['precio_compra']
        objetivo_venta = estado_previo['objetivo_venta']
        qty            = estado_previo['qty']
        precio_maximo  = estado_previo.get('precio_maximo', precio_compra)
        ciclos         = estado_previo.get('ciclos', 0)
        ts_compra_str  = estado_previo.get('ts_compra')
        ts_compra      = datetime.strptime(ts_compra_str, '%Y-%m-%d %H:%M:%S') if ts_compra_str else datetime.now()
        log.info(f"Estado recuperado: {estado} | {symbol} | Precio compra: ${precio_compra}")
    else:
        estado         = "ANALIZANDO"
        symbol         = SYMBOLS[0]
        precio_compra  = None
        objetivo_venta = None
        qty            = None
        precio_maximo  = None
        ts_compra      = None

    rsi_actual           = None
    ultimo_aviso_bajista = None
    ultimo_trade_ts      = None
    cooldown_ganadores   = {}
    riesgo               = cargar_riesgo()
    ultimo_sharpe_check  = datetime.now()
    ultimo_aviso_vivo    = datetime.now()

    while True:
        try:
            # ── Tareas periodicas ─────────────────────────────────────────────
            ganancias_data = reinvertir_ganancias(ganancias_data)
            capital_actual = ganancias_data['capital']
            enviar_reporte_diario()
            actualizar_monedas_automatico()

            # Aviso de vida cada 2 horas — confirma que el bot sigue activo
            ahora = datetime.now()
            if (ahora - ultimo_aviso_vivo).total_seconds() >= 18000:
                try:
                    capital_binance = float(client.get_asset_balance(asset='USDT')['free'])
                except Exception:
                    capital_binance = capital_actual
                estado_txt = f"En posicion: {symbol}" if estado == "ESPERANDO_SUBIDA" else "Buscando oportunidad"
                telegram(
                    f"🟢 Bot activo\n"
                    f"Capital: ${capital_binance:.4f} USDT\n"
                    f"Ciclos ganados: {ciclos}\n"
                    f"Estado: {estado_txt}"
                )
                ultimo_aviso_vivo = ahora

            # ── Sharpe + ajuste de capital cada 10 ciclos ─────────────────────
            if ciclos > 0 and ciclos % 10 == 0 and (datetime.now() - ultimo_sharpe_check).seconds > 300:
                ganancias_data, sharpe = ajustar_capital_por_sharpe(riesgo, ganancias_data)
                capital_actual = ganancias_data['capital']
                ultimo_sharpe_check = datetime.now()

            # ── Circuit Breaker ───────────────────────────────────────────────
            riesgo, nivel_cb = verificar_circuit_breaker(riesgo, capital_actual)
            if nivel_cb == 3:
                print_dashboard("🚨 DETENIDO — CB NIVEL 3", symbol, 0, None, None, None, None, ciclos, ganancias_data, None)
                log.critical("Circuit Breaker Nivel 3 activo — deteniendo bot")
                break
            if nivel_cb in (1, 2):
                cb_hasta = riesgo.get('cb_hasta', '')
                print_dashboard(f"⛔ CB NIVEL {nivel_cb} — pausado hasta {cb_hasta}", symbol, 0, None, None, None, None, ciclos, ganancias_data, None)
                time.sleep(60)
                continue

            precio_actual = get_price(symbol)

            # ── Regimen de mercado ────────────────────────────────────────────
            btc_seguro, btc_c, btc_t = estado_btc()
            regimen, factor_capital  = regimen_mercado(btc_c, (btc_c / 12))  # aprox 15m
            capital_operando         = round(capital_actual, 4)  # usa todo el USDT disponible

            # ── ANALIZAR ──────────────────────────────────────────────────────
            if estado == "ANALIZANDO":
                print_dashboard(f"Analizando... [{regimen}]", symbol, precio_actual, None, None, None, None, ciclos, ganancias_data, None, btc_info=(btc_c, btc_t))

                if regimen == "CRISIS":
                    log.warning("Regimen CRISIS — bot en espera")
                    ahora = datetime.now()
                    if not ultimo_aviso_bajista or (ahora - ultimo_aviso_bajista).seconds > 1800:
                        telegram(f"🚨 Regimen CRISIS detectado\nBTC: {btc_c:+.2f}%\nBot en espera...")
                        ultimo_aviso_bajista = ahora
                    time.sleep(120)
                    continue

                symbol_elegido, rsi_elegido, precio_elegido = elegir_mejor_moneda(cooldown_ganadores)
                if symbol_elegido is None:
                    ahora = datetime.now()
                    if not ultimo_aviso_bajista or (ahora - ultimo_aviso_bajista).seconds > 1800:
                        telegram(f"⚠️ BTC bajista ({btc_c:+.2f}%)\nBot en pausa temporal...")
                        ultimo_aviso_bajista = ahora
                    time.sleep(120)
                else:
                    symbol        = symbol_elegido
                    rsi_actual    = rsi_elegido
                    precio_actual = precio_elegido
                    log.info(f"Regimen: {regimen} | Capital operando: ${capital_operando} | Moneda: {symbol}")
                    estado = "COMPRANDO"

            # ── COMPRAR ───────────────────────────────────────────────────────
            elif estado == "COMPRANDO":
                print_dashboard(estado, symbol, precio_actual, None, None, None, None, ciclos, ganancias_data, rsi_actual, btc_info=(btc_c, btc_t))

                # Checklist pre-trade: todos los checks deben pasar
                ok, razon = validar_pre_trade(symbol, capital_operando, riesgo, ultimo_trade_ts)
                if not ok:
                    log.warning(f"[PRE-TRADE] Check fallido: {razon} — volviendo a analizar")
                    estado = "ANALIZANDO"
                    time.sleep(CHECK_INTERVAL)
                    continue

                precio_esperado       = get_price(symbol)
                precio_compra, qty    = ejecutar_compra(symbol, capital_operando)
                slippage_real         = abs(precio_compra - precio_esperado) / precio_esperado
                profit_dinamico       = calcular_objetivo_dinamico(symbol)
                objetivo_venta        = precio_compra * (1 + profit_dinamico / 100)
                precio_maximo         = precio_compra
                ts_compra             = datetime.now()
                estado                = "ESPERANDO_SUBIDA"

                # Circuit breaker nivel 1 por slippage excesivo
                if slippage_real > riesgo['slippage_estimado'] * CB_SLIPPAGE_MULT:
                    log.warning(f"[SLIPPAGE] Real {slippage_real:.4f} > Estimado {riesgo['slippage_estimado']:.4f} x{CB_SLIPPAGE_MULT}")
                    riesgo['perdidas_consecutivas'] += 1

                guardar_estado({'symbol': symbol, 'estado': estado, 'precio_compra': precio_compra,
                                'objetivo_venta': objetivo_venta, 'qty': qty,
                                'precio_maximo': precio_maximo, 'ciclos': ciclos,
                                'ts_compra': ts_compra.strftime('%Y-%m-%d %H:%M:%S')})
                telegram(f"🟢 COMPRA [{regimen}]\nMoneda: {symbol}\nPrecio: ${precio_compra:.8f}\nObjetivo: ${objetivo_venta:.8f} (+{profit_dinamico}%)\nRSI: {rsi_actual}\nCapital: ${capital_operando}")

            # ── ESPERAR SUBIDA ────────────────────────────────────────────────
            elif estado == "ESPERANDO_SUBIDA":
                rsi_actual      = get_rsi(symbol)
                _, btc_c, btc_t = estado_btc()

                ts_compra_local = ts_compra if ts_compra else datetime.now()

                if precio_actual > precio_maximo:
                    precio_maximo = precio_actual
                    guardar_estado({'symbol': symbol, 'estado': estado, 'precio_compra': precio_compra,
                                    'objetivo_venta': objetivo_venta, 'qty': qty,
                                    'precio_maximo': precio_maximo, 'ciclos': ciclos,
                                    'ts_compra': ts_compra_local.strftime('%Y-%m-%d %H:%M:%S')})

                # Trailing solo activa cuando alcanzamos el objetivo — garantiza ganancia
                umbral_trailing = objetivo_venta
                trailing_stop   = precio_maximo * (1 - TRAILING_PCT / 100)
                ya_activo_trail = precio_maximo >= umbral_trailing
                horas_en_pos    = (datetime.now() - ts_compra_local).total_seconds() / 3600
                caida_pct       = (precio_actual - precio_compra) / precio_compra * 100

                if ya_activo_trail:
                    estado_txt = f"Trailing activo — max ${precio_maximo:.8f}"
                else:
                    estado_txt = f"Esperando subida... {caida_pct:+.2f}% ({horas_en_pos:.1f}h)"

                print_dashboard(estado_txt, symbol, precio_actual, precio_compra, objetivo_venta, precio_maximo, qty, ciclos, ganancias_data, rsi_actual, btc_info=(btc_c, btc_t))

                if ya_activo_trail and precio_actual <= trailing_stop:
                    log.info(f"[TRAILING] Max=${precio_maximo:.8f} | Vendiendo en ${precio_actual:.8f}")
                    estado = "VENDIENDO"
                elif not ya_activo_trail and precio_actual >= objetivo_venta:
                    # Llego al objetivo — activa trailing en lugar de vender directo
                    log.info(f"[TRAILING ACTIVADO] Precio supero objetivo ${objetivo_venta:.8f} — siguiendo subida")

            # ── VENDER ────────────────────────────────────────────────────────
            elif estado == "VENDIENDO":
                print_dashboard("VENDIENDO...", symbol, precio_actual, precio_compra, objetivo_venta, precio_maximo, qty, ciclos, ganancias_data, rsi_actual)
                precio_esperado  = precio_actual
                precio_venta     = ejecutar_venta(symbol)
                ts_compra_local  = ts_compra if ts_compra else datetime.now()
                duracion_seg     = (datetime.now() - ts_compra_local).total_seconds()
                if precio_venta:
                    ganancia_ciclo = (precio_venta - precio_compra) * qty
                    ganancia_pct   = (precio_venta - precio_compra) / precio_compra * 100
                    slippage_real  = abs(precio_venta - precio_esperado) / precio_esperado
                    ganancias_data['total_usdt'] += ganancia_ciclo
                    ciclos += 1
                    guardar_ganancias(ganancias_data)
                    riesgo = registrar_resultado_ciclo(riesgo, ganancia_pct, slippage_real)
                    registrar_en_reporte(ganancia_ciclo, symbol)
                    registrar_csv(symbol, precio_compra, precio_venta, qty,
                                  ganancia_ciclo, ganancia_pct, slippage_real,
                                  ganancias_data['capital'], duracion_seg, 'TRAILING/OBJETIVO')
                    ultimo_trade_ts = datetime.now()
                    # Cooldown: si gano, esperar antes de re-entrar a esta moneda
                    if ganancia_ciclo > 0:
                        cooldown_ganadores[symbol] = datetime.now() + timedelta(minutes=COOLDOWN_GANADOR_MIN)
                    log.info(f"[CICLO {ciclos}] {ganancia_pct:+.3f}% | ${ganancia_ciclo:+.4f} USDT | Slippage: {slippage_real*100:.4f}%")
                    sharpe = calcular_sharpe(riesgo)
                    telegram(
                        f"💰 GANANCIA — Ciclo #{ciclos}\n"
                        f"Moneda: {symbol}\n"
                        f"Compra:  ${precio_compra:.8f}\n"
                        f"Venta:   ${precio_venta:.8f}\n"
                        f"Ganancia: {ganancia_pct:+.3f}% (${ganancia_ciclo:+.4f} USDT)\n"
                        f"Reservado: ${ganancias_data['total_usdt']:.4f} USDT\n"
                        f"Capital: ${ganancias_data['capital']:.2f} USDT\n"
                        f"Sharpe: {sharpe:.2f}" if sharpe else ""
                    )
                estado = "ANALIZANDO"
                precio_compra = objetivo_venta = qty = precio_maximo = ts_compra = None
                borrar_estado()

            # Sin stop loss — el bot espera hasta ganar

            time.sleep(CHECK_INTERVAL)

        except BinanceAPIException as e:
            log.error(f"[ERROR Binance] {e}")
            time.sleep(15)

        except KeyboardInterrupt:
            if estado not in ("ANALIZANDO", "COMPRANDO"):
                guardar_estado({'symbol': symbol, 'estado': estado, 'precio_compra': precio_compra,
                                'objetivo_venta': objetivo_venta, 'qty': qty,
                                'precio_maximo': precio_maximo, 'ciclos': ciclos})
            guardar_ganancias(ganancias_data)
            log.info("Bot pausado.")
            telegram(f"⏸ Bot pausado\nReservado: ${ganancias_data['total_usdt']:.4f} USDT\nCapital: ${ganancias_data['capital']:.2f} USDT\nCiclos: {ciclos}")
            print("\nBot pausado. Al reiniciar continuara donde se quedo.")
            break

        except Exception as e:
            log.error(f"[ERROR] {e}")
            time.sleep(15)

if __name__ == "__main__":
    if 'PEGA_TU' in (API_KEY or ''):
        print("ERROR: Abre config.env y pega tus API Keys de Binance.")
        input("Presiona Enter para salir...")
    else:
        run()
