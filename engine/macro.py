"""重大總經事件日曆（FOMC/CPI/NFP）：事件前 48 小時降槓桿、24 小時內暫停新倉。

FOMC 為官方公布日程；CPI 為近似日（BLS 偶有調整）；NFP 以每月第一個週五近似。
"""
import datetime as dt

from .util import DAY_MS

FOMC = [  # 決議日（第二天）
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
CPI = [  # 近似
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11",
    "2025-07-15", "2025-08-12", "2025-09-11", "2025-10-24", "2025-11-13", "2025-12-18",
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
    "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-13", "2026-11-10", "2026-12-10",
]


def _nfp_dates(year: int) -> list[str]:
    out = []
    for m in range(1, 13):
        d = dt.date(year, m, 1)
        while d.weekday() != 4:  # Friday
            d += dt.timedelta(days=1)
        out.append(d.isoformat())
    return out


def _all_events() -> list[tuple[str, str]]:
    ev = [(d, "FOMC 決議") for d in FOMC] + [(d, "CPI（約）") for d in CPI]
    for y in (2024, 2025, 2026, 2027):
        ev += [(d, "非農（約）") for d in _nfp_dates(y)]
    return sorted(ev)


EVENTS = _all_events()


def _ts(date_s: str, hour_utc: float) -> int:
    d = dt.datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000 + hour_utc * 3_600_000)


def macro_gate(now_ms: int) -> dict:
    """回傳 {events:[{name,date,hours_until}], within_24h, within_48h}（僅看未來事件）。"""
    upcoming = []
    for date_s, name in EVENTS:
        # 公布時間近似：FOMC 19:00 UTC、CPI/NFP 13:30 UTC
        t = _ts(date_s, 19.0 if "FOMC" in name else 13.5)
        dh = (t - now_ms) / 3_600_000
        if -6 <= dh <= 72:
            upcoming.append({"name": name, "date": date_s, "hours_until": round(dh, 1)})
    w24 = any(0 <= e["hours_until"] <= 24 for e in upcoming)
    w48 = any(0 <= e["hours_until"] <= 48 for e in upcoming)
    return {"events": upcoming, "within_24h": w24, "within_48h": w48}
