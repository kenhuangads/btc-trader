"""市場資料快照：把 fetchers.fetch_all() 的結果凍在檔案裡，讓 A/B 與回歸測試可重現。

為什麼需要：**每次抓取的結果並不相同**——K 線來源會容錯切換（Binance→OKX→Bybit，
同一根日 K 收盤會差幾美元）、OKX rubik 的 OI/taker/LSR 只回約 180 天而缺口由
history/derivatives.csv 回填、交易所也會回補舊資料（實測 funding 的 180 日分位曾在
四小時內由 21% 變 20%，剛好跨過因子門檻）。不先 pin 住輸入，任何 A/B 的差異都可能
只是抓取抖動而非改動本身的效果。

用法：
  python -m tools.market_snapshot save            # 抓一次，存到 .cache/market.pkl
  python -m tools.market_snapshot save other.pkl  # 指定路徑
  python -m tools.market_snapshot info            # 看快照裡有什麼

其他腳本用 load() / master() 取用；pickle 只保證同一台機器、同一組套件版本可讀。
"""
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT = ROOT / ".cache" / "market.pkl"


def save(path: Path = DEFAULT) -> dict:
    from engine import fetchers as FE
    m = FE.fetch_all()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(m, f)
    return m


def load(path: Path = DEFAULT) -> dict:
    if not Path(path).exists():
        raise SystemExit(f"找不到快照 {path}——先跑 `python -m tools.market_snapshot save`")
    with open(path, "rb") as f:
        return pickle.load(f)


def master(m: dict):
    """快照 → 日線主表（與 run_daily 同一條路徑，含 history csv 回補）。"""
    from engine import factors as F
    from engine import fetchers as FE
    from engine.util import date_to_ts
    hist = FE.load_history_csv(m["src_deriv"])
    if hist is not None:
        hist = hist.copy()
        hist["date_ms"] = hist["date"].map(date_to_ts)
    return F.build_master(m, hist)


def _describe(m: dict) -> str:
    return (f"K線 {m['src_klines']} / 衍生品 {m['src_deriv']}｜"
            f"日線 {len(m['d'])} 根、4H {len(m['h4'])} 根、1H {len(m['h1'])} 根｜"
            f"funding {len(m['funding'])} 筆、OI {len(m['oi'])} 天、"
            f"taker {len(m['taker'])} 天、LSR {len(m['lsr'])} 天")


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "info"
    path = Path(args[1]) if len(args) > 1 else DEFAULT
    if cmd == "save":
        m = save(path)
        print(f"已存 {path}\n  {_describe(m)}")
    elif cmd == "info":
        m = load(path)
        from engine.util import ts_to_date
        print(f"{path}\n  {_describe(m)}\n"
              f"  日線區間 {ts_to_date(int(m['d']['ts'].iloc[0]))} ~ "
              f"{ts_to_date(int(m['d']['ts'].iloc[-1]))}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
