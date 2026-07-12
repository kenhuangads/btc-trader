"""走勢前推（walk-forward）回測：逐日重播訊號與交易生命週期，零未來資訊。

用途：首次部署時建立基準統計、初始化因子權重與觸價機率曲線，
讓儀表板第一天就有有意義的復盤數據（明確標記為「回測」樣本）。
以日 K 保守模擬（同棒先進場後停損），實盤追蹤則用 1h K 精確重播。
"""
import numpy as np

from . import review as R
from . import signalgen as SG
from .util import DAY_MS, log, ts_to_date

WARMUP = 240


def run_backtest(D, state: dict, touch: dict, h4=None,
                 funding_by_day: dict | None = None) -> tuple[list[dict], list[dict]]:
    trades: list[dict] = []
    factor_history: list[dict] = []
    atr_by_day = {int(ts): float(a) for ts, a in zip(D["ts"], D["atr"]) if not np.isnan(a)}
    bar_cols = ["ts", "open", "high", "low", "close"]

    for t in range(WARMUP, len(D)):
        bar = D.iloc[[t]][bar_cols]
        for tr in trades:
            if tr["status"] in ("pending", "open"):
                R.process_trade(tr, bar, atr_by_day, state["params"], funding_by_day)
                if tr["status"] in ("closed", "cancelled"):
                    R.add_lessons(tr, D)

        res = SG.decide(D, t, state, trades, touch, h4=h4)
        factor_history.append({"date": ts_to_date(res["signal_ts"]), "ts": int(D["ts"].iloc[t]),
                               "scores": {f["name"]: f["score"] for f in res["sig"]["factors"] if f["ok"]}})
        has_active = any(tr["status"] in ("pending", "open") for tr in trades)
        if res["direction"] != "FLAT" and not has_active:
            trades.append(R.new_trade(res["plan"], res["sig"], "backtest"))
        elif res["direction"] != "FLAT" and has_active:
            # 反向強訊號 → 標記在倉單提前離場（下一根開盤）
            for tr in trades:
                if tr["status"] == "open" and tr["direction"] != res["direction"] \
                        and res["sig"]["confidence"] >= 70:
                    tr["force_exit"] = True

    for tr in trades:
        R.add_lessons(tr, D)
    n_closed = sum(1 for t in trades if t["status"] == "closed")
    log(f"回測完成：{len(trades)} 筆訊號、{n_closed} 筆成交結案（{ts_to_date(int(D['ts'].iloc[WARMUP]))} ~）")
    return trades, factor_history
