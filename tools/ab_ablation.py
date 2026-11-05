"""特徵消融 A/B（在釘住的快照上）：把優化日誌裡每一項「上線／否決」的決定逐一退回，量它在**同一份輸入**上的貢獻。

為什麼需要：日誌裡的 A/B 數字各自在不同日期、不同抓取結果、不同權重版本下算出，而且 fetch 固定抓 980 天，
「739 個訊號日」其實是**滑動視窗**——彼此不能直接相減，也分不清差異是改動本身還是抓取抖動。
本腳本用同一份快照、同一組現行參數，每次只退回一項，回答「拿掉它會少多少」；
再用 --compare 對照兩份不同來源的快照（例如本機 Binance K 線 vs CI 的 OKX K 線）：
兩邊的 Δ 之差就是抓取抖動的量級，Δ 若連正負號都撐不過換來源，那項改動就不算被證實。

回放口徑與首次部署的 walk-forward 回測相同（日 K、同棒先進場後停損、雙軌倉位、強反向訊號提前離場）。
觸價表預設「每週滾動重算、只看過去」（= 實盤每週日再校準的路徑，零未來資訊）；
--touch static 則與 run_daily --bootstrap 完全一致（整段資料一張表），搭配 --base default
可與 tools.pinned_run --bootstrap 的產出逐筆對帳（parity）。

--replay 4h：在途單改用 4H K 重播（每天 6 輪、對齊 cron 節奏）。迴圈與 tools.ab_signal_freeze 的 frozen
模式相同——只有當日第一輪決策並建單，其餘輪次純追蹤在途單。日 K 回放無法排序同一根 K 棒內的事件
（掛單在第幾小時成交、TP1 之後棘輪／吊燈／停損誰先碰到），對棘輪、掛單效期、棒內閘門這類參數太粗，
它們的 A/B 必須用這個模式才算數。--grid intrabar 就是這三類參數的網格（一次只動一個、其餘維持 live）。
--compare 對 intrabar 網格會自動套用上線標準（三來源 Δ 同號且量級高於全距、三分位不得由正轉負、
DD 不得惡化），並逐筆列出「誰被濾掉／誰多出來／誰的 R 變了」與「逾時（≥48h）才成交的單」的期望值。

用法：
  python -m tools.market_snapshot save                                   # 先 pin 住輸入
  python -m tools.ab_ablation --workers 8 --json out_bn.json             # 現行參數為基準，逐項退回
  python -m tools.ab_ablation --snapshot .cache/market_okx.pkl --daily-bars 979 --workers 8 --json out_okx.json
  python -m tools.ab_ablation --compare out_bn.json out_okx.json [out_bybit.json ...]   # 各快照 Δ 對照（抖動）
  python -m tools.ab_ablation --only V0 --touch static --base default    # parity 用
  python -m tools.ab_ablation --replay 4h --grid intrabar --daily-bars 979 --workers 8 --json out4h_bn.json
  python -m tools.ab_ablation --replay 4h --only V0 --touch static       # 與 ab_signal_freeze 的 frozen 欄位對帳
  python -m tools.ab_ablation --compare out4h_bn.json out4h_okx.json out4h_bybit.json   # 含上線標準判定
  python -m tools.ab_ablation --list [--grid intrabar]

--daily-bars N 只取最後 N 根日 K：不同交易所對同一個「980 根」請求回的根數不同（OKX 實測 999），
兩份快照要先對齊到同一段訊號日才能比。
"""
import copy
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import optimizer as OPT          # noqa: E402
from engine import review as R               # noqa: E402
from engine import signalgen as SG           # noqa: E402
from engine.backtest import WARMUP           # noqa: E402
from engine.util import DAY_MS, STATE, date_to_ts, jload, ts_to_date  # noqa: E402
from tools.market_snapshot import DEFAULT, _describe, load, master    # noqa: E402

BAR_COLS = ["ts", "open", "high", "low", "close"]
H4_MS = DAY_MS // 6
CRON_OFFSET = 7 * 60_000        # 與 tools.ab_signal_freeze 相同：cron "7 0,4,8,12,16,20 * * *"
REPLAY_WINDOW = 20 * DAY_MS     # 每輪重餵的 4H 視窗（對齊實盤 1h 500 根 ≈ 20 天；processed_end 會跳過重複棒）
LATE_H = 48                     # 「逾時成交」分界 = 現行掛單效期 48h；成交時點分布以此切桶
FILL_BUCKETS = ("<48h", "48-72h", "72-96h", ">=96h")

