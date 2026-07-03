#!/usr/bin/env python3
"""
Gold Trading Decision Engine — XAU/USD
规则引擎：获取 GC=F 数据，按 9 条交易规则逐项判断，生成 HTML 报告 + Telegram 推送。
Author: QClaw | 2026-07-01
"""
import os, sys, json, math, datetime as dt
from typing import List, Dict, Any, Optional, Tuple

import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
# Vercel 环境下 state 用内存或外部存储，不写文件
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
REPORT_PATH = os.environ.get("REPORT_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.html"))
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine.log")

# ---------------- Config & State ----------------

def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    # Vercel 环境变量覆盖
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("TELEGRAM_ENABLED"):
        cfg["telegram_enabled"] = os.environ["TELEGRAM_ENABLED"].lower() in ("true", "1", "yes")
    return cfg

def load_state() -> Dict[str, Any]:
    default_state = {
        "last_signal_ts": None, "signals_today": [], "daily_loss_R": 0.0, "trade_date": None,
        "consecutive_loss_dir": None, "consecutive_loss_count": 0,
        "flipped_today": False, "stopped_today": False,
        "pending_flip_check": False,  # 连亏3笔但大盘方向未改, 等待确认
        "trade_results": [],
    }
    if not os.path.exists(STATE_PATH):
        return default_state
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            # 合并默认值
            for k, v in default_state.items():
                if k not in loaded:
                    loaded[k] = v
            return loaded
    except Exception:
        return default_state

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Vercel 只读，忽略

def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------- Data Fetch ----------------

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

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

