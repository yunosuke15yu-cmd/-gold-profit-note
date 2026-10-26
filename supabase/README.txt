Account-scoped MT5 sync

functions/mt5-sync/index.ts is the deployed Edge Function. The app requests
?account=all&tf=M15 and displays combined daily P/L, balances, equity, positions
and a single chronological trade history. Price candles come from one
representative gold market and are never summed. The oldest account sync time
and per-account freshness metadata prevent a partial outage from showing LIVE.
Scoped ?account=<id> reads remain available for verification. Omitting account
keeps the original API default for compatibility. Upload credentials resolve to exactly one account;
source login/server/currency and trade symbols must match that account.

Database records in daily_pnl, mt5_status, mt5_trades and xau_candles use
account_id in their composite primary keys. mt5_accounts holds display metadata;
mt5_account_auth holds private per-account hashes and source identities.
Both metadata/auth tables have RLS enabled with no client policies or grants;
only the authenticated function's service role reads them. Do not put upload
credentials, actual login numbers or runtime config files in this repository.
The live database migration and its verification were recorded separately.

The sync/gold_sync.py uploader accepts --config <path-to-config.json>.
Keep config, encrypted credential, checkpoint and logs in a separate directory
for each account. Each scheduled worker must attach to its designated running
terminal; it must not switch terminal logins. The default profile remains
compatible with the original account.

Active HFM suffixes: 5620, 4701. The closed 4597 account has is_active=false.
All historical P/L and trades, including 4597, remain in the combined diary.
Only active accounts contribute live balances, equity, positions, freshness,
chart source and the account count. Archived accounts reject new uploads.
The is_active column defaults true and retains the existing RLS protections.
Combined views discover metadata from mt5_accounts. Use one running terminal
and worker profile per active account. A reused terminal directory may keep
its old name; source login/server/currency checks still bind it to its account.

Checks (Node 18+ / Python 3.12):
  node tests/multi-account.cjs
  python tests/test_sync.py
