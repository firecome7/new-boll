#!/usr/bin/env python3.12
"""检查引擎状态 vs 交易所持仓"""
from gate_api import GateAPI
api = GateAPI()

pos = api.fetch_positions()
print(f"交易所持仓: {len(pos)}个")
for p in pos:
    n = float(p.get('notional', 0))
    print(f"  {p['coin']}: {p['size']}张 ${n:.0f}")
