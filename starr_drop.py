from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

MATCH_RESULT_STATES = {
    "end_victory",
    "end_defeat",
    "end_draw",
    "end_1st",
    "end_2nd",
    "end_3rd",
    "end_4th",
}

SLEEP_STATES = {"match_making", "match"}

_TEMPLATE_SUBDIR = Path("images") / "star_drop_types"

DEFAULT_THRESHOLD = 0.80
DEFAULT_INTERVAL_SECONDS = 0.35
DEFAULT_TAP_COUNT = 5
DEFAULT_TAP_INTERVAL_SECONDS = 1.0
DEFAULT_POST_STANDARD_TAP_DELAY_SECONDS = 7.0
DEFAULT_HOLD_TIMEOUT_SECONDS = 20
DEFAULT_HOLD_CHECK_INTERVAL_SECONDS = 0.20
DEFAULT_POST_HOLD_TAP_DELAY_SECONDS = 7.0
DEFAULT_CHAOS_TAP_INTERVAL_SECONDS = 0.5
DEFAULT_CHAOS_TAP_TIMEOUT_SECONDS = 30.0
DEFAULT_ACTION_COOLDOWN_SECONDS = 2.0

REFERENCE_WIDTH = 1920
REFERENCE_HEIGHT = 1080
DEFAULT_ROI_FRACTIONS = (0.18, 0.08, 0.64, 0.82)

DROP_TYPE_NAMES = {
    "star_drop": "standard",
    "angelic_star_drop": "angelic",
    "demonic_star_drop": "demonic",
    "starr_nova_star_drop": "starr nova",
    "starr_nova": "starr nova",
    "starr_nova_ex": "starr nova",
    "chaos_drop": "chaos",
}

SPECIAL_HOLD_DROP_TYPES = {"demonic", "angelic", "starr nova"}
CHAOS_TAP_DROP_TYPES = {"chaos"}
STANDARD_TAP_DROP_TYPES = {"standard", "star drop", "starr drop"}
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


@dataclass(frozen=True)
class TemplateImage:
    path: Path
    drop_type: str
    bgr: np.ndarray
    gray: np.ndarray
    sat: np.ndarray
    edges: np.ndarray


@dataclass(frozen=True)
class MatchResult:
    chance: float
    drop_type: str
    path: Path
    scale: float
    box: Tuple[int, int, int, int]
    raw_gray: float
    raw_sat: float
    raw_edges: float


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "window_controller.py").exists() and (candidate / "cfg").exists():
            return candidate
    return here


def _load_general_config() -> dict:
    try:
        root = _project_root()
        cfg_path = root / "cfg" / "general_config.toml"
        from utils import load_toml_as_dict  # type: ignore
        return load_toml_as_dict(str(cfg_path))
    except Exception:
        return {}


def _load_config_flag() -> bool:
    cfg = _load_general_config()
    val = cfg.get("starr_drop_detect", True)
    if isinstance(val, bool):
        return val
    return str(val).lower() not in {"false", "0", "no", "off"}


def _canonical_drop_type(raw_type: str) -> str:
    raw = raw_type.strip().lower()
    normalized = raw.replace("_", " ").replace("-", " ")
    if raw in DROP_TYPE_NAMES:
        return DROP_TYPE_NAMES[raw]
    if "chaos" in normalized:
        return "chaos"
    if normalized.startswith("starr nova") or normalized.startswith("starrnova"):
        return "starr nova"
    if "angelic" in normalized:
        return "angelic"
    if "demonic" in normalized:
        return "demonic"
    if normalized in {"star drop", "starr drop", "standard"}:
        return "standard"
    return normalized


