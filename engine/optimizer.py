"""迭代優化器：用復盤結果自動調參（全部有界、留痕），並重估掛單觸價機率曲線。

- 因子權重：以「因子方向 vs 未來 3 日報酬方向」的命中率緩慢調整（0.3~1.6）
- 掛單深度：成交率 <55% 拉近、>85% 且期望值不佳加深（0.7~1.4）
- 停損緩衝：被插針掃損比例 >30% 加寬（0.4~1.1 ATR）
- 分數門檻：依觀望率與勝率微調（18~32）
每次變更都寫入優化日誌（何時、改了什麼、為什麼）。
"""
import datetime as dt

import numpy as np

from .util import DAY_MS, ts_to_date

DEFAULT_STATE = {
    "version": 1,
    "tuned_at": None,
    "closed_at_last_tune": 0,
    "params": {
        "entry_offsets_atr": [0.32, 0.70, 1.10],
        "entry_depth_mult": 1.0,
        "rung_weights": [0.40, 0.35, 0.25],
        "stop_buffer_atr": 0.6,
        "stop_max_atr": 2.2,
        "stop_min_atr": 1.0,
        "trail_atr_mult": 2.5,
        "risk_pct_base": 1.0,
        "score_threshold": 20,
        "confidence_floor": 55,
        "entry_validity_hours": 48,
        "max_hold_days": 7,
        "max_leverage": 5,
    },
    "weights": {"trend_daily": 1.0, "trend_4h": 0.8, "momentum": 0.7, "funding": 0.9,
                "oi_price": 0.9, "taker_flow": 0.6, "rvol": 0.5, "wick_magnet": 0.5,
                "levels": 0.6, "squeeze_setup": 0.7},
    "history": [],
}


def touch_prob_table(D, horizon_days: int = 2, lookback: int = 420) -> dict:
    """歷史觸價機率：收盤價 ±k×ATR 的限價單，在接下來 horizon 天內成交的頻率。"""
    lo = max(30, len(D) - lookback)
    offs = [round(x, 1) for x in np.arange(0.1, 2.6, 0.1)]
    long_hit = {o: [0, 0] for o in offs}
    short_hit = {o: [0, 0] for o in offs}
    closes = D["close"].to_numpy()
    atrs = D["atr"].to_numpy()
    lows, highs = D["low"].to_numpy(), D["high"].to_numpy()
    for t in range(lo, len(D) - horizon_days):
        c, a = closes[t], atrs[t]
        if not a or np.isnan(a):
            continue
        w_lo = lows[t + 1:t + 1 + horizon_days].min()
        w_hi = highs[t + 1:t + 1 + horizon_days].max()
        for o in offs:
            long_hit[o][1] += 1
            short_hit[o][1] += 1
            if w_lo <= c - o * a:
                long_hit[o][0] += 1
            if w_hi >= c + o * a:
                short_hit[o][0] += 1
    fmt = lambda d: {f"{o:.1f}": round(v[0] / v[1], 3) for o, v in d.items() if v[1] > 50}
    return {"long": fmt(long_hit), "short": fmt(short_hit), "horizon_days": horizon_days}


def factor_edges(factor_history: list[dict], D) -> dict:
    """每個因子的方向命中率（vs 未來 3 日報酬）。"""
    idx_by_ts = {int(ts): i for i, ts in enumerate(D["ts"])}
    closes = D["close"].to_numpy()
    edges = {}
    recent = factor_history[-150:]
    for name in DEFAULT_STATE["weights"]:
        hits, n = 0, 0
        for fh in recent:
            s = fh["scores"].get(name)
            i = idx_by_ts.get(fh["ts"])
            if s is None or abs(s) < 10 or i is None or i + 3 >= len(closes):
                continue
            fwd = closes[i + 3] / closes[i] - 1
            if abs(fwd) < 0.002:
                continue
            n += 1
            if (s > 0) == (fwd > 0):
                hits += 1
        edges[name] = {"edge": round(hits / n, 3) if n >= 25 else None, "n": n}
    return edges


