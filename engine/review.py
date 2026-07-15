"""復盤引擎：以 K 棒重播推薦單的完整生命週期，統計勝率/盈虧比/掛單成交率，自動產生復盤筆記。

保守模擬原則（避免高估）：
- 同一根 K 棒同時觸及進場與停損 → 以「先成交、後停損」計（吃虧算法）
- 新成交當根 K 棒不計止盈
- 停損出場計 0.03% 滑價；進場與限價止盈 maker 0.02%、市價出場 taker 0.055% 手續費
- 持倉期間資金費率逐日計入損益（順逆風誠實入帳）

出場結構（大賺小賠的實作）：
- TP1/TP2 平倉比例按「實際成交權重」等比縮放——部分成交時 30/30/40 結構不變形
- TP1 後停損移保本，且吊燈式移動停損同步啟動（取保本與吊燈較高者）
- 鎖利棘輪：浮盈曾達 0.6R 的單，停損上移到 -0.25R（浮盈不該變全虧）
- 停滯出場：進場 N 天仍無進展（MFE 低於門檻）→ 下一根開盤離場，不佔用資金與心力
"""
import numpy as np

from .util import DAY_MS, ts_to_date

FEE_MAKER, FEE_TAKER, SLIP = 0.0002, 0.00055, 0.0003
MAKER_EXITS = {"tp1", "tp2"}  # 限價止盈單以 maker 費率成交

# 出場時停損來源 → 出場原因代碼
_STOP_REASON = {"init": "stop", "ratchet": "protect_stop", "be": "be_stop", "trail": "trail_stop"}


def new_trade(plan: dict, sig: dict, mode: str) -> dict:
    return {"id": f"{ts_to_date(plan['signal_ts'])}-{plan['direction']}",
            "date": ts_to_date(plan["signal_ts"]), "mode": mode,
            "tier": plan.get("tier", "standard"),
            "direction": plan["direction"], "confidence": sig["confidence"],
            "score": sig["score"], "plan": plan,
            "status": "pending", "fills": [], "exits": [],
            "stop_now": plan["stop"], "stop_src": "init",
            "tp_done": [False, False], "trail": None,
            "force_exit": False, "mae_r": 0.0, "mfe_r": 0.0,
            "r": None, "fees_r": 0.0, "funding_r": 0.0,
            "lessons": [], "processed_end": 0}


def _avg_fill(tr):
    w = sum(f["w"] for f in tr["fills"])
    return (sum(f["price"] * f["w"] for f in tr["fills"]) / w, w) if w else (None, 0.0)


def _exit(tr, price, frac, ts, reason):
    tr["exits"].append({"price": float(price), "frac": float(frac), "ts": int(ts), "reason": reason})


def _remaining(tr):
    _, fw = _avg_fill(tr)
    return max(0.0, fw - sum(e["frac"] for e in tr["exits"]))


def _raise_stop(tr: dict, sgn: int, cand: float, src: str) -> None:
    """只朝有利方向移動停損；記錄來源供出場原因判讀。"""
    better = cand > tr["stop_now"] if sgn == 1 else cand < tr["stop_now"]
    if better:
        tr["stop_now"] = cand
        tr["stop_src"] = src


