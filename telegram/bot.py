import asyncio
import io
import sys
from pathlib import Path

import cv2

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

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

        photo = BufferedInputFile(
            buffer.tobytes(),
            filename="screen.png"
        )

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

        photo = BufferedInputFile(
            buffer.tobytes(),
            filename="esp_debug.png"
        )

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

        stats = ["bot online"]

        if hasattr(play_instance, "current_brawler"):
            stats.append(f"brawler: {play_instance.current_brawler}")

        if (
            hasattr(play_instance, "current_frame")
            and play_instance.current_frame is not None
        ):
            frame = play_instance.current_frame
            state = get_state(frame)
            stats.append(f"state: {state}")

        try:
            brawler_data = load_toml_as_dict("latest_brawler_data.json")

            if brawler_data:
                stats.append(f"trophies: {len(brawler_data)}")

        except Exception:
            pass

        await message.answer("\n".join(stats))

    except Exception as e:
        await message.answer(f"error: {str(e)}")


async def main():
    if not bot_token:
        print("Telegram bot token not configured")
        return

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())