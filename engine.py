"""核心交易引擎"""
from __future__ import annotations
import time
import logging
from typing import Optional
from dataclasses import dataclass, field

from config import (
    FIXED_POSITION_VALUE, LEVERAGE, MAX_POSITIONS,
    TP_PCT, SL_PCT, ORDER_LIFETIME_BARS, TIMEOUT_SECONDS,
    TAKER_FEE, MAKER_FEE, DRY_RUN,
)
from strategy import train_offsets, precalc_bb, predict_bands
from gate_api import GateAPI

logger = logging.getLogger('engine')


@dataclass
class CoinState:
    """单个币的状态"""
    symbol: str

    # 偏移量（训练期计算）
    up_offset: float = 0.0
    low_offset: float = 0.0

    # 当前K线的预测布林带
    pred_upper: float = 0.0
    pred_lower: float = 0.0

    # 本根K线的限价（预设）
    long_limit_price: float = 0.0
    short_limit_price: float = 0.0

    # 入场限价挂单
    entry_order_id: Optional[str] = None
    entry_price: float = 0.0
    entry_side: str = ''
    entry_bar_count: int = 0

    # 持仓状态
    holding: bool = False
    position_side: str = ''
    position_size: float = 0.0
    entry_fill_price: float = 0.0

    # 出场
    stop_order_id: Optional[str] = None
    take_order_id: Optional[str] = None
    stop_price: float = 0.0
    take_price: float = 0.0

    # 触价倒计时
    exit_triggered_at: Optional[float] = None
    exit_triggered_type: str = ''

    # 信号已触发（同K线内不再重复）
    long_signal_fired: bool = False
    short_signal_fired: bool = False

    # 跳过去重
    last_skip_price: float = 0.0

    @property
    def pending_entry(self) -> bool:
        return self.entry_order_id is not None

    @property
    def pending_exit(self) -> bool:
        return self.exit_triggered_at is not None

    @property
    def status(self) -> str:
        if self.holding:
            return (f"持仓{self.position_side} 入场${self.entry_fill_price:.6f} "
                    f"止盈${self.take_price:.6f} 止损${self.stop_price:.6f}")
        if self.entry_order_id:
            return f"挂单{self.entry_side} ${self.entry_price:.6f}"
        return "等待"