def _preprocess(bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat = cv2.GaussianBlur(hsv[:, :, 1], (3, 3), 0)
    edges = cv2.Canny(gray, 70, 160)
    return gray, sat, edges


def load_templates(template_dir: Path) -> list[TemplateImage]:
    if not template_dir.exists():
        raise FileNotFoundError(f"starr drop template folder not found: {template_dir}")
    templates: list[TemplateImage] = []
    for path in sorted(template_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            continue
        raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if raw is None or raw.size == 0:
            print(f"starr drop skipping unreadable template: {path}")
            continue
        if raw.ndim == 2:
            bgr = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
        elif raw.shape[2] == 4:
            bgr = raw[:, :, :3].copy()
            alpha = raw[:, :, 3]
            if alpha.min() < 255:
                yx = np.argwhere(alpha > 0)
                if yx.size:
                    y0, x0 = yx.min(axis=0)
                    y1, x1 = yx.max(axis=0) + 1
                    bgr = bgr[y0:y1, x0:x1]
        else:
            bgr = raw[:, :, :3].copy()
        if bgr.shape[0] < 20 or bgr.shape[1] < 20:
            print(f"starr drop skipping too-small template: {path}")
            continue
        gray, sat, edges = _preprocess(bgr)
        drop_type = _canonical_drop_type(path.stem)
        templates.append(TemplateImage(
            path=path, drop_type=drop_type,
            bgr=bgr, gray=gray, sat=sat, edges=edges,
        ))
    if not templates:
        raise RuntimeError(f"starr drop no usable templates found in: {template_dir}")
    return templates


def _default_roi(frame: np.ndarray) -> Tuple[int, int, int, int]:
    h, w = frame.shape[:2]
    fx, fy, fw, fh = DEFAULT_ROI_FRACTIONS
    x = int(w * fx)
    y = int(h * fy)
    return x, y, max(1, min(int(w * fw), w - x)), max(1, min(int(h * fh), h - y))


def _scale_candidates(frame: np.ndarray) -> list[float]:
    h, w = frame.shape[:2]
    base = min(w / REFERENCE_WIDTH, h / REFERENCE_HEIGHT)
    low = max(0.20, base * 0.55)
    high = min(3.00, base * 1.65)
    values = np.unique(np.round(np.linspace(low, high, 23), 3))
    return [float(v) for v in values if v > 0]


def _safe_match(screen: np.ndarray, tmpl: np.ndarray) -> Tuple[float, Tuple[int, int]]:
    if screen.shape[0] < tmpl.shape[0] or screen.shape[1] < tmpl.shape[1]:
        return 0.0, (0, 0)
    res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(res)
    if np.isnan(mx) or np.isinf(mx):
        return 0.0, (0, 0)
    return float(mx), ml


def _edge_overlap(screen_edges: np.ndarray, tmpl_edges: np.ndarray,
                  loc: Tuple[int, int]) -> float:
    x, y = loc
    h, w = tmpl_edges.shape[:2]
    patch = screen_edges[y:y + h, x:x + w]
    if patch.shape[:2] != tmpl_edges.shape[:2]:
        return 0.0
    on_t = tmpl_edges > 0
    return float(np.logical_and(on_t, patch > 0).sum() / max(1, int(on_t.sum())))


def _best_for_template(
    roi_gray: np.ndarray,
    roi_sat: np.ndarray,
    roi_edges: np.ndarray,
    template: TemplateImage,
    scales: Sequence[float],
    roi_offset: Tuple[int, int],
) -> Optional[MatchResult]:
    best: Optional[MatchResult] = None
    for scale in scales:
        tw = int(template.gray.shape[1] * scale)
        th = int(template.gray.shape[0] * scale)
        if tw < 20 or th < 20 or tw > roi_gray.shape[1] or th > roi_gray.shape[0]:
            continue
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        sg = cv2.resize(template.gray, (tw, th), interpolation=interp)
        ss = cv2.resize(template.sat, (tw, th), interpolation=interp)
        se = cv2.resize(template.edges, (tw, th), interpolation=cv2.INTER_NEAREST)
        gray_score, loc = _safe_match(roi_gray, sg)
        if gray_score <= 0:
            continue
        sat_score, _ = _safe_match(roi_sat, ss)
        edge_score = _edge_overlap(roi_edges, se, loc)
        chance = max(0.0, min(1.0, 0.68 * gray_score + 0.20 * sat_score + 0.12 * edge_score))
        ox, oy = roi_offset
        x, y = loc
        r = MatchResult(
            chance=chance, drop_type=template.drop_type, path=template.path,
            scale=scale, box=(ox + x, oy + y, tw, th),
            raw_gray=max(0.0, min(1.0, gray_score)),
            raw_sat=max(0.0, min(1.0, sat_score)),
            raw_edges=max(0.0, min(1.0, edge_score)),
        )
        if best is None or r.chance > best.chance:
            best = r
    return best


def detect_star_drop(
    frame_bgr: np.ndarray,
    templates: Sequence[TemplateImage],
    roi: Optional[Tuple[int, int, int, int]] = None,
) -> list[MatchResult]:
    if frame_bgr is None or frame_bgr.size == 0:
        return []
    x, y, w, h = roi or _default_roi(frame_bgr)
    roi_bgr = frame_bgr[y:y + h, x:x + w]
    if roi_bgr.size == 0:
        return []
    roi_gray = cv2.GaussianBlur(cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    roi_sat = cv2.GaussianBlur(
        cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)[:, :, 1], (3, 3), 0
    )
    roi_edges = cv2.Canny(roi_gray, 70, 160)
    scales = _scale_candidates(frame_bgr)
    matches: list[MatchResult] = []
    for tmpl in templates:
        r = _best_for_template(roi_gray, roi_sat, roi_edges, tmpl, scales, (x, y))
        if r is not None:
            matches.append(r)
    matches.sort(key=lambda m: m.chance, reverse=True)
    return matches


class StarrDropIntegration:
    def __init__(
        self,
        window_controller=None,
        threshold: float = DEFAULT_THRESHOLD,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        tap_count: int = DEFAULT_TAP_COUNT,
        tap_interval: float = DEFAULT_TAP_INTERVAL_SECONDS,
        post_standard_tap_delay: float = DEFAULT_POST_STANDARD_TAP_DELAY_SECONDS,
        hold_timeout: float = DEFAULT_HOLD_TIMEOUT_SECONDS,
        hold_check_interval: float = DEFAULT_HOLD_CHECK_INTERVAL_SECONDS,
        post_hold_tap_delay: float = DEFAULT_POST_HOLD_TAP_DELAY_SECONDS,
        chaos_tap_interval: float = DEFAULT_CHAOS_TAP_INTERVAL_SECONDS,
        chaos_tap_timeout: float = DEFAULT_CHAOS_TAP_TIMEOUT_SECONDS,
        action_cooldown: float = DEFAULT_ACTION_COOLDOWN_SECONDS,
    ) -> None:
        self._wc = window_controller
        self._threshold = threshold
        self._interval = interval
        self._tap_count = tap_count
        self._tap_interval = tap_interval
        self._post_std_delay = post_standard_tap_delay
        self._hold_timeout = hold_timeout
        self._hold_check = hold_check_interval
        self._post_hold_delay = post_hold_tap_delay
        self._chaos_tap_interval = chaos_tap_interval
        self._chaos_tap_timeout = chaos_tap_timeout
        self._cooldown = action_cooldown

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._sleep_evt = threading.Event()
        self._sleep_evt.set()

        self._force_active_until: float = 0.0
        self._force_lock = threading.Lock()

        self._enabled: bool = _load_config_flag()

    def start(self) -> None:
        if not self._enabled:
            print(" starr_drop_detect = false — detector disabled.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="StarrDropDetector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._sleep_evt.set()  # unblock sleeping thread so it can exit cleanly
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        print("starr drop detect stopped.")

    def force_active_for(self, seconds: float) -> None:
        if not self._enabled:
            return
        with self._force_lock:
            self._force_active_until = time.monotonic() + seconds
        print(f"starr drop: force-active for {seconds:.0f}s after game end")
        self._sleep_evt.set()

    def _is_force_active(self) -> bool:
        with self._force_lock:
            return time.monotonic() < self._force_active_until

    def _cancel_force_active(self) -> None:
        with self._force_lock:
            self._force_active_until = 0.0

    def notify_state(self, state: Optional[str]) -> None:
        if not self._enabled:
            return
        if state in SLEEP_STATES:
            if self._is_force_active():
                joystick_moving = bool(getattr(self._wc, "are_we_moving", False))
                if joystick_moving:
                    print(f"starr drop: force-active cancelled — joystick moving in state: {state}")
                    self._cancel_force_active()
                    self._sleep_evt.clear()
                else:
                    return
            else:
                if self._sleep_evt.is_set():
                    print(f"starr drop: pausing detector — state: {state}")
                self._sleep_evt.clear()
        elif state in MATCH_RESULT_STATES:
            if not self._sleep_evt.is_set():
                print(f"starr drop: resuming detector — match ended ({state})")
            self._sleep_evt.set()
        else:
            if not self._sleep_evt.is_set():
                print(f"starr drop: resuming detector — state: {state}")
            self._sleep_evt.set()

    def reload_enabled(self) -> None:
        new_val = _load_config_flag()
        if new_val and not self._enabled:
            self._enabled = True
            self.start()
        elif not new_val and self._enabled:
            self._enabled = False
            self.stop()

    def _screenshot(self) -> np.ndarray:
        if self._wc is None:
            raise RuntimeError("no WindowController attached.")
        frame = self._wc.screenshot()
        if frame is None or frame.size == 0:
            raise RuntimeError("WindowController returned empty screenshot.")
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def _tap_q(self, count: int = 1, interval: float = 0.0) -> None:
        count = max(1, count)
        for i in range(count):
            self._wc.press_key("Q", delay=0.06, touch_down=True, touch_up=True)
            if i < count - 1:
                time.sleep(interval)

    def _q_down(self) -> None:
        self._wc.press_key("Q", delay=0.01, touch_down=True, touch_up=False)

    def _q_up(self) -> None:
        self._wc.press_key("Q", delay=0.01, touch_down=False, touch_up=True)

    def _perform_standard_tap(self, match: MatchResult) -> str:
        self._tap_q(self._tap_count, self._tap_interval)
        time.sleep(self._post_std_delay)
        self._tap_q(1)
        return (
            f"ACTION_DONE standard tap: Q x{self._tap_count} "
            f"interval={self._tap_interval:.2f}s, "
            f"then Q after {self._post_std_delay:.1f}s "
            f"[{match.drop_type} {match.chance*100:.1f}%]"
        )

    def _perform_special_hold(
        self,
        match: MatchResult,
        templates: Sequence[TemplateImage],
    ) -> str:
        started = time.monotonic()
        reason = "timeout"
        self._q_down()
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= self._hold_timeout:
                    reason = f"timeout {elapsed:.1f}s"
                    break
                time.sleep(max(0.05, self._hold_check))
                try:
                    frame = self._screenshot()
                    roi = _default_roi(frame)
                    matches = detect_star_drop(frame, templates, roi=roi)
                    best = matches[0] if matches else None
                    if not best or best.chance < self._threshold:
                        reason = f"disappeared after {elapsed:.1f}s"
                        break
                    if _canonical_drop_type(best.drop_type) not in SPECIAL_HOLD_DROP_TYPES:
                        reason = f"changed to {best.drop_type} after {elapsed:.1f}s"
                        break
                except Exception as exc:
                    reason = f"check error: {exc}"
                    break
        finally:
            self._q_up()
        time.sleep(self._post_hold_delay)
        self._tap_q(1)
        return (
            f"ACTION_DONE special hold: held Q, released ({reason}), "
            f"Q after {self._post_hold_delay:.1f}s "
            f"[{match.drop_type} {match.chance*100:.1f}%]"
        )

    def _perform_chaos_tap(
        self,
        match: MatchResult,
        templates: Sequence[TemplateImage],
    ) -> str:
        started = time.monotonic()
        taps = 0
        reason = "timeout"
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= self._chaos_tap_timeout:
                break
            self._tap_q(1)
            taps += 1
            time.sleep(max(0.05, self._chaos_tap_interval))
            try:
                frame = self._screenshot()
                roi = _default_roi(frame)
                matches = detect_star_drop(frame, templates, roi=roi)
                best = matches[0] if matches else None
                if not best or best.chance < self._threshold:
                    reason = f"disappeared after {taps} taps ({elapsed:.1f}s)"
                    break
            except Exception as exc:
                reason = f"check error after {taps} taps: {exc}"
                break
        return (
            f"ACTION_DONE chaos tap: tapped Q {taps}x "
            f"interval={self._chaos_tap_interval:.2f}s, stopped: {reason} "
            f"[{match.drop_type} {match.chance*100:.1f}%]"
        )

    def _perform_action(self, match: MatchResult, templates: Sequence[TemplateImage]) -> str:
        if match.chance < self._threshold:
            return (
                f"ACTION_SKIPPED chance {match.chance*100:.1f}% "
                f"< threshold {self._threshold*100:.1f}%"
            )
        dt = _canonical_drop_type(match.drop_type)
        if dt in CHAOS_TAP_DROP_TYPES:
            return self._perform_chaos_tap(match, templates)
        if dt in SPECIAL_HOLD_DROP_TYPES:
            return self._perform_special_hold(match, templates)
        if dt in STANDARD_TAP_DROP_TYPES:
            return self._perform_standard_tap(match)
        return f"ACTION_SKIPPED unknown type: {match.drop_type}"

    def _run(self) -> None:
        try:
            template_dir = _project_root() / _TEMPLATE_SUBDIR
            templates = load_templates(template_dir)

            next_action_at = 0.0

            while not self._stop_evt.is_set():

                if not self._sleep_evt.is_set():
                    if self._is_force_active():
                        self._stop_evt.wait(timeout=max(0.05, self._interval))
                        continue
                    self._sleep_evt.wait(timeout=1.0)
                    continue

                if not _load_config_flag():
                    self._stop_evt.wait(timeout=2.0)
                    continue

                cfg = _load_general_config()
                threshold = float(cfg.get("starr_drop_threshold", self._threshold))
                self._post_std_delay = float(cfg.get("starr_drop_post_tap_delay", self._post_std_delay))
                self._post_hold_delay = float(cfg.get("starr_drop_post_hold_delay", self._post_hold_delay))

                try:
                    frame = self._screenshot()
                    roi = _default_roi(frame)
                    matches = detect_star_drop(frame, templates, roi=roi)
                    detected = bool(matches and matches[0].chance >= threshold)

                    if detected and matches:
                        now_mono = time.monotonic()
                        if now_mono >= next_action_at:
                            best = matches[0]
                            print(
                                f"starr drop detected {best.drop_type} "
                                f"{best.chance*100:.1f}% — performing action"
                            )
                            self._threshold = threshold  # keep in sync for hold/chaos re-checks
                            result = self._perform_action(best, templates)
                            print(f"starr drop [{time.strftime('%H:%M:%S')}] {result}")
                            next_action_at = time.monotonic() + self._cooldown

                except Exception as exc:
                    print(f"starr drop error — {exc}")

                self._stop_evt.wait(timeout=max(0.05, self._interval))

        except Exception as fatal:
            print(f"starr drop fatal error in detector thread: {fatal}")
