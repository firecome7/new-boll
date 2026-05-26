"""布林带预测 + 偏移量训练 / 信号生成"""
from __future__ import annotations
import numpy as np
import pandas as pd
import logging

from config import BB_PERIOD, BB_STD, TRAINING_BARS, GROWTH_WINDOW, GROWTH_METHOD

logger = logging.getLogger('strategy')


def calc_bb(closes: list[float]) -> tuple[float, float, float]:
    """BB(25,2)"""
    arr = np.array(closes[-BB_PERIOD:])
    mid = float(np.mean(arr))
    std = float(np.std(arr, ddof=0))
    return mid, mid + BB_STD * std, mid - BB_STD * std


def train_offsets(ohlcv: list[list]) -> dict:
    """训练期：统计前100根K线的击穿深度 → 返回偏移量
    ohlcv: [[ts, o, h, l, c, v], ...]
    返回: {up_offset, low_offset} 百分比数值
    """
    n = len(ohlcv)
    if n < TRAINING_BARS:
        return {'up_offset': 0.0, 'low_offset': 0.0}

    df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])

    up_pens = []
    low_pens = []

    for i in range(BB_PERIOD - 1, TRAINING_BARS):
        closes = df['c'].values[i - BB_PERIOD + 1:i + 1].tolist()
        _, upper, lower = calc_bb(closes)
        if upper == 0 or lower == 0:
            continue
        high, low = df.iloc[i]['h'], df.iloc[i]['l']
        if high > upper:
            up_pens.append((high - upper) / upper)
        if low < lower:
            low_pens.append((lower - low) / lower)

    avg_up = float(np.mean(up_pens)) if up_pens else 0.0
    avg_low = float(np.mean(low_pens)) if low_pens else 0.0

    return {
        'up_offset': 2.0 * avg_up,
        'low_offset': 2.0 * avg_low,
        'up_samples': len(up_pens),
        'low_samples': len(low_pens),
    }


def precalc_bb(ohlcv: list[list]) -> dict[int, dict]:
    """预计算所有K线的BB值
    返回: {bar_idx: {mid, upper, lower}}
    """
    df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
    result = {}
    for i in range(BB_PERIOD - 1, len(ohlcv)):
        closes = df['c'].values[i - BB_PERIOD + 1:i + 1].tolist()
        mid, upper, lower = calc_bb(closes)
        result[i] = {'mid': mid, 'upper': upper, 'lower': lower}
    return result


def predict_bands(bb_data: dict[int, dict], bar_idx: int) -> tuple[float, float]:
    """预测第bar_idx根K线的上下轨
    用 bar_idx-6 到 bar_idx-1 的增速
    返回: (pred_upper, pred_lower)
    """
    if bar_idx < GROWTH_WINDOW + 1 or bar_idx - 1 not in bb_data:
        last = bb_data.get(bar_idx - 1)
        if last:
            return last['upper'], last['lower']
        return 0, 0

    # Upper growth rates
    up_rates = []
    for j in range(bar_idx - GROWTH_WINDOW, bar_idx):
        if j - 1 not in bb_data or j not in bb_data:
            continue
        prev = bb_data[j - 1]['upper']
        curr = bb_data[j]['upper']
        if prev > 0:
            up_rates.append((curr - prev) / prev)

    # Lower growth rates
    low_rates = []
    for j in range(bar_idx - GROWTH_WINDOW, bar_idx):
        if j - 1 not in bb_data or j not in bb_data:
            continue
        prev = bb_data[j - 1]['lower']
        curr = bb_data[j]['lower']
        if prev > 0:
            low_rates.append((curr - prev) / prev)

    if GROWTH_METHOD == 'mean':
        up_g = float(np.mean(up_rates)) if up_rates else 0.0
        low_g = float(np.mean(low_rates)) if low_rates else 0.0
    else:
        up_g = max(up_rates) if up_rates else 0.0
        low_g = max(low_rates) if low_rates else 0.0

    last_up = bb_data[bar_idx - 1]['upper']
    last_low = bb_data[bar_idx - 1]['lower']

    pred_up = last_up * (1 + up_g)
    pred_low = last_low * (1 + low_g)

    # Safety cap: ±50%
    max_change = 0.5
    pred_up = max(last_up * (1 - max_change), min(pred_up, last_up * (1 + max_change)))
    pred_low = max(last_low * (1 - max_change), min(pred_low, last_low * (1 + max_change)))

    return pred_up, pred_low