class TradingEngine:
    """交易引擎"""

    def __init__(self, api: GateAPI, coins: list[str]):
        self.api = api
        self.state: dict[str, CoinState] = {}
        self.active_coins: set[str] = set()
        self._tick_count = 0

        # Step 1: 训练偏移量
        self._train_all(coins)

        # Step 2: 初始化状态
        for coin in coins:
            if coin in self._trained_offsets:
                off = self._trained_offsets[coin]
                cs = CoinState(symbol=coin)
                cs.up_offset = off['up_offset']
                cs.low_offset = off['low_offset']
                self.state[coin] = cs
                self.active_coins.add(coin)

        logger.info(f"引擎启动: {len(self.state)}个币活跃 (训练{len(coins)}个)")

        # Step 3: 同步持仓/订单
        self._sync_positions()
        self._sync_orders()

    def _train_all(self, coins: list[str]):
        """训练所有币的偏移量"""
        import time as _time
        self._trained_offsets = {}
        for idx, coin in enumerate(coins):
            try:
                ohlcv = self.api.fetch_ohlcv(coin, limit=TRAINING_BARS + 50)
                if len(ohlcv) < TRAINING_BARS + 25:
                    logger.debug(f"[{coin}] 训练跳过: 仅{len(ohlcv)}根K线")
                    _time.sleep(0.05)
                    continue
                off = train_offsets(ohlcv)
                if off['up_offset'] > 0 or off['low_offset'] > 0:
                    self._trained_offsets[coin] = off
                if idx % 20 == 0:
                    logger.info(f"  训练进度: {idx}/{len(coins)} ({len(self._trained_offsets)}个有效)")
                _time.sleep(0.05)
            except Exception as e:
                logger.warning(f"[{coin}] 训练失败: {e}")

        n = len(self._trained_offsets)
        logger.info(f"训练完成: {n}/{len(coins)}个币有有效偏移量")

    # ── 同步 ──

    def _sync_positions(self):
        """启动时从交易所同步已有持仓"""
        positions = self.api.fetch_positions()
        logger.info(f"交易所持仓同步: 共{len(positions)}个活跃持仓")
        for p in positions:
            coin = p['coin']
            if coin in self.state:
                cs = self.state[coin]
                cs.holding = True
                cs.position_side = p['side']
                cs.position_size = p['size']
                cs.entry_fill_price = p['entry_price']
                if cs.position_side == 'long':
                    cs.stop_price = cs.entry_fill_price * (1 - SL_PCT)
                    cs.take_price = cs.entry_fill_price * (1 + TP_PCT)
                else:
                    cs.stop_price = cs.entry_fill_price * (1 + SL_PCT)
                    cs.take_price = cs.entry_fill_price * (1 - TP_PCT)
                self.active_coins.add(coin)
                logger.info(f"  [{coin}] 恢复持仓 {cs.position_side} "
                            f"入场${cs.entry_fill_price:.4f}")

    def _sync_orders(self):
        """启动时从交易所同步未成交订单"""
        orders = self.api.fetch_open_orders()
        n_entry = 0
        for o in orders:
            coin = o['coin']
            if coin in self.state:
                cs = self.state[coin]
                if not o['reduce_only']:
                    cs.entry_order_id = o['id']
                    cs.entry_price = o['price']
                    cs.entry_side = o['side']
                    self.active_coins.add(coin)
                    n_entry += 1
        logger.info(f"订单同步: {n_entry}个入场挂单")

    # ── K线更新 ──

    def on_new_bar(self, coin: str, ohlcv: list[list]):
        """每根新K线开始：预测布林带 + 预设限价"""
        if coin not in self.state:
            return
        cs = self.state[coin]

        # 重置同K线信号
        cs.long_signal_fired = False
        cs.short_signal_fired = False

        # 预计算BB
        bb_data = precalc_bb(ohlcv)
        last_idx = len(ohlcv) - 1
        if last_idx not in bb_data:
            return

        # 预测当前根K线的布林带
        pred_up, pred_low = predict_bands(bb_data, last_idx)
        cs.pred_upper = pred_up
        cs.pred_lower = pred_low

        # 预设限价
        if cs.low_offset > 0:
            cs.long_limit_price = pred_low * (1 - cs.low_offset)
        else:
            cs.long_limit_price = 0
        if cs.up_offset > 0:
            cs.short_limit_price = pred_up * (1 + cs.up_offset)
        else:
            cs.short_limit_price = 0

        logger.debug(f"[{coin}] 新K线 预测上轨${pred_up:.4f} 下轨${pred_low:.4f} "
                     f"做多限价${cs.long_limit_price:.4f} 做空限价${cs.short_limit_price:.4f}")

        # 挂单超时：上一根K线的挂单未成交，撤掉
        if cs.pending_entry and not cs.holding:
            cs.entry_bar_count += 1
            if cs.entry_bar_count >= ORDER_LIFETIME_BARS:
                ok = self.api.cancel_order(coin, cs.entry_order_id)
                logger.info(f"[{coin}] 📋 入场挂单超时1K线 "
                            f"撤单{'✅' if ok else '❌'}")
                cs.entry_order_id = None
                cs.entry_price = 0.0
                cs.entry_bar_count = 0
                self.active_coins.discard(coin)

    # ── Tick ──

    def on_tick(self, coin: str, price: float) -> list[dict]:
        """实时价格更新（每15秒）"""
        if coin not in self.state:
            return []
        cs = self.state[coin]
        self._tick_count += 1

        actions = []

        # 持仓检查出场
        if cs.holding:
            actions.extend(self._check_exit(coin, price))

        # 触价倒计时
        if cs.pending_exit:
            actions.extend(self._check_timeout(coin, price))

        # 入场信号
        if not cs.holding and not cs.pending_entry:
            action = self._check_entry(coin, price)
            if action:
                actions.append(action)

        return actions

    # ── 入场 ──

    def _check_entry(self, coin: str, price: float) -> Optional[dict]:
        """检查入场信号：价格触及预测轨 → 挂限价"""
        cs = self.state[coin]

        if cs.pred_upper == 0 or cs.pred_lower == 0:
            return None

        # 仓位上限
        holding_count = sum(1 for s in self.state.values() if s.holding)
        if holding_count >= MAX_POSITIONS:
            return None

        # ── 做多：价格跌到预测下轨 ──
        if price <= cs.pred_lower and cs.low_offset > 0 and not cs.long_signal_fired:
            cs.long_signal_fired = True
            limit_price = cs.long_limit_price
            if limit_price <= 0:
                return None

            # 检查最小张数
            if not self.api.can_open_position(coin, limit_price, FIXED_POSITION_VALUE):
                if abs(limit_price - cs.last_skip_price) / max(limit_price, 1e-10) > 0.001:
                    logger.warning(f"[{coin}] ❌ 做多跳过: ${limit_price:.6f} 张数不足")
                    cs.last_skip_price = limit_price
                return None

            logger.info(f"[{coin}] 📈 做多信号! 价格${price:.4f}≤预测下轨${cs.pred_lower:.4f} "
                        f"限价${limit_price:.4f}")
            return self._place_entry(coin, 'buy', limit_price, 'long')

        # ── 做空：价格涨到预测上轨 ──
        if price >= cs.pred_upper and cs.up_offset > 0 and not cs.short_signal_fired:
            cs.short_signal_fired = True
            limit_price = cs.short_limit_price
            if limit_price <= 0:
                return None

            if not self.api.can_open_position(coin, limit_price, FIXED_POSITION_VALUE):
                if abs(limit_price - cs.last_skip_price) / max(limit_price, 1e-10) > 0.001:
                    logger.warning(f"[{coin}] ❌ 做空跳过: ${limit_price:.6f} 张数不足")
                    cs.last_skip_price = limit_price
                return None

            logger.info(f"[{coin}] 📉 做空信号! 价格${price:.4f}≥预测上轨${cs.pred_upper:.4f} "
                        f"限价${limit_price:.4f}")
            return self._place_entry(coin, 'sell', limit_price, 'short')

        return None

    def _place_entry(self, coin: str, side: str, limit_price: float,
                     pos_side: str) -> Optional[dict]:
        """挂入场限价单"""
        cs = self.state[coin]

        if DRY_RUN:
            logger.info(f"[{coin}] 🔍 [验证模式] {pos_side} 限价${limit_price:.6f}")
            cs.entry_bar_count = 0
            return {'time': time.time(), 'coin': coin, 'type': 'dry_run_signal',
                    'side': pos_side, 'price': limit_price}

        order = self.api.create_limit_entry(coin, side, FIXED_POSITION_VALUE, limit_price)
        if order is None:
            logger.warning(f"[{coin}] ❌ 入场挂单失败")
            return None

        cs.entry_order_id = order['id']
        cs.entry_price = limit_price
        cs.entry_side = side
        cs.entry_bar_count = 0
        self.active_coins.add(coin)

        logger.info(f"[{coin}] ✅ 入场挂单 {pos_side} ${limit_price:.6f} "
                    f"名义${FIXED_POSITION_VALUE:.0f} ID={order['id']}")

        return {'time': time.time(), 'coin': coin, 'type': 'entry_order',
                'side': pos_side, 'price': limit_price, 'order_id': order['id']}

    # ── 出场 ──

    def _check_exit(self, coin: str, price: float) -> list[dict]:
        """检查是否触发出场条件"""
        cs = self.state[coin]
        if cs.pending_exit:
            return []

        if cs.position_side == 'long':
            stop_hit = price <= cs.stop_price
            take_hit = price >= cs.take_price
        else:
            stop_hit = price >= cs.stop_price
            take_hit = price <= cs.take_price

        if not stop_hit and not take_hit:
            return []

        result = 'take_profit' if take_hit else 'stop_loss'
        cs.exit_triggered_at = time.time()
        cs.exit_triggered_type = result

        # 盈亏
        if cs.position_side == 'long':
            pnl_pct = (price / cs.entry_fill_price - 1) * 100
        else:
            pnl_pct = (1 - price / cs.entry_fill_price) * 100
        pnl_usd = pnl_pct / 100 * FIXED_POSITION_VALUE

        logger.info(f"[{coin}] 🚨 {result}触发! 当前${price:.4f} "
                    f"盈亏{'+' if pnl_usd>=0 else ''}${pnl_usd:.2f}({pnl_pct:+.2f}%) "
                    f"等待{TIMEOUT_SECONDS}s限价成交")

        return [{'time': time.time(), 'coin': coin, 'type': result,
                 'price': price, 'pnl_pct': round(pnl_pct, 2),
                 'pnl_usd': round(pnl_usd, 2)}]

    def _check_timeout(self, coin: str, price: float) -> list[dict]:
        """触价后15秒转市价"""
        cs = self.state[coin]
        if not cs.pending_exit or not cs.holding:
            if cs.pending_exit:
                cs.exit_triggered_at = None
                cs.exit_triggered_type = ''
            return []

        elapsed = time.time() - cs.exit_triggered_at
        if elapsed < TIMEOUT_SECONDS:
            return []

        close_side = 'sell' if cs.position_side == 'long' else 'buy'
        logger.info(f"[{coin}] ⏰ 限价{TIMEOUT_SECONDS}s未成交，市价平仓")
        try:
            order = self.api.create_market_close(coin, close_side, cs.position_size)
            if order:
                logger.info(f"[{coin}] ✅ 市价平仓成功")
        except Exception as e:
            if 'empty position' in str(e).lower() or 'REDUCE_EXCEEDED' in str(e):
                logger.info(f"[{coin}] ℹ️ 市价平仓时仓位已空: {e}")
            else:
                logger.error(f"[{coin}] ❌ 市价平仓异常: {e}")

        self.clear_position(coin, reason=f"market_close_{cs.exit_triggered_type}")
        return [{'time': time.time(), 'coin': coin, 'type': f'{cs.exit_triggered_type}_market'}]

    # ── 订单成交 ──

    def on_order_filled(self, coin: str, order_id: str, fill_price: float,
                        filled_amount: float, side: str, reduce_only: bool):
        """处理订单成交"""
        if coin not in self.state:
            return
        cs = self.state[coin]

        if reduce_only:
            exit_type = '止盈' if cs.exit_triggered_type == 'take_profit' else '止损'
            logger.info(f"[{coin}] ✅ {exit_type}成交 @ ${fill_price:.6f}")
            self.clear_position(coin, reason=f"limit_{cs.exit_triggered_type}")
            return

        # 入场成交
        pos_side = 'long' if side == 'buy' else 'short'
        cs.holding = True
        cs.position_side = pos_side
        cs.position_size = filled_amount
        cs.entry_fill_price = fill_price
        cs.entry_order_id = None

        # 止盈止损价
        if pos_side == 'long':
            cs.stop_price = fill_price * (1 - SL_PCT)
            cs.take_price = fill_price * (1 + TP_PCT)
        else:
            cs.stop_price = fill_price * (1 + SL_PCT)
            cs.take_price = fill_price * (1 - TP_PCT)

        close_side = 'sell' if pos_side == 'long' else 'buy'

        tp_order = self.api.create_limit_close(coin, close_side, cs.position_size, cs.take_price)
        if tp_order:
            cs.take_order_id = tp_order['id']
            logger.info(f"[{coin}] 止盈限价单: ${cs.take_price:.6f} ID={tp_order['id']}")
        else:
            logger.warning(f"[{coin}] ⚠️ 止盈挂单失败 @ ${cs.take_price:.6f}")

        sl_order = self.api.create_stop_loss_close(coin, close_side, cs.position_size, cs.stop_price)
        if sl_order:
            cs.stop_order_id = sl_order['id']
            logger.info(f"[{coin}] 🔒 止损条件单: 触发${cs.stop_price:.6f} ID={sl_order['id']}")
        else:
            logger.warning(f"[{coin}] ⚠️ 止损挂单失败 @ ${cs.stop_price:.6f}")

        holding_count = sum(1 for s in self.state.values() if s.holding)
        logger.info(f"[{coin}] ✅ 入场成交 {pos_side} @${fill_price:.6f} "
                    f"止盈${cs.take_price:.6f} 止损${cs.stop_price:.6f} "
                    f"持仓{holding_count}/{MAX_POSITIONS}")

    def clear_position(self, coin: str, reason: str = ''):
        """清空持仓状态"""
        if coin not in self.state:
            return
        cs = self.state[coin]
        was_holding = cs.holding

        for oid_field in ('stop_order_id', 'take_order_id'):
            oid = getattr(cs, oid_field, None)
            if oid:
                try:
                    self.api.ex.cancel_order(oid, self.api.swap_symbol(coin))
                except Exception:
                    pass

        cs.holding = False
        cs.position_side = ''
        cs.position_size = 0.0
        cs.entry_fill_price = 0.0
        cs.stop_order_id = None
        cs.take_order_id = None
        cs.stop_price = 0.0
        cs.take_price = 0.0
        cs.exit_triggered_at = None
        cs.exit_triggered_type = ''
        cs.entry_order_id = None
        cs.entry_price = 0.0
        cs.entry_bar_count = 0

        if was_holding:
            logger.info(f"[{coin}] 🗑️ 清空持仓 [{reason}]")

    def sync_positions(self, exchange_positions: list[dict]):
        """从交易所持仓同步引擎状态"""
        exchange_coins = {p['coin'] for p in exchange_positions}
        for coin, cs in self.state.items():
            if cs.holding and coin not in exchange_coins:
                logger.info(f"[{coin}] 🔄 同步: 交易所已无持仓")
                self.clear_position(coin, reason='sync_cleared')

    @property
    def pending_entry(self) -> bool:
        return any(cs.pending_entry for cs in self.state.values())

    # ── 状态 ──

    def get_summary(self) -> str:
        holding = sum(1 for s in self.state.values() if s.holding)
        pending = sum(1 for s in self.state.values() if s.pending_entry)
        timing = sum(1 for s in self.state.values() if s.pending_exit)
        lines = [
            f"持仓 {holding}/{MAX_POSITIONS}  挂单入场 {pending}  倒计时 {timing}"
        ]
        for coin in sorted(self.active_coins):
            lines.append(f"  {coin:<12} {self.state[coin].status}")
        return '\n'.join(lines)

    def get_ticks(self) -> int:
        return self._tick_count
