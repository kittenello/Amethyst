# fuck this shit ass, i was tried about 5 hours to refactor this

from difflib import SequenceMatcher
import os
import time

import cv2

from typization import BrawlerName
from state_finder import get_state
from utils import (
    extract_text_and_positions,
    extract_text_strings,
    count_hsv_pixels,
    load_toml_as_dict,
    load_brawlers_info,
)

gray_pixels_treshold = load_toml_as_dict("./cfg/bot_config.toml")['idle_pixels_minimum']


class LobbyAutomation:

    def __init__(self, window_controller):
        self.coords_cfg = load_toml_as_dict("./cfg/lobby_config.toml")
        self.window_controller = window_controller
        self.selected_brawler_key = None
        self.selected_brawler_name = None
        self.known_brawler_names = self._load_known_brawler_names()


    def _read_state(self):
        try:
            screenshot = self.window_controller.screenshot()
            if screenshot is None:
                return None
            return get_state(screenshot)
        except Exception as e:
            debug_enabled = str(load_toml_as_dict("cfg/general_config.toml").get("super_debug", "no")).lower() in ("yes", "true", "1")
            if debug_enabled:
                print(f"Could not read state while opening brawler menu: {e}")
            return None

    @staticmethod
    def _load_known_brawler_names():
        try:
            return {
                LobbyAutomation.normalize_ocr_name(name)
                for name in load_brawlers_info().keys()
                if name
            }
        except Exception:
            return set()

    def is_probably_brawler_selection_screen(self, screenshot=None):
        try:
            if screenshot is None:
                screenshot = self.window_controller.screenshot()
            if screenshot is None:
                return False
            results = extract_text_and_positions(screenshot)
        except Exception:
            return False

        known_names = getattr(self, "known_brawler_names", None) or self._load_known_brawler_names()
        self.known_brawler_names = known_names

        normalized_texts = {
            self.resolve_ocr_typos(self.normalize_ocr_name(text))
            for text in results.keys()
        }
        known_hits = len(normalized_texts & known_names)
        selection_words = {
            "brawlers", "brawler", "sortby", "leasttrophies",
            "mosttrophies", "trophies", "locked", "upgrade",
        }
        selection_word_hits = len(normalized_texts & selection_words)
        return known_hits >= 2 or (known_hits >= 1 and selection_word_hits >= 1)

    def click_visible_brawler_menu_button(self):
        try:
            screenshot = self.window_controller.screenshot()
            if screenshot is None:
                return False
            results = extract_text_and_positions(screenshot)
        except Exception:
            return False

        for text, box in results.items():
            if self.normalize_ocr_name(text) not in {"brawlers", "brawler"}:
                continue
            center = box.get("center")
            if not center:
                continue
            x, y = center
            if x > screenshot.shape[1] * 0.35:
                continue
            self.window_controller.click(int(x), int(y))
            return True
        return False

    def open_brawler_selection(self, attempts=None):
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio
        cfg_point = tuple(self.coords_cfg.get("lobby", {}).get("brawler_btn", (110, 490)))
        brawler_button_points = (
            (70, 500), (90, 500), (110, 490), (128, 500), (60, 535),
            (145, 505), cfg_point, (76, 420), (98, 420), (122, 420),
            (72, 455), (100, 455), (132, 455), (82, 385), (112, 385),
        )
        if attempts is None:
            attempts = len(brawler_button_points)

        state = self._read_state()
        if state == "brawler_selection":
            return True
        if state == "shop" and self.is_probably_brawler_selection_screen():
            return True

        if state == "lobby" and self.click_visible_brawler_menu_button():
            time.sleep(0.8)
            state = self._read_state()
            if state == "brawler_selection":
                return True
            if state == "shop" and self.is_probably_brawler_selection_screen():
                return True

        for attempt in range(attempts):
            if state == "shop":
                if self.is_probably_brawler_selection_screen():
                    return True
                self.press_back()
                time.sleep(0.8)
                state = self._read_state()
                if state == "brawler_selection":
                    return True
                if state == "shop" and self.is_probably_brawler_selection_screen():
                    return True
                if state == "lobby" and self.click_visible_brawler_menu_button():
                    time.sleep(0.8)
                    state = self._read_state()
                    if state == "brawler_selection":
                        return True
                    if state == "shop" and self.is_probably_brawler_selection_screen():
                        return True

            x, y = brawler_button_points[min(attempt, len(brawler_button_points) - 1)]
            self.window_controller.click(int(x * wr), int(y * hr))
            time.sleep(0.8)
            state = self._read_state()
            if state == "brawler_selection":
                return True
            if state == "shop" and self.is_probably_brawler_selection_screen():
                return True
            if state == "shop":
                continue
            if state is None:
                return True

        return False

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
        self.window_controller.screenshot()
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio
        general_config = load_toml_as_dict("cfg/general_config.toml")
        debug_enabled = str(general_config.get("super_debug", "no")).lower() in ("yes", "true", "1")
        try:
            ocr_scale = float(general_config.get("ocr_scale_down_factor", 0.65))
        except (TypeError, ValueError):
            ocr_scale = 0.65
        ocr_scale = max(1.2, ocr_scale * 1.8)
        target_key = self.normalize_ocr_name(brawler)

        if not self.open_brawler_selection():
            print(f"WARNING: Could not open brawler selection menu for '{brawler}'. "
                  "Continuing with the currently selected brawler instead of crashing.")
            self.press_back()
            return False

        c = 0
        found_brawler = False

        for i in range(50):
            screenshot_full = self.window_controller.screenshot()
            full_h = screenshot_full.shape[0]

            fh_full, fw_full = screenshot_full.shape[:2]
            try:
                _scale = 2.0
                _big = cv2.resize(screenshot_full, None, fx=_scale, fy=_scale,
                                  interpolation=cv2.INTER_CUBIC)
                _gray = cv2.cvtColor(_big, cv2.COLOR_RGB2GRAY)
                _, _thresh = cv2.threshold(_gray, 180, 255, cv2.THRESH_BINARY)
                _ocr_input = cv2.cvtColor(_thresh, cv2.COLOR_GRAY2RGB)
                ocr_results_raw = extract_text_and_positions(_ocr_input)
                ocr_results = {}
                for text, box in ocr_results_raw.items():
                    scaled_box = {}
                    for key, val in box.items():
                        if key in ("center", "top_left", "top_right", "bottom_right", "bottom_left"):
                            # val is (x, y) or [x, y]
                            scaled_box[key] = (val[0] / _scale, val[1] / _scale)
                        else:
                            scaled_box[key] = val
                    ocr_results[text] = scaled_box
            except Exception as _e:
                print(f"OCR error: {_e}")
                ocr_results = {}

            matches = []
            for raw_text, box in ocr_results.items():
                norm = self.resolve_ocr_typos(self.normalize_ocr_name(raw_text))
                if self.names_match(norm, target_key):
                    score = self.name_match_score(norm, target_key)
                    center = box.get("center")
                    if center:
                        cx, cy = int(center[0]), int(center[1])
                        matches.append((score, norm, cx, cy))
                        print(f"match: {raw_text!r} -> {norm!r} score={score:.2f} pos=({cx},{cy})")


            if matches:
                matches.sort(key=lambda item: item[0], reverse=True)
                _, detected_name, click_x, click_y = matches[0]
                self.window_controller.click(click_x, click_y)
                print(f"found brawler {brawler} {detected_name} clicking at {click_x}, {click_y}")
                time.sleep(1.0)

                verify_screenshot = self.window_controller.screenshot()
                verify_state = get_state(verify_screenshot)
                card_is_open = verify_state in ("brawler_selection", "shop")
                if not card_is_open:
                    try:
                        select_words = {"select", "selegt", "selec", "selct", "selert"}
                        card_is_open = any(
                            self.normalize_ocr_name(text) in select_words
                            for text in extract_text_strings(verify_screenshot)
                        )
                        if card_is_open:
                            print(f"ok")
                    except Exception:
                        pass

                if not card_is_open:
                    time.sleep(0.5)
                    continue

                select_x = self.coords_cfg['lobby']['select_btn'][0]
                select_y = self.coords_cfg['lobby']['select_btn'][1]
                self.window_controller.click(select_x, select_y, already_include_ratio=False)
                time.sleep(0.5)
                print(f"Selected brawler {brawler}")
                self.selected_brawler_key = target_key
                self.selected_brawler_name = brawler
                found_brawler = True
                break

            if c == 0:
                wr = self.window_controller.width_ratio
                hr = self.window_controller.height_ratio
                self.window_controller.swipe(int(1740 * wr), int(900 * hr), int(1740 * wr), int(675 * hr), duration=0.6)
                c += 1
                continue

            self.window_controller.swipe(int(1740 * wr), int(900 * hr), int(1740 * wr), int(675 * hr), duration=0.7)
            time.sleep(1)

        if not found_brawler:
            print(f"WARNING: Brawler '{brawler}' was not found after 50 scroll attempts. "
                  "The bot will continue with the currently selected brawler.")
            return False
        return True

    def select_lowest_trophy_brawler(self):
        wr = self.window_controller.width_ratio
        hr = self.window_controller.height_ratio

        def tap(x, y, wait=0.6):
            self.window_controller.click(int(x * wr), int(y * hr))
            time.sleep(wait)

        tap(128, 500, 1.4)
        tap(1210, 45, 0.6)
        tap(1210, 426, 1.0)
        tap(422, 359, 1.0)
        tap(260, 991, 1.0)
        if self.ensure_lobby_after_selection():
            return True
        
        self.press_back()
        time.sleep(0.8)
        tap(260, 991, 1.0)
        return self.ensure_lobby_after_selection()

    def ensure_lobby_after_selection(self, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = get_state(self.window_controller.screenshot())
            except Exception as e:
                return False
            if state == "lobby":
                return True
            if state == "brawler_selection":
                self.window_controller.click(
                    int(260 * self.window_controller.width_ratio),
                    int(991 * self.window_controller.height_ratio),
                )
            elif state == "match":
                self.press_back()
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
            'shey':         BrawlerName.Shelly.value,
            'shlly':        BrawlerName.Shelly.value,
            'larryslawrie': BrawlerName.Larry.value,
            '[eon':         BrawlerName.Leon.value,
            'brock':        BrawlerName.Brock.value,
            'brok':         BrawlerName.Brock.value,
            'gal':          BrawlerName.Gale.value,
            'gale':         BrawlerName.Gale.value,
            'darryl':       BrawlerName.Darryl.value,
            'daryl':        BrawlerName.Darryl.value,
            'dary':         BrawlerName.Darryl.value,
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
        if len(target_name) <= 2:
            return False
        if len(target_name) == 3 and len(detected_name) < 3:
            return False
        if len(target_name) >= 4 and (target_name in detected_name or detected_name in target_name):
            coverage = len(detected_name) / max(1, len(target_name))
            if coverage >= 0.55:
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
