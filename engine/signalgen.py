"""決策層：因子綜合 → 紀律閘門（逆勢、冷卻、總經事件、信心門檻）→ 交易計畫。

回測與每日實盤共用同一條路徑，確保復盤統計與線上推薦口徑一致。
"""
from .util import DAY_MS
from . import factors as F
from . import planner as P
from .macro import macro_gate
from .review import cooling_active


def decide(D, t: int, state: dict, trades: list[dict], touch: dict, h4=None) -> dict:
    p = state["params"]
    sig = F.compute_signal(D, t, state["weights"], h4=h4)
    ts_signal = int(D["ts"].iloc[t]) + DAY_MS
    macro = macro_gate(ts_signal)
    gates, direction = [], "FLAT"

    if sig["score"] >= p["score_threshold"]:
        direction = "LONG"
    elif sig["score"] <= -p["score_threshold"]:
        direction = "SHORT"
    else:
        gates.append(f"綜合分數 {sig['score']:+.0f} 未達門檻 ±{p['score_threshold']:.0f} → 觀望")

    # 逆勢保護：強勢單邊行情中不逆勢接刀（除非軋空/殺多醞釀分數夠強）
    if direction != "FLAT":
        td = next((f["score"] for f in sig["factors"] if f["name"] == "trend_daily"), 0)
        sq = next((f["score"] for f in sig["factors"] if f["name"] == "squeeze_setup"), 0)
        if direction == "LONG" and td <= -55 and sq < 40:
            direction = "FLAT"
            gates.append("日線強勢空頭中不逆勢做多（軋空條件未成形）")
        if direction == "SHORT" and td >= 55 and sq > -40:
            direction = "FLAT"
            gates.append("日線強勢多頭中不逆勢做空（殺多條件未成形）")

    # 冷卻紀律：連續 2 筆停損 → 強制觀望一日
    if direction != "FLAT" and cooling_active(trades, ts_signal):
        direction = "FLAT"
        gates.append("紀律冷卻：連續 2 筆停損，今日強制觀望（防報復性交易）")

    # 總經事件閘門
    risk_mult = 1.0
    if direction != "FLAT" and macro["within_24h"] and abs(sig["score"]) < 45:
        direction = "FLAT"
        ev = "、".join(f"{e['name']} {e['date']}" for e in macro["events"][:2])
        gates.append(f"重大事件 24h 內（{ev}）→ 暫停新倉")
    elif direction != "FLAT" and macro["within_48h"]:
        risk_mult = 0.5
        gates.append("重大事件 48h 內 → 風險折半、槓桿減半")

    plan = None
    if direction != "FLAT":
        conf = sig["confidence"]
        risk = p["risk_pct_base"]
        if conf >= 80:
            risk = min(p["risk_pct_base"] * 1.5, 2.0)
        elif conf < 65:
            risk = p["risk_pct_base"] * 0.5
        risk = round(risk * risk_mult, 2)
        plan = P.build_plan(direction, D, t, sig, state, touch, risk)
        if plan["rr_to_res"] is not None and plan["rr_to_res"] < 1.6:
            sig["confidence"] = max(30, sig["confidence"] - 12)
        if sig["confidence"] < p["confidence_floor"]:
            gates.append(f"信心 {sig['confidence']:.0f} 低於門檻 {p['confidence_floor']} → 觀望"
                         + ("（近壓力空間不足）" if plan["warnings"] else ""))
            direction, plan = "FLAT", None

    watch = P.watch_conditions(sig) if direction == "FLAT" else []
    return {"sig": sig, "direction": direction, "plan": plan,
            "gates": gates, "macro": macro, "watch": watch, "signal_ts": ts_signal}
