"""全歷史 walk-forward A/B：實盤「每 4h 重新決策」 vs「一天只定案一次（訊號凍結）」。

背景：cron 一天跑 6 次，註解原本宣稱其餘輪次「訊號不變、冪等」。掃 docs/data/archive
的 git 歷史證實不成立——同一份程式碼下，48 個 signal_date 裡有 3 天在當天翻過方向、
9 天分數飄移過。本腳本量化這件事對績效的代價。

兩個變體吃完全相同的輸入（同一份快照、同一組權重參數、同一張觸價表、關閉調參），
在途單一律用 4H K 重播（對齊 cron 的 4 小時節奏），唯一差別是：
  live_now  第 2~6 輪也重跑 signalgen.decide() 並據以建單／改判（＝修正前的行為）
  frozen    只有當日第一輪決策，其餘輪次純追蹤在途單（＝修正後的行為）

可回放的差異只有「閘門讀在途單即時狀態」這一條（冷卻／回撤保護／同結構閘門）；
資料源容錯切換與交易所回補造成的分數飄移無法回放，凍結後同樣一併消除，
因此本腳本的數字是**保守下界**。

用法：
  python -m tools.market_snapshot save      # 先 pin 住輸入
  python -m tools.ab_signal_freeze          # 跑 A/B（約 2 分鐘）
  python -m tools.ab_signal_freeze --snapshot other.pkl --json out.json
  python -m tools.ab_signal_freeze --snapshot other.pkl --daily-bars 979   # 與另一份快照對齊訊號日
"""
import copy
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import optimizer as OPT          # noqa: E402
from engine import review as R               # noqa: E402
from engine import signalgen as SG           # noqa: E402
from engine.backtest import WARMUP           # noqa: E402
from engine.util import DAY_MS, STATE, jload, ts_to_date  # noqa: E402
from tools.market_snapshot import DEFAULT, load, master   # noqa: E402

H4_MS = DAY_MS // 6
CRON_OFFSET = 7 * 60_000        # cron "7 0,4,8,12,16,20 * * *"
REPLAY_WINDOW = 20 * DAY_MS     # 對齊實盤每輪重餵的 1h 視窗（500 根 ≈ 20 天）


def run(mode: str, D, h4, state, touch, funding_by_day, atr_by_day):
    """回傳 (trades, flips)。flips = 當天 6 輪決策不一致的日子。"""
    trades, flips = [], []
    ts4 = h4["ts"].to_numpy()
    for t in range(WARMUP, len(D)):
        ts_signal = int(D["ts"].iloc[t]) + DAY_MS
        signal_date = ts_to_date(ts_signal)
        decisions = []
        for k in range(6):
            rt = ts_signal + k * H4_MS + CRON_OFFSET
            # 本輪已收盤的 4H K；視窗對齊實盤，process_trade 的 processed_end 會跳過重複棒
            lo = int(np.searchsorted(ts4, rt - REPLAY_WINDOW, "left"))
            hi = int(np.searchsorted(ts4, rt - H4_MS, "right"))
            bars = h4.iloc[lo:hi]
            if len(bars):
                for tr in trades:
                    if tr["status"] in ("pending", "open"):
                        R.process_trade(tr, bars, atr_by_day, state["params"], funding_by_day)
            if mode == "frozen" and k > 0:
                continue
            res = SG.decide(D, t, state, trades, touch, h4=h4)
            decisions.append((res["direction"], res.get("tier"),
                              res["plan"]["stop"] if res["plan"] else None))
            active = [tr for tr in trades if tr["status"] in ("pending", "open")]
            already_today = any(tr["date"] == signal_date for tr in trades)
            if res["direction"] != "FLAT":
                slot_taken = any(tr.get("tier", "standard") == res["tier"] for tr in active)
                if not slot_taken and not already_today:
                    trades.append(R.new_trade(res["plan"], res["sig"], "backtest"))
                if res["sig"]["confidence"] >= 70:
                    for tr in active:
                        if tr["status"] == "open" and tr["direction"] != res["direction"]:
                            tr["force_exit"] = True
        if len(set(decisions)) > 1:
            flips.append({"date": signal_date, "rounds": decisions})
    for tr in trades:
        if tr["status"] in ("closed", "cancelled"):
            R.add_lessons(tr, D)
    return trades, flips


ROWS = [("n_signals", "出手筆數", 0), ("n_closed", "結案筆數", 0), ("fill_rate", "成交率", 3),
        ("total_r", "總 R", 2), ("expectancy_r", "每筆期望值 R", 3),
        ("expectancy_per_signal_r", "每訊號期望值 R", 3), ("profit_factor", "獲利因子", 2),
        ("win_rate", "勝率", 3), ("win_rate_ex_scratch", "勝率(去平手)", 3),
        ("max_dd_r", "最大回撤 R", 2), ("long_n", "多單結案", 0), ("short_n", "空單結案", 0)]


