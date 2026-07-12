"""多因子訊號引擎：每個因子輸出 -100~+100 分數（正=偏多）、繁中解讀、資料完整旗標。

因子設計對應研究文件之核心概念：
資金費率極值（逆向）、OI×價格矩陣、CVD 代理（主動買賣）、RVOL 量能、
長影線磁吸（CrypNuevo）、關鍵價位群／謝林點、軋空／殺多醞釀（吸收）、趨勢與動能。
"""
import numpy as np
import pandas as pd

from . import indicators as ind
from .util import DAY_MS


# ---------------------------------------------------------------- 主資料表
def build_master(m: dict, hist_csv=None) -> pd.DataFrame:
    """把 K 線與衍生品序列合併成單一日線主表（含所有預計算指標，皆因果）。"""
    D = m["d"].copy()
    D["date_ms"] = (D["ts"] // DAY_MS) * DAY_MS

    def attach(df, col, out, agg="last"):
        if df is None or not len(df):
            D[out] = np.nan
            return
        t = df.copy()
        t["date_ms"] = (t["ts"] // DAY_MS) * DAY_MS
        s = t.groupby("date_ms")[col].agg(agg)
        D[out] = D["date_ms"].map(s)

    attach(m.get("funding"), "rate", "funding")
    attach(m.get("oi"), "oi_usd", "oi")
    attach(m.get("lsr"), "ratio", "lsr")
    tk = m.get("taker")
    if tk is not None and len(tk):
        attach(tk, "buy", "tkr_buy")
        attach(tk, "sell", "tkr_sell")
    else:
        D["tkr_buy"] = np.nan
        D["tkr_sell"] = np.nan

    # 用累積的 history csv 往回補洞（同來源）
    if hist_csv is not None and len(hist_csv):
        h = hist_csv.set_index("date_ms")
        for src_col, dst in (("funding", "funding"), ("oi_usd", "oi"),
                             ("taker_buy", "tkr_buy"), ("taker_sell", "tkr_sell"), ("lsr", "lsr")):
            if src_col in h.columns:
                fill = D["date_ms"].map(h[src_col])
                D[dst] = D[dst].fillna(fill)

    # CVD 代理：優先 K 線內的 taker_buy（Binance），否則用衍生品 taker 買賣量
    if "taker_buy" in D.columns and D["taker_buy"].notna().sum() > 50:
        buy = D["taker_buy"]
        sell = (D["volume"] - D["taker_buy"]).clip(lower=1e-9)
    else:
        buy, sell = D["tkr_buy"], D["tkr_sell"]
    D["buy_frac"] = buy / (buy + sell)

    c = D["close"]
    D["e20"], D["e50"], D["e100"] = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 100)
    D["rsi"] = ind.rsi(c)
    D["atr"] = ind.atr(D)
    D["macd_h"] = ind.macd_hist(c)
    D["don20_hi"], D["don20_lo"] = ind.donchian(D, 20)
    D["don55_hi"], D["don55_lo"] = ind.donchian(D, 55)
    D["vol_sma20"] = D["volume"].rolling(20).mean().shift(1)
    D["fund_pct"] = ind.rolling_pct_rank(D["funding"], 180)
    D["oi_pct"] = ind.rolling_pct_rank(D["oi"], 90)
    bf5 = D["buy_frac"].rolling(5).mean()
    mu, sd = bf5.rolling(60).mean(), bf5.rolling(60).std()
    D["cvd_z"] = ((bf5 - mu) / sd.replace(0, np.nan)).clip(-3, 3)
    return D


def _f(name, label, score, note, ok=True, weight_key=None):
    return {"name": name, "label": label, "score": round(float(np.clip(score, -100, 100)), 1),
            "note": note, "ok": bool(ok)}


def _v(D, t, col):
    x = D[col].iloc[t]
    return None if pd.isna(x) else float(x)


# ---------------------------------------------------------------- 各因子
def f_trend_daily(D, t):
    c, e20, e50, e100 = (_v(D, t, "close"), _v(D, t, "e20"), _v(D, t, "e50"), _v(D, t, "e100"))
    a = _v(D, t, "atr") or 1
    if None in (c, e20, e50, e100):
        return _f("trend_daily", "日線趨勢", 0, "資料不足", ok=False)
    stack = 0.0
    stack += 0.5 if e20 > e50 else -0.5
    stack += 0.3 if e50 > e100 else -0.3
    stack += 0.2 if c > e20 else -0.2
    slope = (e20 - float(D["e20"].iloc[t - 5])) / (5 * a) if t >= 5 else 0
    slope = float(np.clip(slope * 2.5, -1, 1))
    hi, lo = _v(D, t, "don20_hi"), _v(D, t, "don20_lo")
    pos = 0.0
    if hi and lo and hi > lo:
        pos = float(np.clip((c - lo) / (hi - lo) * 2 - 1, -1, 1))
    score = 45 * stack + 35 * slope + 20 * pos
    desc = "多頭排列" if stack > 0.5 else ("空頭排列" if stack < -0.5 else "均線糾結")
    return _f("trend_daily", "日線趨勢", score,
              f"{desc}，EMA20 斜率 {slope * 100:+.0f}%（ATR 標準化），區間位置 {(pos + 1) / 2:.0%}")


def f_trend_4h(h4, t_close):
    if h4 is None or len(h4) < 80:
        return _f("trend_4h", "4小時結構", 0, "回測期無 4H 資料", ok=False)
    c = h4["close"]
    e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
    base = 40 if (e20.iloc[-1] > e50.iloc[-1] and c.iloc[-1] > e20.iloc[-1]) else \
           (-40 if (e20.iloc[-1] < e50.iloc[-1] and c.iloc[-1] < e20.iloc[-1]) else 0)
    piv = ind.pivots(h4.tail(60).reset_index(drop=True), 2)
    hh = [p for p in piv if p["kind"] == "H"]
    ll = [p for p in piv if p["kind"] == "L"]
    st = 0
    if len(hh) >= 2 and len(ll) >= 2:
        st += 15 if hh[-1]["price"] > hh[-2]["price"] else -15
        st += 15 if ll[-1]["price"] > ll[-2]["price"] else -15
    hi, lo = c.tail(90).max(), c.tail(90).min()
    pos = float(np.clip((c.iloc[-1] - lo) / max(hi - lo, 1e-9) * 2 - 1, -1, 1)) * 30
    note = ("4H 多頭結構" if base > 0 else "4H 空頭結構" if base < 0 else "4H 中性") + \
           ("，高低點墊高" if st > 0 else "，高低點下移" if st < 0 else "")
    return _f("trend_4h", "4小時結構", base + st + pos * 0.5, note)


def f_momentum(D, t):
    r, mh = _v(D, t, "rsi"), _v(D, t, "macd_h")
    if r is None:
        return _f("momentum", "動能", 0, "資料不足", ok=False)
    score = float(np.clip((r - 50) * 1.6, -45, 45))
    mh_prev = _v(D, t - 1, "macd_h") or 0
    if mh is not None:
        score += (15 if mh > 0 else -15) + (8 if mh > mh_prev else -8)
    tag = ""
    if r >= 78:
        score = min(score, 25)
        tag = "（超買，追多不利）"
    elif r <= 22:
        score = max(score, -25)
        tag = "（超賣，追空不利）"
    return _f("momentum", "動能", score, f"日 RSI {r:.0f}{tag}，MACD 柱{'走強' if mh and mh > mh_prev else '走弱'}")


def f_funding(D, t):
    fp, f_now = _v(D, t, "fund_pct"), _v(D, t, "funding")
    if fp is None or f_now is None:
        return _f("funding", "資金費率", 0, "無資金費率資料", ok=False)
    if fp >= 0.97:
        s = -70
    elif fp >= 0.92:
        s = -45
    elif fp >= 0.80:
        s = -15
    elif fp <= 0.03:
        s = 70
    elif fp <= 0.08:
        s = 45
    elif fp <= 0.20:
        s = 15
    else:
        s = 0
    apr = f_now * 3 * 365 * 100
    crowd = "多頭擁擠，逆風" if s < 0 else ("空頭擁擠，軋空燃料" if s > 0 else "中性")
    return _f("funding", "資金費率", s,
              f"費率 {f_now * 100:.4f}%（年化 {apr:+.0f}%），180 日分位 {fp:.0%} → {crowd}")


def f_oi_price(D, t):
    if t < 4:
        return _f("oi_price", "OI×價格", 0, "資料不足", ok=False)
    oi_now, oi_prev = _v(D, t, "oi"), _v(D, t - 3, "oi")
    if not oi_now or not oi_prev:
        return _f("oi_price", "OI×價格", 0, "無未平倉量資料", ok=False)
    dp = _v(D, t, "close") / _v(D, t - 3, "close") - 1
    doi = oi_now / oi_prev - 1
    P, O = 0.007, 0.015
    if dp > P and doi > O:
        s, txt = 45, "增倉上漲：新資金進場，趨勢健康"
    elif dp > P and doi < -O:
        s, txt = -10, "縮倉上漲：空頭回補推動，續航力存疑"
    elif dp < -P and doi > O:
        s, txt = -45, "增倉下跌：空方主動進攻"
    elif dp < -P and doi < -O:
        s, txt = 15, "縮倉下跌：多頭去槓桿近尾聲"
    else:
        s, txt = 0, "價格與 OI 皆窄幅，觀察"
    op = _v(D, t, "oi_pct")
    if op is not None and op > 0.92 and dp > 0:
        s -= 12
        txt += "；OI 位於 90 日高分位，槓桿擁擠"
    return _f("oi_price", "OI×價格", s, f"3日 價格{dp * 100:+.1f}%／OI {doi * 100:+.1f}% → {txt}")


def f_taker_flow(D, t):
    z = _v(D, t, "cvd_z")
    if z is None:
        return _f("taker_flow", "主動買賣流(CVD)", 0, "無主動買賣量資料", ok=False)
    dp5 = _v(D, t, "close") / _v(D, t - 5, "close") - 1 if t >= 5 else 0
    s = float(np.clip(z * 22, -45, 45))
    div = ""
    if z > 0.5 and dp5 < -0.01:
        div = "；價跌但主動買盤增強（吸籌跡象）"
    elif z < -0.5 and dp5 > 0.01:
        div = "；價漲但主動買盤轉弱（背離警訊）"
    return _f("taker_flow", "主動買賣流(CVD)", s, f"5日主動買盤 z={z:+.1f}{div}")


def f_rvol(D, t):
    v, vs = _v(D, t, "volume"), _v(D, t, "vol_sma20")
    if not v or not vs:
        return _f("rvol", "相對量能", 0, "資料不足", ok=False)
    rv = v / vs
    o, c = _v(D, t, "open"), _v(D, t, "close")
    if rv >= 1.6:
        s = 25 if c > o else -25
        txt = f"RVOL {rv:.1f}×放量{'收漲' if c > o else '收跌'}，方向可信度高"
    elif rv < 0.7:
        s, txt = 0, f"RVOL {rv:.1f}× 量能低迷，突破易假"
    else:
        s, txt = (8 if c > o else -8), f"RVOL {rv:.1f}× 正常量能"
    return _f("rvol", "相對量能", s, txt)


def f_wick_magnet(D, t, wicks):
    c = _v(D, t, "close")
    a = _v(D, t, "atr") or 1
    unfilled = [w for w in wicks if not w["filled"] and w["i"] < t]
    above = [w for w in unfilled if w["side"] == "up" and 0 < (w["mid"] - c) < 3 * a]
    below = [w for w in unfilled if w["side"] == "down" and 0 < (c - w["mid"]) < 3 * a]
    s = 12 * min(len(above), 2) - 12 * min(len(below), 2)
    parts = []
    if above:
        parts.append(f"上方未回補影線 {len(above)} 條（磁吸目標 ~{max(w['mid'] for w in above):,.0f}）")
    if below:
        parts.append(f"下方未回補影線 {len(below)} 條（磁吸 ~{min(w['mid'] for w in below):,.0f}）")
    return _f("wick_magnet", "影線磁吸", s, "；".join(parts) if parts else "近期無顯著未回補長影線")


def f_levels(D, t, clusters):
    c = _v(D, t, "close")
    a = _v(D, t, "atr") or 1
    sups = [x for x in clusters if x["price"] < c]
    ress = [x for x in clusters if x["price"] > c]
    s, notes = 0.0, []
    if ress:
        near_r = ress[0]
        dr = (near_r["price"] - c) / a
        if dr < 0.6 and near_r["strength"] >= 2:
            s -= 20
            notes.append(f"壓力 {near_r['price']:,.0f} 貼臉（{near_r['strength']} 重合流）")
    if sups:
        near_s = sups[-1]
        ds = (c - near_s["price"]) / a
        if ds < 0.6 and near_s["strength"] >= 2:
            s += 15
            notes.append(f"支撐 {near_s['price']:,.0f} 在腳下（{near_s['strength']} 重合流）")
    # 昨日→今日是否放量穿越價位群（突破確認）
    if t >= 1:
        c_prev = _v(D, t - 1, "close")
        rv = (_v(D, t, "volume") or 0) / max(_v(D, t, "vol_sma20") or 1e9, 1e-9)
        for cl in clusters:
            if cl["strength"] >= 2 and c_prev < cl["price"] <= c and rv > 1.3:
                s += 25
                notes.append(f"放量突破 {cl['price']:,.0f}")
                break
            if cl["strength"] >= 2 and c_prev > cl["price"] >= c and rv > 1.3:
                s -= 25
                notes.append(f"放量跌破 {cl['price']:,.0f}")
                break
    return _f("levels", "關鍵價位", float(np.clip(s, -40, 40)), "；".join(notes) if notes else "距主要價位群尚有空間")


def f_squeeze(D, t):
    fp = _v(D, t, "fund_pct")
    if fp is None or t < 10:
        return _f("squeeze_setup", "軋空/殺多醞釀", 0, "資料不足", ok=False)
    oi_now, oi_prev = _v(D, t, "oi"), _v(D, t - 10, "oi")
    doi = (oi_now / oi_prev - 1) if (oi_now and oi_prev) else 0
    dp10 = _v(D, t, "close") / _v(D, t - 10, "close") - 1
    z = _v(D, t, "cvd_z") or 0
    s, txt = 0.0, "無明顯擁擠結構"
    if fp <= 0.15 and doi > 0.03 and abs(dp10) < 0.03:
        s = 40 + (15 if z < -0.3 else 0)
        txt = "空頭付費增倉但價格拒跌 → 軋空醞釀（賣壓被吸收）"
    elif fp >= 0.85 and doi > 0.03 and abs(dp10) < 0.03:
        s = -40 - (15 if z > 0.3 else 0)
        txt = "多頭付費增倉但價格滯漲 → 多殺多風險（買盤被吸收）"
    return _f("squeeze_setup", "軋空/殺多醞釀", s, txt)


# ---------------------------------------------------------------- 價位群建構
def build_clusters(D, t, wicks) -> list[dict]:
    c = float(D["close"].iloc[t])
    raw = []
    for lv in ind.round_levels(c):
        if abs(lv - c) / c < 0.12:
            raw.append({"price": lv, "src": "整數關卡"})
    piv = [p for p in ind.pivots(D.iloc[max(0, t - 120):t + 1].reset_index(drop=True), 2)
           if p["confirm"] <= t - max(0, t - 120)]
    for p in piv:
        raw.append({"price": p["price"], "src": "擺動高點" if p["kind"] == "H" else "擺動低點"})
    for col, name in (("don20_hi", "20日高"), ("don20_lo", "20日低"),
                      ("don55_hi", "55日高"), ("don55_lo", "55日低")):
        v = _v(D, t, col)
        if v:
            raw.append({"price": v, "src": name})
    if t >= 1:
        raw.append({"price": float(D["high"].iloc[t]), "src": "當日高"})
        raw.append({"price": float(D["low"].iloc[t]), "src": "當日低"})
    for w in wicks:
        if not w["filled"]:
            raw.append({"price": w["mid"], "src": "影線50%"})
    clusters = ind.cluster_levels(raw)
    return [x for x in clusters if abs(x["price"] - c) / c < 0.15]


# ---------------------------------------------------------------- 綜合
FACTOR_FNS = ["trend_daily", "trend_4h", "momentum", "funding", "oi_price",
              "taker_flow", "rvol", "wick_magnet", "levels", "squeeze_setup"]


def compute_signal(D, t, weights: dict, h4=None) -> dict:
    wicks = ind.find_wicks(D, D["atr"], t)
    clusters = build_clusters(D, t, wicks)
    facs = [
        f_trend_daily(D, t),
        f_trend_4h(h4, None),
        f_momentum(D, t),
        f_funding(D, t),
        f_oi_price(D, t),
        f_taker_flow(D, t),
        f_rvol(D, t),
        f_wick_magnet(D, t, wicks),
        f_levels(D, t, clusters),
        f_squeeze(D, t),
    ]
    ok = [f for f in facs if f["ok"]]
    wsum = sum(weights.get(f["name"], 1.0) for f in ok) or 1.0
    score = sum(f["score"] * weights.get(f["name"], 1.0) for f in ok) / wsum
    nonzero = [f for f in ok if abs(f["score"]) >= 8]
    agree = 0.5
    if nonzero:
        pos = sum(1 for f in nonzero if f["score"] > 0)
        agree = max(pos, len(nonzero) - pos) / len(nonzero)
    missing = len(facs) - len(ok)
    # 信心量程：門檻級分數(±20)≈60、強共振(±40)≈80、極端(±55)≈90，
    # 讓「加碼(≥75)／減碼(<62)／反向平倉(≥70)」的分級門檻真正用得到
    conf = 45 + 0.8 * abs(score) + 16 * (agree - 0.5) * 2 - 3 * missing
    conf = float(np.clip(conf, 30, 95))
    return {"score": round(float(score), 1), "confidence": round(conf, 0),
            "factors": facs, "agree": round(agree, 2), "missing": missing,
            "clusters": clusters, "wicks": [w for w in wicks if not w["filled"]]}