# (key, 說明, 參數覆寫, 日誌當時記錄的數字)
# mods 把該項決定退回「上線前」的狀態；否決項則反過來把它打開。基準 V0 = 現行全部開啟。
VARIANTS = [
    ("V0", "現行（全部開啟）", {}, "—"),
    ("V0m", "現行，但掛單深度倍率 1.0", {"entry_depth_mult": 1.0},
     "8/13 口徑（當時倍率 1.0；現行 0.81 為優化器自調）"),
    ("L1m", "掛單梯退回固定 ATR 檔位（倍率 1.0）", {"entry_prob_targets": None, "entry_depth_mult": 1.0},
     "8/13：固定→機率導向 總R +6.6→+8.2、PF 1.62→1.85、DD −2.1→−1.7、三分位全正"),
    ("L1", "掛單梯退回固定 ATR 檔位", {"entry_prob_targets": None}, "（同上，但維持現行倍率 0.81）"),
    ("B1", "停損緩衝 0.5→0.6", {"stop_buffer_atr": 0.6}, "8/13：0.5 單獨測 總R +7.1 vs 基準 +6.6、PF 1.68、勝率 60%"),
    ("G1", "關閉同結構閘門", {"dup_stop_atr": None}, "8/20：DD −1.7→−1.4、勝率 58.9→60.8%、總R −0.2"),
    ("G2", "同結構閘門不豁免卸險單", {"dup_risk_floor": 0.0}, "8/25：總R +6.8→+7.3、PF 1.80→1.91、DD 持平"),
    ("G3", "冷卻不分方向", {"cooling_directional": False}, "8/20：全歷史零成本（情境未在回測路徑出現）"),
    ("E1", "關閉效率比閘門", {"er_gate": None}, "10/5：期望值 0.094→0.182R、PF 1.68→2.55、每訊號 0.057→0.104R"),
    ("P1", "關閉利潤棘輪", {"profit_ratchet_r": None}, "8/29：期望值 0.100→0.109R、PF 1.77→1.86、勝率 60→64%"),
    ("W1", "關閉 TP1 擋牆", {"tp_wall": False}, "8/22：期望值 0.098→0.102R、PF 1.70→1.76、TP1 命中 34→40"),
    ("W2", "關閉 TP2 結構對齊", {"tp2_struct": False}, "8/22：與 TP1 擋牆同批上線，無單獨數字"),
    ("R1", "打開 rr_floor 0.3（否決項）", {"rr_floor": 0.3}, "8/20 否決：總R +6.5→+4.1、近 180 日全面惡化"),
    ("H1", "掛單效期 48→72h（否決項）", {"entry_validity_hours": 72}, "7/28 否決：勝率 56.6→51.4%、DD −2.1→−4.0"),
    ("S1", "打開 4H 反向結構閘門（觀察中）", {"struct_gate": True}, "10/30：0.126→0.148R、PF 1.81→1.95，但僅濾 2 筆、n 不足"),
    ("OLD", "退回 7 月底規格（本輪迭代全拿掉）",
     {"entry_prob_targets": None, "entry_depth_mult": 1.0, "stop_buffer_atr": 0.6, "dup_stop_atr": None,
      "cooling_directional": False, "er_gate": None, "profit_ratchet_r": None, "tp_wall": False, "tp2_struct": False},
     "累積效果：日誌各項相加的對照"),
]

# 棒內／效期類參數網格（候選變更，一次只動一個、其餘維持 live）。日 K 回放對這三類太粗，請配 --replay 4h。
# 「日誌當時」欄只是對照用的歷史脈絡（單次抓取、不同日期），不是證據；比較對象永遠是同一份快照上的 V0。
INTRABAR = [
    ("V0", "現行（棘輪0.6／效期48h／結構閘門關）", {}, "—"),
    ("PR0", "利潤棘輪 關閉", {"profit_ratchet_r": None},
     "8/29 上線：期望值 0.100→0.109R；11/05 日 K 三來源：關閉反而 +0.4～+0.6R"),
    ("PR45", "利潤棘輪 0.45", {"profit_ratchet_r": 0.45}, "—"),
    ("PR80", "利潤棘輪 0.8", {"profit_ratchet_r": 0.8}, "—"),
    ("VH60", "掛單效期 60h", {"entry_validity_hours": 60}, "—"),
    ("VH72", "掛單效期 72h", {"entry_validity_hours": 72},
     "7/28 否決：勝率 56.6→51.4%、DD −2.1→−4.0；11/05 日 K 三來源：+0.5～+2.2R、2/3 來源 DD 惡化"),
    ("VH96", "掛單效期 96h", {"entry_validity_hours": 96}, "—"),
    ("SG1", "4H 反向結構閘門 開", {"struct_gate": True},
     "10/30 觀察：0.126→0.148R、僅濾 2 筆；11/05 日 K 三來源：+0.5～+1.0R、濾 2～4 筆"),
]
GRIDS = {"log": VARIANTS, "intrabar": INTRABAR}
# Δ 的讀法：log 網格是「退回決定」→ 報「現行優勢 = V0 − 變體」；intrabar 是「候選變更」→ 報「ΔR = 變體 − V0」
ADV_LABEL = {"log": "現行優勢", "intrabar": "ΔR"}