def process_trade(tr: dict, bars, atr_by_day: dict, params: dict,
                  funding_by_day: dict | None = None) -> None:
    """把 bars（ts,open,high,low,close 升冪）餵給交易；bars 可為 1h（實盤）或 1d（回測）。"""
    if tr["status"] in ("closed", "cancelled"):
        return
    plan = tr["plan"]
    sgn = 1 if tr["direction"] == "LONG" else -1
    dist = plan["dist"]
    bar_ms = int(bars["ts"].iloc[1] - bars["ts"].iloc[0]) if len(bars) > 1 else DAY_MS
    stag_ms = params.get("stagnation_days", 5) * DAY_MS
    trail_after = 0 if params.get("trail_after", "tp1") == "tp1" else 1

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
        # 停滯出場：進場滿 N 天、未觸及任一止盈、浮盈從未有像樣進展 → 開盤離場
        if (fw > 0 and _remaining(tr) > 0 and not any(tr["tp_done"])
                and ts - tr["fills"][0]["ts"] >= stag_ms
                and tr["mfe_r"] < params.get("stagnation_mfe_r", 0.35)):
            _exit(tr, o, _remaining(tr), ts, "stagnation")
            _close(tr, params)
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

        # 停損（含棘輪/保本/移動停損）
        eff = tr["stop_now"]
        stopped = l <= eff if sgn == 1 else h >= eff
        if stopped:
            px = eff * (1 - sgn * SLIP)
            _exit(tr, px, _remaining(tr), ts, _STOP_REASON.get(tr["stop_src"], "stop"))
            _close(tr, params)
            return

        # 止盈（新成交當根不計，保守）；平倉量按實際成交權重等比縮放，
        # 部分成交時 30/30/40 結構不變形、40% 趨勢單永遠存在
        if not filled_this_bar:
            for i, tp in enumerate(plan["tps"]):
                if tr["tp_done"][i]:
                    continue
                hit_tp = h >= tp["price"] if sgn == 1 else l <= tp["price"]
                if hit_tp and _remaining(tr) > 0:
                    _exit(tr, tp["price"], min(tp["frac"] * fw, _remaining(tr)), ts, tp["name"].lower())
                    tr["tp_done"][i] = True
                    if i == 0:  # TP1 → 保本
                        _raise_stop(tr, sgn, avg, "be")
                    if _remaining(tr) <= 1e-9:
                        _close(tr, params)
                        return

        # 鎖利棘輪：浮盈曾達門檻，停損上移到 -ratchet_lock_r（浮盈單不該變全額虧損）
        if not tr["tp_done"][0] and tr["mfe_r"] >= params.get("ratchet_mfe_r", 0.6):
            _raise_stop(tr, sgn, avg - sgn * params.get("ratchet_lock_r", 0.25) * dist, "ratchet")

        # 吊燈式移動停損：TP1（可調）後啟動，每完成一個 UTC 日更新；與保本取較有利者
        if tr["tp_done"][trail_after]:
            day_end = (ts + bar_ms) % DAY_MS == 0
            if day_end or bar_ms >= DAY_MS:
                day_key = ((ts + bar_ms) // DAY_MS - 1) * DAY_MS
                a = atr_by_day.get(day_key)
                if a:
                    cand = c - sgn * params["trail_atr_mult"] * a
                    tr["trail"] = cand if tr["trail"] is None else (max(tr["trail"], cand) if sgn == 1 else min(tr["trail"], cand))
                    _raise_stop(tr, sgn, tr["trail"], "trail")

        # 資金費率：每完成一個 UTC 日，按持倉比例入帳（費率為當日末筆 8h 費率 ×3 的近似）
        if funding_by_day and _remaining(tr) > 0:
            day_end = (ts + bar_ms) % DAY_MS == 0
            if day_end or bar_ms >= DAY_MS:
                day_key = ((ts + bar_ms) // DAY_MS - 1) * DAY_MS
                f = funding_by_day.get(day_key)
                if f is not None:
                    # 多單付正費率、空單收正費率；換算成 R（除以停損幅度）
                    stop_pct = dist / plan["avg_entry"]
                    tr["funding_r"] = round(tr["funding_r"]
                                            - sgn * f * 3 * _remaining(tr) / stop_pct, 4)


def _close(tr: dict, params: dict) -> None:
    plan = tr["plan"]
    sgn = 1 if tr["direction"] == "LONG" else -1
    avg, fw = _avg_fill(tr)
    dist = plan["dist"]
    stop_pct = dist / plan["avg_entry"]
    gross = sum(sgn * (e["price"] - avg) / dist * e["frac"] for e in tr["exits"])
    fees = FEE_MAKER * fw / stop_pct
    fees += sum((FEE_MAKER if e["reason"] in MAKER_EXITS else FEE_TAKER) * e["frac"] / stop_pct
                for e in tr["exits"])
    tr["fees_r"] = round(fees, 3)
    tr["r"] = round(gross - fees + tr.get("funding_r", 0.0), 3)
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
            lessons.append("到期平盤出場：進場後動能不足")
        if last_reason == "stagnation":
            after = daily[(daily["ts"] > tr["exit_ts"]) & (daily["ts"] <= tr["exit_ts"] + 3 * DAY_MS)]
            if len(after):
                tp1 = plan["tps"][0]["price"]
                ran = (after["high"] >= tp1).any() if sgn == 1 else (after["low"] <= tp1).any()
                lessons.append("停滯出場過早：離場後 3 日內達 TP1" if ran
                               else "停滯出場正確：離場後行情持續無進展")
        if last_reason == "protect_stop":
            lessons.append(f"鎖利棘輪出場：浮盈曾達 {tr['mfe_r']:.1f}R，守住不讓它變全虧")
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
    # 去除 ±0.1R 內的「平手單」後的勝率（停滯/到期的零和出場不該稀釋統計）
    meaningful = [t for t in closed if abs(t["r"]) > 0.1]
    scratch_n = len(closed) - len(meaningful)
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
    tiers = {}
    for tier in ("standard", "scout"):
        seg = [t for t in closed if t.get("tier", "standard") == tier]
        seg_done = [t for t in done if t.get("tier", "standard") == tier]
        if seg_done:
            tiers[tier] = {
                "n_signals": len(seg_done), "n_closed": len(seg),
                "win_rate": round(sum(1 for t in seg if t["r"] > 0) / len(seg), 2) if seg else None,
                "expectancy_r": round(float(np.mean([t["r"] for t in seg])), 3) if seg else None,
                "total_r": round(float(np.sum([t["r"] for t in seg])), 2) if seg else 0.0,
            }
    return {
        "tiers": tiers,
        "n_signals": len(done), "n_closed": len(closed), "n_cancelled": len(cancelled),
        "fill_rate": round(fill_rate, 3) if fill_rate is not None else None,
        "rung_fill_rates": rung_fills,
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "win_rate_ex_scratch": round(sum(1 for t in meaningful if t["r"] > 0) / len(meaningful), 3)
        if meaningful else None,
        "scratch_n": scratch_n,
        "funding_r_total": round(float(np.sum([t.get("funding_r", 0.0) for t in closed])), 2) if closed else 0.0,
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
    """連續 2 筆實質停損（r < -0.5R）且最近一筆在 3 日內 → 冷卻（今日強制觀望）。
    停滯/到期的 ±0 小額出場不觸發冷卻——那不是判斷錯誤，只是行情沒來。"""
    closed = sorted([t for t in trades if t["status"] == "closed" and t["r"] is not None],
                    key=lambda x: x.get("exit_ts", 0))
    if len(closed) < 2:
        return False
    a, b = closed[-2], closed[-1]
    recent = now_ms - b.get("exit_ts", 0) <= 3 * DAY_MS
    return a["r"] < -0.5 and b["r"] < -0.5 and recent
