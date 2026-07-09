"""技術指標（皆為因果／不偷看未來）。輸入為 pandas Series/DataFrame（升冪時間）。"""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    dn = (-delta).clip(lower=0.0)
    # Wilder 平滑
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def macd_hist(close: pd.Series, fast=12, slow=26, sig=9) -> pd.Series:
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False).mean()
    return line - signal


def donchian(df: pd.DataFrame, n: int) -> tuple[pd.Series, pd.Series]:
    """回傳 (最高, 最低)，不含當前K棒（shift 1，避免自我參照）。"""
    hh = df["high"].rolling(n).max().shift(1)
    ll = df["low"].rolling(n).min().shift(1)
    return hh, ll


def rolling_pct_rank(s: pd.Series, n: int) -> pd.Series:
    """滾動視窗內的分位（0~1），因果。"""
    def _rank(w):
        return (w[:-1] <= w[-1]).mean() if len(w) > 1 else 0.5
    return s.rolling(n, min_periods=max(10, n // 4)).apply(_rank, raw=True)


def pivots(df: pd.DataFrame, k: int = 2) -> list[dict]:
    """分形擺動點：i 的高點高於左右各 k 根 → 'H'；低點同理 → 'L'。
    確認時間 = i+k（因果使用時需 t >= i+k）。"""
    hs, ls = df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for i in range(k, len(df) - k):
        win_h = hs[i - k:i + k + 1]
        win_l = ls[i - k:i + k + 1]
        if hs[i] == win_h.max() and (win_h.argmax() == k):
            out.append({"i": i, "price": float(hs[i]), "kind": "H", "confirm": i + k})
        if ls[i] == win_l.min() and (win_l.argmin() == k):
            out.append({"i": i, "price": float(ls[i]), "kind": "L", "confirm": i + k})
    return out


def round_levels(price: float) -> list[float]:
    """謝林點：整數關卡。依價位量級取主要/次要網格（BTC 10 萬級 → 5000/1000）。"""
    import math
    mag = 10 ** math.floor(math.log10(max(price, 1)))
    major = mag / 2 / 10  # 100k -> 5000
    levels = set()
    for step in (major, major / 5):
        base = math.floor(price / step) * step
        for j in range(-3, 4):
            levels.add(round(base + j * step, 2))
    return sorted(levels)


def find_wicks(df: pd.DataFrame, atr_s: pd.Series, t: int, lookback: int = 40, min_atr: float = 1.1) -> list[dict]:
    """掃描 t 往回 lookback 根的長影線；判斷 50% 回補位是否已被後續價格觸及（截至 t）。"""
    out = []
    lo_i = max(1, t - lookback)
    for i in range(lo_i, t + 1):
        o, h, l, c = (float(df["open"].iloc[i]), float(df["high"].iloc[i]),
                      float(df["low"].iloc[i]), float(df["close"].iloc[i]))
        a = float(atr_s.iloc[i]) if not np.isnan(atr_s.iloc[i]) else None
        if not a or a <= 0:
            continue
        body_hi, body_lo = max(o, c), min(o, c)
        up_w, dn_w = h - body_hi, body_lo - l
        for side, wick, tip, base in (("up", up_w, h, body_hi), ("down", dn_w, l, body_lo)):
            if wick < min_atr * a:
                continue
            mid = (tip + base) / 2
            filled = False
            if i < t:
                seg = df.iloc[i + 1:t + 1]
                filled = bool((seg["high"] >= mid).any()) if side == "up" else bool((seg["low"] <= mid).any())
            out.append({"i": i, "ts": int(df["ts"].iloc[i]), "side": side,
                        "tip": float(tip), "mid": float(mid), "atr_mult": round(wick / a, 2),
                        "filled": filled})
    return out


def cluster_levels(raw: list[dict], tol_frac: float = 0.006) -> list[dict]:
    """把相近價位合併成價位群（strength = 來源數）。raw: [{price, src}]"""
    if not raw:
        return []
    raw = sorted(raw, key=lambda x: x["price"])
    clusters = []
    cur = [raw[0]]
    for item in raw[1:]:
        if item["price"] <= cur[-1]["price"] * (1 + tol_frac):
            cur.append(item)
        else:
            clusters.append(cur)
            cur = [item]
    clusters.append(cur)
    out = []
    for c in clusters:
        price = float(np.mean([x["price"] for x in c]))
        srcs = sorted({x["src"] for x in c})
        out.append({"price": price, "strength": len(c), "srcs": srcs})
    return out