def atr(bars: List[Dict[str, float]], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l = bars[i]["high"], bars[i]["low"]
        pc = bars[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    # simple moving average of TR
    return sum(trs[-period:]) / period if len(trs) >= period else (sum(trs) / len(trs) if trs else 0.0)

def points_to_R(points: float, atr_val: float, atr_mult: float = 1.6) -> float:
    """把点数换算成 R。1R = 止损距离 = max(1.6*ATR, 结构点距离) 这里用 1.6*ATR 近似"""
    if atr_val <= 0:
        return 0.0
    stop_dist = atr_val * atr_mult
    return points / stop_dist if stop_dist > 0 else 0.0

# ---------------- Structure Detection ----------------

def detect_hh_hl(bars: List[Dict[str, float]]) -> Dict[str, Any]:
    """检测 1h 的 HH/HL (上升趋势) 或 LH/LL (下降趋势)。
    简化：找最近 N 根中的 swing high/low 序列。
    """
    if len(bars) < 5:
        return {"direction": "unknown", "swings": [], "detail": f"数据不足({len(bars)}根, 需≥5)"}

    # 用 fractal 方法：3 根窗口中中间最高/最低
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    swing_highs = []
    swing_lows = []
    w = 1
    for i in range(w, len(bars) - w):
        if highs[i] == max(highs[i-w:i+w+1]):
            swing_highs.append({"i": i, "price": highs[i], "dt": bars[i]["dt"]})
        if lows[i] == min(lows[i-w:i+w+1]):
            swing_lows.append({"i": i, "price": lows[i], "dt": bars[i]["dt"]})

    # 去重连续
    def dedup(swings):
        out = []
        for s in swings:
            if not out or abs(s["price"] - out[-1]["price"]) > 0.1:
                out.append(s)
        return out

    swing_highs = dedup(swing_highs)
    swing_lows = dedup(swing_lows)

    # 判定方向：最近两个 swing high 和两个 swing low
    direction = "range"
    detail = []
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        sh1, sh2 = swing_highs[-2], swing_highs[-1]
        sl1, sl2 = swing_lows[-2], swing_lows[-1]
        hh = sh2["price"] > sh1["price"]
        hl = sl2["price"] > sl1["price"]
        lh = sh2["price"] < sh1["price"]
        ll = sl2["price"] < sl1["price"]
        detail.append(f"SH1={sh1['price']:.1f} SH2={sh2['price']:.1f} {'HH' if hh else 'LH'}")
        detail.append(f"SL1={sl1['price']:.1f} SL2={sl2['price']:.1f} {'HL' if hl else 'LL'}")
        if hh and hl:
            direction = "up"
        elif lh and ll:
            direction = "down"

    return {"direction": direction, "swing_highs": swing_highs[-3:], "swing_lows": swing_lows[-3:], "detail": "; ".join(detail)}

def asian_session_range(bars_1h: List[Dict[str, float]]) -> Dict[str, Any]:
    """取亚洲时段（GMT+8 08:00-14:00，即 UTC 00:00-06:00）的 high/low。
    返回亚盘区间 high/low，用于后续突破方向判断。
    """
    asian_bars = [b for b in bars_1h if 0 <= b["dt"].hour < 6]
    today = dt.datetime.utcnow().date()
    asian_today = [b for b in asian_bars if b["dt"].date() == today]
    if not asian_today:
        # 用最近一天
        if asian_bars:
            last_date = asian_bars[-1]["dt"].date()
            asian_today = [b for b in asian_bars if b["dt"].date() == last_date]
    if not asian_today:
        return {"high": None, "low": None, "range": 0, "bars": 0, "detail": "无亚盘数据", "open": None, "close": None}
    h = max(b["high"] for b in asian_today)
    l = min(b["low"] for b in asian_today)
    o = asian_today[0]["open"]
    c = asian_today[-1]["close"]
    return {"high": h, "low": l, "range": h - l, "bars": len(asian_today),
            "open": o, "close": c,
            "detail": f"亚盘 {len(asian_today)} 根 H={h:.1f} L={l:.1f} Range={h-l:.1f} (O={o:.1f} C={c:.1f})"}

def day_move_points(bars_1h: List[Dict[str, float]]) -> Dict[str, Any]:
    """从亚盘开始到欧盘早段（欧盘开盘后半小时）的幅度，并判断方向（向上/向下/震荡）。
    
    时间窗口 (UTC):
      - 亚盘: 00:00-06:00 (GMT+8 08:00-14:00)
      - 欧盘早段: 07:00-07:30 (GMT+8 15:00-15:30)
      - R4 判断窗口: 00:00 - 07:30 UTC
    
    判定逻辑：
      - 只取 UTC 00:00 到 07:30 之间的 K线（欧盘开盘后半小时内）
      - 开盘价 = 窗口内第一根 K线的 open
      - 当前价 = 窗口内最后一根 K线的 close
      - 幅度 = 窗口内 high - low
      - 方向: 当前价 vs 开盘价，偏差超过幅度 1/3 判定为有方向，否则震荡
    """
    today = dt.datetime.utcnow().date()
    # R4 判断窗口：UTC 00:00 - 07:30
    # 1h K线: 取 hour < 7 的全部 + hour==7 的（如果有的话，1h K线 hour=7 覆盖 07:00-08:00）
    # 更精确做法：取 hour < 8 的所有今日 K线（即 UTC 00:00-08:00），
    # 但只用到 07:30 的数据。由于 1h K线粒度，hour=7 这根代表 07:00-08:00，
    # 在欧盘开盘后半小时这个时点，这根 K线还在形成中，但我们可以用它作为参考。
    # 简化：取 UTC hour 0-7 的今日 K线
    window_bars = [b for b in bars_1h if b["dt"].date() == today and b["dt"].hour <= 7]
    if not window_bars:
        # 数据不足，回退到今日全部 K线
        window_bars = [b for b in bars_1h if b["dt"].date() == today]
    if not window_bars:
        return {"points": 0, "detail": "今日无 1h 数据", "direction": "unknown", "open": None, "last": None, "high": None, "low": None}
    day_high = max(b["high"] for b in window_bars)
    day_low = min(b["low"] for b in window_bars)
    day_open = window_bars[0]["open"]
    day_last = window_bars[-1]["close"]
    points = day_high - day_low
    # 方向判定：当前价相对开盘价的偏移 vs 幅度
    offset = day_last - day_open
    if points > 0:
        ratio = abs(offset) / points
    else:
        ratio = 0
    if ratio < 0.33:
        move_dir = "震荡"
    elif offset > 0:
        move_dir = "向上"
    else:
        move_dir = "向下"
    window_end = "07:30 UTC (15:30 GMT+8)"
    return {
        "points": points, "high": day_high, "low": day_low,
        "open": day_open, "last": day_last, "offset": offset, "direction": move_dir,
        "detail": f"窗口: UTC 00:00-{window_end} | H={day_high:.1f} L={day_low:.1f} 幅度={points:.1f}点 | O={day_open:.1f} C={day_last:.1f} 偏移={offset:+.1f} → 方向={move_dir}"
    }

def detect_5min_signal(bars_5m: List[Dict[str, float]], direction: str, atr_5m: float) -> Dict[str, Any]:
    """检测 5min 入场信号 H1/H2。
    H2 (做多): 连续两根更高的 high + 第二根收盘在第一根 high 附近或以上，且低点不破前低
    L2 (做空): 连续两根更低的 low + 第二根收盘在第一根 low 附近或以下，且高点不破前高
    H1 (做多): 单根吞没/强阳线
    L1 (做空): 单根吞没/强阴线
    """
    if len(bars_5m) < 3:
        return {"signal": "none", "type": None, "detail": "5min 数据不足"}

    last3 = bars_5m[-3:]
    last2 = bars_5m[-2:]
    c1, c2 = last2[0], last2[1]

    if direction in ("up", "range"):
        # H2
        if c2["high"] > c1["high"] and c2["low"] >= c1["low"] and c2["close"] > c1["close"]:
            return {"signal": "H2", "type": "long", "detail": f"H2 做多信号: 两根连续抬高，C2 close={c2['close']:.1f} > C1 close={c1['close']:.1f}"}
        # H1
        body = c2["close"] - c2["open"]
        rng = c2["high"] - c2["low"]
        if body > 0 and rng > 0 and body / rng > 0.6 and c2["close"] > c1["high"]:
            return {"signal": "H1", "type": "long", "detail": f"H1 做多信号: 强阳线 close={c2['close']:.1f}"}

    if direction in ("down", "range"):
        # L2
        if c2["low"] < c1["low"] and c2["high"] <= c1["high"] and c2["close"] < c1["close"]:
            return {"signal": "L2", "type": "short", "detail": f"L2 做空信号: 两根连续降低，C2 close={c2['close']:.1f} < C1 close={c1['close']:.1f}"}
        # L1
        body = c2["open"] - c2["close"]
        rng = c2["high"] - c2["low"]
        if body > 0 and rng > 0 and body / rng > 0.6 and c2["close"] < c1["low"]:
            return {"signal": "L1", "type": "short", "detail": f"L1 做空信号: 强阴线 close={c2['close']:.1f}"}

    return {"signal": "none", "type": None, "detail": "无 H1/H2/L1/L2 信号"}

def scan_signal_history(bars_5m: List[Dict[str, float]], bars_1h: List[Dict[str, float]], atr_5m: float, max_signals: int = 50) -> List[Dict[str, Any]]:
    """扫描 5min K线历史，检测所有 H1/H2/L1/L2 信号点，并计算事后盈亏。
    每个信号记录: time, signal_type, direction(long/short), entry_price, stop_price, target_price,
    之后追踪到止损或止盈，记录结果(win/loss/ongoing)和盈亏点数。
    """
    signals = []
    if len(bars_5m) < 5:
        return signals
    
    # 用 1h 方向作为全局方向参考
    struct_1h = detect_hh_hl(bars_1h)
    global_dir = struct_1h.get("direction", "range")
    
    for i in range(2, len(bars_5m) - 1):
        c1 = bars_5m[i - 1]
        c2 = bars_5m[i]
        detected = None
        
        # 尝试做多信号 (up 或 range 方向)
        if global_dir in ("up", "range"):
            # H2
            if c2["high"] > c1["high"] and c2["low"] >= c1["low"] and c2["close"] > c1["close"]:
                detected = {"signal": "H2", "type": "long", "entry": c2["close"],
                           "reason": f"连续两根抬高: H2({c2['high']:.1f})>H1({c1['high']:.1f}), L2({c2['low']:.1f})≥L1({c1['low']:.1f}), Close({c2['close']:.1f})>前Close({c1['close']:.1f})"}
            # H1
            else:
                body = c2["close"] - c2["open"]
                rng = c2["high"] - c2["low"]
                if body > 0 and rng > 0 and body / rng > 0.6 and c2["close"] > c1["high"]:
                    detected = {"signal": "H1", "type": "long", "entry": c2["close"],
                               "reason": f"强阳线吞没: 实体占比{body/rng*100:.0f}%, Close({c2['close']:.1f})>前High({c1['high']:.1f})"}
        
        # 尝试做空信号 (down 或 range 方向)
        if not detected and global_dir in ("down", "range"):
            # L2
            if c2["low"] < c1["low"] and c2["high"] <= c1["high"] and c2["close"] < c1["close"]:
                detected = {"signal": "L2", "type": "short", "entry": c2["close"],
                           "reason": f"连续两根降低: L2({c2['low']:.1f})<L1({c1['low']:.1f}), H2({c2['high']:.1f})≤H1({c1['high']:.1f}), Close({c2['close']:.1f})<前Close({c1['close']:.1f})"}
            # L1
            else:
                body = c2["open"] - c2["close"]
                rng = c2["high"] - c2["low"]
                if body > 0 and rng > 0 and body / rng > 0.6 and c2["close"] < c1["low"]:
                    detected = {"signal": "L1", "type": "short", "entry": c2["close"],
                               "reason": f"强阴线吞没: 实体占比{body/rng*100:.0f}%, Close({c2['close']:.1f})<前Low({c1['low']:.1f})"}
        
        if not detected:
            continue
        
        # 计算止损止盈
        stop_dist = atr_5m * 1.6
        if detected["type"] == "long":
            stop = detected["entry"] - stop_dist
            target = detected["entry"] + stop_dist * 1.5  # 震荡日 1.5R
        else:
            stop = detected["entry"] + stop_dist
            target = detected["entry"] - stop_dist * 1.5
        
        # 追踪后续 bars 判断结果
        result_status = "ongoing"
        exit_price = None
        exit_time = None
        pnl_points = 0.0
        bars_after = 0
        
        for j in range(i + 1, len(bars_5m)):
            bars_after += 1
            bar = bars_5m[j]
            # 最多追踪 60 根 5min K线 (5小时)
            if bars_after > 60:
                break
            
            if detected["type"] == "long":
                # 止损优先
                if bar["low"] <= stop:
                    result_status = "loss"
                    exit_price = stop
                    exit_time = bar["dt"].strftime("%H:%M")
                    pnl_points = stop - detected["entry"]
                    break
                if bar["high"] >= target:
                    result_status = "win"
                    exit_price = target
                    exit_time = bar["dt"].strftime("%H:%M")
                    pnl_points = target - detected["entry"]
                    break
            else:
                if bar["high"] >= stop:
                    result_status = "loss"
                    exit_price = stop
                    exit_time = bar["dt"].strftime("%H:%M")
                    pnl_points = detected["entry"] - stop
                    break
                if bar["low"] <= target:
                    result_status = "win"
                    exit_price = target
                    exit_time = bar["dt"].strftime("%H:%M")
                    pnl_points = detected["entry"] - target
                    break
        
        # 如果还没结束，用最后一根 bar 的 close 作为当前浮动盈亏
        if result_status == "ongoing" and bars_after > 0:
            last_bar = bars_5m[-1]
            if detected["type"] == "long":
                pnl_points = last_bar["close"] - detected["entry"]
            else:
                pnl_points = detected["entry"] - last_bar["close"]
            exit_price = last_bar["close"]
            exit_time = "--"
        
        signals.append({
            "time": c2["dt"].strftime("%m-%d %H:%M"),
            "ts": c2["ts"],
            "signal": detected["signal"],
            "type": detected["type"],
            "reason": detected.get("reason", ""),
            "global_dir": global_dir,
            "entry": detected["entry"],
            "stop": stop,
            "target": target,
            "result": result_status,
            "exit": exit_price,
            "exit_time": exit_time,
            "pnl": pnl_points,
            "bars_after": bars_after,
        })
    
    # 去重: 同方向同类型 5 根 K线内只保留第一个
    deduped = []
    last_sig_key = None
    last_sig_idx = -10
    for i, s in enumerate(signals):
        key = f"{s['type']}_{s['signal']}"
        if key == last_sig_key and (i - last_sig_idx) < 5:
            continue
        deduped.append(s)
        last_sig_key = key
        last_sig_idx = i
    
    # === 连亏反手模拟 (基于大盘方向确认) ===
    # 规则: 同方向连亏3笔 → 检查当前信号的大盘方向(global_dir)是否已改变:
    #   - 大盘方向已改变 → 跟随新方向 (标记为flipped)
    #   - 大盘方向未改变 → 不反手, 继续原方向 (标记为pending)
    # 反手后再亏2笔 → 当天停止交易
    sim_consec_dir = None  # 当前连亏方向 (long/short)
    sim_consec_count = 0
    sim_flipped = False
    sim_stopped = False
    sim_stop_date = None
    sim_pending = False  # 连亏3笔但大盘方向未改
    
    for s in deduped:
        trade_date = s["time"][:5]  # MM-DD
        
        # 新的一天重置状态
        if sim_stop_date and trade_date != sim_stop_date:
            sim_stopped = False
            sim_stop_date = None
            sim_consec_dir = None
            sim_consec_count = 0
            sim_flipped = False
            sim_pending = False
        
        if sim_stopped:
            s["sim_action"] = "skipped_stopped"
            s["sim_note"] = "当日已停止交易"
            continue
        
        # 判断是否触发反手检查
        should_flip = False
        flip_blocked = False  # 连亏3笔但方向未改
        
        if sim_consec_count >= 3 and sim_consec_dir and not sim_flipped:
            # 连亏3笔 → 检查大盘方向是否已改变
            expected_flip_dir = "down" if sim_consec_dir == "long" else "up"
            signal_market_dir = s.get("global_dir", "")  # up/down/range
            if signal_market_dir == expected_flip_dir:
                # 大盘方向已改变 → 反手
                should_flip = True
                sim_flipped = True
                sim_consec_count = 0
                sim_consec_dir = None
                sim_pending = False
            else:
                # 大盘方向未改变 → 不反手, 标记 pending
                flip_blocked = True
                sim_pending = True
        elif sim_consec_count >= 2 and sim_flipped:
            # 反手后连亏2笔 → 停止
            sim_stopped = True
            sim_stop_date = trade_date
            s["sim_action"] = "skipped_stopped"
            s["sim_note"] = f"反手后连亏{sim_consec_count}笔→停止"
            continue
        
        if should_flip:
            s["sim_action"] = "flipped"
            s["sim_note"] = f"连亏3笔+大盘方向已转→反手"
        elif flip_blocked:
            s["sim_action"] = "skipped_pending"
            market_text = {'up': '↑', 'down': '↓', 'range': '→'}.get(s.get("global_dir", ""), s.get("global_dir", ""))
            s["sim_note"] = f"连亏{sim_consec_count}笔, 大盘={market_text}未改→不开仓等待"
            # pending 状态下不开仓, 但仍需追踪信号结果以判断连亏是否应该重置
            # 如果信号结果为win, 说明方向对了, 重置连亏; 如果loss, 连亏继续累加
            if s["result"] == "loss":
                if sim_consec_dir == s["type"]:
                    sim_consec_count += 1
                else:
                    sim_consec_dir = s["type"]
                    sim_consec_count = 1
            elif s["result"] == "win":
                sim_consec_dir = None
                sim_consec_count = 0
                sim_pending = False
            continue
        else:
            s["sim_action"] = "normal"
            s["sim_note"] = ""
        
        # 更新连亏计数
        if s["result"] == "loss":
            if sim_consec_dir == s["type"]:
                sim_consec_count += 1
            else:
                sim_consec_dir = s["type"]
                sim_consec_count = 1
        elif s["result"] == "win":
            sim_consec_dir = None
            sim_consec_count = 0
            sim_pending = False
        # ongoing 不影响连亏计数
    
    return deduped[-max_signals:]


def key_levels(bars_5m: List[Dict[str, float]], bars_1h: List[Dict[str, float]]) -> Dict[str, Any]:
    """找关键支撑/阻力位"""
    # 取最近 1h 的 swing high/low 作为阻力/支撑
    struct = detect_hh_hl(bars_1h)
    resistance = max((s["price"] for s in struct.get("swing_highs", [])), default=None)
    support = min((s["price"] for s in struct.get("swing_lows", [])), default=None)
    # 5min 最近 20 根高低点
    recent_5m = bars_5m[-20:] if len(bars_5m) >= 20 else bars_5m
    r5m = max(b["high"] for b in recent_5m)
    s5m = min(b["low"] for b in recent_5m)
    return {
        "resistance_1h": resistance,
        "support_1h": support,
        "resistance_5m": r5m,
        "support_5m": s5m,
        "detail": f"1H R={resistance:.1f} S={support:.1f} | 5M R={r5m:.1f} S={s5m:.1f}" if resistance and support else "关键位计算中"
    }

# ---------------- Rule Engine ----------------

def now_sh() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

def is_trading_window(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    now = now_sh()
    start = dt.datetime.strptime(cfg["trading_window_start"], "%H:%M").time()
    end = dt.datetime.strptime(cfg["trading_window_end"], "%H:%M").time()
    t = now.time()
    if start <= t <= end:
        return True, f"交易时段内 ({now.strftime('%H:%M')} GMT+8)"
    return False, f"不在交易时段 ({now.strftime('%H:%M')} GMT+8, 窗口 {cfg['trading_window_start']}-{cfg['trading_window_end']})"

def is_news_blackout() -> Tuple[bool, str]:
    """简化版：检查今日是否有重大数据（可手动维护日历或接入 API）。
    这里先留接口，默认不屏蔽。"""
    return False, "今日无已知重大数据发布（需手动维护经济日历）"

def check_rules(cfg: Dict, bars_1h: List, bars_5m: List) -> Dict[str, Any]:
    now = now_sh()
    checks = {}

    # Rule 2: 交易时段
    in_window, window_msg = is_trading_window(cfg)
    checks["r2_trading_window"] = {"label": "R2 交易时段 (15:00-23:30 GMT+8)", "pass": in_window, "detail": window_msg}

    # Rule 2b: 新闻屏蔽
    news_block, news_msg = is_news_blackout()
    checks["r2b_news"] = {"label": "R2b 新闻数据屏蔽期", "pass": not news_block, "detail": news_msg}

    # Rule 2c: 23:30 后不持仓
    after_close = now_sh().time() > dt.time(23, 30)
    checks["r2c_after_hours"] = {"label": "R2c 23:30 后不入场", "pass": not after_close, "detail": "23:30 后不操作" if after_close else "在可操作时段内"}

    # Rule 3: 1h 方向
    struct_1h = detect_hh_hl(bars_1h)
    direction = struct_1h["direction"]
    dir_map = {"up": "只做多", "down": "只做空", "range": "震荡 - 方向不明"}
    checks["r3_direction"] = {
        "label": "R3 1h 方向 (HH/HL 结构)",
        "pass": direction in ("up", "down"),
        "detail": f"方向={dir_map.get(direction, direction)} | {', '.join(struct_1h.get('detail', []))}" if isinstance(struct_1h.get('detail'), list) else f"方向={dir_map.get(direction, direction)} | {struct_1h.get('detail','')}"
    }

    # Rule 4: 模式判断 (趋势日 / 震荡日)
    # 三个子条件取交集，判断窗口 = 亚盘到欧盘早段 (UTC 00:00-07:30 = GMT+8 08:00-15:30)
    #   4a 幅度够 (窗口内 ≥50 点) + 方向 (向上/向下/震荡)
    #   4b 单向结构 (窗口内 HH/HL 向上 或 LL/LH 向下)
    #   4c 突破亚盘区间 (向上突破 / 向下突破)
    move = day_move_points(bars_1h)
    asian = asian_session_range(bars_1h)

    # R4b: 窗口内的单向结构检测
    today = dt.datetime.utcnow().date()
    window_bars_1h = [b for b in bars_1h if b["dt"].date() == today and b["dt"].hour <= 7]
    if not window_bars_1h:
        window_bars_1h = [b for b in bars_1h if b["dt"].date() == today]
    struct_window = detect_hh_hl(window_bars_1h)
    window_direction = struct_window["direction"]

    # 4a 幅度+方向
    amplitude_ok = move["points"] >= cfg["trend_day_min_points"]
    move_dir = move.get("direction", "unknown")
    amplitude_pass = amplitude_ok and move_dir in ("向上", "向下")
    r4a_detail = f"幅度={move['points']:.0f}点 ({'>=50 ✓' if amplitude_ok else '<50 ✗'}) | 方向={move_dir} {'✓' if move_dir in ('向上','向下') else '✗'} | {move['detail']}"

    # 4b 单向结构 (基于窗口内的 1h K线)
    structure_type = "无"
    if window_direction == "up":
        structure_type = "HH/HL (向上)"
    elif window_direction == "down":
        structure_type = "LL/LH (向下)"
    else:
        structure_type = "无明显单向结构"
    structure_ok = window_direction in ("up", "down")
    struct_detail_parts = struct_window.get("detail", "")
    if isinstance(struct_detail_parts, list):
        struct_detail_str = ", ".join(struct_detail_parts)
    else:
        struct_detail_str = struct_detail_parts
    r4b_detail = f"结构={structure_type} {'✓' if structure_ok else '✗'} | {struct_detail_str}"

    # 4c 突破亚盘区间 — 用 R4a 窗口 H/L 作为区间基准，最新实时价判断是否突破
    # 逻辑:
    #   - 区间上沿 = move["high"] (R4a 窗口内最高)
    #   - 区间下沿 = move["low"]  (R4a 窗口内最低)
    #   - 当前价 > 上沿 → 向上突破
    #   - 当前价 < 下沿 → 向下突破
    #   - 突破后回到中间 / 从未突破 → 震荡
    breakout_dir = "无"
    breakout_ok = False
    current_price = bars_5m[-1]["close"] if bars_5m else (bars_1h[-1]["close"] if bars_1h else None)
    range_high = move.get("high")
    range_low = move.get("low")
    if range_high and range_low and current_price:
        if current_price > range_high:
            breakout_dir = "向上突破"
            breakout_ok = True
        elif current_price < range_low:
            breakout_dir = "向下突破"
            breakout_ok = True
        else:
            # 在区间内 — 检查是否曾经突破过又回到中间
            # 用5m K线检查窗口后的最高/最低是否突破过区间
            today_utc = dt.datetime.utcnow().date()
            post_window_bars = [b for b in bars_5m if b["dt"].date() == today_utc and b["dt"].hour >= 7]
            if post_window_bars:
                post_high = max(b["high"] for b in post_window_bars)
                post_low = min(b["low"] for b in post_window_bars)
                broke_up = post_high > range_high
                broke_down = post_low < range_low
                if broke_up and not broke_down:
                    breakout_dir = "曾向上突破后回落 (震荡)"
                elif broke_down and not broke_up:
                    breakout_dir = "曾向下突破后回升 (震荡)"
                elif broke_up and broke_down:
                    breakout_dir = "双向突破后回归 (宽幅震荡)"
                else:
                    breakout_dir = "未突破 (震荡)"
            else:
                breakout_dir = "区间内 (震荡)"
            breakout_ok = False
    r4c_detail = f"突破={breakout_dir} {'✓' if breakout_ok else '✗'} | 区间 H={range_high:.1f} L={range_low:.1f} | 当前价={current_price:.1f}"

    # 三者交集
    is_trend_day = amplitude_pass and structure_ok and breakout_ok
    day_mode = "趋势日" if is_trend_day else "震荡日"

    # 趋势方向一致性检查（幅度方向、结构方向、突破方向是否一致）
    trend_dir = "unknown"
    if is_trend_day:
        dirs = []
        if move_dir in ("向上", "向下"):
            dirs.append(move_dir)
        if window_direction == "up":
            dirs.append("向上")
        elif window_direction == "down":
            dirs.append("向下")
        if breakout_dir == "向上突破":
            dirs.append("向上")
        elif breakout_dir == "向下突破":
            dirs.append("向下")
        if len(set(dirs)) == 1 and len(dirs) == 3:
            trend_dir = dirs[0]
        else:
            trend_dir = "不一致"
            is_trend_day = False
            day_mode = "震荡日 (三因素方向不一致)"

    checks["r4a_amplitude"] = {
        "label": "R4a 幅度够 + 方向 (亚盘→欧盘早段 ≥50点, 向上/向下)",
        "pass": amplitude_pass,
        "detail": r4a_detail
    }
    checks["r4b_structure"] = {
        "label": "R4b 单向结构 (窗口内 HH/HL 向上 或 LL/LH 向下)",
        "pass": structure_ok,
        "detail": r4b_detail
    }
    checks["r4c_breakout"] = {
        "label": "R4c 突破亚盘区间 (窗口内向上/向下突破)",
        "pass": breakout_ok,
        "detail": r4c_detail
    }
    checks["r4_day_mode"] = {
        "label": f"R4 当日模式 = {day_mode}" + (f" (方向: {trend_dir})" if is_trend_day else ""),
        "pass": True,  # 模式本身不是 pass/fail，只是分类
        "detail": f"三条件交集: {'全部满足 → 趋势日' if is_trend_day else '至少一项不满足 → 震荡日'} | 幅度方向={move_dir} | 结构={structure_type} | 突破={breakout_dir}" + (f" | 趋势方向一致={trend_dir}" if is_trend_day else ""),
        "is_trend_day": is_trend_day,
        "trend_dir": trend_dir if is_trend_day else None
    }

    # === 加载状态 (提前到 R5 之前，因为 R5 需要连亏状态) ===
    state = load_state()
    today_str = now.strftime("%Y-%m-%d")
    if state.get("trade_date") != today_str:
        state["trade_date"] = today_str
        state["signals_today"] = []
        state["daily_loss_R"] = 0.0
        state["consecutive_loss_dir"] = None
        state["consecutive_loss_count"] = 0
        state["flipped_today"] = False
        state["stopped_today"] = False
        state["pending_flip_check"] = False

    # Rule 5: 入场信号
    # 趋势日用 R4 的 trend_dir (三因素一致方向)，震荡日用 R3 的 direction
    effective_dir = trend_dir if (is_trend_day and trend_dir in ("向上", "向下")) else direction
    effective_dir_en = "up" if effective_dir == "向上" else "down" if effective_dir == "向下" else effective_dir
    atr_5m = atr(bars_5m, cfg["atr_period"])
    
    # === 连续亏损反手机制 (基于大盘方向确认) ===
    # 规则: 同方向连亏3笔 → 检查1H大盘方向是否已改变:
    #   - 大盘方向已改变 → 跟随新方向做单 (反手)
    #   - 大盘方向未改变 → 不反手, 保持原方向 (但记录待反手状态)
    # 反手后再亏2笔 → 当天停止交易
    consec_loss_dir = state.get("consecutive_loss_dir")  # long / short
    consec_loss_count = state.get("consecutive_loss_count", 0)
    flipped_today = state.get("flipped_today", False)
    stopped_today = state.get("stopped_today", False)
    pending_flip_check = state.get("pending_flip_check", False)  # 连亏3笔但方向未改, 待确认
    
    direction_override = None
    direction_override_reason = ""
    
    # 将连亏方向转为 en: long→做多的亏损方向是 long, 反手目标取决于大盘方向
    loss_dir_en = "up" if consec_loss_dir == "long" else "down" if consec_loss_dir == "short" else None
    # 大盘当前方向
    market_dir = direction  # R3 的 1H 方向: up/down/range
    
    if stopped_today:
        direction_override = "none"
        direction_override_reason = f"今日已停止交易 (反手后又连亏2笔)，等待用户指令恢复"
    elif consec_loss_count >= 3 and consec_loss_dir and not flipped_today:
        # 同方向连亏3笔 → 检查大盘方向是否已改变
        expected_flip_dir = "down" if consec_loss_dir == "long" else "up"  # 期望的反手方向
        if market_dir == expected_flip_dir:
            # 大盘方向已改变 → 跟随新方向反手
            direction_override = expected_flip_dir
            direction_override_reason = f"{consec_loss_dir}方向连亏{consec_loss_count}笔 + 大盘1H方向已转为{'向上' if expected_flip_dir == 'up' else '向下'} → 反手为{expected_flip_dir}方向"
            state["flipped_today"] = True
            state["pending_flip_check"] = False
        else:
            # 大盘方向未改变 → 不开仓, 等待大盘方向改变
            state["pending_flip_check"] = True
            market_dir_text = {'up': '向上', 'down': '向下', 'range': '震荡'}.get(market_dir, market_dir)
            direction_override = "none"
            direction_override_reason = f"{consec_loss_dir}方向连亏{consec_loss_count}笔, 大盘1H方向={market_dir_text}未改变 → 不开仓, 等待大盘方向确认"
    elif flipped_today and consec_loss_count >= 2:
        # 反手后又亏2笔 → 停止
        direction_override = "none"
        direction_override_reason = f"反手后{consec_loss_dir}方向再连亏{consec_loss_count}笔 → 今日停止交易"
        stopped_today = True
        state["stopped_today"] = True
    
    # 应用方向覆盖
    if direction_override == "none":
        effective_dir_en = "none"
        sig = {"signal": "none", "type": None, "detail": direction_override_reason}
        signal_ok = False
        sig_label = "停止交易"
    elif direction_override:
        effective_dir_en = direction_override
        sig = detect_5min_signal(bars_5m, effective_dir_en, atr_5m)
        if is_trend_day:
            needed = ["H2"] if effective_dir_en == "up" else ["L2"]
        else:
            needed = ["H1"] if effective_dir_en in ("up", "range") else ["L1"]
        signal_ok = sig["signal"] in needed
        sig_label = f"反手{'做多' if effective_dir_en == 'up' else '做空'} (大盘方向确认) | " + ("趋势日" if is_trend_day else "震荡日")
    else:
        sig = detect_5min_signal(bars_5m, effective_dir_en, atr_5m)
        if is_trend_day:
            needed = ["H2"] if effective_dir_en == "up" else ["L2"]
        else:
            needed = ["H1"] if effective_dir_en in ("up", "range") else ["L1"]
        signal_ok = sig["signal"] in needed
        sig_label = "趋势日需 H2/L2" if is_trend_day else "震荡日需 H1/L1"
    
    levels = key_levels(bars_5m, bars_1h)
    
    checks["r5_entry_signal"] = {
        "label": f"R5 入场信号 ({sig_label})",
        "pass": signal_ok,
        "detail": f"{sig['detail']} | 关键位: {levels['detail']}" + (f" | ⚠ {direction_override_reason}" if direction_override_reason else "")
    }

    # Rule 6: 止损
    # 止损距离: 取 1.6*ATR 和 结构低/高点距离 两者中更宽的
    # 方向用 effective_dir_en (R4趋势方向 或 R3方向)
    entry_now = bars_5m[-1]["close"] if bars_5m else 0
    stop_atr_dist = atr_5m * cfg["atr_multiplier_stop"] if atr_5m > 0 else 0  # 距离值
    stop_struct_price = None  # 绝对价格
    stop_struct_dist = 0  # 距离值
    if effective_dir_en == "up" and struct_1h.get("swing_lows"):
        stop_struct_price = struct_1h["swing_lows"][-1]["price"] - 1
        stop_struct_dist = max(0, entry_now - stop_struct_price)
    elif effective_dir_en == "down" and struct_1h.get("swing_highs"):
        stop_struct_price = struct_1h["swing_highs"][-1]["price"] + 1
        stop_struct_dist = max(0, stop_struct_price - entry_now)
    # 取更宽者作为止损距离
    stop_dist = max(stop_atr_dist, stop_struct_dist) if (stop_atr_dist and stop_struct_dist) else (stop_atr_dist or stop_struct_dist or 0)
    # 计算止损绝对价格
    if effective_dir_en == "up":
        stop_price_calc = entry_now - stop_dist if stop_dist else None
    elif effective_dir_en == "down":
        stop_price_calc = entry_now + stop_dist if stop_dist else None
    else:
        stop_price_calc = None
    stop_price_calc_str = f"{stop_price_calc:.2f}" if stop_price_calc else "N/A"
    checks["r6_stop_loss"] = {
        "label": "R6 止损 (1.6×ATR 或结构高低点取宽者)",
        "pass": stop_dist > 0,
        "detail": f"ATR(5m,14)={atr_5m:.2f} → 1.6×ATR={stop_atr_dist:.2f}点 | 结构止损位={stop_struct_price} (距离={stop_struct_dist:.2f}) | 取宽者: 距离={stop_dist:.2f} → 止损价={stop_price_calc_str}"
    }

    # Rule 7: 止盈
    if is_trend_day:
        target_min = cfg["trend_target_R_min"]
        target_max = cfg["trend_target_R_max"]
    else:
        target_min = cfg["range_target_R_min"]
        target_max = cfg["range_target_R_max"]
    checks["r7_take_profit"] = {
        "label": f"R7 止盈目标 ({'趋势' if is_trend_day else '震荡'} {target_min}R-{target_max}R)",
        "pass": True,
        "detail": f"目标 {target_min}R-{target_max}R | 到 1R 后改 ATR 移动止损跟踪"
    }

    # Rule 8: 资金风控
    daily_loss = state.get("daily_loss_R", 0.0)
    trades_today = len(state.get("signals_today", []))
    stopped_today = state.get("stopped_today", False)
    consec_loss_dir = state.get("consecutive_loss_dir")
    consec_loss_count = state.get("consecutive_loss_count", 0)
    flipped_today = state.get("flipped_today", False)
    
    risk_ok = (not stopped_today) and daily_loss < cfg["daily_max_loss_R"] and trades_today < cfg["max_trades_per_day"]
    
    consec_detail = ""
    if stopped_today:
        consec_detail = " | ⚠ 今日已停止交易 (反手后又亏2笔)"
    elif flipped_today and consec_loss_count > 0:
        consec_detail = f" | ⚠ 已反手, 反手后{consec_loss_dir}方向连亏{consec_loss_count}笔"
    elif consec_loss_count >= 3 and state.get("pending_flip_check"):
        market_dir_text = {'up': '↑', 'down': '↓', 'range': '→'}.get(direction, direction)
        consec_detail = f" | ⚠ {consec_loss_dir}连亏{consec_loss_count}笔, 待大盘方向确认 (当前{market_dir_text})"
    elif consec_loss_count >= 3:
        consec_detail = f" | ⚠ {consec_loss_dir}方向连亏{consec_loss_count}笔, 触发反手检查"
    elif consec_loss_count > 0:
        consec_detail = f" | {consec_loss_dir}方向连亏{consec_loss_count}笔"
    
    checks["r8_risk"] = {
        "label": f"R8 当日风控 (已亏 {daily_loss:.2f}R / 上限 {cfg['daily_max_loss_R']}R, 已交易 {trades_today}/{cfg['max_trades_per_day']} 笔){consec_detail}",
        "pass": risk_ok,
        "detail": f"{'风控未打满, 可继续' if risk_ok else '风控已打满或已停止, 停止交易'}"
    }

    # Rule 9: re-entry (美盘不开新仓)
    us_session = now_sh().time() >= dt.time(21, 30)  # 美盘开盘约 21:30 GMT+8
    if us_session and not state.get("signals_today"):
        reentry_ok = False
        reentry_msg = "美盘时段且无持仓 - 不开新仓 (期望值为负)"
    elif us_session and state.get("signals_today"):
        reentry_ok = True
        reentry_msg = "美盘时段但有持仓 - 用美盘行情打止盈或反方向离场"
    else:
        reentry_ok = True
        reentry_msg = "非美盘时段 - re-entry 限制不适用"
    checks["r9_reentry"] = {
        "label": "R9 美盘 re-entry 限制",
        "pass": reentry_ok,
        "detail": reentry_msg
    }

    # 综合判断
    gating_keys = ["r2_trading_window", "r2b_news", "r2c_after_hours", "r3_direction", "r5_entry_signal", "r8_risk", "r9_reentry"]
    all_pass = all(checks[k]["pass"] for k in gating_keys)

    # 计算入场价与止损止盈
    entry_price = bars_5m[-1]["close"] if bars_5m else None
    stop_price = stop_price_calc
    r_dist = stop_dist
    if effective_dir_en == "up" and r_dist > 0 and entry_price:
        target_price = entry_price + r_dist * target_max
    elif effective_dir_en == "down" and r_dist > 0 and entry_price:
        target_price = entry_price - r_dist * target_max
    else:
        target_price = None

    return {
        "checks": checks,
        "all_pass": all_pass,
        "direction": direction,
        "effective_dir": effective_dir_en,
        "trend_dir": trend_dir if is_trend_day else None,
        "day_mode": day_mode,
        "is_trend_day": is_trend_day,
        "signal": sig,
        "levels": levels,
        "atr_5m": atr_5m,
        "stop_distance": stop_dist,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "target_R": f"{target_min}R-{target_max}R",
        "now": now.strftime("%Y-%m-%d %H:%M:%S GMT+8"),
        "state": state,
    }

# ---------------- HTML Report (Bloomberg Terminal Style) ----------------

def generate_html(result: Dict[str, Any], bars_1h, bars_5m, signal_history=None) -> str:
    checks = result["checks"]
    last_price = bars_5m[-1]["close"] if bars_5m else 0
    prev_price = bars_5m[-2]["close"] if len(bars_5m) >= 2 else last_price
    chg = last_price - prev_price
    chg_pct = (chg / prev_price * 100) if prev_price else 0
    up_color = "#00e676"
    down_color = "#ff5252"
    warn_color = "#ffab40"
    dim_color = "#666"

    # 分组规则
    groups = {
        "时段": ["r2_trading_window", "r2b_news", "r2c_after_hours"],
        "方向": ["r3_direction"],
        "模式": ["r4a_amplitude", "r4b_structure", "r4c_breakout", "r4_day_mode"],
        "信号": ["r5_entry_signal", "r6_stop_loss", "r7_take_profit"],
        "风控": ["r8_risk", "r9_reentry"],
    }

    # 统计通过/失败
    failed_keys = [k for k, v in checks.items() if not v["pass"]]
    failed_count = len(failed_keys)
    total_count = len(checks)
    pass_count = total_count - failed_count

    # 决策信号灯
    eff_dir = result.get("effective_dir", result["direction"])
    is_trend = result.get("is_trend_day", False)
    day_mode = result["day_mode"]
    signal_name = result["signal"]["signal"]

    if result["all_pass"]:
        verdict_color = up_color
        verdict_bg = "rgba(0,230,118,0.08)"
        verdict_text = f"▶ EXECUTE {'LONG' if eff_dir == 'up' else 'SHORT'}"
        verdict_sub = f"{signal_name} | {day_mode} | {'趋势方向 ' + result.get('trend_dir','')}" if is_trend else f"{signal_name} | {day_mode}"
    elif pass_count > 0:
        verdict_color = warn_color
        verdict_bg = "rgba(255,171,64,0.08)"
        verdict_text = f"◷ WAITING ({failed_count} 条件未满足)"
        verdict_sub = ", ".join(failed_keys[:4])
    else:
        verdict_color = down_color
        verdict_bg = "rgba(255,82,82,0.08)"
        verdict_text = "✕ NO TRADE"
        verdict_sub = "核心条件不满足"

    # 方向标签
    dir_color = up_color if eff_dir == "up" else down_color if eff_dir == "down" else dim_color
    dir_text = {"up": "LONG ↑", "down": "SHORT ↓", "range": "NEUTRAL ◇"}.get(eff_dir, "--")

    # 交易计划
    entry = result.get("entry_price")
    stop = result.get("stop_price")
    target = result.get("target_price")
    stop_dist = result.get("stop_distance", 0)
    entry_str = f"{entry:.2f}" if entry else "--"
    stop_str = f"{stop:.2f}" if stop else "--"
    target_str = f"{target:.2f}" if target else "--"

    # RR 计算
    if entry and stop and target and stop_dist > 0:
        target_dist = abs(target - entry)
        rr_ratio = target_dist / stop_dist if stop_dist > 0 else 0
        rr_str = f"{rr_ratio:.1f}R"
    else:
        rr_str = "--"

    # 1h 方向结构
    r3 = checks.get("r3_direction", {})
    r3_detail = r3.get("detail", "")
    r3_pass = r3.get("pass", False)

    # R4 三条件可视化
    r4a = checks.get("r4a_amplitude", {})
    r4b = checks.get("r4b_structure", {})
    r4c = checks.get("r4c_breakout", {})
    r4a_pass = r4a.get("pass", False)
    r4b_pass = r4b.get("pass", False)
    r4c_pass = r4c.get("pass", False)
    r4 = checks.get("r4_day_mode", {})
    is_trend_day = r4.get("is_trend_day", False)

    # 从 detail 提取信息
    move = result.get("_move", {})
    
    # 风控状态
    r8 = checks.get("r8_risk", {})
    r9 = checks.get("r9_reentry", {})
    state = result.get("state", {})
    daily_loss = state.get("daily_loss_R", 0)
    trades_today = len(state.get("signals_today", []))

    # 构建分组表格
    group_labels = {
        "时段": ("⏰", "SESSION"),
        "方向": ("🎯", "DIRECTION"),
        "模式": ("📊", "DAY MODE"),
        "信号": ("⚡", "SIGNAL"),
        "风控": ("🛡", "RISK"),
    }

    group_html_parts = []
    for gkey, keys in groups.items():
        icon, label = group_labels[gkey]
        g_passed = sum(1 for k in keys if checks.get(k, {}).get("pass", False))
        g_total = len(keys)
        g_color = up_color if g_passed == g_total else (warn_color if g_passed > 0 else down_color)
        
        rows_html = ""
        for k in keys:
            v = checks.get(k, {})
            if not v:
                continue
            status_icon = "✅" if v["pass"] else "❌"
            row_opacity = "" if not v["pass"] else ""
            # R4 总结行特殊处理
            if k == "r4_day_mode":
                if is_trend_day:
                    mode_badge = f"<span style='color:{up_color};font-weight:700'>趋势日</span>"
                else:
                    mode_badge = f"<span style='color:{warn_color};font-weight:700'>震荡日</span>"
                rows_html += f"""
                <tr class="summary-row">
                    <td colspan="3" style="padding:8px 12px;border-top:1px solid #2a2a2a">
                        <span style="color:{g_color}">{status_icon}</span>
                        <span style="color:#aaa;margin-left:6px">当日模式:</span>
                        {mode_badge}
                        <span style="color:#666;margin-left:8px;font-size:11px">{v['detail']}</span>
                    </td>
                </tr>"""
                continue
            
            detail_short = v["detail"]
            # 截断过长的 detail
            if len(detail_short) > 200:
                detail_short = detail_short[:200] + "..."
            
            rows_html += f"""
            <tr>
                <td class="rule-label">{status_icon} {v['label']}</td>
                <td class="rule-detail">{detail_short}</td>
            </tr>"""
        
        group_html_parts.append(f"""
        <div class="rule-group">
            <div class="group-header" style="border-left:3px solid {g_color}">
                <span class="group-icon">{icon}</span>
                <span class="group-title">{label}</span>
                <span class="group-count" style="color:{g_color}">{g_passed}/{g_total}</span>
            </div>
            <table class="rule-table">
                {rows_html}
            </table>
        </div>""")

    groups_html = "\n".join(group_html_parts)

    # R4 三条件交集可视化
    r4_cells = []
    for label, passed, detail in [
        ("幅度≥50点", r4a_pass, r4a.get("detail", "")),
        ("单向结构", r4b_pass, r4b.get("detail", "")),
        ("突破亚盘", r4c_pass, r4c.get("detail", "")),
    ]:
        cell_color = up_color if passed else down_color
        bg = f"rgba(0,230,118,0.06)" if passed else "rgba(255,82,82,0.06)"
        icon = "✓" if passed else "✗"
        # 提取关键信息
        short = detail.split("|")[0].strip() if detail else ""
        r4_cells.append(f"""
        <div class="r4-cell" style="border-color:{cell_color};background:{bg}">
            <div class="r4-icon" style="color:{cell_color}">{icon}</div>
            <div class="r4-label">{label}</div>
            <div class="r4-info">{short}</div>
        </div>""")
    r4_cells_html = "\n".join(r4_cells)
    r4_result_color = up_color if is_trend_day else warn_color
    r4_result_text = "趋势日 TREND DAY" if is_trend_day else "震荡日 RANGE DAY"

    # === 准备 TradingView Lightweight Charts 数据 ===
    # 5min K线数据 (全部)
    chart_bars_5m = []
    for b in bars_5m:
        chart_bars_5m.append({
            "time": b["ts"],
            "open": round(b["open"], 2),
            "high": round(b["high"], 2),
            "low": round(b["low"], 2),
            "close": round(b["close"], 2),
        })
    
    # 1H K线数据 (全部)
    chart_bars_1h = []
    for b in bars_1h:
        chart_bars_1h.append({
            "time": b["ts"],
            "open": round(b["open"], 2),
            "high": round(b["high"], 2),
            "low": round(b["low"], 2),
            "close": round(b["close"], 2),
        })
    
    # 信号 markers
    chart_markers = []
    if signal_history:
        for s in signal_history:
            is_long = s["type"] == "long"
            color = up_color if is_long else down_color
            arrow = "arrowUp" if is_long else "arrowDown"
            position = "belowBar" if is_long else "aboveBar"
            result_icon = "✓" if s["result"] == "win" else ("✗" if s["result"] == "loss" else "…")
            label = f"{s['signal']} {result_icon}"
            chart_markers.append({
                "time": s["ts"],
                "position": position,
                "color": color,
                "shape": arrow,
                "text": label,
                "entry": s["entry"],
                "result": s["result"],
                "pnl": round(s["pnl"], 2),
            })
    
    # 信号历史表
    sig_history_json = json.dumps(signal_history or [], ensure_ascii=False)
    chart_bars_5m_json = json.dumps(chart_bars_5m)
    chart_bars_1h_json = json.dumps(chart_bars_1h)
    chart_markers_json = json.dumps(chart_markers)
    
    # 统计胜率
    if signal_history:
        completed = [s for s in signal_history if s["result"] in ("win", "loss")]
        wins = sum(1 for s in completed if s["result"] == "win")
        losses = len(completed) - wins
        winrate = (wins / len(completed) * 100) if completed else 0
        total_pnl = sum(s["pnl"] for s in completed)
        stats_text = f"{wins}W / {losses}L | 胜率 {winrate:.0f}% | 总盈亏 {total_pnl:+.1f}点 | {len(signal_history)} 信号"
    else:
        stats_text = "暂无历史信号"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="300">
<title>XAU/USD Trading Terminal</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{
  --bg: #0a0a0a; --panel: #111; --border: #1e1e1e; --border2: #2a2a2a;
  --text: #e0e0e0; --dim: #666; --dim2: #444;
  --orange: #ff8800; --green: #00e676; --red: #ff5252; --yellow: #ffab40;
  --mono: 'SF Mono',Menlo,Consolas,'Courier New',monospace;
}}
body {{ background:var(--bg); color:var(--text); font-family:var(--mono); font-size:12px; padding:12px; line-height:1.5; }}

/* === Top Bar === */
.topbar {{ display:flex; align-items:center; gap:16px; padding:10px 14px; background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:10px; }}
.topbar .sym {{ color:var(--orange); font-size:14px; font-weight:700; letter-spacing:1px; }}
.topbar .price {{ color:#fff; font-size:26px; font-weight:700; letter-spacing:-0.5px; }}
.topbar .chg {{ font-size:13px; font-weight:600; }}
.topbar .spacer {{ flex:1; }}
.topbar .clock {{ color:var(--dim); font-size:11px; }}
.topbar .session-dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:4px; }}

/* === Verdict Bar === */
.verdict {{ display:flex; align-items:center; gap:16px; padding:14px 18px; background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:10px; border-left:4px solid {verdict_color}; background:{verdict_bg}; }}
.verdict .main {{ font-size:18px; font-weight:700; color:{verdict_color}; letter-spacing:1px; }}
.verdict .sub {{ color:var(--dim); font-size:11px; margin-top:2px; }}
.verdict .dir-badge {{ padding:4px 12px; border-radius:3px; font-size:14px; font-weight:700; color:{dir_color}; border:1px solid {dir_color}; background:rgba(255,255,255,0.03); }}
.verdict .progress {{ margin-left:auto; text-align:right; }}
.verdict .progress .num {{ font-size:20px; font-weight:700; color:{verdict_color}; }}
.verdict .progress .label {{ font-size:10px; color:var(--dim); text-transform:uppercase; }}

/* === KPI Strip === */
.kpi-strip {{ display:grid; grid-template-columns:repeat(6,1fr); gap:8px; margin-bottom:10px; }}
.kpi {{ background:var(--panel); border:1px solid var(--border); border-radius:5px; padding:8px 10px; }}
.kpi .k {{ color:var(--dim); font-size:9px; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:2px; }}
.kpi .v {{ color:var(--orange); font-size:15px; font-weight:700; }}
.kpi .v.small {{ font-size:12px; }}

/* === Trade Plan === */
.trade-plan {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin-bottom:12px; }}
.tp-card {{ background:var(--panel); border:1px solid var(--border); border-radius:5px; padding:10px 12px; text-align:center; }}
.tp-card .k {{ color:var(--dim); font-size:9px; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; }}
.tp-card .v {{ color:#fff; font-size:18px; font-weight:700; }}
.tp-card.entry {{ border-top:2px solid var(--orange); }}
.tp-card.stop {{ border-top:2px solid var(--red); }}
.tp-card.target {{ border-top:2px solid var(--green); }}
.tp-card.rr {{ border-top:2px solid var(--yellow); }}
.tp-card.signal {{ border-top:2px solid var(--dim2); }}
.tp-card.signal .v {{ color:var(--dim); }}
.tp-card.signal.active .v {{ color:var(--green); }}

/* === R4 Intersection === */
.r4-section {{ background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:12px; overflow:hidden; }}
.r4-header {{ padding:8px 14px; background:#161616; border-bottom:1px solid var(--border2); display:flex; align-items:center; gap:8px; }}
.r4-header .title {{ color:var(--orange); font-size:11px; font-weight:700; letter-spacing:1px; }}
.r4-header .result {{ margin-left:auto; font-size:11px; font-weight:700; color:{r4_result_color}; padding:2px 10px; border:1px solid {r4_result_color}; border-radius:3px; }}
.r4-cells {{ display:grid; grid-template-columns:repeat(3,1fr); gap:0; }}
.r4-cell {{ padding:12px 14px; border-right:1px solid var(--border2); }}
.r4-cell:last-child {{ border-right:none; }}
.r4-cell .r4-icon {{ font-size:20px; font-weight:700; float:left; margin-right:8px; line-height:1; }}
.r4-cell .r4-label {{ color:#aaa; font-size:11px; font-weight:600; margin-bottom:4px; }}
.r4-cell .r4-info {{ color:var(--dim); font-size:10px; line-height:1.4; overflow:hidden; }}
.r4-intersect {{ padding:6px 14px; background:#0d0d0d; border-top:1px solid var(--border2); color:var(--dim); font-size:10px; text-align:center; }}
.r4-intersect .arrow {{ color:var(--orange); margin:0 6px; }}

/* === Rule Groups === */
.rule-groups {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px; }}
.rule-group {{ background:var(--panel); border:1px solid var(--border); border-radius:6px; overflow:hidden; }}
.group-header {{ padding:7px 12px; background:#161616; display:flex; align-items:center; gap:6px; border-bottom:1px solid var(--border2); }}
.group-icon {{ font-size:12px; }}
.group-title {{ color:var(--orange); font-size:10px; font-weight:700; letter-spacing:1px; }}
.group-count {{ margin-left:auto; font-size:11px; font-weight:700; }}
.rule-table {{ width:100%; border-collapse:collapse; }}
.rule-table td {{ padding:5px 12px; border-bottom:1px solid var(--border); vertical-align:top; font-size:11px; }}
.rule-table tr:last-child td {{ border-bottom:none; }}
.rule-table .rule-label {{ color:#bbb; width:45%; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.rule-table .rule-detail {{ color:var(--dim); font-size:10px; }}
.rule-table tr.summary-row td {{ background:#0d0d0d; }}

/* === Footer === */
.footer {{ padding:8px 14px; color:var(--dim2); font-size:10px; border-top:1px solid var(--border); display:flex; gap:16px; }}
.footer .tag {{ color:var(--dim); }}

/* === Chart Section === */
.chart-section {{ background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:12px; overflow:hidden; }}
.chart-header {{ padding:8px 14px; background:#161616; border-bottom:1px solid var(--border2); display:flex; align-items:center; gap:8px; }}
.chart-header .title {{ color:var(--orange); font-size:11px; font-weight:700; letter-spacing:1px; }}
.chart-header .stats {{ margin-left:auto; font-size:10px; color:var(--dim); }}
.chart-legend {{ padding:6px 14px; background:#0d0d0d; border-top:1px solid var(--border2); font-size:10px; }}

/* === Timeframe Buttons === */
.tf-buttons {{ display:flex; gap:4px; margin-left:12px; }}
.tf-btn {{ background:#1a1a1a; border:1px solid var(--border2); color:var(--dim); font-size:10px; font-weight:700; padding:3px 10px; border-radius:3px; cursor:pointer; letter-spacing:0.5px; transition:all 0.15s; }}
.tf-btn:hover {{ border-color:var(--orange); color:var(--orange); }}
.tf-btn.active {{ background:var(--orange); color:#000; border-color:var(--orange); }}

/* === Signal History Table === */
.signal-history-section {{ background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:12px; overflow:hidden; }}
.signal-table-wrap {{ max-height:300px; overflow-y:auto; }}
.signal-table {{ width:100%; border-collapse:collapse; font-size:11px; }}
.signal-table th {{ position:sticky; top:0; background:#161616; color:var(--dim); font-size:9px; text-transform:uppercase; letter-spacing:0.5px; padding:6px 8px; text-align:left; border-bottom:1px solid var(--border2); }}
.signal-table td {{ padding:5px 8px; border-bottom:1px solid var(--border); color:#ccc; }}
.signal-table tr:hover td {{ background:rgba(255,136,0,0.04); }}
.signal-table .win {{ color:var(--green); font-weight:700; }}
.signal-table .loss {{ color:var(--red); font-weight:700; }}
.signal-table .ongoing {{ color:var(--yellow); }}
.signal-table .long-tag {{ color:var(--green); }}
.signal-table .short-tag {{ color:var(--red); }}
.signal-table .pnl-pos {{ color:var(--green); }}
.signal-table .pnl-neg {{ color:var(--red); }}

/* === Signal Analysis === */
.signal-analysis-section {{ background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:12px; overflow:hidden; }}
.analysis-body {{ padding:14px; font-size:11px; line-height:1.8; color:#aaa; }}
.analysis-body h4 {{ color:var(--orange); font-size:11px; margin:0 0 6px 0; letter-spacing:1px; }}
.analysis-body .metric-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:8px; margin-bottom:12px; }}
.analysis-body .metric {{ background:#0d0d0d; border:1px solid var(--border2); border-radius:4px; padding:8px 10px; }}
.analysis-body .metric .label {{ font-size:9px; color:var(--dim2); text-transform:uppercase; letter-spacing:0.5px; }}
.analysis-body .metric .value {{ font-size:16px; font-weight:700; color:#ddd; margin-top:2px; }}
.analysis-body .metric .value.pos {{ color:var(--green); }}
.analysis-body .metric .value.neg {{ color:var(--red); }}
.analysis-body .sub-section {{ margin-bottom:12px; }}
.analysis-body .tag {{ display:inline-block; background:#1a1a1a; border:1px solid var(--border2); border-radius:3px; padding:2px 6px; font-size:10px; margin:2px; color:var(--dim); }}
.analysis-body .tag.win-tag {{ border-color:rgba(0,230,118,0.3); color:var(--green); }}
.analysis-body .tag.loss-tag {{ border-color:rgba(255,82,82,0.3); color:var(--red); }}
.analysis-body ul {{ margin:4px 0 4px 16px; padding:0; }}
.analysis-body li {{ margin:2px 0; }}
.analysis-body .suggestion {{ background:rgba(255,136,0,0.06); border:1px solid rgba(255,136,0,0.2); border-radius:4px; padding:8px 10px; margin-top:8px; }}
.analysis-body .suggestion .label {{ color:var(--orange); font-weight:700; }}

/* === Settings Gear === */
.gear-btn {{ background:none; border:1px solid var(--border2); color:var(--dim); padding:4px 8px; border-radius:4px; cursor:pointer; font-size:14px; font-family:var(--mono); transition:all 0.2s; }}
.gear-btn:hover {{ border-color:var(--orange); color:var(--orange); }}

/* === Modal === */
.modal-overlay {{ display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:1000; justify-content:center; align-items:center; }}
.modal-overlay.show {{ display:flex; }}
.modal {{ background:var(--panel); border:1px solid var(--border2); border-radius:8px; padding:0; width:420px; max-width:90vw; box-shadow:0 8px 32px rgba(0,0,0,0.6); }}
.modal-header {{ padding:12px 16px; border-bottom:1px solid var(--border2); display:flex; align-items:center; gap:8px; }}
.modal-header .title {{ color:var(--orange); font-size:13px; font-weight:700; letter-spacing:1px; }}
.modal-header .close {{ margin-left:auto; background:none; border:none; color:var(--dim); font-size:18px; cursor:pointer; padding:0 4px; }}
.modal-header .close:hover {{ color:var(--red); }}
.modal-body {{ padding:16px; }}
.modal-body .field {{ margin-bottom:14px; }}
.modal-body .field label {{ display:block; color:var(--dim); font-size:10px; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; }}
.modal-body .field input {{ width:100%; background:#0d0d0d; border:1px solid var(--border2); border-radius:4px; padding:8px 10px; color:var(--text); font-family:var(--mono); font-size:12px; }}
.modal-body .field input:focus {{ outline:none; border-color:var(--orange); }}
.modal-body .toggle-row {{ display:flex; align-items:center; gap:8px; margin-bottom:14px; }}
.modal-body .toggle-row label {{ color:var(--text); font-size:12px; }}
.modal-body .toggle {{ position:relative; width:40px; height:20px; background:#333; border-radius:10px; cursor:pointer; transition:background 0.2s; }}
.modal-body .toggle.on {{ background:var(--green); }}
.modal-body .toggle::after {{ content:''; position:absolute; top:2px; left:2px; width:16px; height:16px; border-radius:50%; background:#fff; transition:left 0.2s; }}
.modal-body .toggle.on::after {{ left:22px; }}
.modal-body .status-msg {{ font-size:11px; padding:6px 10px; border-radius:4px; margin-bottom:10px; display:none; }}
.modal-body .status-msg.show {{ display:block; }}
.modal-body .status-msg.ok {{ background:rgba(0,230,118,0.1); color:var(--green); border:1px solid rgba(0,230,118,0.3); }}
.modal-body .status-msg.err {{ background:rgba(255,82,82,0.1); color:var(--red); border:1px solid rgba(255,82,82,0.3); }}
.modal-footer {{ padding:10px 16px; border-top:1px solid var(--border2); display:flex; gap:8px; justify-content:flex-end; }}
.modal-footer button {{ padding:6px 14px; border-radius:4px; font-family:var(--mono); font-size:11px; cursor:pointer; border:1px solid var(--border2); background:none; color:var(--text); }}
.modal-footer button:hover {{ border-color:var(--orange); }}
.modal-footer .btn-primary {{ background:var(--orange); color:#000; border-color:var(--orange); font-weight:700; }}
.modal-footer .btn-primary:hover {{ opacity:0.85; }}
.modal-footer .btn-test {{ border-color:var(--border2); color:var(--dim); }}
.modal-footer .btn-test:hover {{ border-color:var(--green); color:var(--green); }}
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
    <span class="sym">XAU/USD</span>
    <span class="price">{last_price:.2f}</span>
    <span class="chg" style="color:{up_color if chg>=0 else down_color}">{chg:+.2f} ({chg_pct:+.2f}%)</span>
    <span class="spacer"></span>
    <span class="clock">
        <span class="session-dot" style="background:{up_color if checks.get('r2_trading_window',{}).get('pass') else down_color}"></span>
        {result['now']}
    </span>
    <button class="gear-btn" onclick="openSettings()" title="Telegram 通知设置">⚙</button>
</div>

<!-- Verdict Bar -->
<div class="verdict">
    <div>
        <div class="main">{verdict_text}</div>
        <div class="sub">{verdict_sub}</div>
    </div>
    <div class="dir-badge">{dir_text}</div>
    <div class="progress">
        <div class="num">{pass_count}/{total_count}</div>
        <div class="label">Rules Passed</div>
    </div>
</div>

<!-- KPI Strip -->
<div class="kpi-strip">
    <div class="kpi"><div class="k">1H Direction</div><div class="v">{result['direction'].upper()}</div></div>
    <div class="kpi"><div class="k">Day Mode</div><div class="v small">{day_mode}</div></div>
    <div class="kpi"><div class="k">ATR(5m,14)</div><div class="v">{result['atr_5m']:.2f}</div></div>
    <div class="kpi"><div class="k">Stop Dist</div><div class="v">{result['stop_distance']:.2f}</div></div>
    <div class="kpi"><div class="k">Target R</div><div class="v small">{result['target_R']}</div></div>
    <div class="kpi"><div class="k">Daily P&L</div><div class="v" style="color:{up_color if daily_loss==0 else down_color}">{daily_loss:.1f}R / 2.0R</div></div>
</div>

<!-- Trade Plan -->
<div class="trade-plan">
    <div class="tp-card entry"><div class="k">Entry</div><div class="v">{entry_str}</div></div>
    <div class="tp-card stop"><div class="k">Stop Loss</div><div class="v">{stop_str}</div></div>
    <div class="tp-card target"><div class="k">Target</div><div class="v">{target_str}</div></div>
    <div class="tp-card rr"><div class="k">R:R Ratio</div><div class="v">{rr_str}</div></div>
    <div class="tp-card signal {'active' if signal_name != 'none' else ''}"><div class="k">Signal</div><div class="v">{signal_name.upper()}</div></div>
</div>

<!-- R4 Intersection -->
<div class="r4-section">
    <div class="r4-header">
        <span class="title">R4 DAY MODE — 三条件交集</span>
        <span class="result">{r4_result_text}</span>
    </div>
    <div class="r4-cells">
        {r4_cells_html}
    </div>
    <div class="r4-intersect">
        幅度方向 <span class="arrow">∩</span> 结构方向 <span class="arrow">∩</span> 突破方向 <span class="arrow">=</span>
        <strong style="color:{r4_result_color}">{r4_result_text}</strong>
        <span style="margin-left:8px;color:var(--dim2)">不一致则降级为震荡日, 排除宽幅震荡误判</span>
    </div>
</div>

<!-- Rule Groups -->
<div class="rule-groups">
    {groups_html}
</div>

<!-- TradingView Chart + Signal History -->
<div class="chart-section">
    <div class="chart-header">
        <span class="title">📈 K线图 + 信号标记</span>
        <div class="tf-buttons">
            <button class="tf-btn active" data-tf="5m" onclick="switchTf('5m')">5min</button>
            <button class="tf-btn" data-tf="1h" onclick="switchTf('1h')">1H</button>
        </div>
        <span class="stats" id="chartStats">{stats_text}</span>
    </div>
    <div id="tradingChart" style="width:100%;height:500px;background:#0a0a0a;"></div>
    <div class="chart-legend">
        <span style="color:{up_color}">▲ 做多 (H1/H2)</span>
        <span style="color:{down_color};margin-left:16px">▼ 做空 (L1/L2)</span>
        <span style="color:var(--dim);margin-left:16px">✓止盈 ✗止损 …进行中</span>
        <span style="color:var(--dim);margin-left:16px">鼠标拖拽平移 · 滚轮缩放</span>
    </div>
</div>

<div class="signal-history-section">
    <div class="chart-header">
        <span class="title">📋 信号历史记录</span>
        <span class="stats">事后验证盈亏</span>
    </div>
    <div class="signal-table-wrap">
        <table class="signal-table" id="signalTable">
            <thead>
                <tr>
                    <th>时间</th>
                    <th>信号</th>
                    <th>方向</th>
                    <th>触发逻辑</th>
                    <th>入场价</th>
                    <th>止损</th>
                    <th>目标</th>
                    <th>结果</th>
                    <th>出场价</th>
                    <th>出场时间</th>
                    <th>盈亏(点)</th>
                    <th>K线数</th>
                    <th>策略动作</th>
                </tr>
            </thead>
            <tbody id="signalTableBody"></tbody>
        </table>
    </div>
</div>

<!-- Signal Analysis -->
<div class="signal-analysis-section">
    <div class="chart-header">
        <span class="title">📊 信号历史整体分析</span>
        <span class="stats" id="analysisStats">自动生成</span>
    </div>
    <div id="signalAnalysis" class="analysis-body"></div>
</div>

<!-- Footer -->
<div class="footer">
    <span class="tag">Data: Yahoo Finance (GC=F)</span>
    <span class="tag">Auto-refresh: 300s · 手动刷新即时取最新数据</span>
    <span class="tag">Session: 15:00-23:30 GMT+8</span>
    <span style="margin-left:auto;color:var(--dim2)">顺势交易 · R计量风险 · 个人可执行性优先</span>
</div>

<!-- Settings Modal -->
<div class="modal-overlay" id="settingsModal" onclick="if(event.target===this)closeSettings()">
    <div class="modal">
        <div class="modal-header">
            <span class="title">⚙ TELEGRAM 通知设置</span>
            <button class="close" onclick="closeSettings()">×</button>
        </div>
        <div class="modal-body">
            <div class="status-msg" id="tgStatus"></div>
            <div class="toggle-row">
                <div class="toggle" id="tgToggle" onclick="toggleTg()"></div>
                <label>启用 Telegram 推送</label>
            </div>
            <div class="field">
                <label>Bot Token</label>
                <input type="text" id="tgToken" placeholder="1234567890:ABCdefGHI..." autocomplete="off">
            </div>
            <div class="field">
                <label>Chat ID</label>
                <input type="text" id="tgChatId" placeholder="你的 Telegram Chat ID" autocomplete="off">
            </div>
        </div>
        <div class="modal-footer">
            <button class="btn-test" onclick="testTelegram()">📡 测试连接</button>
            <button class="btn-primary" onclick="saveTelegram()">💾 保存</button>
        </div>
    </div>
</div>

__SCRIPT_PLACEHOLDER__

</body>
</html>"""
    # JS 部分包含大量花括号，不能放在 f-string 中
    js_code = '''<script>
let tgEnabled = false;

async function loadSettings() {
    try {
        const r = await fetch('/config');
        const cfg = await r.json();
        document.getElementById('tgToken').value = cfg.telegram_bot_token || '';
        document.getElementById('tgChatId').value = cfg.telegram_chat_id || '';
        tgEnabled = !!cfg.telegram_enabled;
        updateToggle();
    } catch(e) { console.error('loadSettings error', e); }
}

function updateToggle() {
    const el = document.getElementById('tgToggle');
    if (tgEnabled) el.classList.add('on'); else el.classList.remove('on');
}

function toggleTg() { tgEnabled = !tgEnabled; updateToggle(); }

function openSettings() {
    loadSettings();
    document.getElementById('settingsModal').classList.add('show');
    document.getElementById('tgStatus').classList.remove('show');
}

function closeSettings() {
    document.getElementById('settingsModal').classList.remove('show');
}

function showStatus(msg, isOk) {
    const el = document.getElementById('tgStatus');
    el.textContent = msg;
    el.className = 'status-msg show ' + (isOk ? 'ok' : 'err');
}

async function testTelegram() {
    const token = document.getElementById('tgToken').value.trim();
    const chatId = document.getElementById('tgChatId').value.trim();
    if (!token || !chatId) { showStatus('请先填写 Bot Token 和 Chat ID', false); return; }
    showStatus('正在测试...', false);
    try {
        const r = await fetch(`/api/telegram/test?token=${encodeURIComponent(token)}&chat_id=${encodeURIComponent(chatId)}`);
        const data = await r.json();
        if (data.ok) showStatus('✅ ' + data.message, true);
        else showStatus('❌ ' + data.error, false);
    } catch(e) { showStatus('❌ ' + e.message, false); }
}

async function saveTelegram() {
    const token = document.getElementById('tgToken').value.trim();
    const chatId = document.getElementById('tgChatId').value.trim();
    try {
        const r = await fetch('/api/telegram', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ bot_token: token, chat_id: chatId, enabled: tgEnabled })
        });
        const data = await r.json();
        if (data.ok) {
            showStatus('✅ ' + data.message, true);
            setTimeout(closeSettings, 1200);
        } else {
            showStatus('❌ ' + data.error, false);
        }
    } catch(e) { showStatus('❌ ' + e.message, false); }
}

// === Lightweight Charts ===
const CHART_BARS_5M = ''' + chart_bars_5m_json + ''';
const CHART_BARS_1H = ''' + chart_bars_1h_json + ''';
const CHART_MARKERS = ''' + chart_markers_json + ''';
const SIG_HISTORY = ''' + sig_history_json + ''';

let g_chart = null;
let g_series = null;
let g_currentTf = '5m';

function loadChart() {
    const container = document.getElementById('tradingChart');
    if (!container || typeof LightweightCharts === 'undefined') return;
    
    g_chart = LightweightCharts.createChart(container, {
        layout: {
            background: { type: 'solid', color: '#0a0a0a' },
            textColor: '#888',
            fontSize: 11,
        },
        grid: {
            vertLines: { color: '#141414' },
            horzLines: { color: '#141414' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: '#ff8800', labelBackgroundColor: '#ff8800', width: 1, style: LightweightCharts.LineStyle.Dashed },
            horzLine: { color: '#ff8800', labelBackgroundColor: '#ff8800', width: 1, style: LightweightCharts.LineStyle.Dashed },
        },
        rightPriceScale: {
            borderColor: '#2a2a2a',
            scaleMargins: { top: 0.08, bottom: 0.08 },
        },
        timeScale: {
            borderColor: '#2a2a2a',
            timeVisible: true,
            secondsVisible: false,
            rightOffset: 5,
            barSpacing: 6,
        },
        width: container.clientWidth,
        height: 500,
    });
    
    applyTfData('5m');
    
    // 响应式
    new ResizeObserver(entries => {
        if (entries[0] && g_chart) {
            g_chart.applyOptions({ width: entries[0].contentRect.width });
        }
    }).observe(container);
}

function applyTfData(tf) {
    if (!g_chart) return;
    g_currentTf = tf;
    
    // 移除旧 series
    if (g_series) {
        g_chart.removeSeries(g_series);
        g_series = null;
    }
    
    const data = tf === '5m' ? CHART_BARS_5M : CHART_BARS_1H;
    
    g_series = g_chart.addCandlestickSeries({
        upColor: '#00e676',
        downColor: '#ff5252',
        borderUpColor: '#00e676',
        borderDownColor: '#ff5252',
        wickUpColor: '#00e676',
        wickDownColor: '#ff5252',
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    
    g_series.setData(data);
    
    // 信号 markers — 只在 5min 图上显示
    if (tf === '5m' && CHART_MARKERS.length > 0) {
        g_series.setMarkers(CHART_MARKERS.map(m => ({
            time: m.time,
            position: m.position,
            color: m.color,
            shape: m.shape,
            text: m.text,
        })));
    }
    
    g_chart.timeScale().fitContent();
    
    // 更新统计
    updateChartStats(tf);
}

function switchTf(tf) {
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tf-btn[data-tf="' + tf + '"]').classList.add('active');
    applyTfData(tf);
}

function updateChartStats(tf) {
    const el = document.getElementById('chartStats');
    if (!el) return;
    const data = tf === '5m' ? CHART_BARS_5M : CHART_BARS_1H;
    const count = data.length;
    const lastBar = data[data.length - 1];
    const firstBar = data[0];
    el.textContent = `${tf.toUpperCase()} · ${count} 根K线 · ${firstBar ? new Date(firstBar.time * 1000).toLocaleDateString() : ''} → ${lastBar ? new Date(lastBar.time * 1000).toLocaleDateString() : ''}`;
}

function renderSignalTable() {
    const tbody = document.getElementById('signalTableBody');
    if (!tbody) return;
    
    if (!SIG_HISTORY || SIG_HISTORY.length === 0) {
        tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;color:var(--dim);padding:20px">暂无历史信号记录</td></tr>';
        return;
    }
    
    tbody.innerHTML = SIG_HISTORY.slice().reverse().map(s => {
        const resultClass = s.result === 'win' ? 'win' : s.result === 'loss' ? 'loss' : 'ongoing';
        const resultText = s.result === 'win' ? '✅ 止盈' : s.result === 'loss' ? '❌ 止损' : '⏳ 进行中';
        const dirClass = s.type === 'long' ? 'long-tag' : 'short-tag';
        const dirText = s.type === 'long' ? '做多' : '做空';
        const pnlClass = s.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        const reasonText = s.reason || '--';
        const globalDirText = s.global_dir === 'up' ? '↑UP' : s.global_dir === 'down' ? '↓DOWN' : 'RANGE';
        // 策略动作
        let actionText = '--';
        let actionClass = '';
        if (s.sim_action === 'flipped') {
            actionText = '🔄 反手';
            actionClass = 'style="color:var(--orange);font-weight:700"';
        } else if (s.sim_action === 'skipped_stopped') {
            actionText = '⛔ 跳过';
            actionClass = 'style="color:var(--red);font-weight:700"';
        } else if (s.sim_action === 'skipped_pending') {
            actionText = '⏸ 不开仓';
            actionClass = 'style="color:var(--yellow);font-weight:700"';
        } else if (s.sim_action === 'pending') {
            actionText = '⏸ 不开仓';
            actionClass = 'style="color:var(--yellow);font-weight:700"';
        } else if (s.sim_action === 'normal') {
            actionText = '✓ 正常';
            actionClass = 'style="color:var(--dim)"';
        }
        const simNote = s.sim_note ? `<br><span style="font-size:9px;color:var(--dim2)">${s.sim_note}</span>` : '';
        return `<tr>
            <td>${s.time}</td>
            <td><strong>${s.signal}</strong></td>
            <td class="${dirClass}">${dirText}</td>
            <td style="font-size:10px;color:#999;max-width:280px;white-space:normal">${reasonText}<br><span style="color:var(--dim2)">1H方向:${globalDirText}</span></td>
            <td>${s.entry.toFixed(2)}</td>
            <td>${s.stop.toFixed(2)}</td>
            <td>${s.target.toFixed(2)}</td>
            <td class="${resultClass}">${resultText}</td>
            <td>${s.exit ? s.exit.toFixed(2) : '--'}</td>
            <td>${s.exit_time || '--'}</td>
            <td class="${pnlClass}">${s.pnl >= 0 ? '+' : ''}${s.pnl.toFixed(1)}</td>
            <td>${s.bars_after}</td>
            <td ${actionClass}>${actionText}${simNote}</td>
        </tr>`;
    }).join('');
    
    renderAnalysis();
}

function renderAnalysis() {
    const el = document.getElementById('signalAnalysis');
    if (!el) return;
    
    if (!SIG_HISTORY || SIG_HISTORY.length === 0) {
        el.innerHTML = '<div style="text-align:center;color:var(--dim);padding:20px">暂无信号数据，无法分析</div>';
        return;
    }
    
    const all = SIG_HISTORY;
    const completed = all.filter(s => s.result === 'win' || s.result === 'loss');
    const ongoing = all.filter(s => s.result === 'ongoing');
    const wins = completed.filter(s => s.result === 'win');
    const losses = completed.filter(s => s.result === 'loss');
    const longs = all.filter(s => s.type === 'long');
    const shorts = all.filter(s => s.type === 'short');
    const longCompleted = longs.filter(s => s.result !== 'ongoing');
    const shortCompleted = shorts.filter(s => s.result !== 'ongoing');
    const longWins = longCompleted.filter(s => s.result === 'win');
    const shortWins = shortCompleted.filter(s => s.result === 'win');
    
    const winrate = completed.length > 0 ? (wins.length / completed.length * 100) : 0;
    const longWinrate = longCompleted.length > 0 ? (longWins.length / longCompleted.length * 100) : 0;
    const shortWinrate = shortCompleted.length > 0 ? (shortWins.length / shortCompleted.length * 100) : 0;
    const totalPnl = completed.reduce((sum, s) => sum + s.pnl, 0);
    const avgWin = wins.length > 0 ? wins.reduce((sum, s) => sum + s.pnl, 0) / wins.length : 0;
    const avgLoss = losses.length > 0 ? losses.reduce((sum, s) => sum + s.pnl, 0) / losses.length : 0;
    const avgBars = completed.length > 0 ? completed.reduce((sum, s) => sum + s.bars_after, 0) / completed.length : 0;
    const profitFactor = avgLoss !== 0 ? Math.abs(avgWin / avgLoss) : 0;
    
    // 信号类型统计
    const sigTypes = {};
    all.forEach(s => {
        const key = s.signal;
        if (!sigTypes[key]) sigTypes[key] = {total: 0, wins: 0, losses: 0, ongoing: 0, pnl: 0};
        sigTypes[key].total++;
        if (s.result === 'win') { sigTypes[key].wins++; sigTypes[key].pnl += s.pnl; }
        else if (s.result === 'loss') { sigTypes[key].losses++; sigTypes[key].pnl += s.pnl; }
        else sigTypes[key].ongoing++;
    });
    
    // 期望值计算
    const expectancy = completed.length > 0 ? totalPnl / completed.length : 0;
    
    // 生成分析HTML
    let html = '';
    
    // 指标网格
    html += '<h4>核心指标</h4>';
    html += '<div class="metric-grid">';
    html += `<div class="metric"><div class="label">总信号数</div><div class="value">${all.length}</div></div>`;
    html += `<div class="metric"><div class="label">已完成</div><div class="value">${completed.length}</div></div>`;
    html += `<div class="metric"><div class="label">进行中</div><div class="value" style="color:var(--yellow)">${ongoing.length}</div></div>`;
    html += `<div class="metric"><div class="label">胜率</div><div class="value ${winrate >= 50 ? 'pos' : 'neg'}">${winrate.toFixed(0)}%</div></div>`;
    html += `<div class="metric"><div class="label">总盈亏</div><div class="value ${totalPnl >= 0 ? 'pos' : 'neg'}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(1)}点</div></div>`;
    html += `<div class="metric"><div class="label">期望值/笔</div><div class="value ${expectancy >= 0 ? 'pos' : 'neg'}">${expectancy >= 0 ? '+' : ''}${expectancy.toFixed(1)}点</div></div>`;
    html += `<div class="metric"><div class="label">盈亏比(PF)</div><div class="value ${profitFactor >= 1 ? 'pos' : 'neg'}">${profitFactor.toFixed(2)}</div></div>`;
    html += `<div class="metric"><div class="label">平均持仓</div><div class="value">${avgBars.toFixed(0)}根</div></div>`;
    html += `<div class="metric"><div class="label">平均盈利</div><div class="value pos">+${avgWin.toFixed(1)}</div></div>`;
    html += `<div class="metric"><div class="label">平均亏损</div><div class="value neg">${avgLoss.toFixed(1)}</div></div>`;
    html += `<div class="metric"><div class="label">连亏反手</div><div class="value" style="color:var(--orange)">${flippedSignals.length}次</div></div>`;
    html += `<div class="metric"><div class="label">待确认</div><div class="value" style="color:var(--yellow)">${pendingSignals.length}次</div></div>`;
    html += `<div class="metric"><div class="label">停止交易</div><div class="value" style="color:var(--red)">${skippedSignals.length}次</div></div>`;
    html += '</div>';
    
    // 多空对比
    html += '<h4>多空对比</h4>';
    html += '<div class="metric-grid">';
    html += `<div class="metric"><div class="label">做多信号</div><div class="value" style="color:var(--green)">${longs.length}笔</div></div>`;
    html += `<div class="metric"><div class="label">做多胜率</div><div class="value ${longWinrate >= 50 ? 'pos' : 'neg'}">${longCompleted.length > 0 ? longWinrate.toFixed(0) + '%' : '--'}</div></div>`;
    html += `<div class="metric"><div class="label">做空信号</div><div class="value" style="color:var(--red)">${shorts.length}笔</div></div>`;
    html += `<div class="metric"><div class="label">做空胜率</div><div class="value ${shortWinrate >= 50 ? 'pos' : 'neg'}">${shortCompleted.length > 0 ? shortWinrate.toFixed(0) + '%' : '--'}</div></div>`;
    html += '</div>';
    
    // 信号类型分解
    html += '<h4>信号类型分解</h4>';
    html += '<div style="margin-bottom:10px">';
    Object.keys(sigTypes).forEach(key => {
        const t = sigTypes[key];
        const wr = (t.wins + t.losses) > 0 ? (t.wins / (t.wins + t.losses) * 100) : 0;
        const cls = wr >= 50 ? 'win-tag' : 'loss-tag';
        html += `<span class="tag ${cls}">${key}: ${t.total}笔 | ${t.wins}W/${t.losses}L | 胜率${wr.toFixed(0)}% | PNL${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(1)}</span>`;
    });
    html += '</div>';
    
    // 诊断与建议
    html += '<h4>诊断与改进建议</h4>';
    const suggestions = [];
    
    // 连亏反手统计
    const flippedSignals = all.filter(s => s.sim_action === 'flipped');
    const skippedSignals = all.filter(s => s.sim_action === 'skipped_stopped');
    const pendingSignals = all.filter(s => s.sim_action === 'skipped_pending' || s.sim_action === 'pending');
    const normalSignals = all.filter(s => s.sim_action === 'normal');
    
    if (flippedSignals.length > 0 || skippedSignals.length > 0 || pendingSignals.length > 0) {
        suggestions.push(`连亏反手模拟: ${flippedSignals.length}次反手(大盘方向确认), ${pendingSignals.length}次不开仓(大盘方向未改), ${skippedSignals.length}次跳过停止`);
    }
    
    // 反手后的表现
    if (flippedSignals.length > 0) {
        const afterFlip = all.filter((s, i) => {
            // 找反手之后的信号
            const prevFlipped = all.slice(0, i).some(p => p.sim_action === 'flipped');
            return prevFlipped && s.sim_action !== 'skipped_stopped';
        });
        const afterFlipCompleted = afterFlip.filter(s => s.result !== 'ongoing');
        const afterFlipWins = afterFlipCompleted.filter(s => s.result === 'win');
        if (afterFlipCompleted.length > 0) {
            const flipWinrate = (afterFlipWins.length / afterFlipCompleted.length * 100).toFixed(0);
            suggestions.push(`反手后胜率: ${flipWinrate}% (${afterFlipWins.length}W/${afterFlipCompleted.length - afterFlipWins.length}L), 验证反手策略是否有效`);
        }
    }
    
    if (completed.length < 10) {
        suggestions.push(`样本量不足: 仅${completed.length}笔已完成交易，统计意义有限，建议积累至少30笔再评估策略有效性`);
    }
    if (winrate < 40 && completed.length >= 5) {
        suggestions.push(`胜率偏低(${winrate.toFixed(0)}%): 入场条件可能过于宽松，考虑提高H1/L1的实体占比阈值(当前0.6)或增加额外过滤条件`);
    }
    if (profitFactor < 1 && completed.length >= 5) {
        suggestions.push(`盈亏比<1(${profitFactor.toFixed(2)}): 总体亏损，需优化止损距离(ATR×1.6)或止盈倍数(当前1.5R)`);
    }
    if (Math.abs(longWinrate - shortWinrate) > 30 && longCompleted.length >= 3 && shortCompleted.length >= 3) {
        const better = longWinrate > shortWinrate ? '做多' : '做空';
        const worse = longWinrate > shortWinrate ? '做空' : '做多';
        suggestions.push(`${better}显著优于${worse}: 多空胜率差距${Math.abs(longWinrate - shortWinrate).toFixed(0)}%，可能存在方向偏好，检查1H方向判断逻辑`);
    }
    if (avgBars >= 50) {
        suggestions.push(`持仓时间偏长(${avgBars.toFixed(0)}根5min K线≈${(avgBars*5/60).toFixed(1)}小时): 信号可能入场时机偏早，等待更明确的突破确认`);
    }
    if (ongoing.length > 5) {
        suggestions.push(`过多进行中信号(${ongoing.length}笔): 可能是止盈/止损距离过远，或信号频繁但趋势不明显`);
    }
    // 检查连续亏损
    let maxConsecLoss = 0, curConsec = 0;
    completed.forEach(s => {
        if (s.result === 'loss') { curConsec++; maxConsecLoss = Math.max(maxConsecLoss, curConsec); }
        else curConsec = 0;
    });
    if (maxConsecLoss >= 3) {
        suggestions.push(`最大连续亏损${maxConsecLoss}笔: 需要风控机制应对连续亏损，建议单日最大亏损2R后停止交易`);
    }
    if (suggestions.length === 0 && completed.length >= 10) {
        suggestions.push('策略表现稳定，继续保持当前规则执行');
    }
    
    html += '<div class="suggestion"><span class="label">改进建议:</span><ul>';
    suggestions.forEach(s => html += `<li>${s}</li>`);
    html += '</ul></div>';
    
    // 后续完善方向
    html += '<h4>后续完善方向</h4>';
    html += '<ul>';
    html += '<li><strong>方向判断优化:</strong> 当前历史回扫用全局1H方向，应改为滑动窗口方向判断，每个信号点用其前方的1H结构</li>';
    html += '<li><strong>信号去重:</strong> 当前5根K线去重可能过于简单，应结合实际波动幅度动态调整去重间隔</li>';
    html += '<li><strong>趋势日/震荡日区分:</strong> 历史回扫未区分趋势日/震荡日，应引入R4三条件判断每个信号当时的模式</li>';
    html += '<li><strong>止盈优化:</strong> 当前固定1.5R止盈，可考虑动态止盈(ATR扩展或移动止损)</li>';
    html += '<li><strong>时段过滤:</strong> 加入交易时段过滤，排除亚盘低波动时段的虚假信号</li>';
    html += '<li><strong>信号标注:</strong> 在5min图上标注信号点的止损/目标位，可视化每笔交易的完整路径</li>';
    html += '<li><strong>连亏反手验证:</strong> 反手机制已实现，需积累更多样本验证反手后胜率是否确实更高</li>';
    html += '<li><strong>实盘状态同步:</strong> 当前state在Vercel只读，需用外部存储(如KV)同步连亏状态到下次扫描</li>';
    html += '</ul>';
    
    el.innerHTML = html;
    
    // 更新统计
    const statsEl = document.getElementById('analysisStats');
    if (statsEl) statsEl.textContent = `${completed.length}笔完成 · ${ongoing.length}笔进行中 · 期望值${expectancy >= 0 ? '+' : ''}${expectancy.toFixed(1)}点/笔`;
}

// 加载 Lightweight Charts SDK
function loadScript(src) {
    return new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

loadScript('https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js')
    .then(() => {
        loadChart();
        renderSignalTable();
    })
    .catch(e => {
        console.error('Failed to load Lightweight Charts:', e);
        const container = document.getElementById('tradingChart');
        if (container) container.innerHTML = '<div style="padding:20px;color:#ff5252;text-align:center">图表加载失败: ' + (e.message || e) + '<br><span style="color:#666">CDN: jsdelivr.net/lightweight-charts</span></div>';
    });
</script>'''
    html = html.replace('__SCRIPT_PLACEHOLDER__', js_code)
    return html

# ---------------- Telegram ----------------

def send_telegram(cfg: Dict, text: str) -> bool:
    token = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token or not chat_id:
        log("Telegram 未配置 Bot Token, 跳过推送")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            log(f"Telegram 推送成功 → chat_id={chat_id}")
            return True
        else:
            log(f"Telegram 推送失败: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        log(f"Telegram 推送异常: {e}")
        return False

def build_signal_message(result: Dict) -> str:
    d = result
    dir_text = "做多 🟢" if d["direction"] == "up" else "做空 🔴" if d["direction"] == "down" else "中性"
    checks = d["checks"]
    lines = [
        f"<b>XAU/USD 交易信号触发</b>",
        f"",
        f"方向: {dir_text} ({d['direction']})",
        f"模式: {d['day_mode']}",
        f"信号: {d['signal']['signal']} - {d['signal']['detail']}",
        f"",
        f"📊 交易计划:",
        f"  Entry: {d.get('entry_price','--')}",
        f"  Stop:  {d.get('stop_price','--')} (距离 {d['stop_distance']:.2f})",
        f"  Target: {d.get('target_price','--')} ({d['target_R']})",
        f"",
        f"✅ 规则检查:",
    ]
    for k, v in checks.items():
        icon = "✅" if v["pass"] else "❌"
        lines.append(f"  {icon} {v['label']}: {v['detail']}")
    lines.append(f"")
    lines.append(f"⏰ {d['now']}")
    return "\n".join(lines)

# ---------------- Main ----------------

def run_engine() -> dict:
    """供 serverless 调用：执行引擎并返回 HTML + result，不写文件"""
    cfg = load_config()
    # Vercel 环境变量覆盖
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("TELEGRAM_ENABLED"):
        cfg["telegram_enabled"] = os.environ["TELEGRAM_ENABLED"].lower() in ("true", "1", "yes")

    in_window, window_msg = is_trading_window(cfg)

    r1h = fetch_yahoo(cfg["symbol_yahoo"], cfg["data_range_1h"], cfg["data_interval_1h"])
    bars_1h = to_bars(r1h)
    r5m = fetch_yahoo(cfg["symbol_yahoo"], cfg["data_range_5m"], cfg["data_interval_5m"])
    bars_5m = to_bars(r5m)

    result = check_rules(cfg, bars_1h, bars_5m)
    
    # 扫描历史信号
    atr_5m = result.get("atr_5m", 10.0)
    sig_history = scan_signal_history(bars_5m, bars_1h, atr_5m)
    
    html = generate_html(result, bars_1h, bars_5m, signal_history=sig_history)

    # 推送逻辑
    should_push = False
    push_reason = ""
    if not in_window:
        push_reason = "不在交易时段"
    elif result["all_pass"]:
        should_push = True
        push_reason = "全部条件满足, 推送信号"
    else:
        push_reason = f"{sum(1 for v in result['checks'].values() if not v['pass'])} 项未满足"

    if should_push:
        try:
            msg = build_signal_message(result)
            send_telegram(cfg, msg)
        except Exception as e:
            push_reason += f" (推送异常: {e})"

    return {"html": html, "result": result, "push_reason": push_reason, "in_window": in_window}


def main():
    cfg = load_config()
    log("=== Gold Trading Decision Engine 开始运行 ===")

    # 检查交易时段（非交易时段也生成报告，但不推送）
    in_window, window_msg = is_trading_window(cfg)
    log(f"时段检查: {window_msg}")

    try:
        log("获取 1h 数据...")
        r1h = fetch_yahoo(cfg["symbol_yahoo"], cfg["data_range_1h"], cfg["data_interval_1h"])
        bars_1h = to_bars(r1h)
        log(f"1h bars: {len(bars_1h)}, last close={bars_1h[-1]['close']:.2f}")

        log("获取 5m 数据...")
        r5m = fetch_yahoo(cfg["symbol_yahoo"], cfg["data_range_5m"], cfg["data_interval_5m"])
        bars_5m = to_bars(r5m)
        log(f"5m bars: {len(bars_5m)}, last close={bars_5m[-1]['close']:.2f}")
    except Exception as e:
        log(f"数据获取失败: {e}")
        return

    # 规则引擎
    result = check_rules(cfg, bars_1h, bars_5m)
    log(f"规则引擎: all_pass={result['all_pass']}, direction={result['direction']}, mode={result['day_mode']}, signal={result['signal']['signal']}")

    # 生成 HTML
    atr_5m_local = result.get("atr_5m", 10.0)
    sig_history_local = scan_signal_history(bars_5m, bars_1h, atr_5m_local)
    html = generate_html(result, bars_1h, bars_5m, signal_history=sig_history_local)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"HTML 报告已写入: {REPORT_PATH}")

    # 推送逻辑
    state = result.get("state", {})
    should_push = False
    push_reason = ""

    if not in_window:
        push_reason = "不在交易时段, 不推送"
    elif result["all_pass"]:
        # 检查是否同一信号已推送过（防重复）
        sig_key = f"{result['direction']}_{result['signal']['signal']}_{result['signal']['type']}"
        recent = state.get("signals_today", [])
        last_sig_ts = state.get("last_signal_ts")
        # 同方向同信号 30 分钟内不重复推送
        now_ts = dt.datetime.now().timestamp()
        if last_sig_ts and (now_ts - last_sig_ts) < 1800 and any(s.get("key") == sig_key for s in recent):
            push_reason = f"30分钟内已推送过相同信号 {sig_key}, 跳过"
        else:
            should_push = True
            push_reason = "全部条件满足, 推送信号"
            state.setdefault("signals_today", []).append({"key": sig_key, "ts": now_ts, "time": result["now"]})
            state["last_signal_ts"] = now_ts
    else:
        push_reason = f"{sum(1 for v in result['checks'].values() if not v['pass'])} 项未满足, 不推送"

    log(f"推送决策: {push_reason}")

    if should_push:
        msg = build_signal_message(result)
        send_telegram(cfg, msg)

    save_state(state)
    log("=== 运行结束 ===\n")

if __name__ == "__main__":
    main()