def main() -> int:
    argv = sys.argv[1:]
    snap, out_json = DEFAULT, None
    for flag, setter in (("--snapshot", "snap"), ("--json", "out_json")):
        if flag in argv:
            i = argv.index(flag)
            val = Path(argv[i + 1])
            if setter == "snap":
                snap = val
            else:
                out_json = val
            del argv[i:i + 2]

    m = load(snap)
    if "--daily-bars" in argv:        # 對齊視窗：不同交易所對「980 根」回的根數不同（OKX 實測 999）
        i = argv.index("--daily-bars")
        m = dict(m)
        m["d"] = m["d"].iloc[-int(argv[i + 1]):].reset_index(drop=True)
        del argv[i:i + 2]
    D, h4 = master(m), m["h4"]
    live = jload(STATE / "model_state.json") or copy.deepcopy(OPT.DEFAULT_STATE)
    base = {"version": live["version"], "params": copy.deepcopy(live["params"]),
            "weights": copy.deepcopy(live["weights"]), "history": [],
            "tuned_at": None, "closed_at_last_tune": 0}
    OPT.ensure_defaults(base)
    touch = OPT.touch_prob_table(D, lookback=base["params"].get("touch_lookback", 700))
    fbd = {int(x): float(f) for x, f in zip(D["ts"], D["funding"]) if not np.isnan(f)}
    abd = {int(x): float(a) for x, a in zip(D["ts"], D["atr"]) if not np.isnan(a)}
    n_days = len(D) - WARMUP

    print(f"樣本 {ts_to_date(int(D['ts'].iloc[WARMUP]))} ~ {ts_to_date(int(D['ts'].iloc[-1]))}"
          f"（{n_days} 天 × 6 輪／天）｜資料源 {m['src_klines']}/{m['src_deriv']}"
          f"｜參數版本 v{base['version']}\n")

    out = {}
    for mode in ("live_now", "frozen"):
        trades, flips = run(mode, D, h4, copy.deepcopy(base), touch, fbd, abd)
        st = R.compute_stats(trades)
        st["n_signals"] = len(trades)
        out[mode] = {"stats": st, "flips": flips,
                     "trades": {f"{t['date']}-{t['direction']}-{t.get('tier', 'standard')}": t
                                for t in trades}}
        print(f"[{mode}] 訊號單 {len(trades)} 筆｜盤中翻動 {len(flips)} 天")

    a, b = out["live_now"], out["frozen"]
    print("\n" + "=" * 72)
    print(f"{'指標':<18}{'現況(每輪決策)':>16}{'凍結(每日一次)':>16}   差異")
    for key, label, nd in ROWS:
        va, vb = a["stats"].get(key), b["stats"].get(key)
        d = ""
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            d = f"{vb - va:+d}" if nd == 0 else f"{vb - va:+.{nd}f}"
        print(f"{label:<18}{str(va):>18}{str(vb):>18}   {d}")

    print(f"\n盤中翻動：現況 {len(a['flips'])}/{n_days} 天"
          f"（{len(a['flips']) / n_days:.1%}）→ 凍結 {len(b['flips'])} 天（結構上必為 0）")
    for f in a["flips"]:
        print("  " + f["date"] + ": " + " → ".join(f"{d}/{t}/{s}" for d, t, s in f["rounds"]))

    # ---- 差異來自哪些單？改善是分散的還是被一兩筆撐起來的？----
    A, B = a["trades"], b["trades"]
    rr = lambda t: t["r"] if t["r"] is not None else 0.0        # noqa: E731
    both = sorted(set(A) & set(B))
    diff_both = [(k, rr(A[k]), rr(B[k])) for k in both if abs(rr(A[k]) - rr(B[k])) > 1e-9]
    print(f"\n共同的單 {len(both)} 筆，其中結果不同 {len(diff_both)} 筆"
          f"（R 差 {sum(y - x for _, x, y in diff_both):+.2f}）")
    for side, keys, src, title in (("A", sorted(set(A) - set(B)), A, "只有現況才會出手"),
                                   ("B", sorted(set(B) - set(A)), B, "只有凍結才會出手")):
        tot = sum(rr(src[k]) for k in keys)
        print(f"\n{title}的 {len(keys)} 筆，合計 {tot:+.2f}R"
              f"（{sum(1 for k in keys if rr(src[k]) > 0)} 賺 /"
              f" {sum(1 for k in keys if rr(src[k]) < 0)} 賠 /"
              f" {sum(1 for k in keys if rr(src[k]) == 0)} 平手或未結）")
        for k in keys:
            print(f"    {k}: {src[k]['status']:9} {rr(src[k]):+.2f}R  信心 {src[k]['confidence']:.0f}")

    print("\n分年度總 R（只算已結案；改善若集中在單一年份就要當心過度擬合）：")
    for y in sorted({k[:4] for k in set(A) | set(B)}):
        agg = lambda S: (sum(rr(t) for k, t in S.items() if k.startswith(y) and t["status"] == "closed"),  # noqa: E731
                         sum(1 for k, t in S.items() if k.startswith(y) and t["status"] == "closed"))
        (ra, na), (rb, nb) = agg(A), agg(B)
        print(f"    {y}: 現況 {ra:+6.2f}R ({na:2} 筆)   凍結 {rb:+6.2f}R ({nb:2} 筆)   差 {rb - ra:+.2f}R")

    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({k: {"stats": v["stats"], "flips": v["flips"]} for k, v in out.items()},
                      f, ensure_ascii=False, indent=1)
        print(f"\n細節已寫入 {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
