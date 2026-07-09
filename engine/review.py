"""復盤引擎：以 K 棒重播推薦單的完整生命週期，統計勝率/盈虧比/掛單成交率，自動產生復盤筆記。

保守模擬原則（避免高估）：
- 同一根 K 棒同時觸及進場與停損 → 以「先成交、後停損」計（吃虧算法）
- 新成交當根 K 棒不計止盈
- 停損出場計 0.03% 滑價；進場 maker 0.02%、出場 taker 0.055% 手續費
"""
import numpy as np

from .util import DAY_MS, ts_to_date

FEE_MAKER, FEE_TAKER, SLIP = 0.0002, 0.00055, 0.0003


def new_trade(plan: dict, sig: dict, mode: str) -> dict:
    return {"id": f"{ts_to_date(plan['signal_ts'])}-{plan['direction']}",
            "date": ts_to_date(plan["signal_ts"]), "mode": mode,
            "direction": plan["direction"], "confidence": sig["confidence"],
            "score": sig["score"], "plan": plan,
            "status": "pending", "fills": [], "exits": [],
            "stop_now": plan["stop"], "tp_done": [False, False], "trail": None,
            "force_exit": False, "mae_r": 0.0, "mfe_r": 0.0,
            "r": None, "fees_r": 0.0, "lessons": [], "processed_end": 0}


def _avg_fill(tr):
    w = sum(f["w"] for f in tr["fills"])
    return (sum(f["price"] * f["w"] for f in tr["fills"]) / w, w) if w else (None, 0.0)


def _exit(tr, price, frac, ts, reason):
    tr["exits"].append({"price": float(price), "frac": float(frac), "ts": int(ts), "reason": reason})


def _remaining(tr):
    _, fw = _avg_fill(tr)
    return max(0.0, fw - sum(e["frac"] for e in tr["exits"]))


