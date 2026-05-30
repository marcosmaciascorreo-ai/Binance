#!/usr/bin/env python3
"""analisis_posttrade.py — Analiza trades_log.csv y actualiza memoria de Hermes.
Ejecutar periodicamente para mantener conocimiento de que monedas funcionan.
"""
import json
import csv
import os
from datetime import datetime, timedelta

TRADES_CSV = 'trades_log.csv'
MEMORIA_FILE = 'memoria_trades.json'

def analizar():
    if not os.path.exists(TRADES_CSV):
        return []

    stats = {}
    with open(TRADES_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row['par']
            pct = float(row.get('ganancia_pct', 0))
            neto = float(row.get('ganancia_neta_usdt', 0))
            razon = row.get('razon_cierre', '')
            if sym not in stats:
                stats[sym] = {'trades': 0, 'wins': 0, 'losses': 0, 'sl': 0, 'total_neto': 0.0, 'total_pct': 0.0}
            stats[sym]['trades'] += 1
            stats[sym]['total_neto'] += neto
            stats[sym]['total_pct'] += pct
            if pct > 0:
                stats[sym]['wins'] += 1
            else:
                stats[sym]['losses'] += 1
            if razon == 'STOP_LOSS':
                stats[sym]['sl'] += 1

    resumen = []
    for sym, s in stats.items():
        if s['trades'] < 2:
            continue
        wr = s['wins'] / s['trades'] * 100
        resumen.append({
            'moneda': sym,
            'trades': s['trades'],
            'win_rate': round(wr, 1),
            'neto_usdt': round(s['total_neto'], 4),
            'pct_promedio': round(s['total_pct'] / s['trades'], 2),
            'stop_losses': s['sl'],
            'desc': f'{sym}: {s["trades"]} trades, WR {wr:.0f}%, neto ${s["total_neto"]:+.4f}'
        })

    resumen.sort(key=lambda x: x['neto_usdt'], reverse=True)

    # Guardar para uso futuro
    data = {
        'actualizado': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'total_monedas': len(resumen),
        'mejores': resumen[:5],
        'peores': resumen[-3:] if len(resumen) > 3 else [],
        'raw': {s['moneda']: s for s in resumen}
    }
    with open(MEMORIA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    return resumen

if __name__ == '__main__':
    res = analizar()
    print(f'Analisis completado: {len(res)} monedas con suficiente historial')
    for r in res[:5]:
        print(f'  {r["desc"]}')
