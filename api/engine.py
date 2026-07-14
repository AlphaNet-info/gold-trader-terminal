#!/usr/bin/env python3
"""
Gold Trading Decision Engine v2 — XAU/USD
三套交易方法：
  1. 裸K交易系统 (EMA21/55/144趋势 + 关键位 + SB结构入场)
  2. DD结构入场 (趋势 + 61.8%回调 + 双十字星 + EMA20)
  3. 复杂回调系统 (楔形三推 + 关键位 + SB结构 + 高1/低1入场)

Author: QClaw | 2026-07-14
"""
import os, sys, json, math, datetime as dt, base64
from typing import List, Dict, Any, Optional, Tuple

import requests

# ---------------- Config & State ----------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "AlphaNet-info/gold-trader-terminal")
GITHUB_STATE_PATH = "state.json"

def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("TELEGRAM_ENABLED"):
        cfg["telegram_enabled"] = os.environ["TELEGRAM_ENABLED"].lower() in ("true", "1", "yes")
    return cfg

def log(msg: str):
    print(f"[{dt.datetime.now().isoformat()}] {msg}", file=sys.stderr)

def now_sh() -> dt.datetime:
    """当前上海时间"""
    return dt.datetime.utcnow() + dt.timedelta(hours=8)

def to_bjt(utc_dt: dt.datetime) -> dt.datetime:
    return utc_dt + dt.timedelta(hours=8)

def fmt_bjt(utc_dt: dt.datetime, fmt: str = "%m-%d %H:%M") -> str:
    return to_bjt(utc_dt).strftime(fmt)

# ---------------- Data Fetch ----------------

def fetch_yahoo(symbol: str, range_: str, interval: str) -> Dict[str, Any]:
    url = f"{YAHOO_BASE}/{symbol}"
    params = {"range": range_, "interval": interval}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result", [None])[0]
    if not result:
        raise ValueError(f"Yahoo returned empty result for {symbol}")
    return result

def to_bars(result: Dict[str, Any]) -> List[Dict[str, float]]:
    ts_list = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    bars = []
    for i, t in enumerate(ts_list):
        o = quote.get("open", [None]*len(ts_list))[i]
        h = quote.get("high", [None]*len(ts_list))[i]
        l = quote.get("low", [None]*len(ts_list))[i]
        c = quote.get("close", [None]*len(ts_list))[i]
        v = quote.get("volume", [None]*len(ts_list))[i]
        if o is None or h is None or l is None or c is None:
            continue
        bars.append({
            "ts": t,
            "dt": dt.datetime.utcfromtimestamp(t),
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": float(v) if v else 0.0,
        })
    return bars

# ---------------- Indicators ----------------

def ema(values: List[float], period: int) -> List[Optional[float]]:
    """EMA 计算，返回与 values 等长的列表，前面不足 period 的为 None"""
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [None] * len(values)
    # 第一个有效值用前 period 个的 SMA
    if len(values) < period:
        return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    for i in range(period, len(values)):
        prev = result[i - 1]
        result[i] = values[i] * k + prev * (1 - k)
    return result

def ema_of_bars(bars: List[Dict], field: str = "close", period: int = 21) -> List[Optional[float]]:
    values = [b[field] for b in bars]
    return ema(values, period)

