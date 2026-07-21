"""每日主流程：抓數據 → 更新在途交易（1h 重播）→ 產生今日訊號 → 調參 → 輸出儀表板 JSON。

用法：
  python -m engine.run_daily              # 每日更新（首次自動回測建立基準）
  python -m engine.run_daily --bootstrap  # 強制重跑回測（重建統計與權重）
"""
import copy
import sys
import datetime as dt

import numpy as np

from . import alerts as AL
from . import backtest as BT
from . import factors as F
from . import notify as NT
from . import fetchers as FE
from . import optimizer as OPT
from . import review as R
from . import signalgen as SG
from .util import DATA, DAY_MS, STATE, jdump, jload, log, now_ms, taipei_str, ts_to_date


def condensed_trade(tr: dict) -> dict:
    plan = tr["plan"]
    avg, fw = R._avg_fill(tr)
    return {"id": tr["id"], "date": tr["date"], "mode": tr["mode"],
            "tier": tr.get("tier", "standard"),
            "direction": tr["direction"], "confidence": tr["confidence"], "score": tr["score"],
            "status": tr["status"], "r": tr["r"], "fees_r": tr["fees_r"],
            "funding_r": tr.get("funding_r", 0.0),
            "mae_r": round(tr["mae_r"], 2), "mfe_r": round(tr["mfe_r"], 2),
            "avg_fill": round(avg, 1) if avg else None, "filled_w": round(fw, 2),
            "exit_ts": tr.get("exit_ts"), "lessons": tr["lessons"],
            "exits": [{"price": e["price"], "frac": e["frac"], "reason": e["reason"]} for e in tr["exits"]],
            "plan": {"entries": plan["entries"], "stop": plan["stop"], "avg_entry": plan["avg_entry"],
                     "tps": [{"name": x["name"], "price": x["price"], "r": x["r"]} for x in plan["tps"]],
                     "risk_pct": plan["risk_pct"], "leverage": plan["leverage"],
                     "stop_pct": plan["stop_pct"], "dist": plan["dist"]},
            "stop_now": tr["stop_now"]}


def make_headline(res: dict) -> str:
    """給新手看的一句白話結論。"""
    d = res["direction"]
    gates = " ".join(res["gates"])
    scout = res.get("tier") == "scout"
    if d == "LONG":
        return ("輕倉試探偏多（半倉）：訊號夠好但未到滿分，用一半風險掛單等回檔" if scout
                else "偏多佈局：在下方掛限價單等回檔便宜接，跌破停損就認錯出場")
    if d == "SHORT":
        return ("輕倉試探偏空（半倉）：訊號夠好但未到滿分，用一半風險掛單等反彈" if scout
                else "偏空佈局：在上方掛限價單等反彈再空，突破停損就認錯出場")
    if "冷卻" in gates:
        return "連續虧損冷卻中：今天強制休息一天，防止報復性交易"
    if "重大事件" in gates:
        return "重大財經事件將至：先觀望，等數據公布、市場消化後再進場"
    if "逆勢" in gates:
        return "趨勢太強不逆勢接刀：等反轉結構成形再出手"
    return "今天不出手：多空力量互相抵銷，寧可錯過、不可做錯"


