"""Read-only market and journal snapshot for the TradingAgents team. No order APIs."""
import argparse
import calendar
import json
import math
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

API = 'https://qohtfituiipoglrgbrsl.supabase.co/functions/v1/mt5-sync'
JST = timezone(timedelta(hours=9))
TIMEFRAMES = ('M1', 'M5', 'M15', 'H1')


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


def server_offset(moment):
    # Same HFM DST rule as sync/gold_sync.py: GMT+3 from the last Sunday in March to the last in October.
    def last_sunday(month):
        d = date(moment.year, month, calendar.monthrange(moment.year, month)[1])
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return 3 if last_sunday(3) <= moment.date() < last_sunday(10) else 2


def resample(candles, hours):
    """Build H4/D1 bars from H1 in HFM server time, matching the MT5 chart."""
    buckets = {}
    for c in candles:
        t = datetime.fromisoformat(c['candle_time'])
        wall = t + timedelta(hours=server_offset(t))
        key = wall.replace(hour=wall.hour - wall.hour % hours, minute=0, second=0, microsecond=0)
        b = buckets.get(key)
        if b is None:
            buckets[key] = b = dict(c, candle_time=key.strftime('%Y-%m-%dT%H:%M (server)'), hours=0)
        else:
            b.update(high=max(b['high'], c['high']), low=min(b['low'], c['low']), close=c['close'],
                     tick_volume=b['tick_volume'] + c['tick_volume'])
        b['hours'] += 1
    # The first bucket usually starts mid-bar, so it would misstate that bar's open, high and low.
    return list(buckets.values())[1:]


def technicals(candles):
    if len(candles) < 30:
        # Too few bars for the indicators; the recent bars still show levels such as the previous day's range.
        return {'candles': len(candles), 'note': 'too few bars for indicators',
                'bars': [{k: c[k] for k in ('candle_time', 'open', 'high', 'low', 'close')} for c in candles]}
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
    p.add_argument('--balance', type=float, help='HFM KATANA balance in JPY, given by the user')
    args = p.parse_args()
    data = {tf: fetch(args.account, tf) for tf in TIMEFRAMES}
    h1 = data['H1']
    s = h1['status']
    bars = {tf: data[tf]['candles'] for tf in TIMEFRAMES}
    bars['H4'], bars['D1'] = resample(h1['candles'], 4), resample(h1['candles'], 24)
    print(json.dumps({
        'generated_at_jst': datetime.now(JST).isoformat(timespec='minutes'),
        'symbol': h1['account']['symbol'],
        # Orders are planned for the KATANA account, which the dashboard does not sync.
        'trading_account': {'name': 'HFM KATANA', 'currency': 'JPY', 'balance': args.balance,
                            'note': None if args.balance else 'balance not given; ask the user'},
        'reference_account': h1['account']['label'],
        'reference_status': {k: s.get(k) for k in ('balance', 'equity', 'floating_pnl', 'open_positions', 'updated_at')},
        'technicals': {tf: technicals(bars[tf]) for tf in ('D1', 'H4', 'H1', 'M15', 'M5', 'M1')},
        'technicals_note': 'H4 and D1 are built from the last 120 H1 bars in server time; the last bar may still be open. W1 is not available.',
        'journal': journal(h1, args.days),
    }, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main()
