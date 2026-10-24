"""Read HFM MT5 and update the existing KOMA GOLD diary. No order APIs."""
import argparse
import calendar
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
API = 'https://qohtfituiipoglrgbrsl.supabase.co/functions/v1/mt5-sync'


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, protect):
    """Bind the upload credential to the current Windows user."""
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.windll.crypt32
    if protect:
        ok = crypt.CryptProtectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        ok = crypt.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    if not ok:
        raise RuntimeError('Windows credential protection failed')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def last_sunday(year, month):
    d = date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() + 1) % 7)


def server_offset(wall):
    # HFM's published DST: last Sunday in March through last Sunday in October.
    return 3 if last_sunday(wall.year, 3) <= wall.date() < last_sunday(wall.year, 10) else 2


def utc_iso(server_timestamp):
    wall = datetime.fromtimestamp(int(server_timestamp), UTC)
    return (wall - timedelta(hours=server_offset(wall))).isoformat()


def money(value):
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError('MT5 returned a non-finite amount')
    return round(number, 2)


def closed_deals(deals, start, symbol='XAUUSD'):
    return [d for d in deals if d['symbol'] == symbol and d['type'] in (0, 1)
            and d['entry'] in (1, 2, 3)
            and datetime.fromtimestamp(d['time'], UTC).date() >= start]


def build_daily(deals, start, today):
    rows = {}
    d = start
    while d <= today:
        rows[d.isoformat()] = dict(date=d.isoformat(), pnl=0.0, trades=0, wins=0, losses=0)
        d += timedelta(days=1)
    for deal in deals:
        day = datetime.fromtimestamp(deal['time'], UTC).date().isoformat()
        if day not in rows:
            continue
        net = money(sum(deal.get(k, 0) for k in ('profit', 'swap', 'commission', 'fee')))
        row = rows[day]
        row['pnl'] = money(row['pnl'] + net)
        row['trades'] += 1
        row['wins'] += int(net > 0)
        row['losses'] += int(net < 0)
    return list(rows.values())


def closed_position_side(deal):
    # A closing BUY exits a SELL position, and a closing SELL exits a BUY.
    # INOUT closes the old position before reversing; OUT_BY is also an exit.
    if deal['entry'] not in (1, 2, 3) or deal['type'] not in (0, 1):
        raise RuntimeError('Position side requires a closing buy/sell deal')
    return 'sell' if deal['type'] == 0 else 'buy'


def build_trades(deals):
    return [dict(ticket=int(d['ticket']), positionId=int(d['position_id']),
                 time=utc_iso(d['time']), date=datetime.fromtimestamp(d['time'], UTC).date().isoformat(),
                 symbol=d['symbol'], side=closed_position_side(d),
                 volume=float(d['volume']), price=float(d['price']),
                 profit=money(d['profit']), swap=money(d['swap']),
                 commission=money(d['commission']), fee=money(d.get('fee', 0)),
                 netPnl=money(sum(d.get(k, 0) for k in ('profit', 'swap', 'commission', 'fee'))))
            for d in deals]


