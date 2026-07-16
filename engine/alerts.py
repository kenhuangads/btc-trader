"""盤中機會提醒：每 4h 執行時，掃描「兩次日訊號之間發生、值得使用者現在關注」的事件。

純衍生自現有資料（1H K 線、關鍵價位群、在途單、資金費率分位），無額外狀態、冪等——
同一事件在同一根 1H／同一日內只會呈現一次，隨時間自然過期。

提醒僅顯示於儀表板：靜態站沒有伺服器可主動推播；真正的手機推播需要後端服務，暫不提供。
"""
import numpy as np

H_MS = 3_600_000


def _recent_h1(h1, now: int, hours: int) -> list:
    """回傳最近 hours 小時內、已收盤的 1H K（由舊到新）。"""
    if h1 is None or not len(h1):
        return []
    sub = h1[(h1["ts"] >= now - hours * H_MS) & (h1["ts"] + H_MS <= now)]
    return list(sub[["ts", "open", "high", "low", "close"]].itertuples(index=False))


def build_alerts(D, t: int, sig: dict, trades: list, m: dict, now: int) -> list:
    """回傳提醒清單（機會在前、留意其次；同級新事件在前），最多 5 則。

    每則：{kind, level: opp|watch|info, icon, title, detail, ts, ongoing?}
    """
    out: list[dict] = []
    close = float(D["close"].iloc[t])
    atr = float(D["atr"].iloc[t]) if not np.isnan(D["atr"].iloc[t]) else None
    rows = _recent_h1(m.get("h1"), now, 10)

    # ---------- 1) 持倉事件（最貼身）----------
    for tr in trades:
        if tr["status"] not in ("pending", "open"):
            continue
        dw = "做多" if tr["direction"] == "LONG" else "做空"
        reached_tp1 = (tr.get("tp_done", [False])[0]
                       or any(e["reason"] == "tp1" for e in tr.get("exits", [])))
        if tr["status"] == "open" and reached_tp1:
            out.append({"kind": "trade", "level": "opp", "icon": "🎯", "ongoing": True,
                        "ts": tr.get("exit_ts") or now,
                        "title": f"{dw}單已達第一目標",
                        "detail": "已平出一部分並把停損移到保本——這筆已立於不敗，剩餘倉位讓利潤奔跑。"})
        elif tr["status"] == "pending" and rows and tr.get("plan", {}).get("entries"):
            ents = [e["price"] for e in tr["plan"]["entries"]]
            edge = max(ents) if tr["direction"] == "LONG" else min(ents)
            touched = any((r.low <= edge) if tr["direction"] == "LONG" else (r.high >= edge)
                          for r in rows)
            if touched:
                out.append({"kind": "trade", "level": "opp", "icon": "🟢", "ongoing": True,
                            "ts": int(rows[-1].ts),
                            "title": f"{dw}掛單區已被觸及",
                            "detail": f"價格已來到你的{dw}掛單區（約 {edge:,.0f}）——請到交易所確認是否成交、"
                                      "停損停利是否都設好。"})

    # ---------- 2) 盤中插針測試關鍵價位（做多／做空埋伏區）----------
    if atr and rows:
        strong = [c for c in sig.get("clusters", [])
                  if c.get("strength", 0) >= 3 and abs(c["price"] - close) / close < 0.05]
        sup = sorted([c for c in strong if c["price"] <= close * 1.002], key=lambda c: -c["strength"])
        res = sorted([c for c in strong if c["price"] >= close * 0.998], key=lambda c: -c["strength"])
        for c in sup[:1]:
            hit = next((r for r in reversed(rows)
                        if r.low <= c["price"] and r.close >= c["price"] + 0.15 * atr), None)
            if hit:
                out.append({"kind": "level", "level": "opp", "icon": "🟢", "ts": int(hit.ts),
                            "title": f"插針測試支撐 {c['price']:,.0f} 後拉回",
                            "detail": f"{c['strength']} 條線重疊的支撐被下影線測試又收回——做多埋伏區，"
                                      "留意下方掛單是否被吸引成交。"})
        for c in res[:1]:
            hit = next((r for r in reversed(rows)
                        if r.high >= c["price"] and r.close <= c["price"] - 0.15 * atr), None)
            if hit:
                out.append({"kind": "level", "level": "opp", "icon": "🔴", "ts": int(hit.ts),
                            "title": f"插針測試壓力 {c['price']:,.0f} 後回落",
                            "detail": f"{c['strength']} 條線重疊的壓力被上影線測試又壓回——反彈做空的埋伏區，留意。"})

    # ---------- 3) 資金費率極端（人群擁擠度；門檻對齊因子的強分位帶 0.92／0.08）----------
    fp = D["fund_pct"].iloc[t]
    fr = D["funding"].iloc[t]
    if not np.isnan(fp):
        apr_txt = f"，年化約 {float(fr) * 3 * 365 * 100:+.0f}%" if not np.isnan(fr) else ""
        if fp >= 0.92:
            out.append({"kind": "funding", "level": "watch", "icon": "🔥", "ts": int(D["ts"].iloc[t]),
                        "title": f"資金費率偏高（180 日 {fp:.0%} 分位）",
                        "detail": f"做多的人過度擁擠、付費撐倉{apr_txt}——一旦鬆動易引發回檔／軋多，"
                                  "偏空訊號可信度提高。"})
        elif fp <= 0.08:
            out.append({"kind": "funding", "level": "watch", "icon": "🧊", "ts": int(D["ts"].iloc[t]),
                        "title": f"資金費率偏低／轉負（180 日 {fp:.0%} 分位）",
                        "detail": f"做空的人過度擁擠{apr_txt}——空頭燃料充足，易引發軋空反彈，偏多訊號可信度提高。"})

    order = {"opp": 0, "watch": 1, "info": 2}
    out.sort(key=lambda a: (order.get(a["level"], 3), -(a.get("ts") or 0)))
    return out[:5]