def atr(bars: List[Dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l = bars[i]["high"], bars[i]["low"]
        pc = bars[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else (sum(trs) / len(trs) if trs else 0.0)

def macd(bars: List[Dict], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, List]:
    closes = [b["close"] for b in bars]
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = []
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line.append(ema_fast[i] - ema_slow[i])
        else:
            macd_line.append(None)
    # signal line = EMA of macd_line (only non-None part)
    valid = [v for v in macd_line if v is not None]
    sig = ema(valid, signal) if len(valid) >= signal else [None] * len(valid)
    # align signal back
    signal_line = [None] * len(closes)
    idx = 0
    for i in range(len(closes)):
        if macd_line[i] is not None:
            if idx < len(sig):
                signal_line[i] = sig[idx]
            idx += 1
    hist = []
    for i in range(len(closes)):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist.append(macd_line[i] - signal_line[i])
        else:
            hist.append(None)
    return {"macd": macd_line, "signal": signal_line, "hist": hist}

def rsi(bars: List[Dict], period: int = 14) -> List[Optional[float]]:
    closes = [b["close"] for b in bars]
    if len(closes) < period + 1:
        return [None] * len(closes)
    result = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = 100 - (100 / (1 + (avg_gain / avg_loss if avg_loss else 999)))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i-1]
        gain = max(ch, 0)
        loss = max(-ch, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))
    return result

# ---------------- Structure Detection ----------------

def find_swing_highs_lows(bars: List[Dict], window: int = 2) -> Tuple[List[Dict], List[Dict]]:
    """找 swing high / swing low，window=2 表示左右各2根"""
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    sh, sl = [], []
    for i in range(window, len(bars) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            sh.append({"i": i, "price": highs[i], "dt": bars[i]["dt"], "bar": bars[i]})
        if lows[i] == min(lows[i-window:i+window+1]):
            sl.append({"i": i, "price": lows[i], "dt": bars[i]["dt"], "bar": bars[i]})
    # 去重连续
    def dedup(swings):
        out = []
        for s in swings:
            if not out or abs(s["price"] - out[-1]["price"]) > 0.05:
                out.append(s)
        return out
    return dedup(sh), dedup(sl)

def detect_ema_trend(bars: List[Dict], ema21: List, ema55: List, ema144: List) -> Dict[str, Any]:
    """EMA 均线组判断趋势：多头排列(21>55>144)=up, 空头排列(144>55>21)=down, 否则=range"""
    if not bars or ema21[-1] is None or ema55[-1] is None or ema144[-1] is None:
        return {"direction": "unknown", "detail": "EMA 数据不足"}
    e21, e55, e144 = ema21[-1], ema55[-1], ema144[-1]
    price = bars[-1]["close"]
    if e21 > e55 > e144:
        direction = "up"
        detail = f"多头排列 EMA21={e21:.2f} > 55={e55:.2f} > 144={e144:.2f} | 价格={price:.2f}"
    elif e144 > e55 > e21:
        direction = "down"
        detail = f"空头排列 EMA144={e144:.2f} > 55={e55:.2f} > 21={e21:.2f} | 价格={price:.2f}"
    else:
        direction = "range"
        detail = f"均线缠绕 EMA21={e21:.2f}, 55={e55:.2f}, 144={e144:.2f} | 观望"
    return {"direction": direction, "detail": detail, "ema21": e21, "ema55": e55, "ema144": e144}

def find_key_levels(bars: List[Dict], lookback: int = 100, min_touch: int = 3) -> List[Dict]:
    """找关键支撑压力位：接触次数多、同时充当过支撑和压力、画成区间"""
    sh, sl = find_swing_highs_lows(bars[-lookback:], window=2)
    all_swings = sh + sl
    if len(all_swings) < 3:
        return []
    # 聚类：把价格相近的 swing 点合并
    clusters = []
    for s in all_swings:
        placed = False
        for c in clusters:
            if abs(s["price"] - c["center"]) < 2.0:  # 2美元容差
                c["points"].append(s)
                c["center"] = sum(p["price"] for p in c["points"]) / len(c["points"])
                placed = True
                break
        if not placed:
            clusters.append({"center": s["price"], "points": [s]})
    # 过滤：接触次数>=min_touch，且同时有 swing high 和 swing low（充当过支撑和压力）
    levels = []
    for c in clusters:
        if len(c["points"]) < min_touch:
            continue
        has_high = any(p in sh for p in c["points"])
        has_low = any(p in sl for p in c["points"])
        if has_high and has_low:
            prices = [p["price"] for p in c["points"]]
            levels.append({
                "center": c["center"],
                "upper": max(prices),
                "lower": min(prices),
                "touches": len(c["points"]),
                "has_support": has_low,
                "has_resistance": has_high,
            })
    levels.sort(key=lambda x: x["touches"], reverse=True)
    return levels[:8]

def find_fvg(bars: List[Dict]) -> List[Dict]:
    """检测 Fair Value Gap (FVG) — 三根K线中第一根high和第三根low之间的缺口（看涨FVG）
    或第一根low和第三根high之间的缺口（看跌FVG）"""
    fvgs = []
    for i in range(len(bars) - 2):
        b0, b1, b2 = bars[i], bars[i+1], bars[i+2]
        # 看涨FVG: b0.high < b2.low
        if b0["high"] < b2["low"]:
            fvgs.append({"type": "bullish", "index": i+1, "upper": b2["low"], "lower": b0["high"], "dt": b1["dt"]})
        # 看跌FVG: b0.low > b2.high
        elif b0["low"] > b2["high"]:
            fvgs.append({"type": "bearish", "index": i+1, "upper": b0["low"], "lower": b2["high"], "dt": b1["dt"]})
    return fvgs

def detect_sb_structure(bars: List[Dict], direction: str, lookback: int = 20) -> Optional[Dict]:
    """检测 SB 结构（两次逆势突破失败）
    direction='up' 做多：寻找下跌趋势后两次向下突破失败
    direction='down' 做空：寻找上涨趋势后两次向上突破失败
    """
    if len(bars) < 10:
        return None
    recent = bars[-lookback:] if len(bars) >= lookback else bars
    if len(recent) < 10:
        return None

    if direction == "up":
        # 找做空机会的反面：下跌趋势后两次向下突破失败
        # 突破失败 = K线 low 突破前低后收盘回到前低之上
        swings = find_swing_highs_lows(recent, window=1)
        lows = swings[1]  # swing lows
        if len(lows) < 2:
            return None
        # 两次向下突破失败
        failures = 0
        for j in range(len(lows) - 1):
            level = lows[j]["price"]
            next_bar = recent[lows[j+1]["i"]] if lows[j+1]["i"] < len(recent) else None
            if next_bar and next_bar["low"] < level and next_bar["close"] > level:
                failures += 1
        if failures >= 2:
            last_bar = recent[-1]
            return {
                "type": "SB_bullish",
                "signal_bar": last_bar,
                "entry": last_bar["close"],
                "stop": last_bar["low"],
                "detail": f"两次向下突破失败({failures}次) | 入场={last_bar['close']:.2f} 止损={last_bar['low']:.2f}",
            }
    elif direction == "down":
        swings = find_swing_highs_lows(recent, window=1)
        highs = swings[0]
        if len(highs) < 2:
            return None
        failures = 0
        for j in range(len(highs) - 1):
            level = highs[j]["price"]
            next_bar = recent[highs[j+1]["i"]] if highs[j+1]["i"] < len(recent) else None
            if next_bar and next_bar["high"] > level and next_bar["close"] < level:
                failures += 1
        if failures >= 2:
            last_bar = recent[-1]
            return {
                "type": "SB_bearish",
                "signal_bar": last_bar,
                "entry": last_bar["close"],
                "stop": last_bar["high"],
                "detail": f"两次向上突破失败({failures}次) | 入场={last_bar['close']:.2f} 止损={last_bar['high']:.2f}",
            }
    return None

def is_doji(bar: Dict, body_threshold: float = 0.15) -> bool:
    """十字星：实体长度 < 全振幅的 15%"""
    body = abs(bar["close"] - bar["open"])
    full = bar["high"] - bar["low"]
    if full <= 0:
        return False
    return body / full < body_threshold

def find_trend_segment(bars: List[Dict], min_bars: int = 10) -> Optional[Dict]:
    """找到最近一段明确的趋势（用于DD结构）"""
    if len(bars) < min_bars:
        return None
    # 用最近 30 根找趋势
    recent = bars[-30:] if len(bars) >= 30 else bars
    ema21_l = ema_of_bars(recent, "close", 21)
    if ema21_l[-1] is None:
        return None
    # 判断方向
    start_price = recent[0]["close"]
    end_price = recent[-1]["close"]
    if end_price > start_price:
        direction = "up"
    elif end_price < start_price:
        direction = "down"
    else:
        return None
    # 找趋势的起点和终点
    if direction == "up":
        low_idx = min(range(len(recent)), key=lambda i: recent[i]["low"])
        high_idx = max(range(len(recent)), key=lambda i: recent[i]["high"])
        trend_start = recent[low_idx]
        trend_end = recent[high_idx] if high_idx > low_idx else recent[-1]
    else:
        high_idx = max(range(len(recent)), key=lambda i: recent[i]["high"])
        low_idx = min(range(len(recent)), key=lambda i: recent[i]["low"])
        trend_start = recent[high_idx]
        trend_end = recent[low_idx] if low_idx > high_idx else recent[-1]
    return {
        "direction": direction,
        "start": trend_start,
        "end": trend_end,
        "start_price": trend_start["low"] if direction == "up" else trend_start["high"],
        "end_price": trend_end["high"] if direction == "up" else trend_end["low"],
    }

def fibonacci_retracement(start_price: float, end_price: float) -> Dict[str, float]:
    """计算斐波那契回调位"""
    diff = end_price - start_price
    return {
        "0%": end_price,
        "23.6%": end_price - 0.236 * diff,
        "38.2%": end_price - 0.382 * diff,
        "50%": end_price - 0.5 * diff,
        "61.8%": end_price - 0.618 * diff,
        "78.6%": end_price - 0.786 * diff,
        "100%": start_price,
    }

def detect_dd_structure(bars: List[Dict], trend: Dict, ema20: List) -> Optional[Dict]:
    """检测 DD 结构（趋势 + 61.8%回调 + 双十字星）
    必要条件：
    1. 必须有一段明确的趋势
    2. 回调不能跌破整段趋势的61.8%
    3. 两个十字星越靠近EMA20越好
    4. 越靠近极值点越好
    5. 回调必须是简单回调
    """
    if not trend or len(bars) < 15:
        return None
    fib = fibonacci_retracement(trend["start_price"], trend["end_price"])
    fib_618 = fib["61.8%"]
    direction = trend["direction"]
    recent = bars[-15:]
    # 检查回调是否超过61.8%
    if direction == "up":
        # 做多：回调最低点不应跌破61.8%
        min_low = min(b["low"] for b in recent)
        if min_low < fib_618:
            return None
    else:
        # 做空：回调最高点不应超过61.8%
        max_high = max(b["high"] for b in recent)
        if max_high > fib_618:
            return None
    # 找最近两根十字星
    dojis = [b for b in recent[-6:] if is_doji(b)]
    if len(dojis) < 2:
        return None
    # 取最近两根十字星
    d1, d2 = dojis[-2], dojis[-1]
    # 检查是否靠近 EMA20
    ema20_val = ema20[-1] if ema20 and ema20[-1] is not None else None
    ema_dist = 0
    if ema20_val:
        mid_doji = (d1["close"] + d2["close"]) / 2
        ema_dist = abs(mid_doji - ema20_val)
    # 止损放在信号K线底部/顶部
    if direction == "up":
        stop = min(d1["low"], d2["low"])
        entry = d2["close"]
        target = entry + (entry - stop) * 2  # 1:2 盈亏比
    else:
        stop = max(d1["high"], d2["high"])
        entry = d2["close"]
        target = entry - (stop - entry) * 2
    return {
        "type": "DD_structure",
        "direction": direction,
        "doji1": d1,
        "doji2": d2,
        "entry": entry,
        "stop": stop,
        "target": target,
        "fib_618": fib_618,
        "ema20_dist": ema_dist,
        "detail": f"DD结构({direction}) | 双十字星 @ {fmt_bjt(d2['dt'])} | EMA20距离={ema_dist:.2f} | 61.8%={fib_618:.2f}",
    }

def detect_wedge(bars: List[Dict], lookback: int = 30) -> Optional[Dict]:
    """检测楔形形态（三推）"""
    if len(bars) < lookback:
        return None
    recent = bars[-lookback:]
    sh, sl = find_swing_highs_lows(recent, window=2)
    # 需要至少3个swing high和3个swing low
    if len(sh) < 3 or len(sl) < 3:
        return None
    # 下降楔形：swing highs 逐步降低，swing lows 也逐步降低，但 highs 下降幅度 > lows 下降幅度
    # 上升楔形：swing highs 逐步升高，swing lows 也逐步升高，但 lows 上升幅度 > highs 上升幅度
    sh_prices = [s["price"] for s in sh[-3:]]
    sl_prices = [s["price"] for s in sl[-3:]]
    # 三推
    if all(sh_prices[i] > sh_prices[i+1] for i in range(len(sh_prices)-1)) and \
       all(sl_prices[i] > sl_prices[i+1] for i in range(len(sl_prices)-1)):
        # 下降楔形
        high_decline = sh_prices[0] - sh_prices[-1]
        low_decline = sl_prices[0] - sl_prices[-1]
        if high_decline > low_decline:
            # 推动力度递减
            pushes = [sh_prices[i] - sh_prices[i+1] for i in range(len(sh_prices)-1)]
            weakening = all(pushes[i] > pushes[i+1] for i in range(len(pushes)-1)) if len(pushes) >= 2 else False
            return {
                "type": "falling_wedge",
                "direction": "up",  # 下降楔形通常向上突破
                "pushes": pushes,
                "weakening": weakening,
                "start_price": sh_prices[0],
                "end_price": sl_prices[-1],
                "detail": f"下降楔形 三推 衰减={'是' if weakening else '否'} | 起点={sh_prices[0]:.2f}",
            }
    elif all(sh_prices[i] < sh_prices[i+1] for i in range(len(sh_prices)-1)) and \
         all(sl_prices[i] < sl_prices[i+1] for i in range(len(sl_prices)-1)):
        # 上升楔形
        low_rise = sl_prices[-1] - sl_prices[0]
        high_rise = sh_prices[-1] - sh_prices[0]
        if low_rise > high_rise:
            pushes = [sl_prices[i+1] - sl_prices[i] for i in range(len(sl_prices)-1)]
            weakening = all(pushes[i] > pushes[i+1] for i in range(len(pushes)-1)) if len(pushes) >= 2 else False
            return {
                "type": "rising_wedge",
                "direction": "down",  # 上升楔形通常向下突破
                "pushes": pushes,
                "weakening": weakening,
                "start_price": sl_prices[0],
                "end_price": sh_prices[-1],
                "detail": f"上升楔形 三推 衰减={'是' if weakening else '否'} | 起点={sl_prices[0]:.2f}",
            }
    return None

def find_high1_low1(bars: List[Dict], direction: str) -> Optional[Dict]:
    """找 High 1 / Low 1 信号K线
    High 1: 上升趋势中第一次回调后创新高的K线
    Low 1: 下降趋势中第一次反弹后创新低的K线
    """
    if len(bars) < 5:
        return None
    recent = bars[-10:]
    if direction == "up":
        # High 1: 找最近一根创新高的K线（前5根的最高点）
        ref_high = max(b["high"] for b in recent[:-1]) if len(recent) > 1 else 0
        for b in recent[-3:]:
            if b["close"] > ref_high or b["high"] > ref_high:
                return {
                    "type": "High1",
                    "signal_bar": b,
                    "entry": b["close"],
                    "stop": b["low"],
                    "detail": f"High1 信号K线 @ {fmt_bjt(b['dt'])} | 入场={b['close']:.2f} 止损={b['low']:.2f}",
                }
    elif direction == "down":
        ref_low = min(b["low"] for b in recent[:-1]) if len(recent) > 1 else 9999
        for b in recent[-3:]:
            if b["close"] < ref_low or b["low"] < ref_low:
                return {
                    "type": "Low1",
                    "signal_bar": b,
                    "entry": b["close"],
                    "stop": b["high"],
                    "detail": f"Low1 信号K线 @ {fmt_bjt(b['dt'])} | 入场={b['close']:.2f} 止损={b['high']:.2f}",
                }
    return None

def find_self_structured_levels(bars: List[Dict]) -> List[Dict]:
    """找自构关键位：双底、孤立支点、两次不破等"""
    sh, sl = find_swing_highs_lows(bars[-60:], window=2)
    levels = []
    # 双底（double bottom）
    for i in range(len(sl) - 1):
        for j in range(i + 1, len(sl)):
            if abs(sl[i]["price"] - sl[j]["price"]) < 1.5 and sl[i]["price"] == min(sl[i]["price"], sl[j]["price"]):
                levels.append({
                    "type": "double_bottom",
                    "center": (sl[i]["price"] + sl[j]["price"]) / 2,
                    "upper": max(sl[i]["price"], sl[j]["price"]) + 0.5,
                    "lower": min(sl[i]["price"], sl[j]["price"]) - 0.5,
                    "touches": 2,
                    "detail": f"双底 {sl[i]['price']:.2f}≈{sl[j]['price']:.2f}",
                })
    # 双顶（double top）
    for i in range(len(sh) - 1):
        for j in range(i + 1, len(sh)):
            if abs(sh[i]["price"] - sh[j]["price"]) < 1.5 and sh[i]["price"] == max(sh[i]["price"], sh[j]["price"]):
                levels.append({
                    "type": "double_top",
                    "center": (sh[i]["price"] + sh[j]["price"]) / 2,
                    "upper": max(sh[i]["price"], sh[j]["price"]) + 0.5,
                    "lower": min(sh[i]["price"], sh[j]["price"]) - 0.5,
                    "touches": 2,
                    "detail": f"双顶 {sh[i]['price']:.2f}≈{sh[j]['price']:.2f}",
                })
    return levels

# ---------------- Trading Methods ----------------

def method1_naked_k(bars_1h: List[Dict], bars_15m: List[Dict]) -> Dict[str, Any]:
    """方法一：裸K交易系统
    1. 1h EMA(21,55,144) 判断趋势
    2. 1h 找关键支撑压力位（区间）
    3. 15m 关键位附近 SB 结构入场
    """
    ema21 = ema_of_bars(bars_1h, "close", 21)
    ema55 = ema_of_bars(bars_1h, "close", 55)
    ema144 = ema_of_bars(bars_1h, "close", 144)
    trend = detect_ema_trend(bars_1h, ema21, ema55, ema144)
    levels = find_key_levels(bars_1h, lookback=100, min_touch=3)
    direction = trend["direction"]
    signal = None
    if direction in ("up", "down") and levels:
        # 检查15m是否在关键位附近出现SB结构
        current_price = bars_15m[-1]["close"] if bars_15m else 0
        for lvl in levels[:4]:
            if lvl["lower"] - 3 <= current_price <= lvl["upper"] + 3:
                # 在关键位附近
                sb = detect_sb_structure(bars_15m, direction, lookback=20)
                if sb:
                    if direction == "up":
                        stop = sb["stop"]
                        entry = sb["entry"]
                        target = entry + (entry - stop) * 2
                    else:
                        stop = sb["stop"]
                        entry = sb["entry"]
                        target = entry - (stop - entry) * 2
                    signal = {
                        "method": "裸K交易系统",
                        "direction": "做多" if direction == "up" else "做空",
                        "type": sb["type"],
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "level": lvl,
                        "detail": f"关键位[{lvl['lower']:.2f}-{lvl['upper']:.2f}] 附近 {sb['detail']}",
                    }
                    break
    return {
        "name": "裸K交易系统",
        "trend": trend,
        "levels": levels,
        "signal": signal,
        "direction": direction,
    }

def method2_dd_structure(bars_1h: List[Dict], bars_15m: List[Dict]) -> Dict[str, Any]:
    """方法二：DD结构入场
    1. 必须有一段明确的趋势
    2. 回调不能跌破整段趋势的61.8%
    3. 两个十字星越靠近EMA20越好
    4. 越靠近极值点越好
    5. 回调必须是简单回调
    """
    trend = find_trend_segment(bars_1h, min_bars=10)
    ema20 = ema_of_bars(bars_15m, "close", 20)
    dd = None
    if trend:
        dd = detect_dd_structure(bars_15m, trend, ema20)
    fib = None
    if trend:
        fib = fibonacci_retracement(trend["start_price"], trend["end_price"])
    signal = None
    if dd:
        signal = {
            "method": "DD结构入场",
            "direction": "做多" if dd["direction"] == "up" else "做空",
            "type": dd["type"],
            "entry": dd["entry"],
            "stop": dd["stop"],
            "target": dd["target"],
            "detail": dd["detail"],
        }
    return {
        "name": "DD结构入场",
        "trend": trend,
        "fib": fib,
        "dd": dd,
        "signal": signal,
        "direction": trend["direction"] if trend else "unknown",
    }

def method3_complex_pullback(bars_1h: List[Dict], bars_15m: List[Dict]) -> Dict[str, Any]:
    """方法三：复杂回调交易系统
    1. EMA(21,55,144) 判断趋势 + FVG + 趋势K线判断能量
    2. 关键位（验证次数多 + 自构关键位如双底/孤立支点）
    3. 楔形三推 + SB结构 + High1/Low1 入场
    """
    ema21 = ema_of_bars(bars_1h, "close", 21)
    ema55 = ema_of_bars(bars_1h, "close", 55)
    ema144 = ema_of_bars(bars_1h, "close", 144)
    trend = detect_ema_trend(bars_1h, ema21, ema55, ema144)
    fvgs = find_fvg(bars_1h[-50:])
    levels = find_key_levels(bars_1h, lookback=100, min_touch=3)
    self_levels = find_self_structured_levels(bars_1h)
    all_levels = levels + self_levels
    wedge = detect_wedge(bars_15m, lookback=30)
    signal = None
    direction = trend["direction"]
    if direction in ("up", "down") and wedge:
        # 楔形方向与趋势方向一致
        if wedge["direction"] == direction and wedge["weakening"]:
            # 在关键位附近
            current_price = bars_15m[-1]["close"] if bars_15m else 0
            near_level = None
            for lvl in all_levels[:5]:
                upper = lvl.get("upper", lvl.get("center", 0))
                lower = lvl.get("lower", lvl.get("center", 0))
                if lower - 3 <= current_price <= upper + 3:
                    near_level = lvl
                    break
            if near_level:
                # SB结构 + High1/Low1
                sb = detect_sb_structure(bars_15m, direction, lookback=15)
                hl = find_high1_low1(bars_15m, direction)
                entry_signal = sb or hl
                if entry_signal:
                    if direction == "up":
                        stop = entry_signal["stop"]
                        entry = entry_signal["entry"]
                        target = wedge["start_price"]  # 止盈参考楔形起点
                        if target <= entry:
                            target = entry + (entry - stop) * 2
                    else:
                        stop = entry_signal["stop"]
                        entry = entry_signal["entry"]
                        target = wedge["start_price"]
                        if target >= entry:
                            target = entry - (stop - entry) * 2
                    signal = {
                        "method": "复杂回调系统",
                        "direction": "做多" if direction == "up" else "做空",
                        "type": entry_signal["type"],
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "wedge": wedge,
                        "level": near_level,
                        "detail": f"楔形={wedge['type']} | 关键位={near_level.get('detail', near_level.get('center', 0))} | {entry_signal['detail']}",
                    }
    return {
        "name": "复杂回调系统",
        "trend": trend,
        "fvgs": fvgs[-3:] if fvgs else [],
        "levels": all_levels,
        "wedge": wedge,
        "signal": signal,
        "direction": direction,
    }

# ---------------- HTML Generation ----------------

def generate_html(results: List[Dict], bars_1h, bars_15m, current_price: float) -> str:
    """生成三套方法的 HTML 报告"""
    now_str = now_sh().strftime("%Y-%m-%d %H:%M:%S (GMT+8)")
    # 颜色
    C_BG = "#0a0a0a"
    C_PANEL = "#111"
    C_BORDER = "#1e1e1e"
    C_BORDER2 = "#2a2a2a"
    C_TEXT = "#e0e0e0"
    C_DIM = "#666"
    C_ORANGE = "#ff8800"
    C_GREEN = "#00e676"
    C_RED = "#ff5252"
    C_YELLOW = "#ffab40"
    C_BLUE = "#448aff"
    C_PURPLE = "#e040fb"

    # 当前价格
    prev_price = bars_15m[-2]["close"] if len(bars_15m) >= 2 else current_price
    chg = current_price - prev_price
    chg_pct = (chg / prev_price * 100) if prev_price else 0
    price_color = C_GREEN if chg >= 0 else C_RED

    html_parts = []
    # CSS
    html_parts.append(f"""<style>
:root {{ --bg:{C_BG}; --panel:{C_PANEL}; --border:{C_BORDER}; --border2:{C_BORDER2}; --text:{C_TEXT}; --dim:{C_DIM}; --orange:{C_ORANGE}; --green:{C_GREEN}; --red:{C_RED}; --yellow:{C_YELLOW}; --blue:{C_BLUE}; --purple:{C_PURPLE}; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--text); font-family:'SF Mono',Menlo,Consolas,monospace; font-size:13px; line-height:1.6; }}
.container {{ max-width:1400px; margin:0 auto; padding:12px; }}
.topbar {{ display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid var(--border); margin-bottom:16px; }}
.topbar-left {{ display:flex; align-items:center; gap:16px; }}
.topbar-right {{ display:flex; align-items:center; gap:8px; }}
.brand {{ font-size:18px; font-weight:bold; color:var(--orange); letter-spacing:1px; }}
.price-box {{ display:flex; align-items:baseline; gap:8px; }}
.price {{ font-size:22px; font-weight:bold; color:{price_color}; }}
.chg {{ font-size:12px; color:{price_color}; }}
.timestamp {{ font-size:11px; color:var(--dim); }}
.gear-btn {{ background:none; border:1px solid var(--border2); color:var(--dim); padding:4px 8px; border-radius:4px; cursor:pointer; font-size:14px; font-family:inherit; transition:all 0.2s; }}
.gear-btn:hover {{ border-color:var(--orange); color:var(--orange); }}
.method-card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:16px; }}
.method-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; padding-bottom:10px; border-bottom:1px solid var(--border); }}
.method-title {{ font-size:15px; font-weight:bold; color:var(--orange); }}
.method-badge {{ padding:2px 8px; border-radius:4px; font-size:11px; font-weight:bold; }}
.badge-up {{ background:rgba(0,230,118,0.12); color:var(--green); border:1px solid rgba(0,230,118,0.3); }}
.badge-down {{ background:rgba(255,82,82,0.12); color:var(--red); border:1px solid rgba(255,82,82,0.3); }}
.badge-range {{ background:rgba(102,102,102,0.12); color:var(--dim); border:1px solid var(--border2); }}
.badge-signal {{ background:rgba(255,136,0,0.12); color:var(--orange); border:1px solid rgba(255,136,0,0.3); }}
.section {{ margin-bottom:12px; }}
.section-label {{ font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }}
.section-content {{ font-size:12px; color:var(--text); }}
.kv {{ display:flex; gap:6px; margin-bottom:2px; }}
.kv-label {{ color:var(--dim); min-width:80px; }}
.kv-value {{ color:var(--text); }}
.level-item {{ display:inline-block; background:#161616; border:1px solid var(--border2); padding:2px 8px; border-radius:4px; margin:2px; font-size:11px; }}
.signal-box {{ background:rgba(255,136,0,0.06); border:1px solid rgba(255,136,0,0.3); border-radius:6px; padding:12px; margin-top:8px; }}
.signal-box-none {{ background:#0d0d0d; border:1px solid var(--border); border-radius:6px; padding:12px; margin-top:8px; }}
.trade-plan {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:8px; }}
.tp-item {{ background:#0d0d0d; border:1px solid var(--border2); padding:8px; border-radius:4px; text-align:center; }}
.tp-label {{ font-size:10px; color:var(--dim); text-transform:uppercase; margin-bottom:4px; }}
.tp-value {{ font-size:14px; font-weight:bold; }}
.no-signal {{ color:var(--dim); font-size:12px; }}
.divider {{ height:1px; background:var(--border); margin:10px 0; }}
.wedge-info {{ font-size:11px; color:var(--blue); }}
.fvg-item {{ display:inline-block; background:rgba(68,138,255,0.08); border:1px solid rgba(68,138,255,0.2); padding:2px 6px; border-radius:3px; margin:2px; font-size:10px; }}
.fib-row {{ display:flex; gap:4px; flex-wrap:wrap; }}
.fib-level {{ padding:2px 6px; border-radius:3px; font-size:10px; }}
.chart-container {{ margin-top:12px; border:1px solid var(--border); border-radius:6px; overflow:hidden; }}
#chart1h {{ width:100%; height:400px; }}
#chart15m {{ width:100%; height:400px; }}
.tabs {{ display:flex; gap:4px; margin-bottom:4px; }}
.tab {{ padding:4px 12px; border:1px solid var(--border2); background:none; color:var(--dim); cursor:pointer; font-family:inherit; font-size:12px; border-radius:4px 4px 0 0; }}
.tab.active {{ border-color:var(--orange); color:var(--orange); background:rgba(255,136,0,0.05); }}
</style>""")

    # Topbar
    html_parts.append(f"""
<div class="topbar">
  <div class="topbar-left">
    <div class="brand">XAU/USD TERMINAL v2</div>
    <div class="price-box">
      <span class="price">{current_price:.2f}</span>
      <span class="chg">{'+' if chg>=0 else ''}{chg:.2f} ({'+' if chg_pct>=0 else ''}{chg_pct:.2f}%)</span>
    </div>
  </div>
  <div class="topbar-right">
    <span class="timestamp">{now_str}</span>
    <a href="https://macro-dashboard-taupe.vercel.app/" class="gear-btn" title="返回宏观观察台" style="text-decoration:none">← 返回</a>
    <button class="gear-btn" onclick="openSettings()" title="Telegram 通知设置">⚙</button>
  </div>
</div>""")

    # Chart section
    html_parts.append("""
<div class="chart-container">
  <div class="tabs">
    <button class="tab active" onclick="switchChart('1h')">1H</button>
    <button class="tab" onclick="switchChart('15m')">15M</button>
  </div>
  <div id="chart1h"></div>
  <div id="chart15m" style="display:none"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script>
var g_chart1h=null,g_chart15m=null,g_candle1h=null,g_candle15m=null;
var g_ma21_1h=null,g_ma55_1h=null,g_ma144_1h=null;
var g_ma21_15m=null,g_ma55_15m=null;
function initCharts(){
  var d1h=window._data1h||[],d15m=window._data15m||[];
  if(d1h.length&&typeof LightweightCharts!=='undefined'){
    var c1=document.getElementById('chart1h');
    g_chart1h=LightweightCharts.createChart(c1,{layout:{background:{type:'solid',color:'#0a0a0a'},textColor:'#888',fontSize:11},grid:{vertLines:{color:'#141414'},horzLines:{color:'#141414'}},rightPriceScale:{borderColor:'#2a2a2a'},timeScale:{borderColor:'#2a2a2a',timeVisible:true},width:c1.clientWidth,height:400});
    g_candle1h=g_chart1h.addCandlestickSeries({upColor:'#00e676',downColor:'#ff5252',borderUpColor:'#00e676',borderDownColor:'#ff5252',wickUpColor:'#00e676',wickDownColor:'#ff5252'});
    g_candle1h.setData(d1h);
    if(window._ma1h){g_ma21_1h=g_chart1h.addLineSeries({color:'#ff8800',lineWidth:2,priceLineVisible:false,lastValueVisible:false});g_ma21_1h.setData(window._ma1h.e21||[]);g_ma55_1h=g_chart1h.addLineSeries({color:'#448aff',lineWidth:2,priceLineVisible:false,lastValueVisible:false});g_ma55_1h.setData(window._ma1h.e55||[]);g_ma144_1h=g_chart1h.addLineSeries({color:'#e040fb',lineWidth:2,lineStyle:2,priceLineVisible:false,lastValueVisible:false});g_ma144_1h.setData(window._ma1h.e144||[]);}
    g_chart1h.timeScale().fitContent();
    new ResizeObserver(function(e){if(e[0]&&g_chart1h)g_chart1h.applyOptions({width:e[0].contentRect.width});}).observe(c1);
  }
  if(d15m.length&&typeof LightweightCharts!=='undefined'){
    var c2=document.getElementById('chart15m');
    g_chart15m=LightweightCharts.createChart(c2,{layout:{background:{type:'solid',color:'#0a0a0a'},textColor:'#888',fontSize:11},grid:{vertLines:{color:'#141414'},horzLines:{color:'#141414'}},rightPriceScale:{borderColor:'#2a2a2a'},timeScale:{borderColor:'#2a2a2a',timeVisible:true},width:c2.clientWidth,height:400});
    g_candle15m=g_chart15m.addCandlestickSeries({upColor:'#00e676',downColor:'#ff5252',borderUpColor:'#00e676',borderDownColor:'#ff5252',wickUpColor:'#00e676',wickDownColor:'#ff5252'});
    g_candle15m.setData(d15m);
    g_chart15m.timeScale().fitContent();
    new ResizeObserver(function(e){if(e[0]&&g_chart15m)g_chart15m.applyOptions({width:e[0].contentRect.width});}).observe(c2);
  }
}
function switchChart(t){
  document.getElementById('chart1h').style.display=t==='1h'?'block':'none';
  document.getElementById('chart15m').style.display=t==='15m'?'block':'none';
  document.querySelectorAll('.tab').forEach(function(b){b.classList.remove('active');});
  event.target.classList.add('active');
}
initCharts();
</script>""")

    # Method cards
    method_configs = [
        ("1", results[0], C_GREEN, "裸K交易系统"),
        ("2", results[1], C_BLUE, "DD结构入场"),
        ("3", results[2], C_PURPLE, "复杂回调系统"),
    ]

    for idx, (num, r, color, name) in enumerate(method_configs):
        direction = r["direction"]
        dir_badge_class = "badge-up" if direction == "up" else "badge-down" if direction == "down" else "badge-range"
        dir_text = {"up": "做多 ↗", "down": "做空 ↘", "range": "观望 ◇", "unknown": "数据不足"}.get(direction, "--")
        signal = r.get("signal")
        has_signal = signal is not None

        html_parts.append(f'<div class="method-card">')
        html_parts.append(f'<div class="method-header">')
        html_parts.append(f'<div class="method-title">方法 {num} · {name}</div>')
        html_parts.append(f'<div style="display:flex;gap:6px;align-items:center;">')
        if has_signal:
            html_parts.append(f'<span class="method-badge badge-signal">⚡ 信号</span>')
        html_parts.append(f'<span class="method-badge {dir_badge_class}">{dir_text}</span>')
        html_parts.append(f'</div></div>')

        # Trend section
        trend = r.get("trend", {})
        html_parts.append(f'<div class="section"><div class="section-label">趋势判断 (EMA 21/55/144)</div>')
        html_parts.append(f'<div class="section-content">{trend.get("detail", "--")}</div></div>')

        if num == "2":
            # DD structure: show fib levels and trend segment
            trend_seg = r.get("trend")
            fib = r.get("fib")
            if trend_seg:
                html_parts.append(f'<div class="section"><div class="section-label">趋势段</div>')
                html_parts.append(f'<div class="section-content">方向={trend_seg["direction"]} | 起点={trend_seg["start_price"]:.2f} → 终点={trend_seg["end_price"]:.2f}</div></div>')
            if fib:
                html_parts.append(f'<div class="section"><div class="section-label">斐波那契回调</div>')
                html_parts.append(f'<div class="fib-row">')
                for k in ["0%", "23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%"]:
                    v = fib[k]
                    color = "#ff5252" if k == "61.8%" else "#666"
                    html_parts.append(f'<span class="fib-level" style="background:#161616;color:{color};border:1px solid #2a2a2a;">{k}={v:.2f}</span>')
                html_parts.append(f'</div></div>')
            dd = r.get("dd")
            if dd:
                html_parts.append(f'<div class="section"><div class="section-label">DD结构检测</div>')
                html_parts.append(f'<div class="section-content">{dd["detail"]}</div></div>')

        elif num == "3":
            # Complex pullback: FVG, levels, wedge
            fvgs = r.get("fvgs", [])
            if fvgs:
                html_parts.append(f'<div class="section"><div class="section-label">FVG (Fair Value Gap)</div>')
                for fvg in fvgs:
                    html_parts.append(f'<span class="fvg-item">{fvg["type"]} {fvg["lower"]:.2f}-{fvg["upper"]:.2f} @ {fmt_bjt(fvg["dt"])}</span>')
                html_parts.append(f'</div>')
            wedge = r.get("wedge")
            if wedge:
                html_parts.append(f'<div class="section"><div class="section-label">楔形形态</div>')
                html_parts.append(f'<div class="wedge-info">{wedge["detail"]} | 推动力度衰减={"✓" if wedge["weakening"] else "✗"}</div></div>')
            else:
                html_parts.append(f'<div class="section"><div class="section-label">楔形形态</div><div class="no-signal">未检测到楔形</div></div>')

        # Levels
        levels = r.get("levels", [])
        if levels:
            html_parts.append(f'<div class="section"><div class="section-label">关键位 (支撑/压力区间)</div>')
            for lvl in levels[:5]:
                l_type = lvl.get("type", "level")
                html_parts.append(f'<span class="level-item">{l_type}: [{lvl["lower"]:.2f} - {lvl["upper"]:.2f}] ×{lvl.get("touches",1)}</span>')
            html_parts.append(f'</div>')

        # Signal
        if has_signal:
            html_parts.append(f'<div class="signal-box">')
            html_parts.append(f'<div style="color:var(--orange);font-weight:bold;margin-bottom:6px;">⚡ 入场信号 — {signal["direction"]}</div>')
            html_parts.append(f'<div style="font-size:12px;margin-bottom:8px;">{signal["detail"]}</div>')
            html_parts.append(f'<div class="trade-plan">')
            html_parts.append(f'<div class="tp-item"><div class="tp-label">入场</div><div class="tp-value" style="color:var(--green)">{signal["entry"]:.2f}</div></div>')
            html_parts.append(f'<div class="tp-item"><div class="tp-label">止损</div><div class="tp-value" style="color:var(--red)">{signal["stop"]:.2f}</div></div>')
            html_parts.append(f'<div class="tp-item"><div class="tp-label">止盈 (1:2)</div><div class="tp-value" style="color:var(--green)">{signal["target"]:.2f}</div></div>')
            risk = abs(signal["entry"] - signal["stop"])
            reward = abs(signal["target"] - signal["entry"])
            rr = reward / risk if risk > 0 else 0
            html_parts.append(f'<div class="tp-item"><div class="tp-label">盈亏比</div><div class="tp-value" style="color:var(--yellow)">{rr:.1f}R</div></div>')
            html_parts.append(f'</div></div>')
        else:
            no_signal_text = "均线缠绕，观望" if direction == "range" else "关键位附近未出现入场信号" if direction in ("up", "down") else "趋势不明确"
            html_parts.append(f'<div class="signal-box-none"><div class="no-signal">⏸ {no_signal_text}</div></div>')

        html_parts.append(f'</div>')  # close method-card

    # Settings modal
    html_parts.append("""
<div id="settingsModal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);z-index:1000;justify-content:center;align-items:center;">
  <div style="background:#111;border:1px solid #2a2a2a;border-radius:8px;padding:24px;width:400px;">
    <h3 style="color:#ff8800;margin-bottom:16px;">Telegram 通知设置</h3>
    <div style="margin-bottom:12px;"><label style="color:#666;font-size:12px;display:block;margin-bottom:4px;">Bot Token</label><input id="tg_token" style="width:100%;background:#0a0a0a;border:1px solid #2a2a2a;color:#e0e0e0;padding:8px;border-radius:4px;font-family:inherit;" /></div>
    <div style="margin-bottom:12px;"><label style="color:#666;font-size:12px;display:block;margin-bottom:4px;">Chat ID</label><input id="tg_chat" style="width:100%;background:#0a0a0a;border:1px solid #2a2a2a;color:#e0e0e0;padding:8px;border-radius:4px;font-family:inherit;" /></div>
    <div style="margin-bottom:16px;"><label style="color:#666;font-size:12px;display:flex;align-items:center;gap:6px;"><input type="checkbox" id="tg_enabled" /> 启用通知</label></div>
    <div style="display:flex;gap:8px;">
      <button onclick="saveSettings()" style="flex:1;background:#ff8800;color:#0a0a0a;border:none;padding:8px;border-radius:4px;cursor:pointer;font-family:inherit;font-weight:bold;">保存</button>
      <button onclick="testTelegram()" style="flex:1;background:none;border:1px solid #2a2a2a;color:#ff8800;padding:8px;border-radius:4px;cursor:pointer;font-family:inherit;">测试</button>
      <button onclick="document.getElementById('settingsModal').style.display='none'" style="background:none;border:1px solid #2a2a2a;color:#666;padding:8px;border-radius:4px;cursor:pointer;font-family:inherit;">关闭</button>
    </div>
    <div id="tg_result" style="margin-top:12px;font-size:12px;color:#666;"></div>
  </div>
</div>
<script>
function openSettings(){
  fetch('/api/config').then(r=>r.json()).then(d=>{
    document.getElementById('tg_token').value=d.telegram_bot_token||'';
    document.getElementById('tg_chat').value=d.telegram_chat_id||'';
    document.getElementById('tg_enabled').checked=d.telegram_enabled||false;
  }).catch(()=>{});
  document.getElementById('settingsModal').style.display='flex';
}
function saveSettings(){
  var d={bot_token:document.getElementById('tg_token').value,chat_id:document.getElementById('tg_chat').value,enabled:document.getElementById('tg_enabled').checked};
  fetch('/api/telegram',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>r.json()).then(res=>{
    document.getElementById('tg_result').textContent=res.message||'已保存';
  }).catch(e=>{document.getElementById('tg_result').textContent='保存失败:'+e;});
}
function testTelegram(){
  var token=document.getElementById('tg_token').value,chat=document.getElementById('tg_chat').value;
  fetch('/api/telegram/test?token='+encodeURIComponent(token)+'&chat_id='+encodeURIComponent(chat)).then(r=>r.json()).then(res=>{
    document.getElementById('tg_result').textContent=res.ok?'✅ '+res.message:'❌ '+res.error;
  }).catch(e=>{document.getElementById('tg_result').textContent='请求失败:'+e;});
}
</script>""")

    return "\n".join(html_parts)

# ---------------- Chart Data Preparation ----------------

def prepare_chart_data(bars_1h, bars_15m):
    """为前端图表准备 JSON 数据"""
    # 1H candles
    candle_1h = []
    for b in bars_1h[-200:]:
        t = b["ts"]
        candle_1h.append({"time": t, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]})
    # 1H EMAs
    e21_1h = ema_of_bars(bars_1h, "close", 21)
    e55_1h = ema_of_bars(bars_1h, "close", 55)
    e144_1h = ema_of_bars(bars_1h, "close", 144)
    ma1h = {"e21": [], "e55": [], "e144": []}
    for i, b in enumerate(bars_1h[-200:]):
        idx = len(bars_1h) - 200 + i
        if e21_1h[idx] is not None:
            ma1h["e21"].append({"time": b["ts"], "value": e21_1h[idx]})
        if e55_1h[idx] is not None:
            ma1h["e55"].append({"time": b["ts"], "value": e55_1h[idx]})
        if e144_1h[idx] is not None:
            ma1h["e144"].append({"time": b["ts"], "value": e144_1h[idx]})
    # 15m candles
    candle_15m = []
    for b in bars_15m[-300:]:
        candle_15m.append({"time": b["ts"], "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]})
    return candle_1h, ma1h, candle_15m

# ---------------- State (GitHub persistent) ----------------

def load_state() -> Dict[str, Any]:
    """加载状态——Vercel 环境下用 GitHub repo 持久化"""
    if not GITHUB_TOKEN:
        return {}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_STATE_PATH}"
    try:
        r = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            state = json.loads(content)
            state["_gh_sha"] = data["sha"]
            return state
        return {}
    except Exception as e:
        log(f"GitHub state 读取失败: {e}")
        return {}

# ---------------- Telegram Push ----------------

def push_telegram(cfg: Dict, message: str) -> bool:
    token = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token or not chat_id or not cfg.get("telegram_enabled", False):
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        log(f"Telegram push 失败: {e}")
        return False

# ---------------- Main Entry ----------------

def run_engine(manual_override: str = None) -> dict:
    """供 serverless 调用：执行引擎并返回 HTML + result"""
    cfg = load_config()
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("TELEGRAM_ENABLED"):
        cfg["telegram_enabled"] = os.environ["TELEGRAM_ENABLED"].lower() in ("true", "1", "yes")

    # 获取数据
    symbol = cfg.get("symbol_yahoo", "GC=F")
    r_1h = fetch_yahoo(symbol, "3mo", "1h")
    bars_1h = to_bars(r_1h)
    r_15m = fetch_yahoo(symbol, "1mo", "15m")
    bars_15m = to_bars(r_15m)

    current_price = bars_15m[-1]["close"] if bars_15m else (bars_1h[-1]["close"] if bars_1h else 0)

    # 执行三套方法
    m1 = method1_naked_k(bars_1h, bars_15m)
    m2 = method2_dd_structure(bars_1h, bars_15m)
    m3 = method3_complex_pullback(bars_1h, bars_15m)
    results = [m1, m2, m3]

    # 生成 HTML
    html = generate_html(results, bars_1h, bars_15m, current_price)

    # 注入图表数据
    candle_1h, ma1h, candle_15m = prepare_chart_data(bars_1h, bars_15m)
    inject_script = f"""
<script>
window._data1h={json.dumps(candle_1h)};
window._ma1h={json.dumps(ma1h)};
window._data15m={json.dumps(candle_15m)};
</script>
"""
    html = html + inject_script

    # 检查信号并推送 Telegram
    signals_found = []
    for r in results:
        if r.get("signal"):
            signals_found.append(r["signal"])
    push_reason = ""
    if signals_found:
        msg_lines = [f"⚡ XAU/USD 交易信号 @ {now_sh().strftime('%H:%M')} (GMT+8)"]
        msg_lines.append(f"当前价格: {current_price:.2f}")
        msg_lines.append("")
        for s in signals_found:
            msg_lines.append(f"【{s['method']}】{s['direction']}")
            msg_lines.append(f"  类型: {s['type']}")
            msg_lines.append(f"  入场: {s['entry']:.2f}")
            msg_lines.append(f"  止损: {s['stop']:.2f}")
            msg_lines.append(f"  止盈: {s['target']:.2f}")
            msg_lines.append(f"  详情: {s['detail']}")
            msg_lines.append("")
        push_telegram(cfg, "\n".join(msg_lines))
        push_reason = f"发现 {len(signals_found)} 个信号, 已推送 Telegram"
    else:
        push_reason = "无信号"

    return {
        "html": html,
        "result": {
            "price": current_price,
            "signals": signals_found,
        },
        "push_reason": push_reason,
        "in_window": True,
    }
