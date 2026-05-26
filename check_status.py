#!/usr/bin/env python3.12
"""检查当前交易所状态"""
import sys, json
sys.path.insert(0, '/home/admin/new_strategy_live')
from gate_api import GateAPI
import asyncio

async def check():
    api = GateAPI()
    
    pos = api.fetch_positions()
    print(f"=== 持仓: {len(pos)} ===")
    for p in pos:
        print(f"  {p['coin']:>10}: {p['side']:>5} {p['size']:>8.0f}张  入场${p['entry_price']:<10.4f}")
    
    orders = api.fetch_open_orders()
    entry = [o for o in orders if not o.get('reduce_only')]
    exits = [o for o in orders if o.get('reduce_only')]
    print(f"\n=== 入场挂单: {len(entry)} ===")
    for o in entry:
        print(f"  {o['coin']:>10}: {o['side']:>5} {o['amount']:>8.0f}张 @ ${o['price']:<12}")
    print(f"\n=== 止盈止损: {len(exits)} ===")
    for o in exits:
        print(f"  {o['coin']:>10}: {o['side']:>5} {o['amount']:>8.0f}张 @ ${o['price']:<12}")
    
    bal = api.fetch_balance()
    print(f"\n权益: ${bal['equity']:.2f}  可用: ${bal['available']:.2f}")

asyncio.run(check())
