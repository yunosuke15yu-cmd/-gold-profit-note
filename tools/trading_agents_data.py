"""Read-only market and journal snapshot for the TradingAgents team. No order APIs."""
import argparse
import json
import math
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

API = 'https://qohtfituiipoglrgbrsl.supabase.co/functions/v1/mt5-sync'
JST = timezone(timedelta(hours=9))


def fetch(account, tf):
    with urllib.request.urlopen(f'{API}?account={account}&tf={tf}', timeout=30) as res:
        data = json.load(res)
    if not data.get('ok'):
        raise RuntimeError(data.get('error', 'API error'))
    return data


def ema(xs, n):
    k, out = 2 / (n + 1), [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    if len(gains) < n:
        return None
    g, l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for gi, li in zip(gains[n:], losses[n:]):
        g, l = (g * (n - 1) + gi) / n, (l * (n - 1) + li) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(candles, n=14):
    trs = [max(c['high'] - c['low'], abs(c['high'] - p['close']), abs(c['low'] - p['close']))
           for p, c in zip(candles, candles[1:])]
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def technicals(candles):
    if len(candles) < 30:
        return {'candles': len(candles), 'note': 'not enough candles'}
    closes = [c['close'] for c in candles]
    macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    signal = ema(macd, 9)
    recent = candles[-20:]
    mean = sum(closes[-20:]) / 20
    sd = math.sqrt(sum((x - mean) ** 2 for x in closes[-20:]) / 20)
    r = lambda x: None if x is None else round(x, 2)
    return {
        'candles': len(candles),
        'from': candles[0]['candle_time'], 'to': candles[-1]['candle_time'],
        'last': {k: candles[-1][k] for k in ('open', 'high', 'low', 'close')},
        'ema20': r(ema(closes, 20)[-1]), 'ema50': r(ema(closes, 50)[-1]),
        'rsi14': r(rsi(closes)), 'atr14': r(atr(candles)),
        'macd': r(macd[-1]), 'macd_signal': r(signal[-1]), 'macd_hist': r(macd[-1] - signal[-1]),
        'bb20_upper': r(mean + 2 * sd), 'bb20_lower': r(mean - 2 * sd),
        'range20_high': max(c['high'] for c in recent), 'range20_low': min(c['low'] for c in recent),
        'period_high': max(c['high'] for c in candles), 'period_low': min(c['low'] for c in candles),
        'change_pct': r((closes[-1] / closes[0] - 1) * 100),
    }


def journal(data, days):
    cutoff = (datetime.now(JST) - timedelta(days=days)).date().isoformat()
    daily = [d for d in data['daily'] if d['trade_date'] >= cutoff]
    trades = [t for t in data['trades'] if t['trade_date'] >= cutoff]
    by_hour, by_side = defaultdict(lambda: [0.0, 0]), defaultdict(lambda: [0.0, 0, 0])
    for t in trades:
        pnl = float(t['net_pnl'])
        hour = datetime.fromisoformat(t['trade_time']).astimezone(JST).hour
        by_hour[hour][0] += pnl
        by_hour[hour][1] += 1
        # The uploader stores the closed position's direction, not the closing deal's.
        side = 'long' if t['side'] == 'buy' else 'short'
        by_side[side][0] += pnl
        by_side[side][1] += 1
        by_side[side][2] += pnl > 0
    total = sum(float(d['pnl']) for d in daily)
    wins, losses = sum(d['wins'] for d in daily), sum(d['losses'] for d in daily)
    pnls = [float(t['net_pnl']) for t in trades]
    return {
        'window_days': days, 'net_pnl_jpy': round(total, 0),
        'wins': wins, 'losses': losses,
        'win_rate_pct': round(wins / (wins + losses) * 100, 1) if wins + losses else None,
        'avg_win_jpy': round(sum(p for p in pnls if p > 0) / max(1, sum(p > 0 for p in pnls)), 0),
        'avg_loss_jpy': round(sum(p for p in pnls if p < 0) / max(1, sum(p < 0 for p in pnls)), 0),
        'worst_day': min(daily, key=lambda d: float(d['pnl']), default=None),
        'best_day': max(daily, key=lambda d: float(d['pnl']), default=None),
        'by_position_side': {k: {'net_pnl_jpy': round(v[0], 0), 'deals': v[1], 'winning_deals': v[2]}
                             for k, v in by_side.items()},
        'by_jst_hour': {h: {'net_pnl_jpy': round(v[0], 0), 'deals': v[1]} for h, v in sorted(by_hour.items())},
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account', default='all')
    p.add_argument('--days', type=int, default=30)
    args = p.parse_args()
    h1, m15 = fetch(args.account, 'H1'), fetch(args.account, 'M15')
    s = h1['status']
    print(json.dumps({
        'generated_at_jst': datetime.now(JST).isoformat(timespec='minutes'),
        'symbol': h1['account']['symbol'], 'account': h1['account']['label'],
        'account_status': {k: s.get(k) for k in ('balance', 'equity', 'floating_pnl', 'open_positions', 'updated_at')},
        'currency': h1['account'].get('currency'),
        'technicals': {'H1': technicals(h1['candles']), 'M15': technicals(m15['candles'])},
        'journal': journal(h1, args.days),
    }, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main()
