import importlib.util,unittest,ast
from pathlib import Path
from datetime import datetime,date,timezone
source=Path(__file__).resolve().parents[1]/'sync/gold_sync.py'
spec=importlib.util.spec_from_file_location('sync',source)
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)
class SyncTests(unittest.TestCase):
    def test_symbols_are_separate(self):
        stamp=int(datetime(2026,10,23,12,tzinfo=timezone.utc).timestamp())
        deals=[dict(ticket=1,symbol='XAUUSD',type=0,entry=1,time=stamp),dict(ticket=2,symbol='XAUUSDx',type=1,entry=1,time=stamp)]
        self.assertEqual([d['ticket'] for d in sync.closed_deals(deals,date(2026,10,23))],[1])
        self.assertEqual([d['ticket'] for d in sync.closed_deals(deals,date(2026,10,23),'XAUUSDx')],[2])
    def test_closed_position_direction(self):
        for entry in (1,2,3):
            self.assertEqual(sync.closed_position_side(dict(type=0,entry=entry)),'sell')
            self.assertEqual(sync.closed_position_side(dict(type=1,entry=entry)),'buy')
        with self.assertRaises(RuntimeError):sync.closed_position_side(dict(type=0,entry=0))
    def test_costs_and_server_dates(self):
        stamp=int(datetime(2026,10,23,12,tzinfo=timezone.utc).timestamp())
        deal=dict(time=stamp,profit=100,swap=-5,commission=-4,fee=-1)
        daily=sync.build_daily([deal],date(2026,10,23),date(2026,10,24))
        self.assertEqual([(r['pnl'],r['trades']) for r in daily],[(90,1),(0,0)])
        self.assertEqual(sync.utc_iso(stamp),'2026-10-23T09:00:00+00:00')
    def test_no_trading_or_login(self):
        names={n.func.attr for n in ast.walk(ast.parse(source.read_text('utf-8'))) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        self.assertTrue(names.isdisjoint({'order_send','order_check','login'}))
if __name__=='__main__':unittest.main()
