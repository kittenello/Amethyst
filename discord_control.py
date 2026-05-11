from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from runtime_control import PAUSED, RUNNING, read_state, write_state
from utils import _config_bool
from discord_notifier import load_webhook_settings


def _clean_id(value: Any) -> str:
    return str(value or "").strip().strip("<@!>")


def _ids_match(configured: str, actual: int | str | None) -> bool:
    configured = _clean_id(configured)
    if not configured:
        return True
    return configured == str(actual or "").strip()


def command_allowed(settings: dict[str, Any], user_id: int | str, channel_id: int | str | None, guild_id: int | str | None) -> bool:
    allowed_user = _clean_id(settings.get("discord_control_user_id") or settings.get("discord_id"))
    allowed_channel = _clean_id(settings.get("discord_control_channel_id"))
    allowed_guild = _clean_id(settings.get("discord_control_guild_id"))
    return (
        _ids_match(allowed_user, user_id)
        and _ids_match(allowed_channel, channel_id)
        and _ids_match(allowed_guild, guild_id)
    )


def set_runtime_state(state_path: str | Path, paused: bool) -> str:
    state = PAUSED if paused else RUNNING
    write_state(state_path, state)
    return state


class DiscordControlServer:
    def __init__(self, state_path: str | Path, settings_loader=load_webhook_settings):
        self.state_path = Path(state_path)
        self.settings_loader = settings_loader
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: discord.Client | None = None

    def start(self) -> bool:
        settings = self.settings_loader()
        if not _config_bool(settings.get("discord_control_enabled"), False):
            return False

        token = str(settings.get("discord_bot_token") or "").strip()
        if not token:
            print("Discord control skipped: enable it only after filling discord_bot_token in cfg/discord_config.toml.")
            return False

        if self.thread and self.thread.is_alive():
            return True

        self.thread = threading.Thread(target=self._thread_main, args=(token,), daemon=True)
        self.thread.start()
        return True

    def close(self) -> None:
        client = self.client
        loop = self.loop
        if client is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=3)
            except Exception:
                pass

    def _thread_main(self, token: str) -> None:
        try:
            asyncio.run(self._run(token))
        except Exception as exc:
            print(f"Discord control stopped: {exc}")

    async def _run(self, token: str) -> None:
        intents = discord.Intents.default()
        client = discord.Client(intents=intents)
        tree = app_commands.CommandTree(client)
        self.client = client
        self.loop = asyncio.get_running_loop()
        synced = False

        async def _reply(interaction: discord.Interaction, message: str) -> None:
            try:
                await interaction.response.send_message(message, ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(message, ephemeral=True)

        async def _guard(interaction: discord.Interaction) -> bool:
            settings = self.settings_loader()
            if command_allowed(
                settings,
                getattr(interaction.user, "id", ""),
                getattr(interaction.channel, "id", None),
                getattr(interaction.guild, "id", None),
            ):
                return True
            await _reply(interaction, "You are not allowed to control this Amethyst bot.")
            return False

        @tree.command(name="stop", description="Pause Amethyst.")
        async def stop_command(interaction: discord.Interaction) -> None:
            if not await _guard(interaction):
                return
            set_runtime_state(self.state_path, paused=True)
            await _reply(interaction, "Amethyst paused.")

        @tree.command(name="start", description="Resume Amethyst.")
        async def start_command(interaction: discord.Interaction) -> None:
            if not await _guard(interaction):
                return
            set_runtime_state(self.state_path, paused=False)
            await _reply(interaction, "Amethyst resumed.")

        @tree.command(name="status", description="Show whether Amethyst is running or paused.")
        async def status_command(interaction: discord.Interaction) -> None:
            if not await _guard(interaction):
                return
            state = read_state(self.state_path)
            await _reply(interaction, f"Amethyst is {'paused' if state == PAUSED else 'running'}.")

        @client.event
        async def on_ready() -> None:
            nonlocal synced
            if synced:
                return
            settings = self.settings_loader()
            guild_id = _clean_id(settings.get("discord_control_guild_id"))
            try:
                if guild_id:
                    guild = discord.Object(id=int(guild_id))
                    tree.copy_global_to(guild=guild)
                    await tree.sync(guild=guild)
                    print(f"Discord control commands synced for guild {guild_id}: /start /stop /status")
                else:
                    await tree.sync()
                    print("Discord control commands synced globally: /start /stop /status")
                synced = True
            except Exception as exc:
                print(f"Discord control command sync failed: {exc}")

        await client.start(token)
