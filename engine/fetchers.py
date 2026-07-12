"""多來源市場資料抓取（Binance → OKX → Bybit 自動容錯）。

K 線來源優先 Binance（含 taker 買量 → 全歷史 CVD 代理）；
衍生品數據（資金費率/OI/主動買賣/多空比）優先 OKX——
GitHub Actions 美國節點無法連 Binance 合約 API，OKX 在台灣與 CI 都通，
固定 OKX 為主可避免不同來源數據混用造成分位數失真。
"""
import time

import numpy as np
import pandas as pd

from .util import DAY_MS, HISTORY, http_get, log, now_ms, ts_to_date

SLEEP = 0.25  # 禮貌性間隔（OKX rubik 限速 5req/2s）


def _df(rows, cols):
    df = pd.DataFrame(rows, columns=cols)
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = df["ts"].astype("int64")
    df = df.dropna().drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def _drop_unclosed(df, interval_ms):
    return df[df["ts"] + interval_ms <= now_ms()].reset_index(drop=True)


# ---------------------------------------------------------------- Binance
BN = "https://fapi.binance.com"


def bn_klines(interval: str, interval_ms: int, bars: int) -> pd.DataFrame:
    out, end = [], None
    while len(out) < bars:
        p = {"symbol": "BTCUSDT", "interval": interval, "limit": min(1500, bars - len(out))}
        if end:
            p["endTime"] = end
        rows = http_get(f"{BN}/fapi/v1/klines", p)
        if not rows:
            break
        out = rows + out
        end = rows[0][0] - 1
        if len(rows) < p["limit"]:
            break
        time.sleep(SLEEP)
    rows = [[r[0], r[1], r[2], r[3], r[4], r[5], r[9]] for r in out]
    df = _df(rows, ["ts", "open", "high", "low", "close", "volume", "taker_buy"])
    return _drop_unclosed(df, interval_ms)


def bn_funding(days: int) -> pd.DataFrame:
    out, end = [], None
    need = days * 3 + 10
    while len(out) < need:
        p = {"symbol": "BTCUSDT", "limit": 1000}
        if end:
            p["endTime"] = end
        rows = http_get(f"{BN}/fapi/v1/fundingRate", p)
        if not rows:
            break
        out = rows + out
        end = rows[0]["fundingTime"] - 1
        if len(rows) < 1000:
            break
        time.sleep(SLEEP)
    return _df([[r["fundingTime"], r["fundingRate"]] for r in out], ["ts", "rate"])


def bn_derivs(days: int, px: pd.DataFrame) -> dict:
    fund = bn_funding(days)
    oi = http_get(f"{BN}/futures/data/openInterestHist",
                  {"symbol": "BTCUSDT", "period": "1d", "limit": 500})
    oi_df = _df([[r["timestamp"], r["sumOpenInterestValue"]] for r in oi], ["ts", "oi_usd"])
    tk = http_get(f"{BN}/futures/data/takerlongshortRatio",
                  {"symbol": "BTCUSDT", "period": "1d", "limit": 500})
    tk_df = _df([[r["timestamp"], r["buyVol"], r["sellVol"]] for r in tk], ["ts", "buy", "sell"])
    ls = http_get(f"{BN}/futures/data/globalLongShortAccountRatio",
                  {"symbol": "BTCUSDT", "period": "1d", "limit": 500})
    ls_df = _df([[r["timestamp"], r["longShortRatio"]] for r in ls], ["ts", "ratio"])
    return {"funding": fund, "oi": oi_df, "taker": tk_df, "lsr": ls_df, "src": "binance"}


# ---------------------------------------------------------------- OKX
OKX = "https://www.okx.com"


def _okx(path, params):
    j = http_get(f"{OKX}{path}", params)
    if str(j.get("code")) != "0":
        raise RuntimeError(f"okx {path}: {j.get('msg')}")
    return j["data"]


def okx_klines(bar: str, interval_ms: int, bars: int) -> pd.DataFrame:
    inst = "BTC-USDT-SWAP"
    rows = _okx("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": 300})
    out = list(rows)
    while len(out) < bars and rows:
        oldest = out[-1][0]
        time.sleep(SLEEP)
        rows = _okx("/api/v5/market/history-candles",
                    {"instId": inst, "bar": bar, "limit": 100, "after": oldest})
        out += rows
    # 僅保留已確認 K 棒（confirm 欄位為最後一欄 "1"）
    keep = [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in out if r[-1] == "1"]
    return _df(keep, ["ts", "open", "high", "low", "close", "volume"])


