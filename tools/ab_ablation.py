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

用法：
  python -m tools.market_snapshot save                                   # 先 pin 住輸入
  python -m tools.ab_ablation --workers 8 --json out_bn.json             # 現行參數為基準，逐項退回
  python -m tools.ab_ablation --snapshot .cache/market_okx.pkl --daily-bars 979 --workers 8 --json out_okx.json
  python -m tools.ab_ablation --compare out_bn.json out_okx.json [out_bybit.json ...]   # 各快照 Δ 對照（抖動）
  python -m tools.ab_ablation --only V0 --touch static --base default    # parity 用
  python -m tools.ab_ablation --list

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
    _CTX.update(key=key, m=m, D=D, h4=m["h4"], base=base, touch_at=touch_at,
                fbd={int(x): float(f) for x, f in zip(D["ts"], D["funding"]) if not np.isnan(f)},
                abd={int(x): float(a) for x, a in zip(D["ts"], D["atr"]) if not np.isnan(a)})
    return _CTX


def summarize(trades: list[dict], D) -> dict:
    s = R.compute_stats(trades)
    done = [t for t in trades if t["status"] in ("closed", "cancelled")]
    closed = [t for t in trades if t["status"] == "closed" and t["r"] is not None]
    last_ts = int(D["ts"].iloc[-1])
    first_ts = int(D["ts"].iloc[WARMUP]) + DAY_MS
    cuts = [first_ts + (last_ts - first_ts) * k // 3 for k in (1, 2)]   # 全樣本日曆三分位（各變體共用切點）
    terts = [0.0, 0.0, 0.0]
    years: dict[str, float] = {}
    for t in closed:
        ts = date_to_ts(t["date"])
        terts[0 if ts < cuts[0] else 1 if ts < cuts[1] else 2] += t["r"]
        years[t["date"][:4]] = years.get(t["date"][:4], 0.0) + t["r"]
    rec = sum(t["r"] for t in closed if date_to_ts(t["date"]) >= last_ts - 180 * DAY_MS)
    return {"n_signals": len(done), "n_closed": len(closed), "fill_rate": s["fill_rate"],
            "win_rate_ex_scratch": s["win_rate_ex_scratch"], "expectancy_r": s["expectancy_r"],
            "expectancy_per_signal_r": s["expectancy_per_signal_r"], "total_r": s["total_r"],
            "profit_factor": s["profit_factor"], "max_dd_r": s["max_dd_r"],
            "big_wins": sum(1 for t in closed if t["r"] >= 1.0),
            "tertiles": [round(x, 2) for x in terts], "last180_r": round(rec, 2),
            "by_year": {k: round(v, 2) for k, v in sorted(years.items())},
            "long_n": s["long_n"], "short_n": s["short_n"]}


def run_variant(snap: str, touch_mode: str, base_mode: str, key: str, mods: dict,
                daily_bars: int | None = None):
    c = _ctx(snap, touch_mode, base_mode, daily_bars)
    state = copy.deepcopy(c["base"])
    state["params"].update(mods)
    D, h4 = c["D"], c["h4"]
    trades: list[dict] = []
    decisions: list[list] = []        # 每個訊號日的 (日期, 方向, 層級, 分數)——比對兩份快照的決策翻動率
    for t in range(WARMUP, len(D)):
        bar = D.iloc[[t]][BAR_COLS]
        for tr in trades:
            if tr["status"] in ("pending", "open"):
                R.process_trade(tr, bar, c["abd"], state["params"], c["fbd"])
        res = SG.decide(D, t, state, trades, c["touch_at"](t), h4=h4)
        decisions.append([ts_to_date(res["signal_ts"]), res["direction"], res.get("tier"),
                          round(float(res["sig"]["score"]), 1)])
        if res["direction"] != "FLAT":
            active = [tr for tr in trades if tr["status"] in ("pending", "open")]
            if not any(tr.get("tier", "standard") == res["tier"] for tr in active):
                trades.append(R.new_trade(res["plan"], res["sig"], "backtest"))
            for tr in active:
                if tr["status"] == "open" and tr["direction"] != res["direction"] \
                        and res["sig"]["confidence"] >= 70:
                    tr["force_exit"] = True
    for tr in trades:
        R.add_lessons(tr, D)
    slim = [{"date": t["date"], "direction": t["direction"], "tier": t.get("tier", "standard"),
             "status": t["status"], "r": t["r"], "fills": len(t["fills"]),
             "exits": [e["reason"] for e in t["exits"]]} for t in trades]
    return key, summarize(trades, D), slim, decisions


def fmt_row(key: str, label: str, st: dict, base: dict | None) -> str:
    adv = ""
    if base is not None and key != "V0":
        adv = f"{base['total_r'] - st['total_r']:+6.2f}"
    tert = "/".join(f"{x:+.1f}" for x in st["tertiles"])
    return (f"{key:<4} {label:<22} {st['n_signals']:>4} {st['fill_rate'] or 0:>5.0%} "
            f"{st['win_rate_ex_scratch'] or 0:>6.1%} {st['expectancy_r'] or 0:>+7.3f} "
            f"{st['expectancy_per_signal_r'] or 0:>+7.3f} {st['total_r']:>+7.2f} "
            f"{st['profit_factor'] or 0:>5.2f} {st['max_dd_r']:>6.2f} {st['big_wins']:>3} "
            f"{tert:>18} {st['last180_r']:>+6.1f} {adv:>7}")


HEADER = (f"{'鍵':<4} {'變體':<22} {'訊號':>4} {'成交':>5} {'勝率':>6} {'期/筆':>7} {'期/訊':>7} "
          f"{'總R':>7} {'PF':>5} {'MaxDD':>6} {'≥1R':>3} {'三分位':>18} {'近180d':>6} {'現行優勢':>7}")


def _rr(t: dict) -> float:
    return t["r"] if t["r"] is not None else 0.0


def _tkey(t: dict) -> str:
    return f"{t['date']}-{t['direction']}-{t['tier']}"


def compare(paths: list[Path]) -> int:
    """對照 2 份以上快照的消融結果：基準本身差多少、哪些決定的 Δ 在所有快照上同號。"""
    runs = [json.load(open(p, encoding="utf-8")) for p in paths]
    tags = [chr(ord("A") + i) for i in range(len(runs))]
    for tg, p, r in zip(tags, paths, runs):
        print(f"{tg} = {p}\n    {r['snapshot']}")
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

    print("\n各項決定的「現行優勢」（= V0 總R − 變體總R；正值 = 現行設定較好，否決項正值 = 否決正確）：")
    print(f"{'鍵':<4} {'變體':<22}" + "".join(f"{tg + ' ΔR':>8}" for tg in tags)
          + f"{'全距':>7} {'同號':>4}" + "".join(f"{tg + ' ΔPF':>8}" for tg in tags) + "  日誌當時")
    for key, label, _mods, logged in VARIANTS:
        if key == "V0" or any(key not in r["variants"] for r in runs):
            continue
        ds = [r["variants"]["V0"]["stats"]["total_r"] - r["variants"][key]["stats"]["total_r"] for r in runs]
        pfs = [(r["variants"]["V0"]["stats"]["profit_factor"] or 0)
               - (r["variants"][key]["stats"]["profit_factor"] or 0) for r in runs]
        same = "✓" if all(d > 0 for d in ds) or all(d < 0 for d in ds) or all(abs(d) < 0.05 for d in ds) else "✗"
        print(f"{key:<4} {label:<22}" + "".join(f"{d:>+8.2f}" for d in ds)
              + f"{max(ds) - min(ds):>7.2f} {same:>4}" + "".join(f"{p:>+8.2f}" for p in pfs) + f"  {logged}")
    print("\n同號 ✓ = 所有快照上 Δ 正負一致（或全部 |Δ|<0.05）；全距 = 各快照 Δ 的最大差，就是抓取抖動的量級。")
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
    if "--list" in argv:
        for key, label, mods, logged in VARIANTS:
            print(f"{key:<4} {label:<26} {mods}\n     日誌：{logged}")
        return 0
    opts = {"--snapshot": str(DEFAULT), "--json": None, "--touch": "rolling",
            "--base": "live", "--only": None, "--workers": "1", "--daily-bars": None}
    for k in list(opts):
        if k in argv:
            i = argv.index(k)
            opts[k] = argv[i + 1]
            del argv[i:i + 2]
    if opts["--touch"] not in ("rolling", "static") or opts["--base"] not in ("live", "default"):
        print(__doc__)
        return 2
    snap = str(Path(opts["--snapshot"]).resolve())
    only = set(opts["--only"].split(",")) if opts["--only"] else None
    variants = [v for v in VARIANTS if only is None or v[0] in only]
    workers = max(1, int(opts["--workers"]))
    daily_bars = int(opts["--daily-bars"]) if opts["--daily-bars"] else None

    c = _ctx(snap, opts["--touch"], opts["--base"], daily_bars)
    D = c["D"]
    desc = (f"{_describe(c['m'])}｜樣本 {ts_to_date(int(D['ts'].iloc[WARMUP]) + DAY_MS)} ~ "
            f"{ts_to_date(int(D['ts'].iloc[-1]) + DAY_MS)}（{len(D) - WARMUP} 訊號日）"
            f"｜觸價表 {opts['--touch']}｜基準 {opts['--base']} v{c['base'].get('version')}")
    print(desc + "\n", flush=True)

    results: dict[str, tuple[dict, list, list]] = {}
    args = (snap, opts["--touch"], opts["--base"])
    if workers == 1:
        print(HEADER, flush=True)
        for key, label, mods, _ in variants:
            k, st, slim, dec = run_variant(*args, key, mods, daily_bars)
            results[k] = (st, slim, dec)
            print(fmt_row(k, label, st, results["V0"][0] if "V0" in results else None), flush=True)
    else:
        labels = {v[0]: v[1] for v in variants}
        with ProcessPoolExecutor(max_workers=min(workers, len(variants))) as ex:
            futs = [ex.submit(run_variant, *args, key, mods, daily_bars) for key, label, mods, _ in variants]
            for f in as_completed(futs):
                k, st, slim, dec = f.result()
                results[k] = (st, slim, dec)
                print(f"  完成 {k} {labels[k]}", flush=True)
        base = results["V0"][0] if "V0" in results else None
        print("\n" + HEADER)
        for key, label, mods, _ in variants:
            if key in results:
                print(fmt_row(key, label, results[key][0], base))
    print("\n現行優勢 = V0 總R − 變體總R（正值 = 保留現行設定較好；否決項反過來讀：正值 = 否決正確）")

    if opts["--json"]:
        meta = {v[0]: v for v in VARIANTS}
        out = {"snapshot": desc, "touch": opts["--touch"], "base": opts["--base"], "daily_bars": daily_bars,
               "variants": {k: {"label": meta[k][1], "mods": meta[k][2], "logged": meta[k][3],
                                "stats": st, "trades": slim, "decisions": dec}
                            for k, (st, slim, dec) in results.items()}}
        with open(opts["--json"], "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"細節已寫入 {opts['--json']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
