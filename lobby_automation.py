from difflib import SequenceMatcher
import os
import time

import cv2
import numpy as np

from typization import BrawlerName
from state_finder import get_state
from utils import extract_text_and_positions, count_hsv_pixels, load_toml_as_dict, find_template_center

debug = load_toml_as_dict("cfg/general_config.toml")['super_debug'] == "yes"
gray_pixels_treshold = load_toml_as_dict("./cfg/bot_config.toml")['idle_pixels_minimum']


class LobbyAutomation:

    def __init__(self, window_controller):
        self.coords_cfg = load_toml_as_dict("./cfg/lobby_config.toml")
        self.window_controller = window_controller
        self.brawler_icons_dir = os.path.join("api", "assets", "brawler_icons2")
        self.brawler_icons = self.load_brawler_icons()
        self.selected_brawler_key = None
        self.selected_brawler_name = None

    def check_for_idle(self, frame):
        general_config = load_toml_as_dict("cfg/general_config.toml")
        bot_config = load_toml_as_dict("./cfg/bot_config.toml")
        debug_enabled = str(general_config.get("super_debug", "no")).lower() in ("yes", "true", "1")
        gray_pixels_threshold = bot_config.get("idle_pixels_minimum", gray_pixels_treshold)
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio
        x_start, x_end = int(700 * wr), int(1220 * wr)
        y_start, y_end = int(470 * hr), int(620 * hr)
        gray_pixels = count_hsv_pixels(frame[y_start:y_end, x_start:x_end], (0, 0, 18), (10, 20, 100))
        if debug_enabled:
            print(f"gray pixels (if > {gray_pixels_threshold} then bot will try to unidle) :", gray_pixels)
        if gray_pixels > gray_pixels_threshold:
            self.window_controller.click(int(535 * wr), int(615 * hr))

    def select_brawler(self, brawler):
        current_state = self.current_state()
        if current_state != "lobby":
            raise RuntimeError(
                f"Named brawler selection skipped: current state is {current_state}, not lobby."
            )

        target_key = self.normalize_ocr_name(brawler)

        if self.selected_brawler_key == target_key:
            print(f"Brawler {brawler} is already selected; skipping auto-pick.")
            return True

        self.window_controller.screenshot()
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio
        general_config = load_toml_as_dict("cfg/general_config.toml")
        debug_enabled = str(general_config.get("super_debug", "no")).lower() in ("yes", "true", "1")
        try:
            ocr_scale = float(general_config.get("ocr_scale_down_factor", 0.65))
        except (TypeError, ValueError):
            ocr_scale = 0.65
        ocr_scale = max(0.35, min(1.0, ocr_scale))

        x = self.coords_cfg['lobby']['brawler_btn'][0] * wr
        y = self.coords_cfg['lobby']['brawler_btn'][1] * hr
        self.window_controller.click(x, y)
        time.sleep(0.8)

        c = 0
        found_brawler = False
        reworked_results = {}
        icon_available = self.get_brawler_icon(target_key) is not None

        for i in range(50):
            screenshot_full = self.window_controller.screenshot()

            icon_match = self.find_brawler_icon_on_screen(screenshot_full, target_key)
            if icon_match is not None:
                click_x, click_y, score = icon_match
                self.window_controller.click(click_x, click_y)
                print("Found brawler ", brawler, f"(ICON: {score:.2f}) clicking on its icon at ", click_x, click_y)
                time.sleep(1)
                select_x = self.coords_cfg['lobby']['select_btn'][0]
                select_y = self.coords_cfg['lobby']['select_btn'][1]
                self.window_controller.click(select_x, select_y, already_include_ratio=False)
                time.sleep(0.5)
                print("Selected brawler ", brawler)
                self.selected_brawler_key = target_key
                self.selected_brawler_name = brawler
                found_brawler = True
                break

            if not icon_available:
                screenshot = cv2.resize(
                    screenshot_full,
                    (int(screenshot_full.shape[1] * ocr_scale), int(screenshot_full.shape[0] * ocr_scale)),
                    interpolation=cv2.INTER_AREA,
                )

                if debug_enabled:
                    print("extracting text on current screen...")
                results = extract_text_and_positions(screenshot)
                reworked_results = {}
                for key in results.keys():
                    orig_key = key
                    key = self.normalize_ocr_name(key)
                    key = self.resolve_ocr_typos(key)
                    reworked_results[key] = results[orig_key]
                if debug_enabled:
                    print("All detected text while looking for brawler name:", reworked_results.keys())
                    print()
                matches = []
                for detected_name, text_box in reworked_results.items():
                    if self.names_match(detected_name, target_key):
                        score = self.name_match_score(detected_name, target_key)
                        matches.append((score, detected_name, text_box))
                if matches:
                    matches.sort(key=lambda item: item[0], reverse=True)
                    _, detected_name, text_box = matches[0]
                    x, y = text_box['center']
                    click_x = int(x / ocr_scale)
                    click_y = int((y / ocr_scale) - (95 * hr))
                    click_y = max(0, min(screenshot_full.shape[0] - 1, click_y))
                    self.window_controller.click(click_x, click_y)
                    print("Found brawler ", brawler, f"(OCR: {detected_name}) clicking on its icon at ", click_x, click_y)
                    time.sleep(1)
                    select_x = self.coords_cfg['lobby']['select_btn'][0]
                    select_y = self.coords_cfg['lobby']['select_btn'][1]
                    self.window_controller.click(select_x, select_y, already_include_ratio=False)
                    time.sleep(0.5)
                    print("Selected brawler ", brawler)
                    self.selected_brawler_key = target_key
                    self.selected_brawler_name = brawler
                    found_brawler = True
                    break

            if c == 0:
                wr = self.window_controller.width_ratio
                hr = self.window_controller.height_ratio

            scroll_x = int(1794 * wr)
            scroll_y_start = int(900 * hr)
            scroll_y_end = int(600 * hr)
            self.window_controller.swipe(scroll_x, scroll_y_start, scroll_x, scroll_y_end, duration=0.45)
            time.sleep(0.15 if c == 0 else 0.8)
            c += 1
            if c == 1:
                continue

        if not found_brawler:
            print(f"WARNING: Brawler '{brawler}' was not found after 50 scroll attempts. ")
            if reworked_results:
                print(f"Detected brawlers during search: {list(reworked_results.keys())}")
            else:
                print("Detected brawlers during search: []")
            raise ValueError(f"Brawler '{brawler}' could not be found in brawler selection menu.")

        return True

    def load_brawler_icons(self):
        icons = {}
        if not os.path.isdir(self.brawler_icons_dir):
            return icons
        for filename in os.listdir(self.brawler_icons_dir):
            lower = filename.lower()
            if not lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            path = os.path.join(self.brawler_icons_dir, filename)
            icon = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if icon is None:
                continue
            key = self.normalize_ocr_name(os.path.splitext(filename)[0])
            icons[key] = icon
        return icons

    def get_brawler_icon(self, target_key):
        target_key = self.normalize_ocr_name(target_key)
        if target_key in self.brawler_icons:
            return self.brawler_icons[target_key]
        resolved_key = self.resolve_ocr_typos(target_key)
        if resolved_key in self.brawler_icons:
            return self.brawler_icons[resolved_key]
        best_key = None
        best_score = 0.0
        for key in self.brawler_icons.keys():
            if self.names_match(key, target_key) or self.names_match(target_key, key):
                score = self.name_match_score(key, target_key)
                if score > best_score:
                    best_score = score
                    best_key = key
        if best_key is not None:
            return self.brawler_icons[best_key]
        return None

    def find_brawler_icon_on_screen(self, frame, target_key):
        icon = self.get_brawler_icon(target_key)
        if icon is None or frame is None:
            return None

        if len(frame.shape) == 2:
            frame_gray = frame
        else:
            frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if len(icon.shape) == 2:
            icon_gray = icon
        else:
            if icon.shape[2] == 4:
                icon = cv2.cvtColor(icon, cv2.COLOR_BGRA2BGR)
            icon_gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY)

        best_score = 0.0
        best_loc = None
        best_size = None

        for scale in np.arange(0.5, 1.61, 0.1):
            resized = cv2.resize(icon_gray, (0, 0), fx=float(scale), fy=float(scale), interpolation=cv2.INTER_AREA)
            h, w = resized.shape[:2]
            if h < 8 or w < 8:
                continue
            if h >= frame_gray.shape[0] or w >= frame_gray.shape[1]:
                continue
            result = cv2.matchTemplate(frame_gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best_score:
                best_score = float(max_val)
                best_loc = max_loc
                best_size = (w, h)

        if best_loc is None or best_size is None:
            return None

        threshold = 0.58
        if best_score < threshold:
            return None

        x, y = best_loc
        w, h = best_size
        return int(x + w // 2), int(y + h // 2), best_score

    def current_state(self):
        try:
            return get_state(self.window_controller.screenshot())
        except Exception as e:
            print(f"Could not read current state before brawler selection: {e}")
            return "unknown"

    def select_lowest_trophy_brawler(self):
        current_state = self.current_state()
        if current_state != "lobby":
            print(
                "Lowest-trophy brawler selection skipped: "
                f"current state is {current_state}, not lobby."
            )
            return False

        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio

        def tap(x, y, wait=0.6):
            self.window_controller.click(int(x * wr), int(y * hr))
            time.sleep(wait)

        print("Selecting next brawler by sorting lowest trophies.")
        tap(128, 500, 1.4)
        tap(1210, 45, 0.6)
        tap(1210, 426, 1.0)
        tap(422, 359, 1.0)
        tap(260, 991, 1.0)
        if self.ensure_lobby_after_selection():
            return True

        recovery_state = self.current_state()
        if recovery_state != "brawler_selection":
            print(
                "Lowest-trophy brawler selection failed and recovery is blocked: "
                f"current state is {recovery_state}, not brawler_selection."
            )
            return False

        print("Lowest-trophy brawler selection did not return to lobby; trying one recovery pass.")
        tap(260, 991, 1.0)
        return self.ensure_lobby_after_selection()

    def ensure_lobby_after_selection(self, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = get_state(self.window_controller.screenshot())
            except Exception as e:
                print(f"Could not verify lobby after brawler selection: {e}")
                return False
            if state == "lobby":
                return True
            if state == "brawler_selection":
                self.window_controller.click(
                    int(260 * self.window_controller.width_ratio),
                    int(991 * self.window_controller.height_ratio),
                )
            elif state in ("match", "match_making"):
                print(
                    "Brawler selection verification saw "
                    f"{state}; stopping selection so it does not tap during a match."
                )
                return False
            time.sleep(0.7)
        return False

    def press_back(self):
        if hasattr(self.window_controller, "android_back") and self.window_controller.android_back():
            return
        self.window_controller.click(
            int(100 * self.window_controller.width_ratio),
            int(60 * self.window_controller.height_ratio),
        )

    @staticmethod
    def resolve_ocr_typos(potential_brawler_name: str) -> str:
        matched_typo: str | None = {
            'shey': BrawlerName.Shelly.value,
            'shlly': BrawlerName.Shelly.value,
            'larryslawrie': BrawlerName.Larry.value,
            '[eon': BrawlerName.Leon.value,
            'brock': BrawlerName.Brock.value,
            'brok': BrawlerName.Brock.value,
            'gal': BrawlerName.Gale.value,
            'gale': BrawlerName.Gale.value,
            'darryl': BrawlerName.Darryl.value,
            'daryl': BrawlerName.Darryl.value,
            'dary': BrawlerName.Darryl.value,
        }.get(potential_brawler_name, None)

        return matched_typo or potential_brawler_name

    @staticmethod
    def normalize_ocr_name(value: str) -> str:
        normalized = str(value).lower()
        for symbol in [' ', '-', '.', "&", "'", "`", "_"]:
            normalized = normalized.replace(symbol, "")
        return normalized

    @staticmethod
    def bounded_edit_distance(left: str, right: str, limit: int) -> int:
        if abs(len(left) - len(right)) > limit:
            return limit + 1
        previous = list(range(len(right) + 1))
        for i, left_char in enumerate(left, 1):
            current = [i]
            best = current[0]
            for j, right_char in enumerate(right, 1):
                cost = 0 if left_char == right_char else 1
                value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
                current.append(value)
                best = min(best, value)
            if best > limit:
                return limit + 1
            previous = current
        return previous[-1]

    @classmethod
    def names_match(cls, detected_name: str, target_name: str) -> bool:
        if detected_name == target_name:
            return True
        if len(target_name) >= 4 and (target_name in detected_name or detected_name in target_name):
            return True
        limit = 1 if len(target_name) <= 5 else 2
        if cls.bounded_edit_distance(detected_name, target_name, limit) <= limit:
            return True
        return SequenceMatcher(None, detected_name, target_name).ratio() >= 0.84

    @classmethod
    def name_match_score(cls, detected_name: str, target_name: str) -> float:
        if detected_name == target_name:
            return 2.0
        ratio = SequenceMatcher(None, detected_name, target_name).ratio()
        distance = cls.bounded_edit_distance(detected_name, target_name, 3)
        return ratio - (distance * 0.05)