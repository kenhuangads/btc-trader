"""交易計畫產生器：分批限價掛單梯（含成交機率）、結構停損、階梯止盈、槓桿與倉位建議。

原則（對應研究文件）：
- 掛單在回檔至結構支撐處，而非市價追價 → 提高掛單成功率
- 停損放在結構外側再加 ATR 緩衝（寬停損避插針），單筆風險固定 1~2%
- TP1 +1R 平 30% 且停損移保本（絕對無風險狀態）、TP2 +2R 平 30%、
  餘倉 ATR 移動停損讓利潤奔跑 → 大賺小賠
- 槓桿 ≤5x，強平價必須遠於停損
"""
import numpy as np

from .util import DAY_MS, price_rnd


def _interp_prob(touch: dict, off_atr: float) -> float | None:
    """從歷史觸價機率曲線內插（off_atr = 距收盤的 ATR 倍數）。"""
    if not touch:
        return None
    xs = sorted(float(k) for k in touch.keys())
    if not xs:
        return None
    if off_atr <= xs[0]:
        return touch[f"{xs[0]:.1f}"]
    if off_atr >= xs[-1]:
        return touch[f"{xs[-1]:.1f}"]
    for a, b in zip(xs, xs[1:]):
        if a <= off_atr <= b:
            pa, pb = touch[f"{a:.1f}"], touch[f"{b:.1f}"]
            w = (off_atr - a) / (b - a)
            return pa + (pb - pa) * w
    return None


