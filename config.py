"""策略参数"""
from __future__ import annotations
import os, json

# ====== 交易参数 ======
INITIAL_CAPITAL = 100.0            # USDT
FIXED_POSITION_VALUE = 100.0       # 每笔名义价值 USDT
LEVERAGE = 50                      # 杠杆倍数
MAX_POSITIONS = 30                 # 最大同时持仓数
MARGIN_MODE = 'cross'              # 全仓

# ====== 策略参数 ======
TP_PCT = 0.02                      # 止盈 2%
SL_PCT = 0.01                      # 止损 1%
BB_PERIOD = 25                     # 布林带周期
BB_STD = 2                         # 布林带标准差倍数
ORDER_LIFETIME_BARS = 1            # 挂单最多等1根K线
TIMEFRAME = '15m'                  # 时间周期
TRAINING_BARS = 100                # 训练期K线数
GROWTH_WINDOW = 5                  # 增速窗口：T-5到T-1
GROWTH_METHOD = 'mean'             # mean 或 max
TIMEOUT_SECONDS = 15               # 触价后等15秒转市价

# ====== 费用 ======
MAKER_FEE = 0.0002                 # 挂单费率 0.02%
TAKER_FEE = 0.0005                 # 市价费率 0.05%

# ====== 币种 ======
COIN_LIMIT = 150
MIN_VOLUME = 2_000_000
EXCLUDE = {'BTC', 'ETH', 'XRP', 'BNB', 'SOL', 'DOGE', 'ADA',
           'USDC', 'TRX', 'LINK', 'AVAX', 'TON', 'DOT', 'MATIC', 'SHIB'}

# ====== 行情轮询 ======
BAR_SECONDS = 900                  # 15m
POLL_INTERVAL = 15                 # 行情轮询间隔(秒)
ORDER_CHECK_INTERVAL = 5           # 订单检查间隔(秒)
SYNC_INTERVAL = 30                 # 持仓同步间隔(秒)

# ====== API ======
def load_api_keys() -> dict:
    api_key = os.environ.get('GATE_API_KEY')
    api_secret = os.environ.get('GATE_API_SECRET')
    if api_key and api_secret:
        return {'apiKey': api_key, 'secret': api_secret}
    key_file = os.path.join(os.path.dirname(__file__), '../live_bollinger/gate_keys.json')
    if os.path.exists(key_file):
        with open(key_file) as f:
            return json.load(f)
    raise RuntimeError("请设置环境变量 GATE_API_KEY, GATE_API_SECRET")

# ====== 验证模式 ======
DRY_RUN = False
