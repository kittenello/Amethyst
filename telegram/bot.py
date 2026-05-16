import asyncio
import io
import sys
from pathlib import Path

import cv2

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from play_instance_registry import get_play_instance
from state_finder import get_state
from telegram_notifier import load_telegram_settings
from utils import load_toml_as_dict

telegram_settings = load_telegram_settings()
bot_token = telegram_settings.get("bot_token", "")

bot = Bot(token=bot_token)
dp = Dispatcher()

OWNER_ID = telegram_settings.get("chat_id")

COMMANDS_TEXT = (
    "/start — запустить бота\n"
    "/stop — остановить бота\n"
    "/resume — возобновить бота\n"
    "/status — текущий статус\n"
    "/stats — подробная статистика\n"
    "/screen — скриншот текущего экрана\n"
    "/debug — ESP-отладочный скриншот\n"
    "/queue — показать текущую очередь\n"
    "/queuecreate <brawler> <trophies> — добавить в очередь\n"
    "/queuedelete <brawler> — удалить из очереди\n"
    "/queueclear — очистить всю очередь\n"
)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Команды")]],
    resize_keyboard=True,
    one_time_keyboard=False,
)


def get_bot_status():
    try:
        play_instance = get_play_instance()
        if not play_instance:
            return "bot offline"
        if hasattr(play_instance, "current_frame") and play_instance.current_frame is not None:
            frame = play_instance.current_frame
            state = get_state(frame)
            return f"status: {state}"
        return "bot online but without frame"
    except Exception as e:
        return f"error: {str(e)}"


def check_owner(user_id: int):
    return str(user_id) == str(OWNER_ID)