def build_plan(direction: str, D, t: int, sig: dict, state: dict,
               touch_probs: dict, risk_pct: float) -> dict:
    p = state["params"]
    close = float(D["close"].iloc[t])
    atr = float(D["atr"].iloc[t])
    sgn = 1 if direction == "LONG" else -1
    clusters = sig["clusters"]

    # --- 進場掛單梯（回檔限價），並吸附至鄰近價位群 ---
    offsets = [o * p["entry_depth_mult"] for o in p["entry_offsets_atr"]]
    weights = p["rung_weights"]
    entries = []
    for off, w in zip(offsets, weights):
        raw = close - sgn * off * atr
        tag = f"{off:.2f} ATR"
        if direction == "LONG":
            cands = [c for c in clusters if c["price"] < raw and (raw - c["price"]) < 0.25 * atr and c["strength"] >= 2]
            if cands:
                lv = max(cands, key=lambda c: c["price"])
                raw = lv["price"] + 0.05 * atr  # 掛在支撐前緣，搶先成交
                tag = f"支撐群 {lv['price']:,.0f} 前緣"
        else:
            cands = [c for c in clusters if c["price"] > raw and (c["price"] - raw) < 0.25 * atr and c["strength"] >= 2]
            if cands:
                lv = min(cands, key=lambda c: c["price"])
                raw = lv["price"] - 0.05 * atr
                tag = f"壓力群 {lv['price']:,.0f} 前緣"
        off_eff = abs(close - raw) / atr
        prob = _interp_prob(touch_probs.get(direction.lower(), {}), off_eff)
        entries.append({"price": price_rnd(raw), "w": w, "tag": tag,
                        "prob": round(prob, 2) if prob is not None else None})
    # 保證順序（多單由淺到深遞減價格），並合併吸附後過近的檔位
    entries.sort(key=lambda e: -e["price"] * sgn)
    merged = []
    for e in entries:
        if merged and abs(e["price"] - merged[-1]["price"]) < 0.08 * atr:
            merged[-1]["w"] = round(merged[-1]["w"] + e["w"], 2)
        else:
            merged.append(e)
    entries = merged
    for i, e in enumerate(entries):
        e["i"] = i

    avg_entry = sum(e["price"] * e["w"] for e in entries) / sum(e["w"] for e in entries)

    # --- 結構停損 + ATR 緩衝，鎖定寬度上下限 ---
    look = D.iloc[max(0, t - 9):t + 1]
    if direction == "LONG":
        struct = min(float(look["low"].min()), min(e["price"] for e in entries))
        stop = struct - p["stop_buffer_atr"] * atr
        dist = avg_entry - stop
    else:
        struct = max(float(look["high"].max()), max(e["price"] for e in entries))
        stop = struct + p["stop_buffer_atr"] * atr
        dist = stop - avg_entry
    dist = float(np.clip(dist, p["stop_min_atr"] * atr, p["stop_max_atr"] * atr))
    stop = avg_entry - sgn * dist

    # --- 止盈階梯與磁吸目標 ---
    # frac 為「實際成交部位」的比例（復盤引擎按成交權重等比縮放，部分成交結構不變形）
    tp1_r = p.get("tp1_r", 0.7)
    tp2_r = round(tp1_r + 1.0, 2)
    tps = [
        {"name": "TP1", "price": price_rnd(avg_entry + sgn * tp1_r * dist), "frac": 0.30, "r": tp1_r,
         "action": "平 30%，停損移保本＋啟動移動停損"},
        {"name": "TP2", "price": price_rnd(avg_entry + sgn * tp2_r * dist), "frac": 0.30, "r": tp2_r,
         "action": "再平 30%，餘倉讓利潤奔跑"},
    ]
    trail_txt = f"餘倉 40% 以 {p['trail_atr_mult']:.1f}×ATR 吊燈式移動停損讓利潤奔跑（TP1 後啟動）"
    targets = []
    if direction == "LONG":
        res = [c for c in clusters if c["price"] > avg_entry and c["strength"] >= 2][:3]
        targets += [{"price": price_rnd(c["price"]), "why": "+".join(c["srcs"][:2])} for c in res]
        wk = [w for w in sig["wicks"] if w["side"] == "up" and w["mid"] > avg_entry]
        targets += [{"price": price_rnd(w["mid"]), "why": "上影線50%磁吸"} for w in wk[:2]]
    else:
        sup = [c for c in reversed([c for c in clusters if c["price"] < avg_entry and c["strength"] >= 2])][:3]
        targets += [{"price": price_rnd(c["price"]), "why": "+".join(c["srcs"][:2])} for c in sup]
        wk = [w for w in sig["wicks"] if w["side"] == "down" and w["mid"] < avg_entry]
        targets += [{"price": price_rnd(w["mid"]), "why": "下影線50%磁吸"} for w in wk[:2]]
    targets = sorted(targets, key=lambda x: sgn * x["price"])[:4]

    # --- 盈虧比空間檢查（到最近強壓力/支撐的 R 數） ---
    rr_to_res = None
    if direction == "LONG":
        res2 = [c["price"] for c in clusters if c["price"] > avg_entry * 1.002 and c["strength"] >= 2]
        if res2:
            rr_to_res = round((min(res2) - avg_entry) / dist, 2)
    else:
        sup2 = [c["price"] for c in clusters if c["price"] < avg_entry * 0.998 and c["strength"] >= 2]
        if sup2:
            rr_to_res = round((avg_entry - max(sup2)) / dist, 2)

    # --- 倉位與槓桿（固定風險百分比法） ---
    stop_pct = dist / avg_entry
    eq = 10_000.0  # 基準展示值；前端可依本金即時換算
    risk_usd = eq * risk_pct / 100
    notional = risk_usd / stop_pct
    margin = max(eq * 0.10, notional / p["max_leverage"])
    lev = notional / margin
    # 強平價安全檢查：強平需比停損再遠 30% 以上（維持保證金近似 0.6%）
    liq_frac_needed = (dist * 1.3) / avg_entry + 0.006
    lev_safe = 1 / liq_frac_needed if liq_frac_needed > 0 else p["max_leverage"]
    lev = float(min(lev, lev_safe, p["max_leverage"]))
    lev = max(1.0, round(lev * 2) / 2)
    margin = notional / lev
    liq_est = avg_entry * (1 - sgn * (1 / lev - 0.006))

    warnings = []
    if rr_to_res is not None and rr_to_res < 1.6:
        warnings.append(f"到最近強{'壓力' if direction == 'LONG' else '支撐'}僅 {rr_to_res:.1f}R，獲利空間受限")

    ts_signal = int(D["ts"].iloc[t]) + DAY_MS  # 訊號生效時間 = 該日K收盤
    scen_dir = "多" if direction == "LONG" else "空"
    scenarios = {
        "main": f"價格回檔至第 1~2 檔掛單成交後於支撐止穩，先看 TP1(+{tp1_r:g}R) 移保本，趨勢延續則按階梯止盈讓利潤奔跑。",
        "alt": f"若急殺/急拉直達第 3 檔（深水區，影線回補型成交），成本更優，仍按同一套停損停利執行。",
        "stall": f"進場 {p.get('stagnation_days', 4)} 天仍無進展（浮盈未達 {p.get('stagnation_mfe_r', 0.35):.2f}R）"
                 f"→ 開盤離場換下一次機會，不跟死行情耗。",
        "invalid": f"日線收盤{'跌破' if direction == 'LONG' else '站上'} {price_rnd(stop):,} 即結構破壞，"
                   f"做{scen_dir}邏輯失效，無條件離場不凹單。",
    }

    return {"direction": direction, "signal_ts": ts_signal, "ref_close": price_rnd(close),
            "atr": round(atr, 1), "entries": entries, "avg_entry": price_rnd(avg_entry),
            "stop": price_rnd(stop), "dist": round(dist, 1), "stop_pct": round(stop_pct * 100, 2),
            "tps": tps, "trail_mult": p["trail_atr_mult"], "trail_txt": trail_txt,
            "targets": targets, "rr_to_res": rr_to_res,
            "risk_pct": risk_pct, "leverage": lev, "margin_usd_10k": round(margin, 0),
            "notional_usd_10k": round(notional, 0), "qty_btc_10k": round(notional / avg_entry, 4),
            "liq_est": price_rnd(liq_est),
            "validity_ms": ts_signal + p["entry_validity_hours"] * 3_600_000,
            "deadline_ms": ts_signal + p["max_hold_days"] * DAY_MS,
            "scenarios": scenarios, "warnings": warnings}


def watch_conditions(sig: dict) -> list[str]:
    """觀望日：產出「要等到什麼條件才出手」的白話清單。"""
    facs = sorted([f for f in sig["factors"] if f["ok"]], key=lambda f: abs(f["score"]), reverse=True)
    out = []
    score = sig["score"]
    if abs(score) < 8:
        return ["多空因子接近抵銷，等待任一方向出現「放量突破＋衍生品數據共振」再出手"]
    for f in facs:
        if len(out) >= 3:
            break
        # 與綜合方向相反、或大幅拖後腿的因子 → 改善條件
        if score >= 0 and f["score"] <= -12:
            out.append(f"待改善：{f['label']}（{f['note']}）")
        elif score < 0 and f["score"] >= 12:
            out.append(f"待改善：{f['label']}（{f['note']}）")
    if not out:
        out.append("多空因子接近抵銷，等待任一方向出現放量突破＋衍生品數據共振")
    return out