def log(state, **details):
    record = dict(at=datetime.now(UTC).isoformat(), state=state, **details)
    (ROOT / 'last_result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    with (ROOT / 'sync.log').open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(json.dumps(record, ensure_ascii=False))


def collect(config):
    import MetaTrader5 as mt5
    if not mt5.initialize(config['terminal'], portable=bool(config.get('portable', False)), timeout=10000):
        raise RuntimeError(f'MT5 connection failed ({mt5.last_error()[0]})')
    try:
        info, account = mt5.terminal_info(), mt5.account_info()
        if info is None or account is None or not info.connected:
            raise RuntimeError('HFM is disconnected')
        if account.login != config['login'] or account.server != config['server']:
            raise RuntimeError('Account or server changed; upload blocked')
        if account.currency != 'JPY':
            raise RuntimeError('Account currency must be JPY for this diary')
        start = date.fromisoformat(config['start_date'])
        now = datetime.now(UTC)
        wall_now = now + timedelta(hours=server_offset(now))
        history = mt5.history_deals_get(datetime.combine(start, datetime.min.time(), UTC), wall_now + timedelta(minutes=1))
        if history is None:
            raise RuntimeError('History read failed; no zero totals will be sent')
        deals = [d._asdict() for d in history]
        positions = mt5.positions_get()
        if positions is None:
            raise RuntimeError('Position read failed')
        symbol = config.get('symbol', 'XAUUSD')
        closed = closed_deals(deals, start, symbol)
        # Refuse a transient incomplete history, rather than erasing an existing day's totals.
        previous_path = ROOT / 'history_checkpoint.json'
        prior = json.loads(previous_path.read_text('utf-8')) if previous_path.exists() else []
        tickets = {int(d['ticket']) for d in closed}
        if not set(prior).issubset(tickets):
            raise RuntimeError('History is incomplete; upload blocked')
        candles = []
        for tf in ('M1', 'M5', 'M15', 'H1'):
            rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, 'TIMEFRAME_' + tf), 0, 120)
            if rates is None or len(rates) == 0:
                raise RuntimeError(f'{tf} candle read failed')
            for r in rates:
                candles.append(dict(symbol=symbol, timeframe=tf, time=utc_iso(r['time']),
                                    open=float(r['open']), high=float(r['high']), low=float(r['low']),
                                    close=float(r['close']), volume=int(r['tick_volume'])))
        # Recheck the account after collection, before returning data for upload.
        final_account = mt5.account_info()
        if final_account is None or final_account.login != config['login'] or final_account.server != config['server']:
            raise RuntimeError('Account changed while reading; upload blocked')
        payload = dict(accountId=config.get('account_id', 'hfm-5620'),
                       sourceAccount=dict(login=account.login, server=account.server, currency=account.currency),
                       daily=build_daily(closed, start, wall_now.date()),
                       trades=build_trades(closed), candles=candles,
                       status=dict(balance=money(account.balance), equity=money(account.equity),
                                   floatingPnl=money(account.profit), openPositions=len(positions)))
        return payload, sorted(tickets)
    finally:
        mt5.shutdown()


def send(payload):
    key = dpapi((ROOT / 'sync_credential.dpapi').read_bytes(), False).decode('ascii')
    req = urllib.request.Request(API, data=json.dumps(payload, allow_nan=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'x-sync-key': key}, method='POST')
    with urllib.request.urlopen(req, timeout=25) as response:
        result = json.load(response)
    if result.get('ok') is not True:
        raise RuntimeError('Cloud rejected the upload')


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--prepare-key', action='store_true')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.config:
        ROOT = args.config.resolve().parent
    if args.prepare_key:
        destination = ROOT / 'sync_credential.dpapi'
        if destination.exists():
            raise RuntimeError('Credential already exists; refusing replacement')
        key = secrets.token_urlsafe(32).encode('ascii')
        protected = dpapi(key, True)
        if dpapi(protected, False) != key:
            raise RuntimeError('Credential round-trip verification failed')
        destination.write_bytes(protected)
        print(hashlib.sha256(key).hexdigest())
        return
    config = json.loads((args.config or ROOT / 'config.json').read_text('utf-8'))
    payload, tickets = collect(config)
    if args.dry_run:
        (ROOT / 'preview.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        log('PREVIEW_READY', daily=payload['daily'], trades=len(payload['trades']), candles=len(payload['candles']))
        return
    send(payload)
    (ROOT / 'history_checkpoint.json').write_text(json.dumps(tickets), encoding='utf-8')
    log('SYNC_OK', days=len(payload['daily']), trades=len(payload['trades']), candles=len(payload['candles']))


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as error:
        log('SYNC_FAILED', error=f'HTTP {error.code}')
        sys.exit(1)
    except Exception as error:
        log('SYNC_FAILED', error=str(error))
        sys.exit(1)
