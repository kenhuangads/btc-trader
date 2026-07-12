"""決策層：因子綜合 → 紀律閘門（逆勢、冷卻、總經事件、信心門檻）→ 交易計畫。

回測與每日實盤共用同一條路徑，確保復盤統計與線上推薦口徑一致。
"""
from .util import DAY_MS
from . import factors as F
from . import planner as P
from .macro import macro_gate
from .review import cooling_active

H4_MS = DAY_MS // 6


def drawdown_governor(trades: list[dict]) -> float:
    """回撤保護：近 10 筆已結案累計 < -3R → 風險縮到 0.6 倍（隨窗口滾動自動恢復）。
    專家共識（Tharp/Druckenmiller）：手感最差的時候部位要最小。"""
    closed = sorted([t for t in trades if t["status"] == "closed" and t["r"] is not None],
                    key=lambda x: x.get("exit_ts", 0))[-10:]
    if len(closed) >= 5 and sum(t["r"] for t in closed) < -3.0:
        return 0.6
    return 1.0


def decide(D, t: int, state: dict, trades: list[dict], touch: dict, h4=None) -> dict:
    p = state["params"]
    ts_signal = int(D["ts"].iloc[t]) + DAY_MS
    # 4H 資料因果切片：僅保留訊號時間點前已收盤的 4H K（回測與實盤同一條規則）
    if h4 is not None and len(h4):
        h4 = h4[h4["ts"] + H4_MS <= ts_signal]
        if len(h4) < 80:
            h4 = None
    sig = F.compute_signal(D, t, state["weights"], h4=h4)
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
        if conf >= 75:
            risk = min(p["risk_pct_base"] * 1.3, 2.0)
        elif conf < 62:
            risk = p["risk_pct_base"] * 0.7
        gov = drawdown_governor(trades)
        if gov < 1.0:
            gates.append(f"回撤保護：近 10 筆累計虧損超過 3R → 風險降至 {gov:.0%}（恢復前縮小部位）")
        risk = round(risk * risk_mult * gov, 2)
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
