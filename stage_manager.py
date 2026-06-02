import asyncio
import json
import os
import time
import urllib.request

import cv2
import numpy as np

from state_finder import (
    get_state,
    find_game_result,
    is_in_prestige_reward,
    get_prestige_next_button_center,
    get_team_invite_reject_button_center,
    get_star_drop_type,
)
from trophy_observer import TrophyObserver
from utils import find_template_center, load_toml_as_dict, async_notify_user, \
    save_brawler_data, extract_text_strings, normalize_brawler_name
from webapp.server import fetch_brawltracker_player
from adaptive_brain import AdaptiveBrain

debug = load_toml_as_dict("cfg/general_config.toml")['super_debug'] == "yes"


def load_image(image_path, scale_factor):
    # Load the image
    image = cv2.imread(image_path)
    orig_height, orig_width = image.shape[:2]

    # Calculate the new dimensions based on the scale factor
    new_width = int(orig_width * scale_factor)
    new_height = int(orig_height * scale_factor)

    # Resize the image
    resized_image = cv2.resize(image, (new_width, new_height))
    return resized_image

class StageManager:

    def __init__(self, brawlers_data, lobby_automator, window_controller):
        self.Lobby_automation = lobby_automator
        self.lobby_config = load_toml_as_dict("./cfg/lobby_config.toml")
        self.close_popup_icon = None
        self.brawlers_pick_data = brawlers_data
        self.started_trophies_by_brawler = {}
        for brawler in brawlers_data:
            name = str(brawler.get("brawler", "")).lower()
            if name:
                self.started_trophies_by_brawler[name] = brawler.get("trophies", 0)
        brawler_list = [brawler["brawler"] for brawler in brawlers_data]
        self.Trophy_observer = TrophyObserver(brawler_list)
        bot_config = load_toml_as_dict("cfg/bot_config.toml")
        adaptive_enabled = str(bot_config.get("adaptive_brain_enabled", "yes")).lower() in ("yes", "true", "1")
        adaptive_window = int(bot_config.get("adaptive_brain_window", 20))
        self.adaptive_brain = AdaptiveBrain(enabled=adaptive_enabled, window_size=adaptive_window)
        self.post_match_action = str(bot_config.get("post_match_action", "lobby")).strip().lower()
        if self.post_match_action not in ("lobby", "play_again"):
            self.post_match_action = "lobby"
        self.time_since_last_stat_change = time.time()
        # Guards against recording trophies twice when end_game() is re-entered
        # on the same end-of-match screen (e.g. because the dismiss button
        # didn't clear the screen before the outer loop called us again).
        self.last_recorded_result_time = 0.0
        self.last_recorded_result = None
        self.active_end_result = None
        self.last_team_invite_reject_time = 0.0
        self.stop_after_post_match_rewards = False
        self.farming_complete = False
        self.completion_notification_sent = False
        # Remember which queue entry was already auto-selected in the lobby.
        # This makes the webapp checkbox work even when the queue is edited from
        # the browser and prevents re-opening the brawler picker every lobby tick.
        self.last_auto_selected_queue_key = None
        self.push_all_needs_selection = False
        time_thresholds = load_toml_as_dict("./cfg/time_tresholds.toml")
        self.end_screen_dismiss_delay = float(time_thresholds.get("end_screen_dismiss_delay", 0.35))
        self.window_controller = window_controller
        self.starr_drop = None
        self.states = {
            'shop': self.quit_shop,
            'brawler_selection': self.quit_brawler_selection,
            'popup': self.close_pop_up,
            'match': lambda: 0,
            'match_making': lambda: self.window_controller.keys_up(list("wasd")),
            'end_draw': self.end_game,
            'end_victory': self.end_game,
            'end_defeat': self.end_game,
            # Showdown trio: finishing places 1-4
            'end_1st': self.end_game,
            'end_2nd': self.end_game,
            'end_3rd': self.end_game,
            'end_4th': self.end_game,
            'lobby': self.start_game,
            'star_drop': self.handle_star_drop,
            'prestige_reward': self.handle_prestige_reward,
            'trophy_reward': lambda: self.window_controller.press_key("Q")
        }

    def send_webhook_notification(self, event_type, screenshot=None, details=None):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(async_notify_user(event_type, screenshot, details=details or {}))
        finally:
            loop.close()

    def current_target_details(self, extra=None):
        current = self.brawlers_pick_data[0] if self.brawlers_pick_data else {}
        type_to_push = current.get("type", "trophies")
        values = {
            "trophies": self.Trophy_observer.current_trophies,
            "wins": self.Trophy_observer.current_wins,
        }
        instance_id = os.environ.get("PYLA_MULTI_INSTANCE_ID")
        instance_port = os.environ.get("PYLA_MULTI_INSTANCE_PORT")
        instance_serial = os.environ.get("PYLA_MULTI_INSTANCE_SERIAL") or (f"127.0.0.1:{instance_port}" if instance_port else "")
        instance_cfg = os.environ.get("PYLA_MULTI_INSTANCE_CFG")
        instance_label = f"Instance #{instance_id}" if instance_id else ""
        if instance_serial:
            instance_label = f"{instance_label} | {instance_serial}" if instance_label else instance_serial

        details = {
            "instance": instance_label,
            "instance_id": instance_id or "",
            "instance_pid": os.getpid() if instance_id else "",
            "instance_adb": instance_serial,
            "instance_cfg": instance_cfg or "",
            "brawler": current.get("brawler", ""),
            "started_trophies": self.started_trophies_by_brawler.get(
                str(current.get("brawler", "")).lower(),
                current.get("trophies", 0),
            ),
            "trophies": values.get(type_to_push, self.Trophy_observer.current_trophies),
            "target": current.get("push_until", ""),
            "wins": self.Trophy_observer.current_wins,
            "win_streak": self.Trophy_observer.win_streak,
            "brawlers_left": len(self.brawlers_pick_data),
        }
        if extra:
            details.update(extra)
        return details

    def pause_farming_in_lobby(self, reason):
        """Leave the bot idle in the lobby after the requested target is done."""
        if not self.farming_complete:
            print(reason)
            print("Farming target is complete. Staying idle in lobby instead of closing PylaAI.")
        self.farming_complete = True
        self.stop_after_post_match_rewards = False
        self.window_controller.keys_up(list("wasd"))
        # Replace the lobby handler with a no-op so the lobby watchdog
        # and any re-entry of do_state("lobby") cannot press Q and start a match.
        self.states["lobby"] = lambda: self.window_controller.keys_up(list("wasd"))
        save_brawler_data(self.brawlers_pick_data)
        return 0

    @staticmethod
    def validate_trophies(trophies_string):
        trophies_string = trophies_string.lower()
        while "s" in trophies_string:
            trophies_string = trophies_string.replace("s", "5")
        numbers = ''.join(filter(str.isdigit, trophies_string))

        if not numbers:
            return False

        trophy_value = int(numbers)
        return trophy_value

    @staticmethod
    def _number_or_default(value, default=0):
        try:
            if value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _queue_order(queue):
        if not isinstance(queue, list):
            return []
        return [str(row.get("brawler", "")) for row in queue if isinstance(row, dict)]

    @staticmethod
    def _queue_api_url():
        url = os.environ.get("PYLA_QUEUE_API_URL", "http://127.0.0.1:8765/api/queue")
        # Child multi-instance workers set PYLA_QUEUE_API_URL=disabled.
        # Return None so all callers skip the webapp fetch and use only the
        # local latest_brawler_data.json — prevents brawler cross-contamination.
        if url.strip().lower() in ("disabled", "none", ""):
            return None
        return url

    def _fetch_queue_from_webapp(self, timeout=0.75):
        url = self._queue_api_url()
        if url is None:
            return None
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        queue = payload.get("queue") if isinstance(payload, dict) else payload
        return queue if isinstance(queue, list) and queue else None

    def _apply_live_queue(self, queue, source, reason):
        if not isinstance(queue, list) or not queue:
            return False

        old_order = self._queue_order(self.brawlers_pick_data)
        new_order = self._queue_order(queue)
        if not new_order or new_order == old_order:
            return False

        print(f"Queue reloaded from {source} before {reason}: {old_order[:8]} -> {new_order[:8]}")
        self.brawlers_pick_data = queue
        self.last_auto_selected_queue_key = None
        self.push_all_needs_selection = True

        self.started_trophies_by_brawler = {}
        for row in self.brawlers_pick_data:
            name = str(row.get("brawler", "")).lower()
            if name:
                self.started_trophies_by_brawler[name] = row.get("trophies", 0)

        current = self.brawlers_pick_data[0]
        self.Trophy_observer.change_trophies(self._number_or_default(current.get("trophies", 0), 0))
        self.Trophy_observer.current_wins = self._number_or_default(current.get("wins", 0), 0)
        self.Trophy_observer.win_streak = self._number_or_default(current.get("win_streak", 0), 0)
        save_brawler_data(self.brawlers_pick_data)
        return True

    def reload_queue_from_webapp(self, reason="queue check"):
        """Reload the canonical dashboard queue before choosing a brawler.

        For multi-instance child workers (PYLA_QUEUE_API_URL=disabled) the
        webapp is not available, so we fall directly to disk to avoid picking
        the wrong brawler from the main instance's queue.
        """
        if self._queue_api_url() is None:
            # Child worker: always use local file, never talk to main webapp.
            return self.reload_queue_from_disk(reason)
        try:
            webapp_queue = self._fetch_queue_from_webapp(timeout=0.75)
        except Exception as e:
            print(f"Could not reload queue from {self._queue_api_url()} ({reason}); falling back to latest_brawler_data.json. {e}")
            return self.reload_queue_from_disk(reason)
        return self._apply_live_queue(webapp_queue, "webapp /api/queue", reason)

    def reload_queue_from_disk(self, reason="queue check"):
        """Fallback reload from latest_brawler_data.json when the webapp endpoint is unavailable."""
        queue_path = "latest_brawler_data.json"
        if not os.path.exists(queue_path):
            return False

        try:
            with open(queue_path, "r", encoding="utf-8") as f:
                disk_queue = json.load(f)
        except Exception as e:
            print(f"Could not reload queue from latest_brawler_data.json ({reason}); keeping memory queue. {e}")
            return False

        return self._apply_live_queue(disk_queue, "latest_brawler_data.json", reason)

    def _current_queue_key(self):
        if not self.brawlers_pick_data:
            return None
        row = self.brawlers_pick_data[0]
        return (
            str(row.get("brawler", "")),
            str(row.get("type", "trophies")),
            self._number_or_default(row.get("push_until", 0), 0),
        )

    def mark_current_queue_entry_selected(self):
        queue_key = self._current_queue_key()
        if queue_key is not None:
            self.last_auto_selected_queue_key = queue_key

    def current_detected_state(self):
        try:
            return get_state(self.window_controller.screenshot())
        except Exception as e:
            print(f"Could not read current state: {e}")
            return "unknown"

    def ensure_lobby_before_selection(self, action):
        state = self.current_detected_state()
        if state == "lobby":
            return True
        print(f"{action} skipped: current state is {state}, not lobby.")
        self.window_controller.keys_up(list("wasd"))
        return False

    def ensure_lobby_before_start(self):
        state = self.current_detected_state()
        if state == "lobby":
            return True
        print(f"Start press skipped: current state changed to {state}; not pressing Q outside lobby.")
        self.window_controller.keys_up(list("wasd"))
        return False

    def select_current_queue_brawler(self, action="Auto-pick", mark_selected=True):
        """Select the exact brawler at the front of the queue.

        `selection_method` controls how Push All builds/sorts the queue. Once the
        first row is known, the in-game picker must search that named brawler.
        Tapping the first card after sorting can choose a different brawler when
        the Brawl Stars menu order does not match the saved queue.
        """
        if not self.brawlers_pick_data:
            return True

        row = self.brawlers_pick_data[0]
        brawler_name = str(row.get("brawler", "")).strip()
        selection_method = row.get("selection_method", "named_brawler")
        print(f"{action}: selecting queued brawler: {brawler_name or '<legacy lowest trophies>'}")

        try:
            if not self.ensure_lobby_before_selection(action):
                selected = False
            elif brawler_name:
                selected = self.Lobby_automation.select_brawler(brawler_name)
            elif selection_method == "lowest_trophies":
                # Legacy fallback for malformed/old queue rows that do not store
                # a brawler name. Normal webapp/Push All rows always have one.
                selected = self.Lobby_automation.select_lowest_trophy_brawler()
            else:
                selected = False
        except Exception as e:
            print(f"{action} failed with error: {e}")
            selected = False

        if selected and mark_selected:
            self.mark_current_queue_entry_selected()
        return selected

    def auto_select_current_brawler_if_needed(self):
        """Select the current queue brawler before starting a match when enabled.

        The old desktop GUI used the same `automatically_pick` field. The webapp
        now writes that field too, so the runner must honor it for the first
        queue entry as well as for entries reached after a completed target.
        """
        if not self.brawlers_pick_data:
            return True

        row = self.brawlers_pick_data[0]
        if not row.get("automatically_pick"):
            return True

        queue_key = self._current_queue_key()
        if queue_key == self.last_auto_selected_queue_key:
            return True

        brawler_name = row.get("brawler", "")
        print(f"Auto-pick enabled for current queue entry: {brawler_name}")

        if not self.select_current_queue_brawler("Auto-pick"):
            print("Auto-pick failed; match start is delayed so the wrong brawler is not pushed.")
            self.window_controller.keys_up(list("wasd"))
            return False

        return True

    def _prepare_next_push_all_brawler(self, target, type_of_push="trophies"):
        """Remove completed Push All rows and choose the current lowest remaining row.

        Push All queues are built from API trophies at launch, but the queue can
        become stale after each match. Re-sorting here keeps 250/500/750/1000
        targets on the same "least trophies next" behavior the player sees in
        the Brawl Stars brawler menu.
        """
        if not self.brawlers_pick_data:
            return False

        target = self._number_or_default(target, 1000 if type_of_push == "trophies" else 300)
        current_row = self.brawlers_pick_data[0]
        current_row[type_of_push] = self._number_or_default(
            getattr(self.Trophy_observer, f"current_{type_of_push}", current_row.get(type_of_push, 0)),
            current_row.get(type_of_push, 0),
        )
        current_row["win_streak"] = self.Trophy_observer.win_streak

        remaining = self.brawlers_pick_data[1:]
        if type_of_push == "trophies":
            remaining = [
                dict(row)
                for row in remaining
                if self._number_or_default(row.get("trophies", 0), 0)
                < self._number_or_default(row.get("push_until", target), target)
            ]
        else:
            remaining = [
                dict(row)
                for row in remaining
                if self._number_or_default(row.get("wins", 0), 0)
                < self._number_or_default(row.get("push_until", target), target)
            ]

        if not remaining:
            self.brawlers_pick_data = []
            save_brawler_data(self.brawlers_pick_data)
            return False

        if any(row.get("selection_method") == "lowest_trophies" for row in remaining):
            remaining.sort(
                key=lambda row: (
                    self._number_or_default(row.get(type_of_push, 0), 0),
                    str(row.get("brawler", "")),
                )
            )
            for row in remaining:
                row["selection_method"] = "lowest_trophies"
                row["automatically_pick"] = True

        self.brawlers_pick_data = remaining
        self.last_auto_selected_queue_key = None
        next_data = self.brawlers_pick_data[0]
        self.Trophy_observer.change_trophies(self._number_or_default(next_data.get("trophies", 0), 0))
        self.Trophy_observer.current_wins = self._number_or_default(next_data.get("wins", 0), 0)
        self.Trophy_observer.win_streak = self._number_or_default(next_data.get("win_streak", 0), 0)
        save_brawler_data(self.brawlers_pick_data)
        return True

    def refresh_push_all_trophies_from_api(self):
        if not self.brawlers_pick_data:
            return False
        if self.brawlers_pick_data[0].get("type", "trophies") != "trophies":
            return False
        if not any(row.get("selection_method") == "lowest_trophies" for row in self.brawlers_pick_data):
            return False

        old_front_brawler = self.brawlers_pick_data[0].get("brawler")
        try:
            player_data = self.fetch_push_all_player_data()
        except Exception as e:
            print(f"Push All Brawltracker trophy refresh failed; using local trophies. {e}")
            return False

        trophies_by_brawler = {
            normalize_brawler_name(brawler.get("name", "")): int(brawler.get("trophies", 0))
            for brawler in player_data.get("brawlers", [])
        }
        target = self._number_or_default(self.brawlers_pick_data[0].get("push_until", 1000), 1000)
        changed = False
        refreshed_rows = []
        for row in self.brawlers_pick_data:
            key = normalize_brawler_name(row.get("brawler", ""))
            refreshed_row = dict(row)
            is_current = refreshed_row.get("brawler") == old_front_brawler
            if key in trophies_by_brawler:
                api_trophies = trophies_by_brawler[key]
                if is_current:
                    local_trophies = self._number_or_default(
                        getattr(self.Trophy_observer, "current_trophies", refreshed_row.get("trophies", 0)),
                        refreshed_row.get("trophies", 0),
                    )
                    api_trophies = max(api_trophies, local_trophies)
                if refreshed_row.get("trophies") != api_trophies:
                    refreshed_row["trophies"] = api_trophies
                    changed = True
            # Never remove the currently-active brawler here — target completion
            # for the front brawler is handled exclusively by start_game()/end_game().
            # Removing it here causes the next brawler to appear as "current" in
            # notifications and trophies even though the task isn't finished yet.
            if is_current or self._number_or_default(refreshed_row.get("trophies", 0), 0) < target:
                refreshed_rows.append(refreshed_row)

        current_row = next(
            (row for row in refreshed_rows if row.get("brawler") == old_front_brawler),
            None,
        )
        remaining_rows = [
            row for row in refreshed_rows
            if row.get("brawler") != old_front_brawler
        ]

        if current_row is not None:
            remaining_rows.sort(
                key=lambda row: (
                    self._number_or_default(row.get("trophies", 0), 0),
                    str(row.get("brawler", "")),
                )
            )
            refreshed_rows = [current_row] + remaining_rows
            self.push_all_needs_selection = False
        else:
            refreshed_rows = remaining_rows
            self.push_all_needs_selection = bool(refreshed_rows)

        if refreshed_rows:
            # Preserve the first row's auto-pick flag. Older code forced it to
            # False, which made the webapp's "Automatically pick this brawler"
            # checkbox look enabled but do nothing for the active target.
            if "automatically_pick" not in refreshed_rows[0]:
                refreshed_rows[0]["automatically_pick"] = False
            refreshed_rows[0]["selection_method"] = "lowest_trophies"
            for row in refreshed_rows[1:]:
                if row.get("automatically_pick") is not True:
                    changed = True
                row["automatically_pick"] = True
                row["selection_method"] = "lowest_trophies"

        old_order = [row.get("brawler") for row in self.brawlers_pick_data]
        new_order = [row.get("brawler") for row in refreshed_rows]
        if new_order != old_order:
            changed = True

        if not refreshed_rows:
            self.brawlers_pick_data = []
            save_brawler_data(self.brawlers_pick_data)
            print("Push All Brawltracker trophies refreshed: all brawlers reached target.")
            return True

        if len(refreshed_rows) != len(self.brawlers_pick_data):
            changed = True

        old_front = old_order[0] if old_order else None
        new_front = new_order[0] if new_order else None
        if new_front != old_front:
            self.last_auto_selected_queue_key = None
        self.brawlers_pick_data = refreshed_rows

        # Only sync Trophy_observer trophies from the refreshed row when the
        # front brawler is unchanged — otherwise we'd overwrite the live trophy
        # count for the currently-active brawler with a potentially stale API value.
        if new_front == old_front:
            current_trophies = self._number_or_default(self.brawlers_pick_data[0].get("trophies", 0), 0)
            if getattr(self.Trophy_observer, "current_trophies", None) != current_trophies:
                self.Trophy_observer.change_trophies(current_trophies)
                changed = True

        if changed:
            if self.push_all_needs_selection:
                print("Push All Brawltracker trophies refreshed; current brawler reached target, selecting next lowest.")
            else:
                print("Push All Brawltracker trophies refreshed; keeping current brawler until target.")
            save_brawler_data(self.brawlers_pick_data)
        return changed

    @staticmethod
    def fetch_push_all_player_data():
        """Fetch Push All trophies from brawltracker, not official Brawl Stars API."""
        config = load_toml_as_dict("cfg/brawl_stars_api.toml")
        player_tag = str(config.get("player_tag", "")).strip()
        timeout = int(config.get("timeout_seconds", 15) or 15)
        return fetch_brawltracker_player(player_tag, timeout=timeout)

    def start_game(self):
        print("state is lobby, starting game")
        if self.farming_complete:
            self.window_controller.keys_up(list("wasd"))
            return 0
        if self.stop_after_post_match_rewards:
            return self.pause_farming_in_lobby("Post-match rewards cleared after completed target.")
        self.push_all_needs_selection = False
        self.reload_queue_from_webapp("lobby auto-pick")
        self.refresh_push_all_trophies_from_api()
        if not self.brawlers_pick_data:
            return self.pause_farming_in_lobby("All Push All targets completed.")
        values = {
            "trophies": self.Trophy_observer.current_trophies,
            "wins": self.Trophy_observer.current_wins
        }

        type_of_push = self.brawlers_pick_data[0]['type']
        if type_of_push not in values:
            type_of_push = "trophies"
        value = values[type_of_push]
        if value == "" and type_of_push == "wins":
            value = 0
        push_current_brawler_till = self.brawlers_pick_data[0]['push_until']
        if push_current_brawler_till == "" and type_of_push == "wins":
            push_current_brawler_till = 300
        if push_current_brawler_till == "" and type_of_push == "trophies":
            push_current_brawler_till = 1000

        if value >= push_current_brawler_till:
            if len(self.brawlers_pick_data) <= 1:
                print("Brawler reached required trophies/wins. No more brawlers selected for pushing in the menu. "
                      "Bot will now pause itself until closed.", value, push_current_brawler_till)
                screenshot = self.window_controller.screenshot()
                self.send_webhook_notification(
                    "completed",
                    screenshot,
                    self.current_target_details({"target": push_current_brawler_till}),
                )
                return self.pause_farming_in_lobby("All selected targets completed.")
            completed_brawler = self.brawlers_pick_data[0]["brawler"]
            screenshot = self.window_controller.screenshot()
            self.send_webhook_notification(
                "brawler_complete",
                screenshot,
                self.current_target_details({
                    "brawler": completed_brawler,
                    "target": push_current_brawler_till,
                    "brawlers_left": max(0, len(self.brawlers_pick_data) - 1),
                }),
            )
            if not self._prepare_next_push_all_brawler(push_current_brawler_till, type_of_push):
                print("Brawler reached required trophies/wins. No remaining brawlers are below the Push All target.")
                self.send_webhook_notification(
                    "completed",
                    screenshot,
                    self.current_target_details({"target": push_current_brawler_till}),
                )
                return self.pause_farming_in_lobby("All Push All targets completed.")
            if self.brawlers_pick_data[0]["automatically_pick"]:
                print("Picking next automatically picked brawler")
                screenshot = self.window_controller.screenshot()
                current_state = get_state(screenshot)
                if current_state != "lobby":
                    print("Trying to reach the lobby to switch brawler")

                max_attempts = 30
                attempts = 0
                while current_state != "lobby" and attempts < max_attempts:
                    self.window_controller.press_key("Q")
                    print("Pressed Q to return to lobby")
                    time.sleep(1)
                    screenshot = self.window_controller.screenshot()
                    current_state = get_state(screenshot)
                    attempts += 1
                if attempts >= max_attempts:
                    print("Failed to reach lobby after max attempts")
                else:
                    selected = self.select_current_queue_brawler("Next queued brawler selection")
                    if not selected:
                        print("Could not confirm the next brawler selection reached lobby; delaying match start.")
                        self.window_controller.keys_up(list("wasd"))
                        return
                    self.last_auto_selected_queue_key = self._current_queue_key()
            else:
                print("Next brawler is in manual mode, waiting 10 seconds to let user switch.")

        elif self.push_all_needs_selection:
            print("Push All queue changed from API; selecting the first queued brawler by name.")
            selected = self.select_current_queue_brawler("API-refreshed queued brawler selection")
            if not selected:
                print("Could not confirm the API-refreshed brawler selection reached lobby; delaying match start.")
                self.window_controller.keys_up(list("wasd"))
                return
            self.last_auto_selected_queue_key = self._current_queue_key()

        if not self.auto_select_current_brawler_if_needed():
            return 0

        if not self.ensure_lobby_before_start():
            return 0

        # q btn is over the start btn
        self.window_controller.keys_up(list("wasd"))
        self.window_controller.press_key("Q")
        print("Pressed Q to start a match")
    def advance_to_next_brawler_after_prestige(self):
        if not self.brawlers_pick_data:
            return False
        current_brawler = self.brawlers_pick_data[0].get("brawler", "current")
        print(f"Prestige reward detected for {current_brawler}; treating current brawler as completed.")
        self.brawlers_pick_data[0]["trophies"] = max(1000, int(self.brawlers_pick_data[0].get("trophies") or 0))
        self.brawlers_pick_data[0]["push_until"] = max(1000, int(self.brawlers_pick_data[0].get("push_until") or 1000))

        if len(self.brawlers_pick_data) <= 1:
            print("Prestige reward reached, but no next brawler is queued.")
            self.stop_after_post_match_rewards = True
            save_brawler_data(self.brawlers_pick_data)
            return False

        self.brawlers_pick_data.pop(0)
        self.last_auto_selected_queue_key = None
        next_data = self.brawlers_pick_data[0]
        self.Trophy_observer.change_trophies(next_data.get("trophies", 0))
        self.Trophy_observer.current_wins = next_data.get("wins", 0) if next_data.get("wins", "") != "" else 0
        self.Trophy_observer.win_streak = next_data.get("win_streak", 0)
        save_brawler_data(self.brawlers_pick_data)
        return True

    def read_lobby_trophies_from_screenshot(self, screenshot):
        height, width = screenshot.shape[:2]
        width_ratio = width / 1920
        height_ratio = height / 1080
        x1 = int(700 * width_ratio)
        y1 = int(58 * height_ratio)
        x2 = int(990 * width_ratio)
        y2 = int(165 * height_ratio)
        crop = screenshot[y1:y2, x1:x2]
        if crop.size == 0:
            return None

        try:
            crop = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
            texts = extract_text_strings(crop)
        except Exception as e:
            print(f"Could not OCR lobby trophies after reward: {e}")
            return None

        for text in texts:
            value = self.validate_trophies(text)
            if value is not False and 0 <= value <= 5000:
                return value
        print(f"Could not read lobby trophies after reward from OCR: {texts}")
        return None

    def wait_for_lobby_after_reward(self, max_attempts=30):
        screenshot = self.window_controller.screenshot()
        current_state = get_state(screenshot)
        attempts = 0
        while current_state != "lobby" and attempts < max_attempts:
            self.window_controller.press_key("Q")
            time.sleep(1.0)
            screenshot = self.window_controller.screenshot()
            current_state = get_state(screenshot)
            attempts += 1
        return screenshot if current_state == "lobby" else None

    def handle_star_drop(self):
        screenshot = self.window_controller.screenshot()
        screenshot_bgr = cv2.cvtColor(screenshot, cv2.COLOR_RGB2BGR)
        drop_type = get_star_drop_type(screenshot_bgr)
        if drop_type is None:
            return

        print(f"{drop_type.title()} star drop detected; opening with Q.")
        self.window_controller.keys_up(list("wasd"))

        long_press_types = ("angelic", "demonic", "starr_nova")
        attempts = 4 if drop_type in long_press_types else 6
        hold_time = 0.35 if drop_type in long_press_types else 0.06
        delay = 0.35 if drop_type in long_press_types else 0.10
        for _ in range(attempts):
            self.window_controller.press_key("Q", delay=hold_time)
            time.sleep(delay)

    def handle_prestige_reward(self):
        screenshot = self.window_controller.screenshot()
        screenshot_bgr = cv2.cvtColor(screenshot, cv2.COLOR_RGB2BGR)
        next_button_center = get_prestige_next_button_center(screenshot_bgr)
        if next_button_center is None or not is_in_prestige_reward(screenshot_bgr):
            print("Prestige reward state ignored; NEXT button was not confirmed.")
            return

        print("Prestige reward screen detected; clicking NEXT.")
        self.window_controller.keys_up(list("wasd"))
        self.window_controller.click(*next_button_center)
        time.sleep(1.0)

        lobby_screenshot = self.wait_for_lobby_after_reward()
        if lobby_screenshot is None:
            print("Could not reach lobby after reward; will retry from normal state loop.")
            return

        lobby_trophies = self.read_lobby_trophies_from_screenshot(lobby_screenshot)
        if lobby_trophies is not None and self.brawlers_pick_data:
            print(f"Lobby trophies after reward: {lobby_trophies}")
            self.Trophy_observer.change_trophies(lobby_trophies)
            self.brawlers_pick_data[0]["trophies"] = lobby_trophies
            save_brawler_data(self.brawlers_pick_data)

        local_trophies = self.Trophy_observer.current_trophies

        if lobby_trophies is not None:
            if lobby_trophies > 20:
                print(
                    f"Reward screen did not confirm a 1k trophy reset "
                    f"(lobby trophies={lobby_trophies}); not forcing brawler switch."
                )
                return
            print(f"Lobby trophies after prestige reward: {lobby_trophies} — confirmed 1k reset.")
        else:
            target = self._number_or_default(
                self.brawlers_pick_data[0].get("push_until", 1000) if self.brawlers_pick_data else 1000,
                1000,
            )
            if local_trophies < target:
                print(
                    f"Could not read lobby trophies after prestige; "
                    f"local trophies={local_trophies} < target={target}. "
                    f"Brawler has NOT reached {target} yet — skipping brawler switch."
                )
                return
            print(
                f"Could not read lobby trophies after prestige; "
                f"local trophies={local_trophies} >= target={target} — proceeding with brawler switch."
            )

        if not self.advance_to_next_brawler_after_prestige():
            self.window_controller.press_key("Q")
            return

        if not self.select_current_queue_brawler("Prestige reward queued brawler selection"):
            print("Could not switch after prestige reward; delaying next match start.")
            self.window_controller.keys_up(list("wasd"))

    def should_use_play_again(self, value=0, target=0):
        if self.post_match_action != "play_again":
            return False
        try:
            return int(value) < int(target)
        except (TypeError, ValueError):
            return True

    def _scaled_crop(self, image, region):
        if image is None or image.size == 0:
            return None
        height, width = image.shape[:2]
        x, y, w, h = region
        x1 = max(0, int(x * width / 1920))
        y1 = max(0, int(y * height / 1080))
        x2 = min(width, int((x + w) * width / 1920))
        y2 = min(height, int((y + h) * height / 1080))
        crop = image[y1:y2, x1:x2]
        return crop if crop.size else None

    @staticmethod
    def _button_color_ratios(crop):
        import numpy as np
        hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
        blue   = cv2.inRange(hsv, np.array((95, 80, 100),  dtype=np.uint8), np.array((125, 255, 255), dtype=np.uint8))
        green  = cv2.inRange(hsv, np.array((42, 70, 100),  dtype=np.uint8), np.array((82,  255, 255), dtype=np.uint8))
        yellow = cv2.inRange(hsv, np.array((18, 70, 110),  dtype=np.uint8), np.array((38,  255, 255), dtype=np.uint8))
        dark   = cv2.inRange(hsv, np.array((0,  0,  0),    dtype=np.uint8), np.array((179, 255, 90),  dtype=np.uint8))
        total = max(1, crop.shape[0] * crop.shape[1])
        return {
            "button": (cv2.countNonZero(blue) + cv2.countNonZero(green) + cv2.countNonZero(yellow)) / total,
            "dark":   cv2.countNonZero(dark) / total,
        }

    def is_play_again_button_visually_available(self, screenshot):
        play_crop = self._scaled_crop(screenshot, [1030, 850, 360, 150])
        if play_crop is None:
            return False
        ratios = self._button_color_ratios(play_crop)
        return ratios["button"] > 0.18 and ratios["dark"] > 0.035

    def get_play_again_text_state(self, screenshot):
        try:
            height, width = screenshot.shape[:2]
            button_crop = screenshot[int(height * 0.78):height, int(width * 0.72):width]
            texts = extract_text_strings(button_crop)
        except Exception:
            return ""
        normalized_words = [normalize_brawler_name(text) for text in texts]
        normalized_text = " ".join(normalized_words)
        compact_text = "".join(normalized_words)
        play_again_visible = (
            "play" in normalized_text and "again" in normalized_text
        ) or "playagain" in compact_text
        if play_again_visible:
            return "play_again"
        if "exit" in normalized_text:
            return "exit"
        return ""

    def get_play_again_missing_exit_center(self, screenshot, allow_ocr=False):
        if screenshot is None or screenshot.size == 0:
            return None
        play_crop = self._scaled_crop(screenshot, [1030, 850, 360, 150])
        exit_crop = self._scaled_crop(screenshot, [1480, 850, 380, 170])
        if exit_crop is None:
            return None
        exit_ratios = self._button_color_ratios(exit_crop)
        play_ratios = self._button_color_ratios(play_crop) if play_crop is not None else {"button": 0.0, "dark": 0.0}
        if exit_ratios["button"] > 0.20 and exit_ratios["dark"] > 0.035 and play_ratios["button"] < 0.12:
            return (
                int(1660 * self.window_controller.width_ratio),
                int(980  * self.window_controller.height_ratio),
            )
        if not allow_ocr:
            return None
        text_state = self.get_play_again_text_state(screenshot)
        if text_state != "exit":
            return None
        return (
            int(1660 * self.window_controller.width_ratio),
            int(980  * self.window_controller.height_ratio),
        )

    def click_play_again_button(self):
        self.window_controller.click(
            int(1215 * self.window_controller.width_ratio),
            int(935  * self.window_controller.height_ratio),
            delay=0.08,
        )

    def dismiss_end_screen(self, use_play_again=False):
        """Dismiss the post-match end screen, optionally clicking Play Again."""
        self.window_controller.keys_up(list("wasd"))
        if use_play_again:
            screenshot = self.window_controller.screenshot()
            if self.is_play_again_button_visually_available(screenshot):
                print("Post-match action: clicking PLAY AGAIN.")
                self.click_play_again_button()
                return
            exit_center = self.get_play_again_missing_exit_center(screenshot, allow_ocr=False)
            if exit_center is not None:
                print("Play Again unavailable; clicking EXIT to requeue from lobby.")
                self.window_controller.click(*exit_center, delay=0.08)
                return
            text_state = self.get_play_again_text_state(screenshot)
            if text_state == "play_again":
                print("Post-match action: clicking PLAY AGAIN.")
                self.click_play_again_button()
                return
            if text_state == "exit":
                print("Play Again unavailable; clicking EXIT to requeue from lobby.")
                self.window_controller.click(
                    int(1660 * self.window_controller.width_ratio),
                    int(980  * self.window_controller.height_ratio),
                    delay=0.08,
                )
                return
            print("Play Again button is not enabled; pressing continue instead.")
        self.window_controller.press_key("Q")

    def end_game(self):
        screenshot = self.window_controller.screenshot()

        found_game_result = False
        current_state = get_state(screenshot)
        button_pressed = False
        end_screen_time = time.time()

        # If this is a re-entry on the same lingering end-of-match screen,
        # skip recording and just keep trying to dismiss it.
        current_result = current_state.split("_", 1)[1] if current_state.startswith("end_") else None
        already_recorded = current_result is not None and self.active_end_result == current_result
        stats_recorded = already_recorded
        use_play_again = False
        if already_recorded:
            found_game_result = current_result
            print(f"end_game: re-entry on '{current_state}', skipping trophy update")

        while current_state.startswith("end") and time.time() - end_screen_time < 25:
            if not stats_recorded:
                found_game_result = current_state.split("_")[1]
                current_brawler = self.brawlers_pick_data[0]['brawler']
                self.Trophy_observer.add_trophies(found_game_result, current_brawler)
                self.Trophy_observer.add_win(found_game_result)
                self.adaptive_brain.record_result(found_game_result)
                self.time_since_last_stat_change = time.time()
                self.last_recorded_result = found_game_result
                self.last_recorded_result_time = time.time()
                self.active_end_result = found_game_result
                stats_recorded = True
                values = {
                    "trophies": self.Trophy_observer.current_trophies,
                    "wins": self.Trophy_observer.current_wins
                }
                type_to_push = self.brawlers_pick_data[0]['type']
                if type_to_push not in values:
                    type_to_push = "trophies"
                value = values[type_to_push]
                self.brawlers_pick_data[0][type_to_push] = value
                self.brawlers_pick_data[0]['win_streak'] = self.Trophy_observer.win_streak
                save_brawler_data(self.brawlers_pick_data)
                self.send_webhook_notification(
                    "match",
                    screenshot,
                    self.current_target_details({
                        "result": found_game_result,
                        "target": self.brawlers_pick_data[0].get("push_until", ""),
                    }),
                )
                push_current_brawler_till = self.brawlers_pick_data[0]['push_until']

                if value == "" and type_to_push == "wins":
                    value = 0
                if push_current_brawler_till == "" and type_to_push == "wins":
                    push_current_brawler_till = 300
                if push_current_brawler_till == "" and type_to_push == "trophies":
                    push_current_brawler_till = 1000
                push_current_brawler_till = self._number_or_default(
                    push_current_brawler_till,
                    1000 if type_to_push == "trophies" else 300,
                )
                value = self._number_or_default(value, 0)

                use_play_again = self.should_use_play_again(value, push_current_brawler_till)

                if value >= push_current_brawler_till:
                    use_play_again = False
                    if len(self.brawlers_pick_data) <= 1:
                        print(
                            "Brawler reached required trophies/wins. No more brawlers selected for pushing in the menu. "
                            "Bot will finish reward screens before stopping.")
                        self.stop_after_post_match_rewards = True
                        if not self.completion_notification_sent:
                            screenshot = self.window_controller.screenshot()
                            self.send_webhook_notification(
                                "completed",
                                screenshot,
                                self.current_target_details({
                                    "result": found_game_result,
                                    "target": push_current_brawler_till,
                                }),
                            )
                            self.completion_notification_sent = True
                    else:
                        print(
                            "Brawler reached required trophies/wins. "
                            "Will switch brawler as soon as lobby is reached.",
                            value,
                            push_current_brawler_till,
                        )

            self.dismiss_end_screen(use_play_again=use_play_again)
            button_pressed = True

            time.sleep(self.end_screen_dismiss_delay)
            screenshot = self.window_controller.screenshot()
            current_state = get_state(screenshot)

        print("Game has ended", current_state)
        if self.starr_drop is not None:
            self.starr_drop.force_active_for(60)

    def set_starr_drop(self, starr_drop_integration) -> None:
        self.starr_drop = starr_drop_integration

    def quit_shop(self):
        self.window_controller.click(100*self.window_controller.width_ratio, 60*self.window_controller.height_ratio)

    def quit_brawler_selection(self):
        """Close the brawler selection screen by clicking the back arrow in the top-left corner.

        Previously this reused quit_shop (click at 100,60) which does not reliably
        close the brawler picker — the bot would get stuck in a loop:
        brawler_selection -> popup (team invite) -> shop -> brawler_selection -> ...
        The back arrow region is at [0, 0, 175, 110] per lobby_config.toml, so we
        click the centre of that region to navigate back to the lobby.
        """
        try:
            region = self.lobby_config.get('template_matching', {}).get('go_back_arrow', [0, 0, 175, 110])
            x = int((region[0] + region[2] / 2) * self.window_controller.width_ratio)
            y = int((region[1] + region[3] / 2) * self.window_controller.height_ratio)
        except Exception:
            # Fallback: safe centre of back-arrow region
            x = int(87 * self.window_controller.width_ratio)
            y = int(55 * self.window_controller.height_ratio)
        self.window_controller.click(x, y)

    def close_pop_up(self):
        screenshot = self.window_controller.screenshot()
        team_invite_reject = get_team_invite_reject_button_center(screenshot, image_is_rgb=True)
        if team_invite_reject:
            now = time.time()
            if now - self.last_team_invite_reject_time < 0.6:
                return
            self.last_team_invite_reject_time = now
            print("Team invite popup detected; rejecting invite.")
            self.window_controller.keys_up(list("wasd"))
            self.window_controller.click(*team_invite_reject, delay=0.08)
            self.tap_with_adb_fallback(*team_invite_reject, screenshot_shape=screenshot.shape)
            return
        if self.close_popup_icon is None:
            self.close_popup_icon = load_image("images/states/close_popup.png", self.window_controller.scale_factor)
        popup_location = find_template_center(screenshot, self.close_popup_icon)
        if popup_location:
            self.window_controller.click(*popup_location)

    def tap_with_adb_fallback(self, x, y, screenshot_shape=None):
        try:
            device = getattr(self.window_controller, "device", None)
            if device is None:
                return False
            target_x = x
            target_y = y
            if screenshot_shape is not None:
                frame_h, frame_w = screenshot_shape[:2]
                size = device.window_size()
                target_x = x * (size.width / max(1, frame_w))
                target_y = y * (size.height / max(1, frame_h))
            device.shell(f"input tap {int(target_x)} {int(target_y)}")
            return True
        except Exception as e:
            print(f"ADB fallback tap failed: {e}")
            return False

    def do_state(self, state, data=None):
        if not str(state).startswith("end"):
            self.active_end_result = None
        if data is not None:
            self.states[state](data)
            return
        self.states[state]()