def okx_derivs(days: int, px: pd.DataFrame) -> dict:
    inst = "BTC-USDT-SWAP"
    # 資金費率（8 小時一筆，newest first，after 往更舊翻頁）
    rows = _okx("/api/v5/public/funding-rate-history", {"instId": inst, "limit": 100})
    out = list(rows)
    while len(out) < days * 3 + 10 and rows:
        time.sleep(SLEEP)
        rows = _okx("/api/v5/public/funding-rate-history",
                    {"instId": inst, "limit": 100, "after": out[-1]["fundingTime"]})
        if not rows:
            break
        out += rows
    fund = _df([[r["fundingTime"], r["fundingRate"]] for r in out], ["ts", "rate"])

    def rubik(path, extra, cols):
        rows, out2 = None, []
        end = None
        for _ in range(12):
            p = {"period": "1D", **extra}
            if end:
                p["end"] = end
            time.sleep(SLEEP)
            rows = _okx(path, p)
            if not rows:
                break
            out2 += rows
            end = str(int(rows[-1][0]) - 1)
            if len(out2) >= days or len(rows) < 2:
                break
        return _df(out2, cols)

    oi = rubik("/api/v5/rubik/stat/contracts/open-interest-volume",
               {"ccy": "BTC"}, ["ts", "oi_usd", "vol"])[["ts", "oi_usd"]]
    # taker-volume 回傳順序為 [ts, 賣量, 買量]
    tk = rubik("/api/v5/rubik/stat/taker-volume",
               {"ccy": "BTC", "instType": "CONTRACTS"}, ["ts", "sell", "buy"])[["ts", "buy", "sell"]]
    ls = rubik("/api/v5/rubik/stat/contracts/long-short-account-ratio",
               {"ccy": "BTC"}, ["ts", "ratio"])
    return {"funding": fund, "oi": oi, "taker": tk, "lsr": ls, "src": "okx"}


# ---------------------------------------------------------------- Bybit
BB = "https://api.bybit.com"


def _bb(path, params):
    j = http_get(f"{BB}{path}", params)
    if j.get("retCode") != 0:
        raise RuntimeError(f"bybit {path}: {j.get('retMsg')}")
    return j["result"]


def bb_klines(interval: str, interval_ms: int, bars: int) -> pd.DataFrame:
    out, end = [], None
    while len(out) < bars:
        p = {"category": "linear", "symbol": "BTCUSDT", "interval": interval,
             "limit": min(1000, bars - len(out))}
        if end:
            p["end"] = end
        rows = _bb("/v5/market/kline", p)["list"]  # newest first
        if not rows:
            break
        out += rows
        end = int(rows[-1][0]) - 1
        if len(rows) < p["limit"]:
            break
        time.sleep(SLEEP)
    df = _df([[r[0], r[1], r[2], r[3], r[4], r[5]] for r in out],
             ["ts", "open", "high", "low", "close", "volume"])
    return _drop_unclosed(df, interval_ms)


