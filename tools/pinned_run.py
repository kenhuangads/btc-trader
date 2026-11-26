"""用快照跑 production 的 run_daily——上線前的 parity／回歸測試。

只把 fetchers.fetch_all 換成快照，其餘完全走 production 程式碼路徑，所以可以：
  · 新舊版本吃**同一份輸入**，逐欄比 state/trades.json 與 docs/data/*.json
    （抓取抖動被排除後，任何差異都真的來自程式碼改動）
  · 連跑多輪驗證訊號凍結：同一個 signal_date 的封存檔應逐位元相同

⚠ 會就地覆寫 state/ 與 docs/data/。請複製一份沙箱再跑，例如：
    cp -r engine state history docs requirements.txt /tmp/sandbox && cd /tmp/sandbox
  在有 .git 的目錄執行會被擋下（除非明確加 --allow-repo）。

用法：
  python -m tools.pinned_run                          # 一般輪
  python -m tools.pinned_run --bootstrap              # 重建回測基準（保留模式：參數／日誌／實盤單／訊號鎖不動）
  python -m tools.pinned_run --reset-all              # 首次部署式全重置（與 ab_ablation --touch static --base default 對帳用）
  python -m tools.pinned_run --refresh-signal
  python -m tools.pinned_run --snapshot other.pkl     # 指定快照
  python -m tools.pinned_run --no-history-write       # 不讓 derivatives.csv 累積影響下一次
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.market_snapshot import DEFAULT, load  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    snap = DEFAULT
    if "--snapshot" in argv:
        i = argv.index("--snapshot")
        snap = Path(argv[i + 1])
        del argv[i:i + 2]
    no_hist = "--no-history-write" in argv
    allow_repo = "--allow-repo" in argv
    argv = [a for a in argv if a not in ("--no-history-write", "--allow-repo")]

    from engine.util import ROOT
    if (ROOT / ".git").exists() and not allow_repo:
        print(f"拒絕執行：{ROOT} 是 git 工作目錄，跑下去會覆寫 state/ 與 docs/data/。\n"
              f"請複製成沙箱後再跑，或明確加 --allow-repo。", file=sys.stderr)
        return 2

    m = load(snap)
    from engine import fetchers as FE
    FE.fetch_all = lambda *a, **k: m
    if no_hist:
        FE.update_history_csv = lambda *a, **k: None

    sys.argv = [sys.argv[0]] + argv          # run_daily 用 sys.argv 判旗標
    from engine import run_daily
    return run_daily.main()


if __name__ == "__main__":
    sys.exit(main())
