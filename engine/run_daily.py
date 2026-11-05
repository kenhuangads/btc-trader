"""每日主流程：抓數據 → 更新在途交易（1h 重播）→ 產生／沿用今日訊號 → 調參 → 輸出儀表板 JSON。

排程一天跑 6 次（每 4 小時），但**一個 signal_date 只定案一次**：
  · 當日第一輪（日 K 收盤後）：跑 signalgen.decide()，把方向／tier／計畫／分數／因子／閘門
    連同「已收盤日 K 的價格摘要」寫進 state/signal_lock.json 凍結。
  · 其餘每 4 小時：沿用凍結的決策，只重跑「在途單 1h 重播、盤中提醒、統計復盤、推播、調參」。

為什麼要凍結（實測，非推測）：訊號的事實基礎是「已收盤的日 K」，盤中不該改變；
但沒有這道鎖時同一個 signal_date 會被三種盤中變動改寫（見 signal_lock 段落的註解）。

用法：
  python -m engine.run_daily                   # 每 4h 更新（首次自動回測建立基準）
  python -m engine.run_daily --bootstrap       # 重建回測基準：只換掉回測單與上線前的因子紀錄，
                                               #   參數／權重／優化日誌／實盤單／訊號鎖全部保留
  python -m engine.run_daily --reset-all       # 首次部署的全重置：state 回到預設、回測結果取代全部紀錄
                                               #   （會抹掉上線後累積的一切，daily.yml 不暴露）
  python -m engine.run_daily --refresh-signal  # 解鎖今日訊號重新定案（僅供盤中修好程式後手動補跑）
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

SIGNAL_LOCK = STATE / "signal_lock.json"


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


def price_block(D, t: int) -> dict:
    """已收盤日 K 的價格摘要——它是訊號的事實基礎，因此與訊號一起凍結。

    盤中重跑時 K 線來源可能容錯切換（okx ↔ binance），同一根日 K 的收盤會差幾美元；
    讓它跟著訊號凍結，畫面上的「收盤／漲跌幅」才會與當時的決策對得起來。
    """
    closes = D["close"]
    return {"close": float(closes.iloc[t]), "atr": float(D["atr"].iloc[t]),
            "chg_1d": round(float(closes.iloc[t] / closes.iloc[t - 1] - 1) * 100, 2),
            "chg_7d": round(float(closes.iloc[t] / closes.iloc[t - 7] - 1) * 100, 2),
            "funding": (float(D["funding"].iloc[t]) if not np.isnan(D["funding"].iloc[t]) else None),
            "oi": (float(D["oi"].iloc[t]) if not np.isnan(D["oi"].iloc[t]) else None)}


def make_headline(res: dict, plan_tracked: bool = True) -> str:
    """給新手看的一句白話結論。

    plan_tracked=False 代表訊號成立但系統沒有建單（倉位已被佔用）——
    此時不可再喊「掛限價單」，否則使用者會去掛一張系統不追蹤的單。
    """
    d = res["direction"]
    gates = " ".join(res["gates"])
    scout = res.get("tier") == "scout"
    if d in ("LONG", "SHORT"):
        side = "多" if d == "LONG" else "空"
        if not plan_tracked:
            return f"今日判讀偏{side}，但倉位已被在途單佔用 → 不重複開倉，下方計畫僅供參考"
        if d == "LONG":
            return ("輕倉試探偏多（半倉）：訊號夠好但未到滿分，用一半風險掛單等回檔" if scout
                    else "偏多佈局：在下方掛限價單等回檔便宜接，跌破停損就認錯出場")
        return ("輕倉試探偏空（半倉）：訊號夠好但未到滿分，用一半風險掛單等反彈" if scout
                else "偏空佈局：在上方掛限價單等反彈再空，突破停損就認錯出場")
    # 已有在途單佔位（同結構不重複下注）：不是「沒機會」，而是「單已掛出、等成交」
    if "重複下注" in gates or "重複開倉" in gates:
        return "已有在途單等成交中：今日同向訊號不重複下注，計畫看「復盤 → 持倉追蹤」"
    # 冷卻只有「強制觀望」時才算真的擋單；「方向相反 → 放行」不算
    if "冷卻" in gates and "強制觀望" in gates:
        return "連續虧損冷卻中：今天強制休息一天，防止報復性交易"
    if "重大事件" in gates:
        return "重大財經事件將至：先觀望，等數據公布、市場消化後再進場"
    if "逆勢" in gates:
        return "趨勢太強不逆勢接刀：等反轉結構成形再出手"
    if "獲利路徑受阻" in gates or "獲利空間" in gates:
        return "訊號偏多但上方壓力太近：獲利空間不足，等更好的位置再出手"
    return "今天不出手：多空力量互相抵銷，寧可錯過、不可做錯"


def main() -> int:
    reset_flag = "--reset-all" in sys.argv
    bootstrap_flag = "--bootstrap" in sys.argv or reset_flag
    refresh_flag = "--refresh-signal" in sys.argv
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

    # ---------------- 回測基準：首次啟動自動建立；--bootstrap 重建 ----------------
    # 兩種語意，看 state 裡有沒有上線後累積的紀錄（優化日誌或實盤單）：
    #   · 保留模式（已上線的環境；daily.yml 的 bootstrap 勾選框走這裡）：只重建「回測基準」——
    #     mode=backtest 的單與上線日之前的因子紀錄，換成用**現行**參數／權重重跑的結果；參數、權重、
    #     優化日誌、version、實盤單（含 pending/open）、訊號鎖、推播紀錄全部原樣。回測只留上線日
    #     （第一個封存訊號日或第一筆實盤單，取較早者）之前的單：trade id 不含 mode，實盤期間若同時
    #     存在回測單會撞 id，統計與權益曲線也會把同一段行情算兩次。
    #   · 全重置（--reset-all；首次部署、state 裡沒有任何上線紀錄時自動）：state 換成 DEFAULT_STATE、
    #     回測結果取代全部交易紀錄、強制調參初始化權重。這會抹掉上線後累積的一切，所以 daily.yml
    #     不暴露它；tools.ab_ablation --touch static --base default 的 parity 對帳對的也是這條路徑。
    need_bootstrap = bootstrap_flag or (not trades and not factor_history)
    has_record = bool(state.get("history")) or any(tr["mode"] == "live" for tr in trades)
    reset_all = reset_flag or (need_bootstrap and not has_record)
    if need_bootstrap and reset_all:
        log("啟動回測 bootstrap（全重置：state 回到預設、回測結果取代全部交易紀錄）…")
        state = copy.deepcopy(OPT.DEFAULT_STATE)
        touch = OPT.touch_prob_table(D, lookback=state["params"].get("touch_lookback", 700))
        trades, factor_history = BT.run_backtest(D, state, touch, h4=m["h4"],
                                                 funding_by_day=funding_by_day)
        OPT.maybe_tune(state, trades, factor_history, D, now_ms(), force=True)
    elif need_bootstrap:
        kept = [tr for tr in trades if tr["mode"] != "backtest"]
        archive_idx = jload(DATA / "archive" / "index.json") or []
        live_since = min([tr["date"] for tr in kept] + list(archive_idx), default=None)
        log(f"state 已有 {len(state.get('history') or [])} 筆優化日誌、{len(kept)} 筆實盤單 → bootstrap 走保留模式："
            f"只重建回測基準（{'上線日 ' + live_since + ' 之前' if live_since else '全段'}），"
            f"參數／權重／日誌／實盤單／訊號鎖不動；要首次部署式的全重置請改用 --reset-all")
        lb = state["params"].get("touch_lookback", 700)
        if touch.get("lookback") != lb:      # 沿用現行觸價表（每週日本來就會重算），缺了才補
            touch = OPT.touch_prob_table(D, lookback=lb)
        bt_trades, bt_fh = BT.run_backtest(D, state, touch, h4=m["h4"], funding_by_day=funding_by_day)
        if live_since:
            bt_trades = [tr for tr in bt_trades if tr["date"] < live_since]
            bt_fh = ([fh for fh in bt_fh if fh["date"] < live_since]
                     + [fh for fh in factor_history if fh["date"] >= live_since])
        # 「上次調參時的結案數」隨回測單數量平移：實盤累積的結案進度不變，回測單換批也不會觸發調參
        old_done = sum(1 for tr in trades if tr["mode"] == "backtest" and tr["status"] in ("closed", "cancelled"))
        new_done = sum(1 for tr in bt_trades if tr["status"] in ("closed", "cancelled"))
        state["closed_at_last_tune"] = max(0, int(state.get("closed_at_last_tune", 0)) + new_done - old_done)
        n_old_bt = len(trades) - len(kept)
        trades = bt_trades + kept
        factor_history = bt_fh
        log(f"回測基準已重建：回測單 {n_old_bt} → {len(bt_trades)} 筆、因子紀錄 {len(factor_history)} 筆；"
            f"實盤單 {len(kept)} 筆原樣保留")

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
    # 觸價機率曲線：每週日重算；lookback 參數變更（或表尚無標記）時立即重算，
    # 讓機率導向掛單永遠對齊近期波動結構
    lb = state["params"].get("touch_lookback", 700)
    if touch.get("lookback") != lb or dt.datetime.now(dt.timezone.utc).weekday() == 6:
        touch = OPT.touch_prob_table(D, lookback=lb)
    res = SG.decide(D, t, state, trades, touch, h4=m["h4"])
    signal_date = ts_to_date(res["signal_ts"])
    price = price_block(D, t)

    # ---- 訊號凍結：一個 signal_date 只定案一次 ----
    # 訊號的事實基礎是「已收盤的日 K」，盤中不該改變。沒有這道鎖時（實測 48 個 signal_date、
    # 235 組同程式碼的相鄰輪次）有 3 天在同一天內翻過方向、9 天分數飄移過，成因有三：
    #   1) 閘門讀在途單的即時狀態——冷卻（看最後兩筆結案的 exit_ts）、回撤保護、同結構閘門，
    #      而 1h 重播會在盤中改變這些狀態（8/20 FLAT→LONG→FLAT、8/22 LONG→FLAT 都是這條）
    #   2) 每輪重抓數據：K 線來源容錯切換讓同一根日 K 收盤差幾美元、衍生品序列被交易所回補
    #      讓 180 日分位跨過門檻 → 分數飄移（7/16 分數 11.5→15.4 跨過試探門檻而翻多）
    #   3) 優化器每輪都可能調權重／參數、觸價曲線每週日重算 → 分數與掛單價飄移（8/13）
    # 凍結後仍每輪更新的：在途單 1h 重播、盤中提醒、統計與復盤、推播、調參（明日生效）。
    # 保留模式的 bootstrap 也尊重訊號鎖：它只動回測基準，今日決策不是它的事；全重置後鎖裡的
    # 交易紀錄已不存在，才需要重新定案。
    lock = jload(SIGNAL_LOCK) or {}
    frozen = bool(not reset_all and not refresh_flag
                  and lock.get("signal_date") == signal_date and lock.get("res"))
    if frozen:
        res = lock["res"]
        price = lock.get("price") or price
        log(f"訊號已於 {lock.get('decided_taipei')} 定案（{res['direction']}）"
            f"→ 本輪沿用凍結決策，只更新在途單追蹤與提醒")
    elif refresh_flag and lock.get("signal_date") == signal_date:
        log(f"--refresh-signal：解除 {signal_date} 的訊號鎖，重新定案")
    sig, plan = res["sig"], res["plan"]

    if not factor_history or factor_history[-1]["ts"] != int(D["ts"].iloc[t]):
        factor_history.append({"date": signal_date, "ts": int(D["ts"].iloc[t]),
                               "scores": {f["name"]: f["score"] for f in sig["factors"] if f["ok"]}})
        factor_history = factor_history[-500:]

    active = [tr for tr in trades if tr["status"] in ("pending", "open")]
    new_live_trade = None
    if frozen:
        # 「建不建單」同樣屬於決策，只有定案那一輪能建：否則盤中倉位一空出來，就會拿早上的
        # 計畫價去追一張已經過時的單。回測一天只決策一次，實盤必須對齊同一口徑。
        decision_note = lock.get("position_note")
        plan_tracked = bool(lock.get("plan_tracked"))
    else:
        decision_note = None
        plan_tracked = False
        already_today = any(tr["date"] == signal_date for tr in trades)
        if res["direction"] != "FLAT":
            tier_label = "試探" if res["tier"] == "scout" else "標準"
            slot_taken = any(tr.get("tier", "standard") == res["tier"] for tr in active)
            if not slot_taken and not already_today:
                new_live_trade = R.new_trade(plan, sig, "live")
                trades.append(new_live_trade)
                active.append(new_live_trade)
                log(f"新增推薦單：{res['direction']}（{tier_label}單）信心 {sig['confidence']:.0f}")
            elif slot_taken:
                same = next(tr for tr in active if tr.get("tier", "standard") == res["tier"])
                if same["direction"] == res["direction"]:
                    decision_note = f"{tier_label}倉位已有同向在途部位 → 不重複開倉"
                elif sig["confidence"] < 70:
                    decision_note = f"{tier_label}倉位有反向部位、反向訊號信心不足 70 → 續抱原計畫"
            # 這份計畫是否真的被系統追蹤（有建單＝有停損管理、有復盤、有成交/結案推播）。
            # 訊號成立但因倉位佔用而未建單時，畫面必須明說「僅供參考」——否則等於叫使用者
            # 去掛一張系統根本不會管的單。比對方向：--refresh-signal 改判反向時，早上那張
            # 反向單不能算成「這份計畫有被追蹤」。
            plan_tracked = any(tr["date"] == signal_date and tr["mode"] == "live"
                               and tr["direction"] == res["direction"] for tr in trades)
            if not plan_tracked:
                decision_note = ((decision_note + "；") if decision_note else "") + \
                    "本計畫僅供參考：系統未建單追蹤（無停損管理、無成交與結案推播）"

    # 高信心反向訊號 → 所有反向在倉單提前離場（跨層級）。
    # 這條每輪都跑、不凍結：它是「在途單風險管理」而非訊號本身——盤中才成交的反向單也必須
    # 被保護到。旗標只會 False → True，且提示由當下狀態推導，重跑不會翻動。
    force_note = None
    if res["direction"] != "FLAT" and sig["confidence"] >= 70:
        for tr in active:
            if tr["status"] == "open" and tr["direction"] != res["direction"]:
                tr["force_exit"] = True
        hit = next((tr for tr in active if tr["status"] == "open" and tr.get("force_exit")
                    and tr["direction"] != res["direction"]), None)
        if hit:
            force_note = f"高信心反向訊號 → 在途 {hit['direction']} 單將於下一根開盤離場"
    position_note = "；".join(x for x in (decision_note, force_note) if x) or None

    # ---- 寫回訊號鎖 ----
    # 資料降級（衍生品來源全掛）時不凍結：寧可下一輪用完整資料重新定案，
    # 也不要把一份缺因子的決策鎖一整天。
    if frozen:
        lock["rounds"] = int(lock.get("rounds", 1)) + 1
        lock["last_run_taipei"] = taipei_str(now_ms())
        jdump(lock, SIGNAL_LOCK)
    elif m["src_deriv"] == "none":
        log("衍生品資料全數缺漏 → 本輪不凍結訊號，下一輪重新定案")
        lock = {"decided_taipei": taipei_str(now_ms()), "rounds": 1}
    else:
        lock = {"signal_date": signal_date, "signal_ts": res["signal_ts"],
                "decided_at": now_ms(), "decided_taipei": taipei_str(now_ms()),
                "rounds": 1, "version": state["version"],
                "weights": dict(state["weights"]),
                "src": {"klines": m["src_klines"], "deriv": m["src_deriv"]},
                "trade_id": new_live_trade["id"] if new_live_trade else None,
                "plan_tracked": plan_tracked, "position_note": decision_note,
                "price": price, "res": res}
        jdump(lock, SIGNAL_LOCK)

    # ---------------- 迭代優化 ----------------
    tune_logs = OPT.maybe_tune(state, trades, factor_history, D, now_ms())

    # ---------------- 統計與輸出 ----------------
    stats_all = R.compute_stats(trades)
    stats_live = R.compute_stats([x for x in trades if x["mode"] == "live"])
    stats_bt = R.compute_stats([x for x in trades if x["mode"] == "backtest"])
    equity = R.equity_curve(trades)
    edges = OPT.factor_edges(factor_history, D)

    alerts = AL.build_alerts(D, t, sig, trades, m, now_ms())

    # ---------------- 手機推播（ntfy／Telegram；未設定即停用，失敗不影響流程） ----------------
    # 保留模式的 bootstrap 照常推播：成交／結案等狀態轉換只在發生的那一輪偵測得到，這輪不推
    # 下一輪也不會補；全重置後沒有實盤單可言，才整輪不推。
    if not reset_all:
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

    sig_weights = lock.get("weights") or state["weights"]
    latest = {
        "generated_at": now_ms(), "generated_taipei": taipei_str(now_ms()),
        "signal_date": signal_date, "stale": False, "alerts": alerts,
        "src": {"klines": m["src_klines"], "deriv": m["src_deriv"]},
        "price": price,
        "signal_rounds": lock.get("rounds", 1),
        "signal": {"direction": res["direction"], "score": sig["score"],
                   "tier": res.get("tier"),
                   "headline": make_headline(res, plan_tracked),
                   "decided_taipei": lock.get("decided_taipei"),
                   "confidence": sig["confidence"], "agree": sig["agree"],
                   # 權重取「定案當時」那一份：優化器盤中可能調權重，但凍結的分數是用舊權重
                   # 算出來的，畫面若混用兩份，使用者會看到對不起來的因子貢獻。
                   "factors": [{**f, "weight": sig_weights.get(f["name"], 1.0)} for f in sig["factors"]],
                   "gates": res["gates"], "watch": res["watch"], "plan": plan,
                   "position_note": position_note, "plan_tracked": plan_tracked,
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

    # 封存檔記的是「這一天的訊號」，時間戳用定案時間而非本輪執行時間；
    # 凍結之後同一個 signal_date 的封存檔就會逐位元穩定，不再每 4h 被改寫。
    arch = {k: latest[k] for k in ("signal_date", "price", "signal", "levels", "generated_taipei")}
    arch["generated_taipei"] = lock.get("decided_taipei") or latest["generated_taipei"]
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