def bb_derivs(days: int, px: pd.DataFrame) -> dict:
    out, end = [], None
    while len(out) < days * 3 + 10:
        p = {"category": "linear", "symbol": "BTCUSDT", "limit": 200}
        if end:
            p["endTime"] = end
        rows = _bb("/v5/market/funding/history", p)["list"]
        if not rows:
            break
        out += rows
        end = int(rows[-1]["fundingRateTimestamp"]) - 1
        if len(rows) < 200:
            break
        time.sleep(SLEEP)
    fund = _df([[r["fundingRateTimestamp"], r["fundingRate"]] for r in out], ["ts", "rate"])

    oi_rows, cursor = [], None
    for _ in range(10):
        p = {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1d", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        res = _bb("/v5/market/open-interest", p)
        oi_rows += res["list"]
        cursor = res.get("nextPageCursor")
        if not cursor or len(oi_rows) >= days:
            break
        time.sleep(SLEEP)
    oi = _df([[r["timestamp"], r["openInterest"]] for r in oi_rows], ["ts", "oi_btc"])
    # 轉為 USD 名目（用當日收盤近似）
    px_map = px.set_index((px["ts"] // DAY_MS) * DAY_MS)["close"]
    key = (oi["ts"] // DAY_MS) * DAY_MS
    oi["oi_usd"] = oi["oi_btc"].to_numpy() * key.map(px_map).to_numpy()
    oi = oi.dropna()[["ts", "oi_usd"]].reset_index(drop=True)

    ls_rows = _bb("/v5/market/account-ratio",
                  {"category": "linear", "symbol": "BTCUSDT", "period": "1d", "limit": 500})["list"]
    ls = _df([[r["timestamp"], float(r["buyRatio"]) / max(float(r["sellRatio"]), 1e-9)]
              for r in ls_rows], ["ts", "ratio"])
    return {"funding": fund, "oi": oi, "taker": pd.DataFrame(columns=["ts", "buy", "sell"]),
            "lsr": ls, "src": "bybit"}


# ---------------------------------------------------------------- 統整
def fetch_all(days_daily: int = 980) -> dict:
    """回傳 {d, h4, h1, funding, oi, taker, lsr, src_klines, src_deriv}。

    日線視窗 980 天：扣掉 240 天暖機後，回測樣本約 2 年（涵蓋多空循環各段）。
    4H 抓 4500 根（約 750 天）讓回測期的 4H 結構因子與實盤口徑一致。
    """
    d = h4 = h1 = None
    src_k = None
    for name, fn in (("binance", bn_klines), ("okx", okx_klines), ("bybit", bb_klines)):
        try:
            iv = {"binance": ("1d", "4h", "1h"), "okx": ("1Dutc", "4H", "1H"),
                  "bybit": ("D", "240", "60")}[name]
            d = fn(iv[0], DAY_MS, days_daily)
            h4 = fn(iv[1], DAY_MS // 6, 4500)
            h1 = fn(iv[2], DAY_MS // 24, 500)
            src_k = name
            log(f"K線來源: {name}（日線 {len(d)} 根）")
            break
        except Exception as e:  # noqa: BLE001
            log(f"K線來源 {name} 失敗: {e}")
    if d is None or len(d) < 260:
        raise RuntimeError("所有 K 線來源均失敗")

    deriv = None
    for name, fn in (("okx", okx_derivs), ("bybit", bb_derivs), ("binance", bn_derivs)):
        try:
            deriv = fn(days_daily, d)
            log(f"衍生品數據來源: {name}（funding {len(deriv['funding'])} 筆 / OI {len(deriv['oi'])} 天 / "
                f"taker {len(deriv['taker'])} 天 / LSR {len(deriv['lsr'])} 天）")
            break
        except Exception as e:  # noqa: BLE001
            log(f"衍生品來源 {name} 失敗: {e}")
    if deriv is None:
        deriv = {"funding": pd.DataFrame(columns=["ts", "rate"]),
                 "oi": pd.DataFrame(columns=["ts", "oi_usd"]),
                 "taker": pd.DataFrame(columns=["ts", "buy", "sell"]),
                 "lsr": pd.DataFrame(columns=["ts", "ratio"]), "src": "none"}

    return {"d": d, "h4": h4, "h1": h1, **deriv,
            "src_klines": src_k, "src_deriv": deriv["src"]}


def update_history_csv(m: dict) -> None:
    """把衍生品日資料累積到 history/derivatives.csv（repo 自身成為長期資料庫）。"""
    HISTORY.mkdir(exist_ok=True)
    path = HISTORY / "derivatives.csv"
    fund = m["funding"].copy()
    if len(fund):
        fund["date"] = (fund["ts"] // DAY_MS) * DAY_MS
        fund_d = fund.groupby("date")["rate"].last()
    else:
        fund_d = pd.Series(dtype=float)

    def daily_map(df, col):
        if not len(df):
            return pd.Series(dtype=float)
        t = df.copy()
        t["date"] = (t["ts"] // DAY_MS) * DAY_MS
        return t.groupby("date")[col].last()

    frames = {"funding": fund_d, "oi_usd": daily_map(m["oi"], "oi_usd"),
              "taker_buy": daily_map(m["taker"], "buy") if len(m["taker"]) else pd.Series(dtype=float),
              "taker_sell": daily_map(m["taker"], "sell") if len(m["taker"]) else pd.Series(dtype=float),
              "lsr": daily_map(m["lsr"], "ratio")}
    new = pd.DataFrame(frames)
    new.index.name = "date_ms"
    new = new.reset_index()
    new["date"] = new["date_ms"].map(ts_to_date)
    new["source"] = m["src_deriv"]
    if path.exists():
        old = pd.read_csv(path)
        merged = pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=["date", "source"], keep="last")
    else:
        merged = new
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(path, index=False)


def load_history_csv(src: str) -> pd.DataFrame | None:
    path = HISTORY / "derivatives.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df = df[df["source"] == src]
    return df if len(df) else None
