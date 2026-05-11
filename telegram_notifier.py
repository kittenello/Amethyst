from __future__ import annotations

import html
import io
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
from PIL import Image

from utils import _config_bool, load_toml_as_dict


TELEGRAM_CONFIG_PATH = "cfg/telegram_config.toml"


def load_telegram_settings() -> dict[str, Any]:
    if not Path(TELEGRAM_CONFIG_PATH).exists():
        return {}
    config = dict(load_toml_as_dict(TELEGRAM_CONFIG_PATH))
    config["bot_token"] = str(config.get("bot_token", "")).strip()
    config["chat_id"] = str(config.get("chat_id", "")).strip()
    config.setdefault("enabled", False)
    config.setdefault("send_match_summary", False)
    config.setdefault("include_screenshot", True)
    config.setdefault("ping_when_stuck", False)
    config.setdefault("ping_when_target_is_reached", False)
    return config


def _title(event_type: str, details: dict[str, Any]) -> str:
    brawler = str(details.get("brawler") or "").title()
    if event_type == "match":
        result = str(details.get("result") or "finished").title()
        return f"Match finished{f' with {brawler}' if brawler else ''}: {result}"
    if event_type == "brawler_complete":
        return f"{brawler or 'Brawler'} target reached"
    if event_type == "completed":
        return "All queued targets completed"
    if event_type == "bot_is_stuck":
        return "Bot needs attention"
    if event_type == "test":
        return "Telegram logging test"
    return "PylaAI update"


def _format_message(event_type: str, details: dict[str, Any]) -> str:
    if event_type == "test":
        return "Test message"
    labels = {
        "brawler": "Brawler",
        "result": "Result",
        "started_trophies": "Started trophies",
        "trophies": "Current trophies",
        "target": "Target",
        "wins": "Wins",
        "win_streak": "Win streak",
        "brawlers_left": "Brawlers left",
        "ips": "IPS",
        "state": "State",
        "emulator": "Emulator",
        "adb_device": "ADB device",
        "runtime": "Runtime",
    }
    lines = [f"<b>{html.escape(_title(event_type, details))}</b>"]
    reason = details.get("reason") or details.get("message")
    if reason:
        lines.append(html.escape(str(reason)))
    for key, label in labels.items():
        value = details.get(key)
        if value is None or value == "":
            continue
        lines.append(f"<b>{label}:</b> {html.escape(str(value))}")
    return "\n".join(lines)


def _image_bytes(screenshot: Any) -> bytes | None:
    if screenshot is None:
        return None
    if isinstance(screenshot, np.ndarray):
        image = Image.fromarray(screenshot)
    elif isinstance(screenshot, Image.Image):
        image = screenshot
    else:
        return None
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def async_notify_user(
    event_type: str | None = None,
    screenshot: Any = None,
    details: dict[str, Any] | None = None,
) -> bool:
    settings = load_telegram_settings()
    if not _config_bool(settings.get("enabled"), False):
        return False
    token = settings.get("bot_token", "")
    chat_id = settings.get("chat_id", "")
    if not token or not chat_id:
        print("Telegram logging skipped: bot token or chat ID is missing.")
        return False

    event_type = event_type or "update"
    details = dict(details or {})
    if event_type == "match" and not _config_bool(settings.get("send_match_summary"), False):
        return False

    text = _format_message(event_type, details)
    api_base = f"https://api.telegram.org/bot{token}"
    image = _image_bytes(screenshot) if _config_bool(settings.get("include_screenshot"), True) else None

    try:
        async with aiohttp.ClientSession() as session:
            if image:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                form.add_field("caption", text)
                form.add_field("parse_mode", "HTML")
                form.add_field("photo", image, filename="pyla_screenshot.png", content_type="image/png")
                async with session.post(f"{api_base}/sendPhoto", data=form, timeout=20) as response:
                    ok = response.status == 200
            else:
                payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
                async with session.post(f"{api_base}/sendMessage", json=payload, timeout=20) as response:
                    ok = response.status == 200
        print(f"Telegram logging {'sent' if ok else 'failed'}: {event_type}")
        return ok
    except Exception as exc:
        print(f"Telegram logging failed ({event_type}): {exc}")
        return False


async def async_send_test_notification() -> bool:
    return await async_notify_user("test", details={"state": "configured", "message": "Telegram logging is connected."})


async def async_send_test_message() -> bool:
    settings = load_telegram_settings()
    token = settings.get("bot_token", "")
    chat_id = settings.get("chat_id", "")
    if not token or not chat_id:
        print("Telegram test skipped: bot token or chat ID is missing.")
        return False
    payload = {"chat_id": chat_id, "text": "Test message"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=20) as response:
                ok = response.status == 200
        print(f"Telegram test {'sent' if ok else 'failed'}.")
        return ok
    except Exception as exc:
        print(f"Telegram test failed: {exc}")
        return False
