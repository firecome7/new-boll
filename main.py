"""主入口 — New Bollinger 策略实盘"""
#!/usr/bin/env python3.12
from __future__ import annotations

import time
import logging
import sys
from datetime import datetime

from config import (
    load_api_keys, TIMEFRAME, TIMEOUT_SECONDS, FIXED_POSITION_VALUE,
    MAX_POSITIONS, BAR_SECONDS, POLL_INTERVAL, ORDER_CHECK_INTERVAL,
    SYNC_INTERVAL, DRY_RUN, TP_PCT, SL_PCT,
)
from gate_api import GateAPI
from engine import TradingEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('new_boll.log'),
    ]
)
logging.getLogger('ccxt').setLevel(logging.WARNING)
logger = logging.getLogger('main')


class LiveTrader:
    def __init__(self):
        t0 = time.time()
        self.api = GateAPI()
        logger.info(f"Gate.io API连接完成 ({time.time()-t0:.1f}s)")

        # 获取币种列表
        self.coins = self._get_target_coins()
        self.engine = TradingEngine(self.api, self.coins)

        # 当前K线时间戳
        self.last_bar_ts = 0
        self.last_log_ts = 0
        self.last_order_check_ts = 0
        self._tick_count = 0

        # 缓存当前K线的OHLCV（用于新K线检测时一起更新）
        self._candles_cache: dict[str, list[list]] = {}

        # 初始化：拉一次所有币的K线，触发on_new_bar
        self._init_bars()

    def _get_target_coins(self) -> list[str]:
        """获取币种列表（成交量过滤）"""
        from config import MIN_VOLUME, COIN_LIMIT, EXCLUDE
        t0 = time.time()
        all_swaps = self.api.get_available_swaps()
        logger.info(f"Gate.io可用USDT永续: {len(all_swaps)}个")

        filtered = [c for c in all_swaps if c not in EXCLUDE]
        logger.info(f"排除{len(EXCLUDE)}个大市值后: {len(filtered)}个")

        tickers = self.api.fetch_tickers_all()
        vol_list = []
        for coin in filtered:
            t = tickers.get(coin)
            if t:
                vol = float(t.get('quoteVolume', 0) or 0)
                if vol >= MIN_VOLUME:
                    vol_list.append((coin, vol))
        vol_list.sort(key=lambda x: -x[1])
        coins = [c for c, _ in vol_list[:COIN_LIMIT]]
        coins.sort()
        logger.info(f"成交量≥${MIN_VOLUME/1e6:.0f}M: {len(vol_list)}个, 取{len(coins)}个")

        self.api.setup_coins(coins)
        logger.info(f"  (耗时{time.time()-t0:.1f}s)")
        return coins

    def _current_bar_ts(self) -> int:
        now = int(time.time())
        bar_start = now - (now % BAR_SECONDS)
        return bar_start * 1000

    def _init_bars(self):
        """启动时：拉所有币的K线，触发首次on_new_bar"""
        n_ok = 0
        for coin in self.coins:
            try:
                ohlcv = self.api.fetch_ohlcv(coin, limit=50)
                if ohlcv and len(ohlcv) >= 26:
                    self._candles_cache[coin] = ohlcv
                    self.engine.on_new_bar(coin, ohlcv)
                    n_ok += 1
            except Exception as e:
                logger.debug(f"[{coin}] 初始化失败: {e}")
            time.sleep(0.05)
        self.last_bar_ts = self._current_bar_ts()
        logger.info(f"K线初始化: {n_ok}/{len(self.coins)}个币")

    def run(self):
        logger.info("=" * 50)
        logger.info(f"New Boll 策略实盘 {'[验证模式]' if DRY_RUN else ''}")
        logger.info(f"  K线: {TIMEFRAME} | 每笔${FIXED_POSITION_VALUE} | 最多{MAX_POSITIONS}仓")
        logger.info(f"  杠杆50x 全仓 | TP {TP_PCT*100:.0f}% / SL {SL_PCT*100:.0f}%")
        logger.info(f"  行情: 每{POLL_INTERVAL}s | 订单: 每{ORDER_CHECK_INTERVAL}s | 同步: 每{SYNC_INTERVAL}s")
        logger.info("=" * 50)

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("🛑 用户手动停止")
                break
            except Exception as e:
                logger.error(f"❌ 主循环异常: {e}", exc_info=True)
                logger.warning("等待60s自动恢复...")
                time.sleep(60)
            time.sleep(POLL_INTERVAL)

    def _tick(self):
        self._tick_count += 1
        now = time.time()
        bar_ts = self._current_bar_ts()

        # ── 1. 新K线检测 ──
        if bar_ts != self.last_bar_ts:
            bar_dt = datetime.fromtimestamp(bar_ts / 1000).strftime('%H:%M')
            logger.info(f"⏰ ── 新K线 {bar_dt} (tick#{self._tick_count}) ──")
            self._on_new_bar()
            self.last_bar_ts = bar_ts

        # ── 2. 行情轮询 → 引擎入/出场判断 ──
        self._poll_tickers()

        # ── 3. 订单检查 ──
        if now - self.last_order_check_ts >= ORDER_CHECK_INTERVAL:
            self._check_orders()
            self.last_order_check_ts = now

        # ── 4. 持仓同步 ──
        if int(now) % SYNC_INTERVAL < POLL_INTERVAL:
            try:
                positions = self.api.fetch_positions()
                self.engine.sync_positions(positions)
            except Exception as e:
                logger.warning(f"持仓同步失败: {e}")

        # ── 5. 状态日志（每60s）──
        if now - self.last_log_ts >= 60:
            try:
                bal = self.api.fetch_balance()
                n_hold = sum(1 for s in self.engine.state.values() if s.holding)
                n_pend = sum(1 for s in self.engine.state.values() if s.pending_entry)
                logger.info(f"── 状态 [{datetime.now().strftime('%H:%M:%S')}] ── "
                            f"权益${bal.get('total', 0):.2f}(可用${bal.get('free', 0):.2f}) "
                            f"持仓{n_hold}/{MAX_POSITIONS} 挂单{n_pend}")
            except Exception as e:
                logger.warning(f"状态日志失败: {e}")
            self.last_log_ts = now

    def _on_new_bar(self):
        """新K线：更新所有币的预测布林带"""
        n_ok = 0
        n_fail = 0
        for coin in self.coins:
            if coin not in self.engine.state:
                continue
            try:
                ohlcv = self.api.fetch_ohlcv(coin, limit=150)
                if ohlcv:
                    self._candles_cache[coin] = ohlcv
                    self.engine.on_new_bar(coin, ohlcv)
                    n_ok += 1
            except Exception as e:
                n_fail += 1
                logger.debug(f"[{coin}] K线更新失败: {e}")
            time.sleep(0.05)
        logger.info(f"K线更新: {n_ok}个币预测已刷, {n_fail}个失败")

    def _poll_tickers(self):
        """轮询所有币的实时价格"""
        try:
            tickers = self.api.fetch_tickers_all()
        except Exception as e:
            logger.warning(f"获取行情失败: {e}")
            return

        for coin, ticker in tickers.items():
            if coin not in self.engine.state:
                continue
            last_price = ticker.get('last')
            if last_price is None or last_price <= 0:
                continue
            try:
                self.engine.on_tick(coin, float(last_price))
            except Exception:
                pass

    def _check_orders(self):
        """检查订单状态（入场/出场成交检测）"""
        try:
            orders = self.api.fetch_open_orders()
        except Exception as e:
            logger.warning(f"获取订单失败: {e}")
            return

        open_ids = {o['id'] for o in orders}

        for coin, cs in self.engine.state.items():
            # 入场挂单成交检测
            if cs.entry_order_id and cs.entry_order_id not in open_ids:
                self._check_entry_fill(coin, cs)

            # 出场挂单成交检测
            if cs.holding:
                for oid in (cs.stop_order_id, cs.take_order_id):
                    if oid and oid not in open_ids:
                        self._check_exit_fill(coin, cs, oid)

    def _check_entry_fill(self, coin: str, cs) -> bool:
        sym = self.api.swap_symbol(coin)
        try:
            order_info = self.api.ex.fetch_order(cs.entry_order_id, sym)
            if order_info['status'] == 'closed' and float(order_info['filled']) > 0:
                fill_price = float(order_info['price'] or order_info['average'])
                filled = float(order_info['filled'])
                logger.info(f"[{coin}] ✅ 入场单成交: {filled}张 @ ${fill_price:.6f}")
                oid = cs.entry_order_id
                cs.entry_order_id = None
                self.engine.on_order_filled(
                    coin, oid, fill_price, filled,
                    order_info['side'], order_info.get('reduceOnly', False)
                )
                return True
            elif order_info['status'] == 'canceled':
                logger.info(f"[{coin}] 入场挂单被取消 ID={cs.entry_order_id}")
                cs.entry_order_id = None
                cs.entry_price = 0.0
        except Exception as e:
            if 'ORDER_NOT_FOUND' in str(e) or 'not found' in str(e).lower():
                cs.entry_order_id = None
                cs.entry_price = 0.0
        return False

    def _check_exit_fill(self, coin: str, cs, order_id: str) -> bool:
        sym = self.api.swap_symbol(coin)
        try:
            order_info = self.api.ex.fetch_order(order_id, sym)
            if order_info['status'] == 'closed' and float(order_info['filled']) > 0:
                fill_p = float(order_info.get('price', 0) or order_info.get('average', 0))
                otype = '止盈' if order_id == cs.take_order_id else '止损'
                logger.info(f"[{coin}] ✅ {otype}成交 @ ${fill_p:.6f}")
                self.engine.clear_position(coin, reason=f'limit_{otype}')
                return True
        except Exception:
            pass
        return False


if __name__ == '__main__':
    trader = LiveTrader()
    trader.run()
