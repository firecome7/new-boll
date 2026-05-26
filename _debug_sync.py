#!/usr/bin/env python3.12
"""调试：模拟 _sync_positions 看 coin 匹配情况"""
from gate_api import GateAPI
from config import FIXED_POSITION_VALUE, LEVERAGE, MAX_POSITIONS, TP_PCT, SL_PCT, ORDER_LIFETIME_BARS
from strategy import train_offsets

api = GateAPI()

# 选5个币简单训练
test_coins = ['FIL', 'SUI', 'LTC', 'BCH', 'PEPE']
state = {}
trained = {}

for coin in test_coins:
    ohlcv = api.fetch_ohlcv(coin, limit=150)
    if len(ohlcv) >= 100:
        off = train_offsets(ohlcv)
        if off['up_offset'] > 0 or off['low_offset'] > 0:
            trained[coin] = off
            from dataclasses import dataclass, field
            @dataclass
            class CS:
                symbol: str = ''
                up_offset: float = 0.0
                low_offset: float = 0.0
                pred_upper: float = 0.0
                pred_lower: float = 0.0
                long_limit_price: float = 0.0
                short_limit_price: float = 0.0
                entry_order_id = None
                entry_price: float = 0.0
                entry_side: str = ''
                entry_bar_count: int = 0
                holding: bool = False
                position_side: str = ''
                position_size: float = 0.0
                entry_fill_price: float = 0.0
                stop_order_id = None
                take_order_id = None
                stop_price: float = 0.0
                take_price: float = 0.0
                exit_triggered_at = None
                exit_triggered_type: str = ''
                long_signal_fired: bool = False
                short_signal_fired: bool = False
                last_skip_price: float = 0.0
            state[coin] = CS(symbol=coin)

print(f"引擎state中币种: {list(state.keys())}")

# 获取持仓
pos = api.fetch_positions()
print(f"交易所持仓: {len(pos)}个")
for p in pos:
    coin = p['coin']
    in_state = '✅' if coin in state else '❌'
    print(f"  {coin}: 在state中? {in_state}")

# 测试匹配
matches = [p['coin'] for p in pos if p['coin'] in state]
not_matches = [p['coin'] for p in pos if p['coin'] not in state]
print(f"\n匹配: {len(matches)}个 - {matches}")
print(f"不匹配: {len(not_matches)}个 - {not_matches}")