def main() -> int:
    bootstrap_flag = "--bootstrap" in sys.argv
    state = jload(STATE / "model_state.json") or copy.deepcopy(OPT.DEFAULT_STATE)
    OPT.ensure_defaults(state)
    trades = jload(STATE / "trades.json") or []
    factor_history = jload(STATE / "factor_history.json") or []
    touch = jload(STATE / "touch_probs.json") or {}

    # ---------------- 抓取市場資料（多來源容錯） ----------------
    try:
        m = FE.fetch_all()
    except Exception as e:  # noqa: BLE001
        log(f"資料抓取全數失敗：{e} → 以快取標記 stale")
        latest = jload(DATA / "latest.json")
        if latest is None:
            raise
        latest["stale"] = True
        latest["stale_note"] = f"{taipei_str(now_ms())} 更新失敗，顯示前次資料"
        jdump(latest, DATA / "latest.json")
        return 0

    FE.update_history_csv(m)
    hist_csv = FE.load_history_csv(m["src_deriv"])
    if hist_csv is not None:
        from .util import date_to_ts
        hist_csv = hist_csv.copy()
        hist_csv["date_ms"] = hist_csv["date"].map(date_to_ts)
    D = F.build_master(m, hist_csv)
    t = len(D) - 1
    log(f"日線主表 {len(D)} 天，最新收盤日 {ts_to_date(int(D['ts'].iloc[t]))}"
        f"（收盤 {float(D['close'].iloc[t]):,.0f}）")

    funding_by_day = {int(ts): float(f) for ts, f in zip(D["ts"], D["funding"])
                      if not np.isnan(f)}

    # ---------------- 首次啟動：回測建立基準 ----------------
    need_bootstrap = bootstrap_flag or (not trades and not factor_history)
    if need_bootstrap:
        log("啟動回測 bootstrap …")
        touch = OPT.touch_prob_table(D)
        state = copy.deepcopy(OPT.DEFAULT_STATE)
        trades, factor_history = BT.run_backtest(D, state, touch, h4=m["h4"],
                                                 funding_by_day=funding_by_day)
        OPT.maybe_tune(state, trades, factor_history, D, now_ms(), force=True)

    # ---------------- 實盤在途單：以 1h K 精確重播 ----------------
    pre_status = {tr["id"]: tr["status"] for tr in trades}  # 供推播偵測本輪狀態變化
    atr_by_day = {int(ts): float(a) for ts, a in zip(D["ts"], D["atr"]) if not np.isnan(a)}
    h1 = m["h1"][["ts", "open", "high", "low", "close"]]
    for tr in trades:
        if tr["status"] in ("pending", "open"):
            R.process_trade(tr, h1, atr_by_day, state["params"], funding_by_day)
            if tr["status"] in ("closed", "cancelled"):
                R.add_lessons(tr, D)

    # ---------------- 今日訊號 ----------------
    res = SG.decide(D, t, state, trades, touch, h4=m["h4"])
    sig, plan = res["sig"], res["plan"]
    signal_date = ts_to_date(res["signal_ts"])
    if not factor_history or factor_history[-1]["ts"] != int(D["ts"].iloc[t]):
        factor_history.append({"date": signal_date, "ts": int(D["ts"].iloc[t]),
                               "scores": {f["name"]: f["score"] for f in sig["factors"] if f["ok"]}})
        factor_history = factor_history[-500:]

    active = [tr for tr in trades if tr["status"] in ("pending", "open")]
    already_today = any(tr["date"] == signal_date for tr in trades)
    position_note = None
    new_live_trade = None
    if res["direction"] != "FLAT":
        tier_label = "試探" if res["tier"] == "scout" else "標準"
        slot_taken = any(tr.get("tier", "standard") == res["tier"] for tr in active)
        if not slot_taken and not already_today:
            new_live_trade = R.new_trade(plan, sig, "live")
            trades.append(new_live_trade)
            log(f"新增推薦單：{res['direction']}（{tier_label}單）信心 {sig['confidence']:.0f}")
        elif slot_taken:
            same = next(tr for tr in active if tr.get("tier", "standard") == res["tier"])
            if same["direction"] == res["direction"]:
                position_note = f"{tier_label}倉位已有同向在途部位 → 不重複開倉"
            elif sig["confidence"] < 70:
                position_note = f"{tier_label}倉位有反向部位、反向訊號信心不足 70 → 續抱原計畫"
        # 高信心反向訊號 → 所有反向在倉單提前離場（跨層級）
        if sig["confidence"] >= 70:
            for tr in active:
                if tr["status"] == "open" and tr["direction"] != res["direction"]:
                    tr["force_exit"] = True
                    position_note = ((position_note + "；") if position_note else "") + \
                        f"高信心反向訊號 → 在途 {tr['direction']} 單將於下一根開盤離場"

    # ---------------- 迭代優化 ----------------
    tune_logs = OPT.maybe_tune(state, trades, factor_history, D, now_ms())
    wd = dt.datetime.now(dt.timezone.utc).weekday()
    if need_bootstrap or wd == 6:
        touch = OPT.touch_prob_table(D)

    # ---------------- 統計與輸出 ----------------
    stats_all = R.compute_stats(trades)
    stats_live = R.compute_stats([x for x in trades if x["mode"] == "live"])
    stats_bt = R.compute_stats([x for x in trades if x["mode"] == "backtest"])
    equity = R.equity_curve(trades)
    edges = OPT.factor_edges(factor_history, D)

    closes = D["close"]
    alerts = AL.build_alerts(D, t, sig, trades, m, now_ms())

    # ---------------- 手機推播（ntfy／Telegram；未設定即停用，失敗不影響流程） ----------------
    if not need_bootstrap:
        try:
            events = []
            if new_live_trade is not None:
                p2 = new_live_trade["plan"]
                lo2 = min(e["price"] for e in p2["entries"])
                hi2 = max(e["price"] for e in p2["entries"])
                dw = "做多" if res["direction"] == "LONG" else "做空"
                tier_w = "試探單·半倉" if res["tier"] == "scout" else "標準單"
                events.append({"key": f"signal:{signal_date}", "priority": "high",
                               "title": f"{'🟢' if res['direction'] == 'LONG' else '🔴'} 新訊號：{dw}（{tier_w}）",
                               "body": f"掛單 {lo2:,.0f}–{hi2:,.0f}｜停損 {p2['stop']:,.0f}｜TP1 "
                                       f"{p2['tps'][0]['price']:,.0f}｜信心 {sig['confidence']:.0f}。"
                                       f"開儀表板照「幣安版三步驟」掛單。"})
            reason_zh = {"tp1": "止盈1", "tp2": "止盈2", "stop": "停損", "be_stop": "保本出場",
                         "trail_stop": "移動停損", "time": "到期平倉", "reverse_signal": "反向訊號離場",
                         "stagnation": "停滯出場", "protect_stop": "鎖利停損"}
            for tr in trades:
                if tr["mode"] != "live" or tr["id"] not in pre_status:
                    continue
                old, new = pre_status[tr["id"]], tr["status"]
                dw = "做多" if tr["direction"] == "LONG" else "做空"
                if old == "pending" and new == "open":
                    avg2, fw2 = R._avg_fill(tr)
                    events.append({"key": f"fill:{tr['id']}", "priority": "high",
                                   "title": "🟢 掛單成交，倉位已開",
                                   "body": f"{dw}單成交均價約 {avg2:,.0f}（{fw2:.0%} 部位）。"
                                           f"記得掛止盈，並確認停損 {tr['stop_now']:,.0f}。"})
                elif old in ("pending", "open") and new == "closed":
                    r2 = tr["r"] or 0.0
                    lab = reason_zh.get(tr["exits"][-1]["reason"], "出場") if tr["exits"] else "出場"
                    emo = "✅" if r2 > 0.1 else ("🔴" if r2 < -0.1 else "🟡")
                    events.append({"key": f"closed:{tr['id']}", "priority": "default",
                                   "title": f"{emo} 結案 {r2:+.2f}R（{lab}）",
                                   "body": f"{tr['date']} 的{dw}單已全部出場，細節見復盤頁。"})
                elif old == "pending" and new == "cancelled":
                    events.append({"key": f"cxl:{tr['id']}", "priority": "default",
                                   "title": "⌛ 掛單 48 小時未成交，已取消",
                                   "body": "沒等到回檔價位，資金不佔用；等下一個訊號。"})
            for a in alerts:
                events.append({"key": f"alert:{a['kind']}:{a['title']}",
                               "priority": "high" if a["level"] == "opp" else "default",
                               "title": f"{a.get('icon', '🔔')} {a['title']}",
                               "body": a.get("detail", "")})
            n_sent = NT.notify_events(events, STATE / "notify_log.json")
            if n_sent:
                log(f"手機推播 {n_sent} 則")
        except Exception as e:  # noqa: BLE001
            log(f"推播失敗（不影響流程）：{e}")

    latest = {
        "generated_at": now_ms(), "generated_taipei": taipei_str(now_ms()),
        "signal_date": signal_date, "stale": False, "alerts": alerts,
        "src": {"klines": m["src_klines"], "deriv": m["src_deriv"]},
        "price": {"close": float(closes.iloc[t]), "atr": float(D["atr"].iloc[t]),
                  "chg_1d": round(float(closes.iloc[t] / closes.iloc[t - 1] - 1) * 100, 2),
                  "chg_7d": round(float(closes.iloc[t] / closes.iloc[t - 7] - 1) * 100, 2),
                  "funding": (float(D["funding"].iloc[t]) if not np.isnan(D["funding"].iloc[t]) else None),
                  "oi": (float(D["oi"].iloc[t]) if not np.isnan(D["oi"].iloc[t]) else None)},
        "signal": {"direction": res["direction"], "score": sig["score"],
                   "tier": res.get("tier"),
                   "headline": make_headline(res),
                   "confidence": sig["confidence"], "agree": sig["agree"],
                   "factors": [{**f, "weight": state["weights"].get(f["name"], 1.0)} for f in sig["factors"]],
                   "gates": res["gates"], "watch": res["watch"], "plan": plan,
                   "position_note": position_note,
                   "macro": res["macro"]},
        "levels": sorted(sig["clusters"], key=lambda x: -x["strength"])[:14],
        "wicks": sig["wicks"][-8:],
        "candles": [[int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
                    for r in D.tail(120)[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False)],
        "stats_mini": {"win_rate": stats_all["win_rate"], "expectancy_r": stats_all["expectancy_r"],
                       "fill_rate": stats_all["fill_rate"], "total_r": stats_all["total_r"],
                       "n_closed": stats_all["n_closed"], "profit_factor": stats_all["profit_factor"],
                       "win_rate_ex_scratch": stats_all["win_rate_ex_scratch"],
                       "scratch_n": stats_all["scratch_n"]},
        "meta": {"version": state["version"], "params": state["params"]},
    }
    jdump(latest, DATA / "latest.json")

    open_full = [tr for tr in trades if tr["status"] in ("pending", "open")]
    review_out = {
        "generated_at": now_ms(),
        "stats_all": stats_all, "stats_live": stats_live, "stats_backtest": stats_bt,
        "equity": equity,
        "open_trades": [condensed_trade(x) for x in open_full],
        "trades": [condensed_trade(x) for x in
                   sorted(trades, key=lambda x: x["date"], reverse=True)[:80]],
    }
    jdump(review_out, DATA / "review.json")

    jdump({"generated_at": now_ms(), "weights": state["weights"], "params": state["params"],
           "history": state["history"][-120:], "factor_edges": edges,
           "touch_probs": touch, "tuned_at": state.get("tuned_at"),
           "version": state["version"], "recent_changes": tune_logs},
          DATA / "optimizer.json")

    arch = {k: latest[k] for k in ("signal_date", "price", "signal", "levels", "generated_taipei")}
    jdump(arch, DATA / "archive" / f"{signal_date}.json")
    idx = jload(DATA / "archive" / "index.json") or []
    if signal_date not in idx:
        idx.append(signal_date)
    jdump(sorted(idx), DATA / "archive" / "index.json")

    jdump(state, STATE / "model_state.json")
    jdump(trades, STATE / "trades.json")
    jdump(factor_history, STATE / "factor_history.json")
    jdump(touch, STATE / "touch_probs.json")

    log(f"完成：{signal_date} → {res['direction']}（分數 {sig['score']:+.0f}／信心 {sig['confidence']:.0f}）"
        f"｜累積 {stats_all['n_closed']} 筆結案、期望值 {stats_all['expectancy_r']}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