def maybe_tune(state: dict, trades: list[dict], factor_history: list[dict], D,
               now_ms: int, force: bool = False) -> list[dict]:
    """視條件執行調參；就地修改 state，回傳本次日誌。"""
    closed = [t for t in trades if t["status"] in ("closed", "cancelled")]
    n_new = len(closed) - state.get("closed_at_last_tune", 0)
    is_sunday = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).weekday() == 6
    if not force and not (n_new >= 6 or (is_sunday and n_new >= 3)):
        return []
    logs = []
    today = ts_to_date(now_ms)

    def log_change(param, old, new, why):
        if isinstance(old, float):
            old, new = round(old, 3), round(new, 3)
        if old == new:
            return
        entry = {"date": today, "param": param, "old": old, "new": new, "reason": why}
        logs.append(entry)
        state["history"].append(entry)

    # 1) 因子權重
    edges = factor_edges(factor_history, D)
    for name, info in edges.items():
        if info["edge"] is None:
            continue
        w_old = state["weights"][name]
        w_new = float(np.clip(w_old * (0.7 + 0.6 * info["edge"]), 0.3, 1.6))
        w_new = round(w_new, 3)
        if abs(w_new - w_old) >= 0.02:
            log_change(f"權重 {name}", w_old, w_new,
                       f"近 150 日方向命中率 {info['edge']:.0%}（n={info['n']}）")
            state["weights"][name] = w_new

    # 2) 掛單深度（用最近 20 筆訊號的成交率）
    recent = [t for t in closed if t["status"] in ("closed", "cancelled")][-20:]
    if len(recent) >= 10:
        fill_rate = sum(1 for t in recent if t["fills"]) / len(recent)
        rs = [t["r"] for t in recent if t["r"] is not None]
        exp_r = float(np.mean(rs)) if rs else 0
        m_old = state["params"]["entry_depth_mult"]
        if fill_rate < 0.55:
            m_new = float(np.clip(m_old * 0.90, 0.7, 1.4))
            log_change("掛單深度倍率", m_old, m_new, f"近 20 筆成交率僅 {fill_rate:.0%} → 掛近一點")
            state["params"]["entry_depth_mult"] = round(m_new, 3)
        elif fill_rate > 0.85 and exp_r < 0.15:
            m_new = float(np.clip(m_old * 1.08, 0.7, 1.4))
            log_change("掛單深度倍率", m_old, m_new,
                       f"成交率 {fill_rate:.0%} 但期望值 {exp_r:.2f}R 偏低 → 掛深換更好成本")
            state["params"]["entry_depth_mult"] = round(m_new, 3)

    # 3) 停損緩衝（插針掃損比例）
    losses = [t for t in closed if t["status"] == "closed" and (t["r"] or 0) < 0]
    if len(losses) >= 5:
        hunted = sum(1 for t in losses if any("插針掃損" in l for l in t["lessons"]))
        frac = hunted / len(losses)
        b_old = state["params"]["stop_buffer_atr"]
        if frac > 0.3:
            b_new = float(np.clip(b_old + 0.1, 0.4, 1.1))
            log_change("停損緩衝(ATR)", b_old, b_new, f"{frac:.0%} 的虧損疑似被插針掃損 → 加寬停損")
            state["params"]["stop_buffer_atr"] = round(b_new, 2)

    # 4) 分數門檻（交易頻率 × 勝率平衡）
    closed_only = [t for t in closed if t["status"] == "closed" and t["r"] is not None][-30:]
    if len(closed_only) >= 15:
        wr = sum(1 for t in closed_only if t["r"] > 0) / len(closed_only)
        th_old = state["params"]["score_threshold"]
        if wr < 0.42:
            th_new = int(np.clip(th_old + 2, 18, 32))
            log_change("分數門檻", th_old, th_new, f"近 30 筆勝率 {wr:.0%} 偏低 → 提高出手標準")
            state["params"]["score_threshold"] = th_new
        elif wr > 0.60 and th_old > 20:
            th_new = int(np.clip(th_old - 1, 18, 32))
            log_change("分數門檻", th_old, th_new, f"勝率 {wr:.0%} 良好 → 略放寬出手頻率")
            state["params"]["score_threshold"] = th_new

    state["tuned_at"] = today
    state["closed_at_last_tune"] = len(closed)
    state["version"] = state.get("version", 1) + (1 if logs else 0)
    state["history"] = state["history"][-300:]
    return logs
