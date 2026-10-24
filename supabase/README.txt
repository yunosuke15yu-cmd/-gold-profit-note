Account-scoped MT5 sync

functions/mt5-sync/index.ts is the deployed Edge Function. Browser GET requests
select an account with ?account=<id>&tf=M15. Omitting account keeps the original
account as the default. Upload credentials resolve to exactly one account;
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

Checks (Node 18+ / Python 3.12):
  node tests/multi-account.cjs
  python tests/test_sync.py
