"""共用工具：HTTP、JSON 存取、時間、路徑。"""
import json
import time
import datetime as dt
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DATA = DOCS / "data"
STATE = ROOT / "state"
HISTORY = ROOT / "history"

DAY_MS = 86_400_000
H1_MS = 3_600_000

UA = {"User-Agent": "btc-trader/1.0 (research dashboard)"}


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def http_get(url: str, params: dict | None = None, timeout: int = 20, retries: int = 3):
    """GET → parsed JSON，含重試與退避。失敗拋出最後一個例外。"""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
            if r.status_code in (403, 451):  # 地區封鎖：不用重試
                raise PermissionError(f"{url} -> HTTP {r.status_code} (geo-blocked)")
            r.raise_for_status()
            return r.json()
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def jload(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def jdump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _json_default(o):
    import numpy as np
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not serializable: {type(o)}")


def ts_to_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def date_to_ts(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def taipei_str(ms: int) -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M")


def rnd(x, n=1):
    """安全四捨五入（None 直接回傳）。"""
    if x is None:
        return None
    return round(float(x), n)


def price_rnd(p: float) -> float:
    """價格自適應精度：>1000 取整數、>100 取 1 位、其餘 2 位。"""
    if p >= 1000:
        return round(p)
    if p >= 100:
        return round(p, 1)
    return round(p, 2)