def process_trade(tr: dict, bars, atr_by_day: dict, params: dict) -> None:
    """把 bars（ts,open,high,low,close 升冪）餵給交易；bars 可為 1h（實盤）或 1d（回測）。"""
    if tr["status"] in ("closed", "cancelled"):
        return
    plan = tr["plan"]
    sgn = 1 if tr["direction"] == "LONG" else -1
    dist = plan["dist"]
    bar_ms = int(bars["ts"].iloc[1] - bars["ts"].iloc[0]) if len(bars) > 1 else DAY_MS

    for row in bars.itertuples(index=False):
        ts, o, h, l, c = int(row.ts), float(row.open), float(row.high), float(row.low), float(row.close)
        if ts < plan["signal_ts"] or ts < tr.get("processed_end", 0):
            continue
        tr["processed_end"] = ts + bar_ms
        avg, fw = _avg_fill(tr)

        # 反向訊號強制離場（下一根開盤價）
        if tr["force_exit"] and fw > 0 and _remaining(tr) > 0:
            _exit(tr, o, _remaining(tr), ts, "reverse_signal")
            _close(tr, params)
            return
        # 時間停損（超過最大持有期）
        if ts >= plan["deadline_ms"]:
            if fw > 0 and _remaining(tr) > 0:
                _exit(tr, o, _remaining(tr), ts, "time")
                _close(tr, params)
            else:
                tr["status"] = "cancelled"
            return

        filled_this_bar = False
        if ts < plan["validity_ms"]:
            for rung in plan["entries"]:
                ri = rung.get("i", rung["price"])
                if any(f.get("ri") == ri for f in tr["fills"]):
                    continue
                hit = l <= rung["price"] if sgn == 1 else h >= rung["price"]
                if hit:
                    tr["fills"].append({"price": rung["price"], "w": rung["w"], "ts": ts,
                                        "tag": rung["tag"], "ri": ri})
                    filled_this_bar = True
        elif not tr["fills"]:
            tr["status"] = "cancelled"
            return

        avg, fw = _avg_fill(tr)
        if not fw:
            continue
        tr["status"] = "open"

        # MAE/MFE（以實際成交均價、R 單位）
        tr["mfe_r"] = max(tr["mfe_r"], sgn * ((h if sgn == 1 else l) - avg) / dist)
        tr["mae_r"] = min(tr["mae_r"], sgn * ((l if sgn == 1 else h) - avg) / dist)

        # 停損（含保本/移動停損）
        eff = tr["stop_now"]
        stopped = l <= eff if sgn == 1 else h >= eff
        if stopped:
            px = eff * (1 - sgn * SLIP)
            reason = "stop"
            if tr["tp_done"][1] or tr["trail"] is not None:
                reason = "trail_stop"
            elif tr["tp_done"][0]:
                reason = "be_stop"
            _exit(tr, px, _remaining(tr), ts, reason)
            _close(tr, params)
            return

        # 止盈（新成交當根不計，保守）
        if not filled_this_bar:
            for i, tp in enumerate(plan["tps"]):
                if tr["tp_done"][i]:
                    continue
                hit_tp = h >= tp["price"] if sgn == 1 else l <= tp["price"]
                if hit_tp and _remaining(tr) > 0:
                    _exit(tr, tp["price"], min(tp["frac"], _remaining(tr)), ts, tp["name"].lower())
                    tr["tp_done"][i] = True
                    if i == 0:  # TP1 → 保本
                        tr["stop_now"] = max(tr["stop_now"], avg) if sgn == 1 else min(tr["stop_now"], avg)
                    if _remaining(tr) <= 1e-9:
                        _close(tr, params)
                        return

        # 移動停損：TP2 後啟動，每完成一個 UTC 日更新（吊燈式）
        if tr["tp_done"][1]:
            day_end = (ts + bar_ms) % DAY_MS == 0
            if day_end or bar_ms >= DAY_MS:
                day_key = ((ts + bar_ms) // DAY_MS - 1) * DAY_MS
                a = atr_by_day.get(day_key)
                if a:
                    cand = c - sgn * params["trail_atr_mult"] * a
                    tr["trail"] = cand if tr["trail"] is None else (max(tr["trail"], cand) if sgn == 1 else min(tr["trail"], cand))
                    tr["stop_now"] = max(tr["stop_now"], tr["trail"]) if sgn == 1 else min(tr["stop_now"], tr["trail"])


def _close(tr: dict, params: dict) -> None:
    plan = tr["plan"]
    sgn = 1 if tr["direction"] == "LONG" else -1
    avg, fw = _avg_fill(tr)
    dist = plan["dist"]
    stop_pct = dist / plan["avg_entry"]
    gross = sum(sgn * (e["price"] - avg) / dist * e["frac"] for e in tr["exits"])
    fees = FEE_MAKER * fw / stop_pct
    fees += sum(FEE_TAKER * e["frac"] / stop_pct for e in tr["exits"])
    tr["fees_r"] = round(fees, 3)
    tr["r"] = round(gross - fees, 3)
    tr["status"] = "closed"
    tr["exit_ts"] = tr["exits"][-1]["ts"] if tr["exits"] else tr.get("processed_end", 0)


def add_lessons(tr: dict, daily) -> None:
    """出場後對照後續走勢，自動產生復盤筆記（餵給優化器）。"""
    if tr.get("_lessoned"):
        return
    plan = tr["plan"]
    sgn = 1 if tr["direction"] == "LONG" else -1
    lessons = []
    if tr["status"] == "closed":
        first_reason = tr["exits"][0]["reason"] if tr["exits"] else ""
        last_reason = tr["exits"][-1]["reason"] if tr["exits"] else ""
        avg, _ = _avg_fill(tr)
        if last_reason == "stop" and tr["r"] is not None and tr["r"] < 0:
            after = daily[(daily["ts"] > tr["exit_ts"]) & (daily["ts"] <= tr["exit_ts"] + 2 * DAY_MS)]
            if len(after):
                tp1 = plan["tps"][0]["price"]
                rebound = (after["high"] >= tp1).any() if sgn == 1 else (after["low"] <= tp1).any()
                if rebound:
                    lessons.append("疑似插針掃損：停損後 48h 內價格達 TP1 → 停損偏窄")
        if tr["r"] is not None and tr["r"] >= 2.5:
            lessons.append("大賺樣本 ✓ 移動停損成功讓利潤奔跑")
        if last_reason in ("trail_stop", "time") and tr["mfe_r"] - (tr["r"] or 0) > 1.2:
            lessons.append(f"回吐偏多：MFE {tr['mfe_r']:.1f}R → 實收 {tr['r']:.1f}R")
        if last_reason == "time" and abs(tr["r"] or 0) < 0.3:
            lessons.append("7 日到期平盤出場：進場後動能不足")
    elif tr["status"] == "cancelled":
        after = daily[(daily["ts"] > plan["signal_ts"]) & (daily["ts"] <= plan["signal_ts"] + 3 * DAY_MS)]
        if len(after):
            e1 = plan["entries"][0]["price"]
            if sgn == 1 and after["high"].max() >= e1 + 1.5 * plan["dist"]:
                lessons.append("掛單過深錯過行情：未成交但方向正確（+1.5R 內）")
            if sgn == -1 and after["low"].min() <= e1 - 1.5 * plan["dist"]:
                lessons.append("掛單過深錯過行情：未成交但方向正確（+1.5R 內）")
    tr["lessons"] = lessons
    tr["_lessoned"] = True


# ---------------------------------------------------------------- 統計
def compute_stats(trades: list[dict]) -> dict:
    done = [t for t in trades if t["status"] in ("closed", "cancelled")]
    closed = [t for t in done if t["status"] == "closed" and t["r"] is not None]
    cancelled = [t for t in done if t["status"] == "cancelled"]
    wins = [t for t in closed if t["r"] > 0]
    losses = [t for t in closed if t["r"] <= 0]
    fill_rate = len(closed) / len(done) if done else None
    rung_fills = []
    for i in range(3):
        if not done:
            rung_fills.append(None)
            continue
        n_hit = 0
        for t in done:
            ents = t["plan"]["entries"]
            if i < len(ents) and any(f.get("ri") == ents[i].get("i", ents[i]["price"]) for f in t["fills"]):
                n_hit += 1
        rung_fills.append(round(n_hit / len(done), 2))
    rs = [t["r"] for t in closed]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for t in sorted(closed, key=lambda x: x.get("exit_ts", 0)):
        cum += t["r"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    calib = []
    for lo, hi in ((55, 65), (65, 75), (75, 101)):
        seg = [t for t in closed if lo <= t["confidence"] < hi]
        if seg:
            calib.append({"bucket": f"{lo}-{hi - 1 if hi < 101 else 100}",
                          "n": len(seg), "win": round(sum(1 for t in seg if t['r'] > 0) / len(seg), 2),
                          "avg_r": round(float(np.mean([t['r'] for t in seg])), 2)})
    stop_hunted = sum(1 for t in closed if any("插針掃損" in l for l in t["lessons"]))
    missed = sum(1 for t in cancelled if any("錯過行情" in l for l in t["lessons"]))
    return {
        "n_signals": len(done), "n_closed": len(closed), "n_cancelled": len(cancelled),
        "fill_rate": round(fill_rate, 3) if fill_rate is not None else None,
        "rung_fill_rates": rung_fills,
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "avg_win_r": round(float(np.mean([t["r"] for t in wins])), 2) if wins else None,
        "avg_loss_r": round(float(np.mean([t["r"] for t in losses])), 2) if losses else None,
        "profit_factor": round(sum(t["r"] for t in wins) / abs(sum(t["r"] for t in losses)), 2)
        if losses and sum(t["r"] for t in losses) != 0 else None,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else None,
        "expectancy_per_signal_r": round(float(np.sum(rs)) / len(done), 3) if done else None,
        "total_r": round(float(np.sum(rs)), 2) if rs else 0.0,
        "max_dd_r": round(mdd, 2), "calibration": calib,
        "stop_hunted": stop_hunted, "missed_moves": missed,
        "long_n": len([t for t in closed if t["direction"] == "LONG"]),
        "short_n": len([t for t in closed if t["direction"] == "SHORT"]),
        "long_win": round(np.mean([1 if t["r"] > 0 else 0 for t in closed if t["direction"] == "LONG"]), 2)
        if any(t["direction"] == "LONG" for t in closed) else None,
        "short_win": round(np.mean([1 if t["r"] > 0 else 0 for t in closed if t["direction"] == "SHORT"]), 2)
        if any(t["direction"] == "SHORT" for t in closed) else None,
    }


def equity_curve(trades: list[dict], start_eq: float = 10_000.0) -> list[dict]:
    eq, cum = start_eq, 0.0
    out = []
    for t in sorted([t for t in trades if t["status"] == "closed" and t["r"] is not None],
                    key=lambda x: x.get("exit_ts", 0)):
        eq *= 1 + t["r"] * t["plan"]["risk_pct"] / 100
        cum += t["r"]
        out.append({"date": ts_to_date(t["exit_ts"]), "r": round(cum, 2),
                    "eq": round(eq, 0), "mode": t["mode"]})
    return out


def cooling_active(trades: list[dict], now_ms: int) -> bool:
    """連續 2 筆停損虧損且最近一筆在 3 日內 → 冷卻（今日強制觀望）。"""
    closed = sorted([t for t in trades if t["status"] == "closed" and t["r"] is not None],
                    key=lambda x: x.get("exit_ts", 0))
    if len(closed) < 2:
        return False
    a, b = closed[-2], closed[-1]
    recent = now_ms - b.get("exit_ts", 0) <= 3 * DAY_MS
    return a["r"] < 0 and b["r"] < 0 and recent