_CTX: dict = {}


def _ctx(snap: str, touch_mode: str, base_mode: str, daily_bars: int | None = None) -> dict:
    """每個 worker 進程各載入一次快照、建一次主表與觸價表快取。

    daily_bars：只取最後 N 根日 K。不同交易所對同一個「980 根」請求回的根數不同
    （OKX 分頁湊到 ≥980 就停、不截回，實測回 999），比較兩份快照前要先對齊視窗。
    """
    key = (snap, touch_mode, base_mode, daily_bars)
    if _CTX.get("key") == key:
        return _CTX
    m = load(Path(snap))
    if daily_bars:
        m = dict(m)
        m["d"] = m["d"].iloc[-daily_bars:].reset_index(drop=True)
    D = master(m)
    if base_mode == "default":
        base = copy.deepcopy(OPT.DEFAULT_STATE)
    else:
        live = jload(STATE / "model_state.json") or copy.deepcopy(OPT.DEFAULT_STATE)
        base = {"version": live["version"], "params": copy.deepcopy(live["params"]),
                "weights": copy.deepcopy(live["weights"]), "history": [],
                "tuned_at": None, "closed_at_last_tune": 0}
    OPT.ensure_defaults(base)
    lb = base["params"].get("touch_lookback", 700)
    if touch_mode == "static":
        tbl = OPT.touch_prob_table(D, lookback=lb)

        def touch_at(t):
            return tbl
    else:
        cache: dict = {}

        def touch_at(t):
            k = (t - WARMUP) // 7              # 每週第一天用「到當天為止」的資料重算
            if k not in cache:
                cache[k] = OPT.touch_prob_table(D.iloc[:t + 1], lookback=lb)
            return cache[k]
    _CTX.clear()
    _CTX.update(key=key, m=m, D=D, h4=m["h4"], ts4=m["h4"]["ts"].to_numpy(), base=base, touch_at=touch_at,
                fbd={int(x): float(f) for x, f in zip(D["ts"], D["funding"]) if not np.isnan(f)},
                abd={int(x): float(a) for x, a in zip(D["ts"], D["atr"]) if not np.isnan(a)})
    return _CTX


def _first_fill_h(t: dict) -> float | None:
    """首次成交距訊號的小時數（以成交那根 K 的起點計；4H 重播下 48.0 代表第 49～52 小時）。"""
    if not t["fills"]:
        return None
    return (t["fills"][0]["ts"] - t["plan"]["signal_ts"]) / 3_600_000


def _bucket(h: float) -> str:
    return FILL_BUCKETS[0] if h < 48 else FILL_BUCKETS[1] if h < 72 else FILL_BUCKETS[2] if h < 96 else FILL_BUCKETS[3]


