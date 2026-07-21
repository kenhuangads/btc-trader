"""手機推播：ntfy（免帳號）與 Telegram Bot，可擇一或並用；環境變數未設定即靜默停用。

CI 由 GitHub Actions secrets 注入：NTFY_TOPIC（勿寫進程式碼或日誌——公開 repo，
知道主題名等於能訂閱訊號）、TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。
同一事件以 key 去重（state/notify_log.json，保留 21 天），發送失敗不記錄、下輪重試；
任何錯誤都不得中斷主流程。
"""
import os

import requests

from .util import jdump, jload, now_ms

KEEP_MS = 21 * 86_400_000


def _channels() -> list:
    out = []
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        out.append(("ntfy", topic))
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if bot and chat:
        out.append(("telegram", (bot, chat)))
    return out


def send(title: str, body: str, priority: str = "default") -> bool:
    """推送到所有已設定的通道；任一成功即回 True。"""
    ok = False
    for kind, cfg in _channels():
        try:
            if kind == "ntfy":
                r = requests.post("https://ntfy.sh",
                                  json={"topic": cfg, "title": title, "message": body,
                                        "priority": 4 if priority == "high" else 3},
                                  timeout=8)
            else:
                bot, chat = cfg
                r = requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                                  json={"chat_id": chat, "text": f"{title}\n{body}"},
                                  timeout=8)
            ok = ok or r.ok
        except Exception:  # noqa: BLE001
            pass
    return ok


def notify_events(events: list, log_path) -> int:
    """去重後逐則發送。events: [{key, title, body, priority}]，回傳實際送出數。"""
    if not events or not _channels():
        return 0
    log = jload(log_path) or {}
    now = now_ms()
    log = {k: v for k, v in log.items() if now - v < KEEP_MS}
    sent = 0
    for ev in events:
        if ev["key"] in log:
            continue
        if send(ev["title"], ev.get("body", ""), ev.get("priority", "default")):
            log[ev["key"]] = now
            sent += 1
    jdump(log, log_path)
    return sent