def _call_api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    import json
    import urllib.request

    port = 8765
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode())


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        res = _call_api("/api/resume", "POST", {})
        await message.answer(f"бот запущен: {res.get('state', '?')}", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await message.answer(f"ошибка запуска: {e}", reply_markup=MAIN_KEYBOARD)


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        res = _call_api("/api/stop", "POST", {})
        await message.answer(f"бот остановлен: {res.get('state', '?')}")
    except Exception as e:
        await message.answer(f"ошибка остановки: {e}")


@dp.message(Command("resume"))
async def cmd_resume(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        res = _call_api("/api/resume", "POST", {})
        await message.answer(f"бот возобновлён: {res.get('state', '?')}")
    except Exception as e:
        await message.answer(f"ошибка возобновления: {e}")


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    status = get_bot_status()
    await message.answer(status)


@dp.message(Command("screen"))
async def cmd_screen(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        play_instance = get_play_instance()
        if (
            not play_instance
            or not hasattr(play_instance, "current_frame")
            or play_instance.current_frame is None
        ):
            await message.answer("no frame available")
            return
        frame = play_instance.current_frame
        _, buffer = cv2.imencode(
            ".png",
            cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        )
        photo = BufferedInputFile(buffer.tobytes(), filename="screen.png")
        await message.answer_photo(photo)
    except Exception as e:
        await message.answer(f"error: {str(e)}")


@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        play_instance = get_play_instance()
        if (
            not play_instance
            or not hasattr(play_instance, "current_frame")
            or play_instance.current_frame is None
        ):
            await message.answer("no frame available")
            return
        frame = play_instance.current_frame
        detection_data = {
            "player": getattr(play_instance, "_last_player_data", []),
            "enemy": getattr(play_instance, "_last_enemy_data", []),
            "teammate": getattr(play_instance, "_last_teammate_data", []),
            "wall": getattr(play_instance, "_last_wall_data", [])
        }
        esp_image = play_instance.create_esp_debug_image(
            frame,
            detection_data,
            getattr(play_instance, "current_brawler", None)
        )
        if esp_image is None:
            await message.answer("cant create esp image")
            return
        _, buffer = cv2.imencode(
            ".png",
            cv2.cvtColor(esp_image, cv2.COLOR_RGB2BGR)
        )
        photo = BufferedInputFile(buffer.tobytes(), filename="esp_debug.png")
        await message.answer_photo(photo)
    except Exception as e:
        await message.answer(f"error: {str(e)}")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        play_instance = get_play_instance()
        if not play_instance:
            await message.answer("bot offline")
            return

        stats = ["🤖 bot online"]

        current_brawler = None
        if hasattr(play_instance, "current_brawler"):
            current_brawler = play_instance.current_brawler
            stats.append(f"brawler: {current_brawler}")

        if (
            hasattr(play_instance, "current_frame")
            and play_instance.current_frame is not None
        ):
            frame = play_instance.current_frame
            state = get_state(frame)
            stats.append(f"state: {state}")

        try:
            queue = load_toml_as_dict("latest_brawler_data.json")
            if isinstance(queue, list) and queue:
                stats.append(f"\n📋 queue ({len(queue)} brawlers):")
                for row in queue:
                    b = row.get("brawler", "?")
                    current = row.get("trophies", 0) or row.get("wins", 0)
                    target = row.get("push_until", 0)
                    rtype = row.get("type", "trophies")
                    remaining = max(0, int(target) - int(current))
                    marker = "▶" if b == current_brawler else " "
                    stats.append(
                        f"{marker} {b}: {current}/{target} {rtype} "
                        f"(осталось: {remaining})"
                    )
        except Exception:
            pass

        await message.answer("\n".join(stats))
    except Exception as e:
        await message.answer(f"error: {str(e)}")


@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        res = _call_api("/api/queue")
        queue = res.get("queue", [])
        if not queue:
            await message.answer("очередь пуста")
            return
        lines = [f"📋 очередь ({len(queue)} бойцов):"]
        for i, row in enumerate(queue, 1):
            b = row.get("brawler", "?")
            current = row.get("trophies", 0)
            target = row.get("push_until", 0)
            rtype = row.get("type", "trophies")
            method = row.get("selection_method", "lowest_trophies")
            lines.append(f"{i}. {b} — {current}/{target} {rtype} [{method}]")
        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"error: {e}")


@dp.message(Command("queuecreate"))
async def cmd_queuecreate(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("использование: /queuecreate <brawler> <target_trophies>")
        return
    brawler = parts[1].lower()
    try:
        target = int(parts[2])
    except ValueError:
        await message.answer("target_trophies должно быть числом")
        return
    try:
        res = _call_api("/api/queuecreate", "POST", {
            "brawler": brawler,
            "push_until": target,
            "type": "trophies",
            "automatically_pick": True,
            "selection_method": "lowest_trophies",
        })
        await message.answer(f"добавлен: {brawler} → {target} 🏆\nочередь: {len(res.get('queue', []))} бойцов")
    except Exception as e:
        await message.answer(f"error: {e}")


@dp.message(Command("queuedelete"))
async def cmd_queuedelete(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("использование: /queuedelete <brawler>")
        return
    brawler = parts[1].lower()
    try:
        res = _call_api("/api/queuedelete", "POST", {"brawler": brawler})
        await message.answer(f"удалён: {brawler}\nочередь: {len(res.get('queue', []))} бойцов")
    except Exception as e:
        await message.answer(f"error: {e}")


@dp.message(Command("queueclear"))
async def cmd_queueclear(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    try:
        _call_api("/api/queue/clear", "POST", {})
        await message.answer("очередь очищена ✅")
    except Exception as e:
        await message.answer(f"error: {e}")


@dp.message()
async def handle_text(message: types.Message):
    if not check_owner(message.from_user.id):
        return
    if message.text == "Команды":
        await message.answer(COMMANDS_TEXT, reply_markup=MAIN_KEYBOARD)


async def set_bot_commands():
    commands = [
        BotCommand(command="/start", description="запустить бота"),
        BotCommand(command="/stop", description="остановить бота"),
        BotCommand(command="/resume", description="возобновить бота"),
        BotCommand(command="/status", description="текущий статус"),
        BotCommand(command="/stats", description="подробная статистика"),
        BotCommand(command="/screen", description="скриншот экрана"),
        BotCommand(command="/debug", description="ESP скриншот"),
        BotCommand(command="/queue", description="Показать очередь"),
        BotCommand(command="/queuecreate", description="добавить бойца в очередь"),
        BotCommand(command="/queuedelete", description="удалить бойца из очереди"),
        BotCommand(command="/queueclear", description="очистить очередь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())


async def main():
    if not bot_token:
        print("Telegram bot token not configured")
        return
    await set_bot_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
