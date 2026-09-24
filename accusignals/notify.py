"""Signal delivery: console formatting and optional Telegram / webhook push."""
from __future__ import annotations

import json
import os

import requests


def format_signal(s: dict) -> str:
    # Plain ASCII: the Windows console (cp1252) can't print emoji.
    p = _prec(s["entry"])
    lines = [
        f"{s['symbol']} {s['side']}  [{s['market']} {s['interval']}]  confidence {s['confidence']:.0f}%  (score {s['score']:.1f})",
        f"  Entry  {s['entry']:.{p}f}",
        f"  SL     {s['sl']:.{p}f}  ({s['stop_pct']:.2f}%)",
        f"  TP1    {s['tp1']:.{p}f}  (close {int(s['tp1_fraction'] * 100)}%, move SL to breakeven)",
        f"  TP2    {s['tp2']:.{p}f}",
        f"  Why:   {', '.join(s['reasons'])}",
    ]
    if s.get("footprint") is not None:
        lines.append(f"  Footprint: {s['footprint']}")
    lines.append(f"  Bar close: {s['bar_close_time']:%Y-%m-%d %H:%M} UTC")
    return "\n".join(lines)


def _prec(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 10:
        return 3
    if price >= 1:
        return 4
    return 6


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=10)
    return r.ok


def send_webhook(payload: dict, url: str | None = None) -> bool:
    url = url or os.getenv("SIGNAL_WEBHOOK_URL")
    if not url:
        return False
    r = requests.post(url, data=json.dumps(payload, default=str), headers={"Content-Type": "application/json"}, timeout=10)
    return r.ok
