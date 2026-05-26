"""Gate.io REST API封装 — 基于ccxt"""
from __future__ import annotations

import ccxt
import time
from typing import Optional
from config import load_api_keys, LEVERAGE

class GateAPI:
    """ccxt封装的Gate.io永续合约操作"""

    def __init__(self):
        keys = load_api_keys()
        self.ex = ccxt.gate({
            'apiKey': keys['apiKey'],
            'secret': keys['secret'],
            'enableRateLimit': True,
        })
        self.ex.load_markets()
        self.ex.options['defaultType'] = 'swap'
        # 杠杆和双向持仓延迟到 setup_coins 时按需设置

    # ── 工具 ──

    def swap_symbol(self, coin: str) -> str:
        """币名→ccxt永续合约格式, e.g. PEPE → PEPE/USDT:USDT"""
        return f"{coin}/USDT:USDT"

    def parse_coin(self, ccxt_symbol: str) -> str:
        """ccxt symbol→币名"""
        return ccxt_symbol.split('/')[0]

    def can_open_position(self, coin: str, price: float, usd_value: float = 100.0) -> bool:
        """检查给定价格下是否能开至少1张合约"""
        contracts = self._calc_contracts(coin, usd_value, price)
        return contracts >= 1

    def get_available_swaps(self) -> list[str]:
        """返回所有USDT保证金永续合约的币名列表"""
        coins = []
        for sym in self.ex.markets:
            m = self.ex.markets[sym]
            if m['swap'] and m['settle'] == 'USDT' and m['linear'] and m['active']:
                coins.append(self.parse_coin(sym))
        return sorted(coins)

    # ── 持仓模式 ──

    def setup_coins(self, coins: list[str]):
        """对目标币种设置双向持仓+杠杆（按需，只设需要的币）"""
        t0 = time.time()
        n_ok = 0
        n_fail = 0
        for coin in coins:
            sym = self.swap_symbol(coin)
            try:
                self.ex.set_position_mode('dual', sym)
                self.ex.set_leverage(LEVERAGE, sym)
                n_ok += 1
            except Exception:
                n_fail += 1
            if (n_ok + n_fail) % 20 == 0:
                pass  # 静默，main那边会打日志
        print(f"  setup_coins: {n_ok}个成功, {n_fail}个跳过/已设 ({time.time()-t0:.1f}s)", flush=True)

    def _set_hedge_mode(self, symbols: Optional[list[str]] = None):
        """设置为双向持仓（多空可同时持有）"""
        for sym, m in self.ex.markets.items():
            if m['swap'] and m['settle'] == 'USDT' and m['linear'] and m['active']:
                try:
                    self.ex.set_position_mode('dual', sym)
                except Exception:
                    pass  # 已经是双向模式就不管

    # ── 杠杆 ──

    def _set_leverage_all(self):
        """对所有USDT永续设置杠杆"""
        for sym, m in self.ex.markets.items():
            if m['swap'] and m['settle'] == 'USDT' and m['linear'] and m['active']:
                try:
                    self.ex.set_leverage(LEVERAGE, sym)
                except Exception:
                    pass

    # ── 行情 ──

    def fetch_ohlcv(self, coin: str, limit: int = 50) -> list[list]:
        """获取OHLCV数据 15m"""
        return self.ex.fetch_ohlcv(self.swap_symbol(coin), '15m', limit=limit)

    def fetch_ticker(self, coin: str) -> dict:
        """获取实时行情"""
        return self.ex.fetch_ticker(self.swap_symbol(coin))

    def fetch_tickers_all(self) -> dict[str, dict]:
        """获取所有USDT永续实时行情"""
        tickers = self.ex.fetch_tickers()
        result = {}
        for sym, t in tickers.items():
            if sym in self.ex.markets:
                m = self.ex.markets[sym]
                if m['swap'] and m['settle'] == 'USDT' and m['linear']:
                    result[self.parse_coin(sym)] = t
        return result

    # ── 账户 ──

    def fetch_balance(self) -> dict:
        """USDT余额"""
        bal = self.ex.fetch_balance()
        return {
            'total': bal['total'].get('USDT', 0),
            'free': bal['free'].get('USDT', 0),
            'used': bal['used'].get('USDT', 0),
        }

    def fetch_positions(self) -> list[dict]:
        """获取所有活跃持仓"""
        try:
            positions = self.ex.fetch_positions()
            active = []
            for p in positions:
                sz = float(p['contracts'] or 0)
                if sz > 0:
                    active.append({
                        'symbol': p['symbol'],
                        'coin': self.parse_coin(p['symbol']),
                        'side': p['side'],           # long/short
                        'size': sz,                  # 张数
                        'entry_price': float(p['entryPrice']),
                        'unrealized_pnl': float(p['unrealizedPnl'] or 0),
                        'notional': float(p['notional'] or 0),
                        'margin': float(p['initialMargin'] or 0),
                    })
            return active
        except Exception:
            return []

    # ── 订单 ──

    def _contract_size(self, coin: str) -> float:
        """合约乘数（1张=多少币）"""
        sym = self.swap_symbol(coin)
        return float(self.ex.market(sym)['contractSize'])

    def _calc_contracts(self, coin: str, usd_value: float, price: float) -> int:
        """美元名义价值→合约张数（向下取整，返回整数张）"""
        sz = self._contract_size(coin)
        raw = usd_value / (sz * price)
        return int(raw)  # 向下取整

    def create_limit_entry(self, coin: str, side: str, usd_value: float,
                           price: float) -> Optional[dict]:
        """限价开仓
        side: 'buy'(long) / 'sell'(short)
        usd_value: 期望的名义价值 USDT
        """
        sym = self.swap_symbol(coin)
        contracts = self._calc_contracts(coin, usd_value, price)
        if contracts <= 0:
            return None
        contracts = float(self.ex.amount_to_precision(sym, contracts))
        if contracts <= 0:
            return None

        order = self.ex.create_order(sym, 'limit', side, contracts, price)
        return order

    def create_limit_close(self, coin: str, side: str, contracts: float,
                           price: float) -> Optional[dict]:
        """限价平仓（reduceOnly）
        side: 与持仓方向相反 — long仓用'sell', short仓用'buy'
        适用于止盈（价格方向有利，不会穿价成交）
        """
        sym = self.swap_symbol(coin)
        contracts = float(self.ex.amount_to_precision(sym, contracts))
        if contracts <= 0:
            return None
        order = self.ex.create_order(
            sym, 'limit', side, contracts, price,
            {'reduceOnly': True}
        )
        return order

    def create_stop_loss_close(self, coin: str, side: str, contracts: float,
                                trigger_price: float) -> Optional[dict]:
        """止损条件单（stop-limit）
        价格到trigger_price才激活限价平仓单
        避免直接限价单穿价成交（做空止损买价高于市价、做多止损卖价低于市价）
        """
        sym = self.swap_symbol(coin)
        contracts = float(self.ex.amount_to_precision(sym, contracts))
        if contracts <= 0:
            return None
        # 使用stopPrice创建条件单，价格触发后以同价限价平仓
        order = self.ex.create_order(
            sym, 'limit', side, contracts, trigger_price,
            {'stopPrice': trigger_price, 'reduceOnly': True}
        )
        return order

    def create_market_close(self, coin: str, side: str, contracts: float) -> Optional[dict]:
        """市价平仓"""
        sym = self.swap_symbol(coin)
        contracts = float(self.ex.amount_to_precision(sym, contracts))
        if contracts <= 0:
            return None
        order = self.ex.create_order(
            sym, 'market', side, contracts, None,
            {'reduceOnly': True}
        )
        return order

    def cancel_order(self, coin: str, order_id: str) -> bool:
        """撤单"""
        try:
            self.ex.cancel_order(order_id, self.swap_symbol(coin))
            return True
        except Exception:
            return False

    def fetch_open_orders(self, coin: Optional[str] = None) -> list[dict]:
        """未成交订单列表"""
        sym = self.swap_symbol(coin) if coin else None
        orders = self.ex.fetch_open_orders(sym) if sym else self.ex.fetch_open_orders()
        result = []
        for o in orders:
            result.append({
                'id': o['id'],
                'symbol': o['symbol'],
                'coin': self.parse_coin(o['symbol']),
                'side': o['side'],
                'price': float(o['price'] or 0),
                'amount': float(o['amount']),
                'filled': float(o['filled']),
                'remaining': float(o['remaining']),
                'type': o['type'],
                'status': o['status'],
                'reduce_only': o.get('reduceOnly', False),
                'timestamp': o['timestamp'],
            })
        return result