def summarize(trades: list[dict], D) -> dict:
    s = R.compute_stats(trades)
    done = [t for t in trades if t["status"] in ("closed", "cancelled")]
    closed = [t for t in trades if t["status"] == "closed" and t["r"] is not None]
    last_ts = int(D["ts"].iloc[-1])
    first_ts = int(D["ts"].iloc[WARMUP]) + DAY_MS
    cuts = [first_ts + (last_ts - first_ts) * k // 3 for k in (1, 2)]   # 全樣本日曆三分位（各變體共用切點）
    terts = [0.0, 0.0, 0.0]
    years: dict[str, float] = {}
    buckets = {b: [0, 0.0] for b in FILL_BUCKETS}                       # 首次成交時點 → [筆數, 總R]
    for t in closed:
        ts = date_to_ts(t["date"])
        terts[0 if ts < cuts[0] else 1 if ts < cuts[1] else 2] += t["r"]
        years[t["date"][:4]] = years.get(t["date"][:4], 0.0) + t["r"]
        b = buckets[_bucket(_first_fill_h(t))]
        b[0] += 1
        b[1] += t["r"]
    rec = sum(t["r"] for t in closed if date_to_ts(t["date"]) >= last_ts - 180 * DAY_MS)
    return {"n_signals": len(done), "n_closed": len(closed), "fill_rate": s["fill_rate"],
            "win_rate_ex_scratch": s["win_rate_ex_scratch"], "expectancy_r": s["expectancy_r"],
            "expectancy_per_signal_r": s["expectancy_per_signal_r"], "total_r": s["total_r"],
            "profit_factor": s["profit_factor"], "max_dd_r": s["max_dd_r"],
            "big_wins": sum(1 for t in closed if t["r"] >= 1.0),
            "tertiles": [round(x, 2) for x in terts], "last180_r": round(rec, 2),
            "by_year": {k: round(v, 2) for k, v in sorted(years.items())},
            "fill_buckets": {k: [n, round(r, 2)] for k, (n, r) in buckets.items()},
            "long_n": s["long_n"], "short_n": s["short_n"]}


def _book(res: dict, trades: list[dict], decisions: list[list]) -> None:
    """當日決策 → 建單／高信心反向訊號讓反向在倉單提前離場（與 backtest.run_backtest 同一口徑）。"""
    decisions.append([ts_to_date(res["signal_ts"]), res["direction"], res.get("tier"),
                      round(float(res["sig"]["score"]), 1)])
    if res["direction"] == "FLAT":
        return
    active = [tr for tr in trades if tr["status"] in ("pending", "open")]
    if not any(tr.get("tier", "standard") == res["tier"] for tr in active):
        trades.append(R.new_trade(res["plan"], res["sig"], "backtest"))
    for tr in active:
        if tr["status"] == "open" and tr["direction"] != res["direction"] \
                and res["sig"]["confidence"] >= 70:
            tr["force_exit"] = True


def _slim(t: dict) -> dict:
    return {"date": t["date"], "direction": t["direction"], "tier": t.get("tier", "standard"),
            "status": t["status"], "r": t["r"], "confidence": t["confidence"], "fills": len(t["fills"]),
            "fills_h": [round((f["ts"] - t["plan"]["signal_ts"]) / 3_600_000, 1) for f in t["fills"]],
            "mfe_r": round(t["mfe_r"], 2), "exits": [e["reason"] for e in t["exits"]]}


def run_variant(snap: str, touch_mode: str, base_mode: str, key: str, mods: dict,
                daily_bars: int | None = None, replay: str = "daily"):
    c = _ctx(snap, touch_mode, base_mode, daily_bars)
    state = copy.deepcopy(c["base"])
    state["params"].update(mods)
    D, h4 = c["D"], c["h4"]
    trades: list[dict] = []
    decisions: list[list] = []        # 每個訊號日的 (日期, 方向, 層級, 分數)——比對兩份快照的決策翻動率
    if replay == "4h":
        # 與 tools.ab_signal_freeze.run(mode="frozen") 相同的迴圈：每天 6 輪，每輪先餵「本輪已收盤的 4H K」
        # （視窗對齊實盤、processed_end 跳過重複棒），只有第一輪（日 K 收盤後 7 分）決策並建單。
        ts4 = c["ts4"]
        for t in range(WARMUP, len(D)):
            ts_signal = int(D["ts"].iloc[t]) + DAY_MS
            for k in range(6):
                rt = ts_signal + k * H4_MS + CRON_OFFSET
                lo = int(np.searchsorted(ts4, rt - REPLAY_WINDOW, "left"))
                hi = int(np.searchsorted(ts4, rt - H4_MS, "right"))
                bars = h4.iloc[lo:hi]
                if len(bars) > 1:
                    for tr in trades:
                        if tr["status"] in ("pending", "open"):
                            R.process_trade(tr, bars, c["abd"], state["params"], c["fbd"])
                if k == 0:
                    _book(SG.decide(D, t, state, trades, c["touch_at"](t), h4=h4), trades, decisions)
    else:
        for t in range(WARMUP, len(D)):
            bar = D.iloc[[t]][BAR_COLS]
            for tr in trades:
                if tr["status"] in ("pending", "open"):
                    R.process_trade(tr, bar, c["abd"], state["params"], c["fbd"])
            _book(SG.decide(D, t, state, trades, c["touch_at"](t), h4=h4), trades, decisions)
    for tr in trades:
        R.add_lessons(tr, D)
    return key, summarize(trades, D), [_slim(t) for t in trades], decisions


def fmt_row(key: str, label: str, st: dict, base: dict | None, grid: str = "log") -> str:
    adv = ""
    if base is not None and key != "V0":
        d = base["total_r"] - st["total_r"] if grid == "log" else st["total_r"] - base["total_r"]
        adv = f"{d:+6.2f}"
    tert = "/".join(f"{x:+.1f}" for x in st["tertiles"])
    return (f"{key:<4} {label:<22} {st['n_signals']:>4} {st['fill_rate'] or 0:>5.0%} "
            f"{st['win_rate_ex_scratch'] or 0:>6.1%} {st['expectancy_r'] or 0:>+7.3f} "
            f"{st['expectancy_per_signal_r'] or 0:>+7.3f} {st['total_r']:>+7.2f} "
            f"{st['profit_factor'] or 0:>5.2f} {st['max_dd_r']:>6.2f} {st['big_wins']:>3} "
            f"{tert:>18} {st['last180_r']:>+6.1f} {adv:>7}")


def header(grid: str = "log") -> str:
    return (f"{'鍵':<4} {'變體':<22} {'訊號':>4} {'成交':>5} {'勝率':>6} {'期/筆':>7} {'期/訊':>7} "
            f"{'總R':>7} {'PF':>5} {'MaxDD':>6} {'≥1R':>3} {'三分位':>18} {'近180d':>6} {ADV_LABEL[grid]:>7}")


HEADER = header("log")


def fmt_buckets(key: str, st: dict) -> str:
    cells = "".join(f"{n:>5}{r:>+8.2f}" for n, r in (st["fill_buckets"][b] for b in FILL_BUCKETS))
    return f"{key:<5}{cells}"


def _rr(t: dict) -> float:
    return t["r"] if t["r"] is not None else 0.0


def _tkey(t: dict) -> str:
    return f"{t['date']}-{t['direction']}-{t['tier']}"


def _fmt_trade(t: dict) -> str:
    h = t.get("fills_h") or []
    first = f"首次成交 {h[0]:.0f}h" if h else "未成交"
    return (f"{t['status']:9} {_rr(t):+.2f}R 信心 {t['confidence']:.0f} {first}"
            f"{'（' + str(len(h)) + ' 檔）' if h else ''} 出場 {'/'.join(t['exits']) or '-'}")


def _trade_diff(base_tr: list[dict], var_tr: list[dict], tag: str, limit: int = 20) -> None:
    """同一份快照上 V0 vs 變體的逐筆差異：誰多出來、誰被濾掉、誰的 R 變了。"""
    ta, tb = {_tkey(t): t for t in base_tr}, {_tkey(t): t for t in var_tr}
    common = sorted(set(ta) & set(tb))
    rdiff = [(k, _rr(ta[k]), _rr(tb[k])) for k in common if abs(_rr(ta[k]) - _rr(tb[k])) > 1e-9]
    only_b, only_a = sorted(set(tb) - set(ta)), sorted(set(ta) - set(tb))
    print(f"    [{tag}] 共同 {len(common)} 筆（R 不同 {len(rdiff)} 筆，ΔR {sum(y - x for _, x, y in rdiff):+.2f}）"
          f"｜只在變體 {len(only_b)} 筆 {sum(_rr(tb[k]) for k in only_b):+.2f}R"
          f"｜只在 V0 {len(only_a)} 筆 {sum(_rr(ta[k]) for k in only_a):+.2f}R")
    for k in only_b[:limit]:
        print(f"        ＋{k}: {_fmt_trade(tb[k])}")
    for k in only_a[:limit]:
        print(f"        －{k}: {_fmt_trade(ta[k])}")
    for k, x, y in rdiff[:limit]:
        a, b = ta[k], tb[k]
        print(f"        ～{k}: {x:+.2f} → {y:+.2f}R（成交 {a['fills']}→{b['fills']} 檔、"
              f"MFE {a['mfe_r']:.2f}→{b['mfe_r']:.2f}、出場 {'/'.join(a['exits']) or '-'} → {'/'.join(b['exits']) or '-'}）")
    for lst in (only_b, only_a, rdiff):
        if len(lst) > limit:
            print(f"        …另 {len(lst) - limit} 筆略")


def _late_fill_report(var_tr: list[dict], tag: str, late_h: float = LATE_H, limit: int = 20) -> None:
    """效期變體專用：首次成交 ≥late_h 的單（現行效期下會被取消）單獨算期望值；另計「48h 內先成交、逾時再加碼」。"""
    closed = [t for t in var_tr if t["status"] == "closed" and t.get("fills_h")]
    late = [t for t in closed if t["fills_h"][0] >= late_h]
    addon = [t for t in closed if t["fills_h"][0] < late_h and any(h >= late_h for h in t["fills_h"])]
    rs = [_rr(t) for t in late]
    mean = f"{np.mean(rs):+.3f}" if rs else "—"
    print(f"    [{tag}] 首次成交 ≥{late_h:g}h 的單 {len(late)} 筆：ΣR {sum(rs):+.2f}、期望值 {mean}R、"
          f"{sum(1 for r in rs if r > 0)} 賺／{sum(1 for r in rs if r < 0)} 賠"
          f"｜48h 內先成交、逾時再加碼 {len(addon)} 筆（ΣR {sum(_rr(t) for t in addon):+.2f}）")
    for t in late[:limit]:
        print(f"        {_tkey(t)}: {_fmt_trade(t)}")


def _verdict(v0s: list[dict], vs: list[dict]) -> dict:
    """上線標準（2026-11-05 起）：三來源 Δ 同號且量級高於全距、三分位不得「基準為正而變體轉負」、DD 不得惡化。"""
    ds = [v["total_r"] - b["total_r"] for b, v in zip(v0s, vs)]
    rng = max(ds) - min(ds)
    same = all(d > 0 for d in ds) or all(d < 0 for d in ds)
    mag = min(abs(d) for d in ds) > rng
    tert_bad = [f"{tg}:T{i + 1} {b['tertiles'][i]:+.1f}→{v['tertiles'][i]:+.1f}"
                for tg, b, v in zip("ABCDEFG", v0s, vs) for i in range(3)
                if b["tertiles"][i] > 0 and v["tertiles"][i] < 0]
    dd_bad = [f"{tg}:{b['max_dd_r']:.2f}→{v['max_dd_r']:.2f}"
              for tg, b, v in zip("ABCDEFG", v0s, vs) if v["max_dd_r"] < b["max_dd_r"] - 1e-9]
    adopt = all(d > 0 for d in ds) and mag and not tert_bad and not dd_bad
    return {"ds": ds, "range": rng, "same": same, "mag": mag, "tert_bad": tert_bad, "dd_bad": dd_bad,
            "adopt": adopt, "n_delta": [v["n_signals"] - b["n_signals"] for b, v in zip(v0s, vs)]}


def compare(paths: list[Path]) -> int:
    """對照 2 份以上快照的消融結果：基準本身差多少、哪些決定的 Δ 在所有快照上同號。"""
    runs = [json.load(open(p, encoding="utf-8")) for p in paths]
    tags = [chr(ord("A") + i) for i in range(len(runs))]
    grid = runs[0].get("grid", "log")
    order = runs[0].get("order") or [v[0] for v in GRIDS[grid]]
    meta = runs[0]["variants"]
    for tg, p, r in zip(tags, paths, runs):
        print(f"{tg} = {p}\n    {r['snapshot']}")
    if any(r.get("grid", "log") != grid or r.get("replay", "daily") != runs[0].get("replay", "daily") for r in runs):
        print("\n⚠ 各檔的 --grid／--replay 不一致，Δ 不可直接對照")
    v0 = [r["variants"]["V0"]["stats"] for r in runs]

    print("\n基準 V0 在各快照上的差異（程式碼與參數完全相同，差異純粹來自輸入）：")
    print(f"    {'指標':<26}" + "".join(f"{tg:>9}" for tg in tags) + f"{'全距':>9}")
    for k, nd in (("n_signals", 0), ("n_closed", 0), ("total_r", 2), ("profit_factor", 2),
                  ("expectancy_per_signal_r", 3), ("win_rate_ex_scratch", 3), ("max_dd_r", 2), ("last180_r", 2)):
        vals = [s.get(k) for s in v0]
        ok = all(isinstance(x, (int, float)) for x in vals)
        fmt = (lambda x: f"{x:d}") if nd == 0 else (lambda x: f"{x:.{nd}f}")   # noqa: E731
        cells = "".join(f"{(fmt(x) if isinstance(x, (int, float)) else str(x)):>9}" for x in vals)
        print(f"    {k:<26}{cells}{(fmt(max(vals) - min(vals)) if ok else ''):>9}")

    ta = {_tkey(t): t for t in runs[0]["variants"]["V0"]["trades"]}
    da = {d[0]: (d[1], d[2]) for d in runs[0]["variants"]["V0"].get("decisions", [])}
    for tg, r in zip(tags[1:], runs[1:]):
        tb = {_tkey(t): t for t in r["variants"]["V0"]["trades"]}
        common = set(ta) & set(tb)
        rdiff = sum(1 for k in common if abs(_rr(ta[k]) - _rr(tb[k])) > 1e-9)
        line = (f"    V0 的單：A∩{tg} {len(common)} 筆（其中 R 不同 {rdiff}）、"
                f"只在 A {len(set(ta) - set(tb))} 筆、只在 {tg} {len(set(tb) - set(ta))} 筆")
        db = {d[0]: (d[1], d[2]) for d in r["variants"]["V0"].get("decisions", [])}
        days = sorted(set(da) & set(db))
        if days:
            flips = [d for d in days if da[d] != db[d]]
            line += f"｜共同 {len(days)} 個訊號日，決策（方向／層級）不同 {len(flips)} 天（{len(flips) / len(days):.1%}）"
            print(line)
            if flips:
                print("        翻動日：" + "、".join(
                    f"{d} {da[d][0]}/{da[d][1] or '-'}→{db[d][0]}/{db[d][1] or '-'}" for d in flips[:30])
                    + ("…" if len(flips) > 30 else ""))
        else:
            print(line)

    keys = [k for k in order if k != "V0" and all(k in r["variants"] for r in runs)]
    if grid == "log":
        print("\n各項決定的「現行優勢」（= V0 總R − 變體總R；正值 = 現行設定較好，否決項正值 = 否決正確）：")
        print(f"{'鍵':<4} {'變體':<22}" + "".join(f"{tg + ' ΔR':>8}" for tg in tags)
              + f"{'全距':>7} {'同號':>4}" + "".join(f"{tg + ' ΔPF':>8}" for tg in tags) + "  日誌當時")
        for key in keys:
            ds = [r["variants"]["V0"]["stats"]["total_r"] - r["variants"][key]["stats"]["total_r"] for r in runs]
            pfs = [(r["variants"]["V0"]["stats"]["profit_factor"] or 0)
                   - (r["variants"][key]["stats"]["profit_factor"] or 0) for r in runs]
            same = "✓" if all(d > 0 for d in ds) or all(d < 0 for d in ds) or all(abs(d) < 0.05 for d in ds) else "✗"
            print(f"{key:<4} {meta[key]['label']:<22}" + "".join(f"{d:>+8.2f}" for d in ds)
                  + f"{max(ds) - min(ds):>7.2f} {same:>4}" + "".join(f"{p:>+8.2f}" for p in pfs) + f"  {meta[key]['logged']}")
        print("\n同號 ✓ = 所有快照上 Δ 正負一致（或全部 |Δ|<0.05）；全距 = 各快照 Δ 的最大差，就是抓取抖動的量級。")
        return 0

    # ---- 候選變更網格：ΔR = 變體 − V0，自動套用上線標準 ----
    print("\n各候選變更的 ΔR（= 變體總R − V0 總R；正值 = 變體較好）與上線標準判定：")
    print(f"{'鍵':<5}{'變體':<22}" + "".join(f"{tg + ' ΔR':>8}" for tg in tags)
          + f"{'全距':>7} {'同號':>4} {'量級>全距':>8} {'三分位':>6} {'DD':>4}"
          + "".join(f"{tg + ' Δ筆':>6}" for tg in tags) + "".join(f"{tg + ' ΔPF':>8}" for tg in tags) + "  判定")
    verdicts = {}
    for key in keys:
        vs = [r["variants"][key]["stats"] for r in runs]
        v = _verdict(v0, vs)
        verdicts[key] = v
        pfs = [(x["profit_factor"] or 0) - (b["profit_factor"] or 0) for b, x in zip(v0, vs)]
        print(f"{key:<5}{meta[key]['label']:<22}" + "".join(f"{d:>+8.2f}" for d in v["ds"])
              + f"{v['range']:>7.2f} {'✓' if v['same'] else '✗':>4} {'✓' if v['mag'] else '✗':>8} "
              f"{'✓' if not v['tert_bad'] else '✗':>6} {'✓' if not v['dd_bad'] else '✗':>4}"
              + "".join(f"{n:>+6d}" for n in v["n_delta"]) + "".join(f"{p:>+8.2f}" for p in pfs)
              + f"  {'採用' if v['adopt'] else '不採用'}")
    print("\n判定 = 三來源 ΔR 皆 >0 且同號、min|ΔR| > 全距（量級高於抓取抖動）、三分位無「V0 為正而變體轉負」、"
          "MaxDD 無任一來源惡化。\n任一條不符即「不採用（測過而不改、留痕）」。")

    print("\n首次成交時點分布（結案單；筆數／總R，4H 重播下 48h 桶 = 第 49～72 小時才成交）：")
    print(f"{'鍵':<5}" + "".join(f"{b:>13}" for b in FILL_BUCKETS) + "   來源")
    for key in ["V0"] + keys:
        for tg, r in zip(tags, runs):
            print(fmt_buckets(key if tg == "A" else "", r["variants"][key]["stats"]) + f"   {tg}")

    for key in keys:
        v = verdicts[key]
        print(f"\n━━ {key} {meta[key]['label']}（{meta[key]['mods']}）"
              + (f"｜三分位轉負：{'、'.join(v['tert_bad'])}" if v["tert_bad"] else "")
              + (f"｜DD 惡化：{'、'.join(v['dd_bad'])}" if v["dd_bad"] else ""))
        for tg, r in zip(tags, runs):
            _trade_diff(r["variants"]["V0"]["trades"], r["variants"][key]["trades"], tg)
            if "entry_validity_hours" in meta[key]["mods"]:
                _late_fill_report(r["variants"][key]["trades"], tg)
    print("\n＋ 只在變體出現的單、－ 只在 V0 出現的單（被濾掉／被取消）、～ 兩邊都有但 R 不同。")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--compare" in argv:
        i = argv.index("--compare")
        files = []
        for a in argv[i + 1:]:
            if a.startswith("--"):
                break
            files.append(Path(a))
        if len(files) < 2:
            print("--compare 需要至少兩個 json 檔")
            return 2
        return compare(files)
    opts = {"--snapshot": str(DEFAULT), "--json": None, "--touch": "rolling",
            "--base": "live", "--only": None, "--workers": "1", "--daily-bars": None,
            "--replay": "daily", "--grid": "log"}
    for k in list(opts):
        if k in argv:
            i = argv.index(k)
            opts[k] = argv[i + 1]
            del argv[i:i + 2]
    if (opts["--touch"] not in ("rolling", "static") or opts["--base"] not in ("live", "default")
            or opts["--replay"] not in ("daily", "4h") or opts["--grid"] not in GRIDS):
        print(__doc__)
        return 2
    grid, replay = opts["--grid"], opts["--replay"]
    if "--list" in argv:
        for key, label, mods, logged in GRIDS[grid]:
            print(f"{key:<4} {label:<26} {mods}\n     日誌：{logged}")
        return 0
    snap = str(Path(opts["--snapshot"]).resolve())
    only = set(opts["--only"].split(",")) if opts["--only"] else None
    variants = [v for v in GRIDS[grid] if only is None or v[0] in only]
    workers = max(1, int(opts["--workers"]))
    daily_bars = int(opts["--daily-bars"]) if opts["--daily-bars"] else None

    c = _ctx(snap, opts["--touch"], opts["--base"], daily_bars)
    D = c["D"]
    desc = (f"{_describe(c['m'])}｜樣本 {ts_to_date(int(D['ts'].iloc[WARMUP]) + DAY_MS)} ~ "
            f"{ts_to_date(int(D['ts'].iloc[-1]) + DAY_MS)}（{len(D) - WARMUP} 訊號日）"
            f"｜觸價表 {opts['--touch']}｜基準 {opts['--base']} v{c['base'].get('version')}"
            f"｜重播 {'4H K（每天 6 輪）' if replay == '4h' else '日 K'}｜網格 {grid}")
    print(desc + "\n", flush=True)

    results: dict[str, tuple[dict, list, list]] = {}
    args = (snap, opts["--touch"], opts["--base"])
    if workers == 1:
        print(header(grid), flush=True)
        for key, label, mods, _ in variants:
            k, st, slim, dec = run_variant(*args, key, mods, daily_bars, replay)
            results[k] = (st, slim, dec)
            print(fmt_row(k, label, st, results["V0"][0] if "V0" in results else None, grid), flush=True)
    else:
        labels = {v[0]: v[1] for v in variants}
        with ProcessPoolExecutor(max_workers=min(workers, len(variants))) as ex:
            futs = [ex.submit(run_variant, *args, key, mods, daily_bars, replay) for key, label, mods, _ in variants]
            for f in as_completed(futs):
                k, st, slim, dec = f.result()
                results[k] = (st, slim, dec)
                print(f"  完成 {k} {labels[k]}", flush=True)
        base = results["V0"][0] if "V0" in results else None
        print("\n" + header(grid))
        for key, label, mods, _ in variants:
            if key in results:
                print(fmt_row(key, label, results[key][0], base, grid))
    if grid == "log":
        print("\n現行優勢 = V0 總R − 變體總R（正值 = 保留現行設定較好；否決項反過來讀：正值 = 否決正確）")
    else:
        print("\nΔR = 變體總R − V0 總R（正值 = 變體較好）。單一快照的 ΔR 不能當證據：請在三份來源快照各跑一次再 --compare。")
    if replay == "4h":
        print("\n首次成交時點分布（結案單；筆數／總R；桶以成交那根 4H K 的起點計，48h 桶 = 第 49～72 小時）：")
        print(f"{'鍵':<5}" + "".join(f"{b:>13}" for b in FILL_BUCKETS))
        for key, _label, _mods, _ in variants:
            if key in results:
                print(fmt_buckets(key, results[key][0]))

    if opts["--json"]:
        meta = {v[0]: v for v in GRIDS[grid]}
        out = {"snapshot": desc, "touch": opts["--touch"], "base": opts["--base"], "daily_bars": daily_bars,
               "replay": replay, "grid": grid, "order": [v[0] for v in variants if v[0] in results],
               "variants": {k: {"label": meta[k][1], "mods": meta[k][2], "logged": meta[k][3],
                                "stats": st, "trades": slim, "decisions": dec}
                            for k, (st, slim, dec) in results.items()}}
        with open(opts["--json"], "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"細節已寫入 {opts['--json']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
