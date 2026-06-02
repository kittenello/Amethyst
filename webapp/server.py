import html
import importlib.util
import json
import os
import re
import socket
import ssl
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
import asyncio
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import requests
from requests.adapters import HTTPAdapter

from utils import (
    load_toml_as_dict,
    normalize_brawler_name,
    save_brawler_data,
    save_dict_as_toml,
)
from telegram_notifier import async_send_test_notification, async_notify_user, load_telegram_settings, _image_bytes
from play_instance_registry import get_play_instance
import aiohttp


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "webapp" / "static"
BRAWLER_ICONS = ROOT / "api" / "assets" / "brawler_icons"
BRAWLER_ICONS2 = ROOT / "api" / "assets" / "brawler_icons2"
SYNCBRAWLERS2API = ROOT / "api" / "syncbrawlers2api.py"


def title_from_slug(value):
    parts = re.split(r"[^a-zA-Z0-9]+", str(value).replace("8bit", "8 bit"))
    name = " ".join(part.capitalize() for part in parts if part)
    return name.replace("8 Bit", "8bit") or str(value)



def clean_player_tag(tag):
    tag = str(tag or "").strip().upper()
    if tag.startswith("#"):
        tag = tag[1:]
    return tag


def strip_tags(html_text):
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    # Brawltracker sometimes returns brawler names as LARRY &amp; LAWRIE,
    # and in logs/Next HTML this may appear as Larry &Amp; Lawrie.
    # Decode it so local matching can map it to larrylawrie.
    text = re.sub(r"&amp;", "&", text, flags=re.IGNORECASE)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


BRAWLTRACKER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "close",
}


class TLS12HttpAdapter(HTTPAdapter):
    """Requests adapter that forces TLS 1.2 and HTTP/1.1-ish behavior.

    Some Windows/Python/OpenSSL combinations randomly fail on brawltracker with
    SSL EOF during the default TLS negotiation. This adapter is a lightweight
    fallback and does not require Playwright/Selenium/cloudscraper.
    """

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "TLSVersion"):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        pool_kwargs["ssl_context"] = ctx
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _requests_get_text(url, timeout, mode):
    session = requests.Session()
    verify = True

    if mode == "tls12":
        session.mount("https://", TLS12HttpAdapter())
    elif mode == "noverify":
        verify = False
    elif mode == "tls12_noverify":
        adapter = TLS12HttpAdapter()
        session.mount("https://", adapter)
        verify = False

    response = session.get(url, timeout=timeout, headers=BRAWLTRACKER_HEADERS, verify=verify)
    response.raise_for_status()
    return response.text


def _urllib_get_text(url, timeout):
    req = Request(url, headers=BRAWLTRACKER_HEADERS)
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
    html = data.decode("utf-8", errors="replace")
    return html


def _curl_get_text(url, timeout):
    # Windows 10/11 usually has curl.exe installed. If it is missing, this just
    # becomes one more failed fallback and the real error is shown in logs.
    cmd = [
        "curl",
        "-L",
        "--http1.1",
        "--tlsv1.2",
        "--connect-timeout", str(timeout),
        "--max-time", str(timeout + 5),
        "-A", BRAWLTRACKER_HEADERS["User-Agent"],
        "-H", f"Accept: {BRAWLTRACKER_HEADERS['Accept']}",
        "-H", f"Accept-Language: {BRAWLTRACKER_HEADERS['Accept-Language']}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"curl exit code {result.returncode}").strip())
    return result.stdout


def fetch_url_text_robust(url, timeout=15):
    errors = []
    attempts = [
        ("requests", lambda: _requests_get_text(url, timeout, "default")),
        ("requests_tls12", lambda: _requests_get_text(url, timeout, "tls12")),
        ("requests_noverify", lambda: _requests_get_text(url, timeout, "noverify")),
        ("urllib", lambda: _urllib_get_text(url, timeout)),
        ("curl", lambda: _curl_get_text(url, timeout)),
    ]

    for index, (name, getter) in enumerate(attempts, start=1):
        try:

            html = getter()
            if html and "brawltracker" in html.lower() or "BRAWLER" in html.upper() or "Player Icon" in html:
                return html
            if html:
                return html
            raise RuntimeError("empty response")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            time.sleep(0.5)

    raise RuntimeError("Brawltracker request failed after all fallbacks:\n" + "\n".join(errors))


def fetch_brawltracker_player(player_tag, timeout=15):
    """Fetch player name and brawler trophies from brawltracker.com static HTML.

    This is intentionally lightweight: no Playwright/Selenium and no official
    Brawl Stars API token. The webapp only needs name + current trophies.
    """
    tag = clean_player_tag(player_tag)
    if not tag or tag == "YOURTAG":
        raise ValueError("Player tag is empty. Write it in the web UI or cfg/brawl_stars_api.toml")

    url = f"https://brawltracker.com/stats/player/{quote(tag)}"
    html = fetch_url_text_robust(url, timeout=timeout)

    debug_dir = ROOT / "logs"
    debug_dir.mkdir(exist_ok=True)
    try:
        (debug_dir / "debug_brawltracker.html").write_text(html, encoding="utf-8")
        print(f"{debug_dir / 'debug_brawltracker.html'}")
    except OSError as exc:
        print(f"warn: could not save debug log html: {exc}")

    # Profile card name, example: <h2 class="...text-yellow-400">Tojoko</h2>
    player_name = ""
    name_match = re.search(r'<h2[^>]*text-yellow-400[^>]*>(.*?)</h2>', html, re.I | re.S)
    if name_match:
        player_name = strip_tags(name_match.group(1))
    if not player_name:
        any_h2 = re.search(r'<h2[^>]*>(.*?)</h2>', html, re.I | re.S)
        if any_h2:
            player_name = strip_tags(any_h2.group(1))

    brawlers = []
    # Each card from brawltracker starts with this wrapper. It is static in HTML.
    card_parts = re.split(r'<div class="hover:scale-105 cursor-pointer \[transform-style:preserve-3d\] \[backface-visibility:hidden\] ">', html)
    for card in card_parts[1:]:
        # The first image alt in the card is the brawler name, e.g. SHELLY.
        name_match = re.search(r'<img\s+alt="([^"]+)"[^>]+brawlers%2Fportraits%2F', card, re.I | re.S)
        if not name_match:
            name_match = re.search(r'<img\s+alt="([A-Z0-9 _.-]+)"', card, re.I | re.S)
        if not name_match:
            continue
        name = strip_tags(name_match.group(1)).title()
        name = name.replace("El Primo", "El Primo").replace("8 Bit", "8-Bit")
        name = name.replace("Larry & Lawrie", "Larry & Lawrie")

        trophy_match = re.search(r'alt="Trophy".*?<span[^>]*>(\d+)</span>', card, re.I | re.S)
        power_match = re.search(r'alt="Power\s+(\d+)"', card, re.I | re.S)
        if not trophy_match:
            continue
        brawlers.append({
            "name": name,
            "trophies": int(trophy_match.group(1)),
            "power": int(power_match.group(1)) if power_match else 0,
        })

    if not brawlers:
        raise RuntimeError("Brawltracker HTML loaded, but no brawler cards were parsed. Check logs/debug_brawltracker.html")

    # Player icon — brawltracker renders it as alt="Player Icon"
    player_icon = ""
    icon_match = re.search(
        r'<img[^>]+alt=["\']Player Icon["\'][^>]+srcset=["\']([^"\']+)["\']',
        html, re.I | re.S
    )
    if icon_match:
        # srcset may have "url 1x, url 2x" — take the last (2x) entry
        srcset_raw = icon_match.group(1)
        parts = [p.strip().split()[0] for p in srcset_raw.split(",") if p.strip()]
        player_icon = parts[-1] if parts else ""
    if not player_icon:
        # fallback: src attribute
        icon_src_match = re.search(
            r'<img[^>]+alt=["\']Player Icon["\'][^>]+src=["\']([^"\']+)["\']',
            html, re.I | re.S
        )
        if icon_src_match:
            player_icon = icon_src_match.group(1)
    # Resolve relative URLs returned by brawltracker's Next.js image proxy
    if player_icon and player_icon.startswith("/"):
        player_icon = "https://brawltracker.com" + player_icon

    # Total trophies — brawltracker shows it near the profile header
    total_trophies = 0
    # Pattern: alt="Trophy" followed by a number inside a span/div
    trophy_total_match = re.search(
        r'alt=["\']Trophy["\'][^>]*>.*?<(?:span|div|p)[^>]*>\s*([\d,]+)\s*</',
        html, re.I | re.S
    )
    if trophy_total_match:
        total_trophies = int(trophy_total_match.group(1).replace(",", ""))
    if not total_trophies:
        total_trophies = sum(b.get("trophies", 0) for b in brawlers)

    return {
        "name": player_name or "Player",
        "tag": f"#{tag}",
        "brawlers": brawlers,
        "source": "brawltracker",
        "icon": player_icon,
        "total_trophies": total_trophies,
    }



def load_syncbrawlers2api_module():
    """Load api/syncbrawlers2api.py without requiring api to be a package."""
    if not SYNCBRAWLERS2API.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("syncbrawlers2api", str(SYNCBRAWLERS2API))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception as exc:
        return None


def local_brawler_match_key(value):
    # Stronger than older normalize_brawler_name for names like LARRY & LAWRIE -> larrylawrie.
    value = html.unescape(str(value or "")).lower()
    value = value.replace("&amp;", "&")
    value = value.replace("and", "&") if value.strip() == "larry and lawrie" else value
    value = value.replace(".", "").replace("-", "").replace("_", "").replace("&", "")
    value = re.sub(r"[^a-z0-9]+", "", value)
    aliases = {
        "larrylawrie": "larrylawrie",
        "larryandlawrie": "larrylawrie",
        "8bit": "8bit",
        "eightbit": "8bit",
        "mrp": "mrp",
        "misterp": "mrp",
        "rt": "rt",
    }
    return aliases.get(value, value)


def build_icons2_known_map(local_brawlers):
    known = {}
    for brawler in local_brawlers:
        known[local_brawler_match_key(brawler)] = brawler
        try:
            known[normalize_brawler_name(brawler)] = brawler
        except Exception:
            pass
    if BRAWLER_ICONS2.exists():
        for path in BRAWLER_ICONS2.glob("*.png"):
            key = local_brawler_match_key(path.stem)
            match = next((b for b in local_brawlers if local_brawler_match_key(b) == key), None)
            known[key] = match or key
    return known



class MultiInstanceManager:
    """Web Multi-Instance Hub for LDPlayer/MuMu.

    Each bot is launched in an isolated runtime folder:
        .multi_instances/instance_1, instance_2, ...
    The runtime folder contains its own cfg/ and latest_brawler_data.json, so
    the original code can keep using hardcoded relative cfg paths safely.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.runtime_root = self.root / ".multi_instances"
        self.runtime_root.mkdir(exist_ok=True)
        self.logs_dir = self.root / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        # FIX: per-instance locks so two parallel Start requests never race
        # on the same runtime folder (shutil.rmtree + shutil.copytree on cfg).
        self._instance_locks = {i: threading.Lock() for i in range(1, 5)}
        self.instances = {}
        self.max_instances = 4
        self.default_ports = [5555, 5557, 5559, 5561]
        self._prebuilt_all = False

    def _adb_path(self):
        local = self.root / "adb.exe"
        return str(local) if local.exists() else "adb"

    def scan_devices(self, connect=False):
        """Return real ADB devices. When connect=True, try common LDPlayer/MuMu ports first.

        Important: scan must never launch a bot worker. It only calls adb connect/devices.
        """
        adb = self._adb_path()

        def run_adb(args, timeout=8):
            return subprocess.run(
                [adb] + args, cwd=str(self.root), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout
            )

        connect_notes = {}
        if connect:
            for port in self.default_ports:
                serial = f"127.0.0.1:{port}"
                try:
                    res = run_adb(["connect", serial], timeout=4)
                    note = (res.stdout or res.stderr or "").strip()
                    if note:
                        connect_notes[serial] = note
                except Exception as exc:
                    connect_notes[serial] = str(exc)

        devices = []
        try:
            result = run_adb(["devices"], timeout=8)
            for line in result.stdout.splitlines()[1:]:
                line = line.strip()
                if not line or "\t" not in line:
                    continue
                serial, status = line.split("\t", 1)
                port = None
                m = re.search(r":(\d+)$", serial)
                if m:
                    port = int(m.group(1))
                # Keep the hub clean: show only localhost-port emulators used by LDPlayer/MuMu/BlueStacks.
                # Android Studio stale entries like emulator-5554/emulator-5556 are hidden.
                if str(status).strip().lower() != "device":
                    continue
                if not (serial.startswith("127.0.0.1:") or serial.startswith("localhost:")):
                    continue
                # Only LDPlayer localhost ADB ports handled by this hub. Hide emulator-* / Android Studio entries.
                if port not in self.default_ports:
                    continue
                devices.append({
                    "serial": serial,
                    "status": status,
                    "port": port,
                    "emulator": self._emulator_name(port),
                    "note": connect_notes.get(serial, ""),
                })
        except Exception as exc:
            devices.append({"serial": "adb unavailable", "status": str(exc), "port": None, "emulator": "ADB", "note": ""})

        return devices

    def _emulator_name(self, port):
        if port in (5555, 5557, 5559, 5561, 5563):
            return "LDPlayer"
        if port == 5554:
            return "MuMu/Android"
        return "Emulator"

    def _cfg_source(self, instance_id):
        if int(instance_id) <= 1:
            return self.root / "cfg"
        numbered = self.root / f"cfg_{int(instance_id)}"
        return numbered if numbered.exists() else self.root / "cfg"

    def _runtime_dir(self, instance_id):
        return self.runtime_root / f"instance_{int(instance_id)}"

    def _copy_project_runtime(self, runtime_dir, cfg_source):
        """Prepare an isolated runtime folder quickly.

        Old version deleted and recopied the whole project on every Start Next,
        which made launching feel frozen on Windows. Now the heavy project copy is
        done only once per instance folder. On every start we only refresh cfg and
        the small launcher files.
        """
        ignore_names = {
            ".git", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".multi_instances",
            "logs", "build", "dist", "venv", ".venv", "env", "node_modules"
        }
        runtime_dir.mkdir(parents=True, exist_ok=True)
        marker = runtime_dir / ".runtime_ready"

        # Always refresh the isolated cfg; this is small and keeps instance data correct.
        cfg_dest = runtime_dir / "cfg"
        if cfg_dest.exists():
            shutil.rmtree(cfg_dest, ignore_errors=True)
        shutil.copytree(cfg_source, cfg_dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # Always refresh tiny/critical files in case user patched them.
        for name in ("multi_worker.py", "main.py", "runtime_control.py", "stage_manager.py"):
            src = self.root / name
            if src.exists() and src.is_file():
                shutil.copy2(src, runtime_dir / name)

        # stage_manager imports fetch_brawltracker_player from webapp.server,
        # so every isolated runtime must have the lightweight webapp package too.
        webapp_src = self.root / "webapp"
        webapp_dest = runtime_dir / "webapp"
        if webapp_src.exists() and webapp_src.is_dir():
            if webapp_dest.exists():
                shutil.rmtree(webapp_dest, ignore_errors=True)
            shutil.copytree(webapp_src, webapp_dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))

        # Heavy copy only on first preparation. Reusing it makes Start Next fast.
        if marker.exists():
            (runtime_dir / "logs").mkdir(exist_ok=True)
            return

        for item in self.root.iterdir():
            if item.name in ignore_names or item.name.startswith("cfg_") or item.name == "cfg":
                continue
            dest = runtime_dir / item.name
            if dest.exists():
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
                else:
                    shutil.copy2(item, dest)
            except Exception:
                # Keep launching robust: non-critical files should not block an instance start.
                pass
        (runtime_dir / "logs").mkdir(exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")


    def _prebuild_all_runtimes(self):
        """Prepare all supported instance folders once.

        First Start Next can be a little longer, but after this every instance
        launch only refreshes cfg/queue and starts the worker. Limit is 4 by
        design so the hub does not copy unlimited folders.
        """
        if self._prebuilt_all:
            return
        with self.lock:
            if self._prebuilt_all:
                return
            print("[MULTI-HUB] First launch: prebuilding 4 instance runtimes...")
            for instance_id in range(1, self.max_instances + 1):
                port = self.default_ports[instance_id - 1]
                runtime_dir = self._runtime_dir(instance_id)
                cfg_source = self._cfg_source(instance_id)
                self._copy_project_runtime(runtime_dir, cfg_source)
                self._patch_instance_config(runtime_dir, port)
            self._prebuilt_all = True
            print("[MULTI-HUB] Runtime prebuild complete.")

    def _patch_instance_config(self, runtime_dir, port):
        cfg_path = runtime_dir / "cfg" / "general_config.toml"
        config = dict(load_toml_as_dict(str(cfg_path)))
        config["emulator_port"] = int(port)

        # Multi-instance optimization: uncapped 60 FPS + 1280px scrcpy feed makes
        # LDPlayer/Brawl Stars lag hard when two or more workers are active. Keep
        # each isolated runtime lighter by default; users can still raise these in cfg.
        config.setdefault("cpu_or_gpu", "auto")
        config["max_ips"] = int(config.get("max_ips") or 30)
        config["scrcpy_max_fps"] = min(int(config.get("scrcpy_max_fps") or 30), 30)
        config["scrcpy_max_width"] = min(int(config.get("scrcpy_max_width") or 960), 960)
        config["scrcpy_bitrate"] = min(int(config.get("scrcpy_bitrate") or 2000000), 2000000)
        config["onnx_cpu_threads"] = min(int(config.get("onnx_cpu_threads") or 2), 2)
        config["used_threads"] = min(int(config.get("used_threads") or 2), 2)
        config["visual_debug"] = "no"
        config["terminal_logging"] = "no"
        save_dict_as_toml(config, str(cfg_path))

    def _queue_for_instance(self, payload, instance_id):
        queue = payload.get("queue")
        if not isinstance(queue, list) or not queue:
            queue = []
        if not queue:
            # FIX: prefer a per-instance queue file (latest_brawler_data_2.json etc.)
            # so different instances can have fully independent queues set from the UI.
            per_instance_file = self.root / f"latest_brawler_data_{instance_id}.json"
            if instance_id > 1 and per_instance_file.exists():
                queue = WebApp._read_json(per_instance_file, [])
        if not isinstance(queue, list) or not queue:
            # Fallback: use the global queue (instance 1 / main).
            queue = WebApp._read_json(self.root / "latest_brawler_data.json", [])
        if not isinstance(queue, list) or not queue:
            raise ValueError("Queue is empty. Add at least one brawler before starting an instance.")
        return queue

    def start_instance(self, payload):
        instance_id = int(payload.get("id") or 1)
        port = int(payload.get("port") or (5555 + (instance_id - 1) * 2))
        with self.lock:
            old = self.instances.get(instance_id)
            if old and old.get("process") and old["process"].poll() is None:
                return {"ok": True, "alreadyRunning": True, "instance": self._public_instance(instance_id)}

            queue = self._queue_for_instance(payload, instance_id)
            if instance_id < 1 or instance_id > self.max_instances:
                raise ValueError(f"Instance id must be 1-{self.max_instances}")
            self._prebuild_all_runtimes()
            cfg_source = self._cfg_source(instance_id)
            runtime_dir = self._runtime_dir(instance_id)
            # FIX: hold per-instance lock while doing file ops so two concurrent
            # Start requests for the same instance cannot race on cfg/ copy.
            with self._instance_locks.get(instance_id, threading.Lock()):
                # Fast path after first prebuild: only refresh selected cfg and port.
                self._copy_project_runtime(runtime_dir, cfg_source)
                self._patch_instance_config(runtime_dir, port)
                (runtime_dir / "latest_brawler_data.json").write_text(json.dumps(queue, indent=2), encoding="utf-8")

            # Important: old web stop flags/control files are stored inside the reused runtime.
            # If they are not cleared, the next launch can immediately stop and look like
            # the second LDPlayer started but does not move/gas.
            logs_dir = runtime_dir / "logs"
            logs_dir.mkdir(exist_ok=True)
            for stale in [logs_dir / "web_stop_requested.flag"] + list(logs_dir.glob("runtime_control_*.state")):
                try:
                    stale.unlink()
                except OSError:
                    pass

            log_path = self.logs_dir / f"multi_instance_{instance_id}.log"
            log_file = open(log_path, "a", encoding="utf-8", errors="replace")
            env = os.environ.copy()
            env["PYLA_MULTI_INSTANCE_ID"] = str(instance_id)
            env["PYLA_MULTI_INSTANCE_PORT"] = str(port)
            env["PYLA_MULTI_INSTANCE_SERIAL"] = f"127.0.0.1:{port}"
            env["PYLA_MULTI_INSTANCE_NAME"] = self._emulator_name(port)
            env["PYLA_MULTI_INSTANCE_CFG"] = cfg_source.name
            # FIX: Child workers have no webapp of their own.
            # Disable webapp queue polling so they always use their own
            # isolated latest_brawler_data.json instead of stealing the
            # queue from the main instance's webapp (port 8765).
            env["PYLA_QUEUE_API_URL"] = "disabled"
            cmd = [sys.executable, "multi_worker.py"]
            process = subprocess.Popen(
                cmd, cwd=str(runtime_dir), stdout=log_file, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, text=True
            )
            self.instances[instance_id] = {
                "id": instance_id,
                "port": port,
                "serial": f"127.0.0.1:{port}",
                "emulator": self._emulator_name(port),
                "process": process,
                "pid": process.pid,
                "startedAt": time.time(),
                "runtimeDir": str(runtime_dir),
                "logPath": str(log_path),
                "logFile": log_file,  # FIX: kept so stop_instance can close it
                "queue": queue,
                "state": "running",
            }
            return {"ok": True, "instance": self._public_instance(instance_id)}

    def _control_paths(self, instance_id):
        runtime_dir = self._runtime_dir(instance_id)
        return list((runtime_dir / "logs").glob("runtime_control_*.state"))

    def set_paused(self, instance_id, paused):
        instance_id = int(instance_id)
        value = "paused" if paused else "running"
        for path in self._control_paths(instance_id):
            try:
                path.write_text(value, encoding="utf-8")
            except OSError:
                pass
        with self.lock:
            if instance_id in self.instances:
                self.instances[instance_id]["state"] = value
        return {"ok": True, "instance": self._public_instance(instance_id)}

    def stop_instance(self, instance_id):
        instance_id = int(instance_id)
        with self.lock:
            inst = self.instances.get(instance_id)
            if not inst:
                return {"ok": True}
            runtime_dir = Path(inst.get("runtimeDir") or self._runtime_dir(instance_id))
            (runtime_dir / "logs").mkdir(exist_ok=True)
            try:
                (runtime_dir / "logs" / "web_stop_requested.flag").write_text("stopped", encoding="utf-8")
            except OSError:
                pass
            for path in self._control_paths(instance_id):
                try:
                    path.write_text("stopped", encoding="utf-8")
                except OSError:
                    pass
            proc = inst.get("process")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # FIX: close the log file handle to prevent fd leak on repeated start/stop
            log_file = inst.get("logFile")
            if log_file is not None:
                try:
                    log_file.close()
                except Exception:
                    pass
            inst["state"] = "stopped"
            public = self._public_instance(instance_id)
            # Remove stopped cards from the active hub; logs stay on disk and can still be read.
            self.instances.pop(instance_id, None)
            return {"ok": True, "instance": public}

    def stop_all(self):
        for instance_id in list(self.instances.keys()):
            self.stop_instance(instance_id)
        return {"ok": True, "instances": self.public_state()["instances"]}

    def _runtime_json(self, instance_id):
        runtime_file = self._runtime_dir(instance_id) / "logs" / "web_runtime.json"
        try:
            return json.loads(runtime_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _public_instance(self, instance_id):
        inst = self.instances.get(int(instance_id), {"id": int(instance_id)})
        proc = inst.get("process")
        running = bool(proc and proc.poll() is None)
        runtime = self._runtime_json(instance_id)
        control_state = inst.get("state", "running" if running else "stopped")
        for path in self._control_paths(instance_id):
            try:
                control_state = path.read_text(encoding="utf-8", errors="ignore").strip() or control_state
            except OSError:
                pass
        if not running and control_state != "paused":
            control_state = "stopped"
        queue = inst.get("queue") or []
        current = queue[0] if queue else {}
        return {
            "id": int(instance_id),
            "name": inst.get("name") or f"LDPlayer #{int(instance_id)-1}",
            "emulator": inst.get("emulator") or self._emulator_name(inst.get("port")),
            "port": inst.get("port"),
            "serial": inst.get("serial") or (f"127.0.0.1:{inst.get('port')}" if inst.get("port") else ""),
            "pid": inst.get("pid"),
            "running": running,
            "state": runtime.get("state") or control_state,
            "session": current.get("brawler", "none"),
            "currentBrawler": runtime.get("currentBrawler") or current.get("brawler", "none"),
            "progressCurrent": int(current.get(current.get("type", "trophies"), 0) or 0) if current else 0,
            "progressTarget": int(current.get("push_until", 0) or 0) if current else 0,
            "ips": round(float(runtime.get("ips") or 0), 1),
            "startedAt": inst.get("startedAt"),
        }

    def public_state(self):
        with self.lock:
            known_ids = set(self.instances.keys())
            rows = []
            for i in sorted(known_ids):
                row = self._public_instance(i)
                # Do not render empty/stopped placeholders. The device chips show available ADB ports;
                # instance cards are only for actually launched workers.
                if row.get("running") or row.get("state") in ("running", "paused"):
                    rows.append(row)
        return {"ok": True, "devices": self.scan_devices(connect=False), "instances": rows}

    def logs(self, instance_id, limit=26000):
        inst = self.instances.get(int(instance_id))
        log_path = Path(inst.get("logPath")) if inst and inst.get("logPath") else self.logs_dir / f"multi_instance_{int(instance_id)}.log"
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")[-int(limit):]
        except OSError:
            text = ""
        return {"ok": True, "log": text}

class WebApp:
    def __init__(self, data_setter, brawlers, version, latest_version=None, host="127.0.0.1", port=8765):
        self.data_setter = data_setter
        self.brawlers = list(brawlers)
        self.version = version
        self.latest_version = latest_version or version
        self.host = host
        self.port = self._pick_port(port)
        self.ready = threading.Event()
        self.runtime_state = "idle"
        self.runtime_lock = threading.Lock()
        self.server = None
        self.thread = None
        self.selected_data = None
        self.multi = MultiInstanceManager(ROOT)
        self._online_clients: dict = {}   # ip -> last_seen timestamp
        self._online_lock = threading.Lock()

    def start(self):
        app = self

        class Handler(WebRequestHandler):
            web_app = app

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = f"http://{self.host}:{self.port}"
        print(f"amethyst webapp: {url}")
        webbrowser.open(url)
        self.ready.wait()
        return self.selected_data

    @staticmethod
    def _pick_port(first_port):
        for port in range(first_port, first_port + 40):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise OSError("Could not find a free local port for the web UI.")

    # ── Online users tracker ──────────────────────────────────────────────────
    ONLINE_TIMEOUT = 60  # seconds; client pings every 30s

    def ping_online(self, client_ip: str) -> dict:
        """Record a heartbeat from client_ip and return current online count."""
        now = time.time()
        with self._online_lock:
            self._online_clients[client_ip] = now
            # Evict stale entries
            cutoff = now - self.ONLINE_TIMEOUT
            self._online_clients = {ip: ts for ip, ts in self._online_clients.items() if ts > cutoff}
            count = len(self._online_clients)
        return {"online": count}

    def get_online(self) -> dict:
        """Return current online count without updating the caller's timestamp."""
        now = time.time()
        cutoff = now - self.ONLINE_TIMEOUT
        with self._online_lock:
            count = sum(1 for ts in self._online_clients.values() if ts > cutoff)
        return {"online": count}

    # ─────────────────────────────────────────────────────────────────────────

    def build_state(self):
        bot_config = load_toml_as_dict(str(ROOT / "cfg" / "bot_config.toml"))
        general_config = load_toml_as_dict(str(ROOT / "cfg" / "general_config.toml"))
        brawl_api_config = load_toml_as_dict(str(ROOT / "cfg" / "brawl_stars_api.toml"))
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        return {
            "version": self.version,
            "latestVersion": self.latest_version,
            "status": "Idle",
            "authenticated": True,
            "playerTag": brawl_api_config.get("player_tag", ""),
            "brawlers": self.get_brawlers(),
            "queue": queue if isinstance(queue, list) else [],
            "currentPlaystyle": "team_showdown.pyla",
            "playstyles": [],
            "history": self.get_history(),
            "settings": self.get_settings(bot_config, general_config),
            "logging": self.get_logging_settings(),
            "runtime": self.get_runtime_state(queue),
        }

    def get_brawlers(self):
        rows = []
        for brawler in self.brawlers:
            rows.append({
                "id": brawler,
                "name": title_from_slug(brawler),
                "icon": f"/assets/brawler_icons/{brawler}.png",
            })
        return rows


    def get_runtime_state(self, queue):
        with self.runtime_lock:
            state = self.runtime_state

        ips = 0.0
        current_brawler = None
        runtime_file = ROOT / "logs" / "web_runtime.json"

        try:
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            ips = runtime.get("ips", ips)
            current_brawler = runtime.get("currentBrawler")
        except Exception:
            runtime = {}

        current = queue[0] if queue else {}
        return {
            "state": state,
            "running": state in ("running", "starting"),
            "session": current.get("brawler", "none"),
            "currentBrawler": current_brawler or current.get("brawler", "none"),
            "progressCurrent": int(current.get(current.get("type", "trophies"), 0) or 0) if current else 0,
            "progressTarget": int(current.get("push_until", 0) or 0) if current else 0,
            "queued": len(queue),
            "ips": ips,
        }

    def get_history(self):
        data = load_toml_as_dict(str(ROOT / "cfg" / "match_history.toml"))
        rows = []
        totals = {"victory": 0, "defeat": 0, "draw": 0}
        for brawler, stats in data.items():
            if not isinstance(stats, dict):
                continue
            victory = int(stats.get("victory", 0) or 0)
            defeat = int(stats.get("defeat", 0) or 0)
            draw = int(stats.get("draw", 0) or 0)
            if brawler == "total":
                totals = {"victory": victory, "defeat": defeat, "draw": draw}
                continue
            total = victory + defeat + draw
            if total <= 0:
                continue
            rows.append({
                "id": brawler,
                "name": title_from_slug(brawler),
                "icon": f"/assets/brawler_icons/{brawler}.png",
                "victory": victory,
                "defeat": defeat,
                "draw": draw,
                "total": total,
            })
        if not any(totals.values()):
            totals = {
                "victory": sum(row["victory"] for row in rows),
                "defeat": sum(row["defeat"] for row in rows),
                "draw": sum(row["draw"] for row in rows),
            }
        rows.sort(key=lambda row: row["total"], reverse=True)
        return {"total": totals, "brawlers": rows}

    @staticmethod
    def get_settings(bot_config, general_config):
        post_match_action = str(bot_config.get("post_match_action", "lobby")).strip().lower()
        if post_match_action not in ("lobby", "play_again"):
            post_match_action = "lobby"
        return {
            "core": {
                "play_again": post_match_action == "play_again",
                "global_pick_brawlers": bool(bot_config.get("global_pick_brawlers", False)),
            },
            "general": {
                "trophies_multiplier": general_config.get("trophies_multiplier", 1),
                "emulator_port": general_config.get("emulator_port", 5555),
                "brawl_stars_package": general_config.get("brawl_stars_package", "com.supercell.brawlstars"),
                "super_debug": general_config.get("super_debug", "no"),
                "window_controller_fix": int(general_config.get("window_controller_fix", 0)),
                "starr_drop_detect": bool(general_config.get("starr_drop_detect", True)),
            },
            "starr_drop": {
                "starr_drop_threshold": float(general_config.get("starr_drop_threshold", 0.80)),
                "starr_drop_post_tap_delay": float(general_config.get("starr_drop_post_tap_delay", 7.0)),
                "starr_drop_post_hold_delay": float(general_config.get("starr_drop_post_hold_delay", 7.0)),
            },
            "bot": {
                "seconds_to_hold_attack_after_reaching_max": bot_config.get("seconds_to_hold_attack_after_reaching_max", 1.5),
                "idle_pixels_minimum": bot_config.get("idle_pixels_minimum", 3000),
                "super_pixels_minimum": bot_config.get("super_pixels_minimum", 1800),
                "gadget_pixels_minimum": bot_config.get("gadget_pixels_minimum", 1300),
                "hypercharge_pixels_minimum": bot_config.get("hypercharge_pixels_minimum", 1800),
            },
            "timers": load_toml_as_dict(str(ROOT / "cfg" / "time_tresholds.toml")),
        }

    @staticmethod
    def get_logging_settings():
        return {
            "discord": load_toml_as_dict(str(ROOT / "cfg" / "discord_config.toml")),
            "telegram": load_toml_as_dict(str(ROOT / "cfg" / "telegram_config.toml")),
        }

    def _global_pick_enabled(self):
        bot_config = load_toml_as_dict(str(ROOT / "cfg" / "bot_config.toml"))
        return bool(bot_config.get("global_pick_brawlers", False))

    def save_queue_entry(self, payload):
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        if not isinstance(queue, list):
            queue = []
        brawler = str(payload.get("brawler", "")).strip()
        push_type = payload.get("type", "trophies")
        auto_pick = True if self._global_pick_enabled() else bool(payload.get("automatically_pick", bool(queue)))
        data = {
            "brawler": brawler,
            "push_until": self._int_value(payload.get("push_until"), 1000 if push_type == "trophies" else 300),
            "trophies": self._int_value(payload.get("trophies"), 0),
            "wins": self._int_value(payload.get("wins"), 0),
            "type": push_type if push_type in ("trophies", "wins") else "trophies",
            "automatically_pick": auto_pick,
            "selection_method": payload.get("selection_method") if payload.get("selection_method") in ("lowest_trophies", "highest_trophies", "named_brawler") else "named_brawler",
            "win_streak": self._int_value(payload.get("win_streak"), 0),
        }
        queue = [row for row in queue if row.get("brawler") != brawler]
        queue.append(data)
        save_brawler_data(queue)
        return queue

    def start_bot(self, payload):
        logs_dir = ROOT / "logs"
        stop_flag = logs_dir / "web_stop_requested.flag"
        try:
            stop_flag.unlink()
        except OSError:
            pass
        # Clear stale runtime-control files from a previous stopped run.
        # Without this, /api/state can keep seeing an old "stopped" file and
        # the dashboard remains stuck in STOP/START after a restart.
        for path in logs_dir.glob("runtime_control_*.state"):
            try:
                path.unlink()
            except OSError:
                pass
        queue = payload.get("queue")
        if not isinstance(queue, list) or not queue:
            queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        playstyle = "team_showdown.pyla"
        if not queue:
            raise ValueError("Queue is required before starting.")
        bot_config_path = str(ROOT / "cfg" / "bot_config.toml")
        bot_config = dict(load_toml_as_dict(bot_config_path))
        bot_config["current_playstyle"] = playstyle
        save_dict_as_toml(bot_config, bot_config_path)
        save_brawler_data(queue)
        self.selected_data = queue
        self.data_setter(queue)
        self.ready.set()
        with self.runtime_lock:
            self.runtime_state = "running"
        return {"ok": True, "state": "running"}

    def stop_bot(self):
        logs_dir = ROOT / "logs"

        # reset runtime ready state so frontend
        # does not switch back into STOP state
        try:
            self.ready.clear()
        except Exception:
            pass
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / "web_stop_requested.flag").write_text("stopped", encoding="utf-8")
        stopped_any = False
        for path in logs_dir.glob("runtime_control_*.state"):
            try:
                path.write_text("stopped", encoding="utf-8")
                stopped_any = True
            except OSError:
                pass
        with self.runtime_lock:
            self.runtime_state = "stopped"
        return {"ok": True, "stopped": stopped_any, "state": "stopped"}

    def save_queue_bulk(self, payload):
        queue = payload.get("queue")
        if not isinstance(queue, list):
            raise ValueError("Queue must be a list.")
        cleaned = []
        seen = set()
        for row in queue:
            if not isinstance(row, dict):
                continue
            brawler = str(row.get("brawler", "")).strip()
            if not brawler or brawler in seen:
                continue
            seen.add(brawler)
            push_type = row.get("type", "trophies")
            cleaned.append({
                "brawler": brawler,
                "push_until": self._int_value(row.get("push_until"), 1000 if push_type == "trophies" else 300),
                "trophies": self._int_value(row.get("trophies"), 0),
                "wins": self._int_value(row.get("wins"), 0),
                "type": push_type if push_type in ("trophies", "wins") else "trophies",
                "automatically_pick": bool(row.get("automatically_pick", bool(cleaned))),
                "selection_method": row.get("selection_method", "named_brawler"),
                "win_streak": self._int_value(row.get("win_streak"), 0),
            })
        save_brawler_data(cleaned)
        return cleaned

    def clear_queue(self):
        save_brawler_data([])
        return {"queue": []}

    def get_queue(self, instance_id=None):
        # FIX: if instance_id provided and > 1, check for per-instance queue file first.
        if instance_id and int(instance_id) > 1:
            per_file = ROOT / f"latest_brawler_data_{int(instance_id)}.json"
            if per_file.exists():
                queue = self._read_json(per_file, [])
                if isinstance(queue, list) and queue:
                    return {"queue": queue}
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        return {"queue": queue if isinstance(queue, list) else []}

    def create_queue_entry(self, payload):
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        if not isinstance(queue, list):
            queue = []
        brawler = str(payload.get("brawler", "")).strip()
        if not brawler:
            raise ValueError("brawler is required")
        push_type = payload.get("type", "trophies")
        selection_method = payload.get("selection_method", "named_brawler")
        if selection_method not in ("lowest_trophies", "highest_trophies", "named_brawler"):
            selection_method = "named_brawler"
        data = {
            "brawler": brawler,
            "push_until": self._int_value(payload.get("push_until"), 1000 if push_type == "trophies" else 300),
            "trophies": self._int_value(payload.get("trophies"), 0),
            "wins": self._int_value(payload.get("wins"), 0),
            "type": push_type if push_type in ("trophies", "wins") else "trophies",
            "automatically_pick": True if self._global_pick_enabled() else bool(payload.get("automatically_pick", True)),
            "selection_method": selection_method,
            "win_streak": self._int_value(payload.get("win_streak"), 0),
        }
        queue = [row for row in queue if row.get("brawler") != brawler]
        queue.append(data)
        save_brawler_data(queue)
        return {"queue": queue}

    def delete_queue_entry(self, payload):
        brawler = str(payload.get("brawler", "")).strip()
        if not brawler:
            raise ValueError("brawler is required")
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        if not isinstance(queue, list):
            queue = []
        queue = [row for row in queue if row.get("brawler") != brawler]
        save_brawler_data(queue)
        return {"queue": queue}

    def reorder_queue(self, payload):
        order = payload.get("order")
        if not isinstance(order, list):
            raise ValueError("order must be a list of brawler names")
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        if not isinstance(queue, list):
            queue = []
        index_map = {row.get("brawler"): row for row in queue}
        reordered = [index_map[b] for b in order if b in index_map]
        # keep any brawlers not mentioned at the end
        mentioned = set(order)
        for row in queue:
            if row.get("brawler") not in mentioned:
                reordered.append(row)
        save_brawler_data(reordered)
        return {"queue": reordered}

    def resume_bot(self):
        logs_dir = ROOT / "logs"
        stop_flag = logs_dir / "web_stop_requested.flag"
        try:
            stop_flag.unlink()
        except OSError:
            pass
        for path in logs_dir.glob("runtime_control_*.state"):
            try:
                path.write_text("running", encoding="utf-8")
            except OSError:
                pass

        # Load queue and playstyle from saved files so pyla_main can restart.
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        if not isinstance(queue, list):
            queue = []
        bot_config = load_toml_as_dict(str(ROOT / "cfg" / "bot_config.toml"))
        playstyle = "team_showdown.pyla"

        if not queue:
            with self.runtime_lock:
                self.runtime_state = "running"
            self.ready.set()
            return {"ok": True, "state": "running", "warning": "No queue saved; start from dashboard first."}

        self.selected_data = queue
        self.data_setter(queue)
        with self.runtime_lock:
            self.runtime_state = "running"
        self.ready.set()
        return {"ok": True, "state": "running"}

    def update_config(self, payload):
        path_map = {
            "general": ROOT / "cfg" / "general_config.toml",
            "bot": ROOT / "cfg" / "bot_config.toml",
            "core": ROOT / "cfg" / "bot_config.toml",
            "timers": ROOT / "cfg" / "time_tresholds.toml",
            "discord": ROOT / "cfg" / "discord_config.toml",
            "telegram": ROOT / "cfg" / "telegram_config.toml",
            "starr_drop": ROOT / "cfg" / "general_config.toml",
        }
        section = payload.get("section")
        key = payload.get("key")
        if section not in path_map or not key:
            raise ValueError("Invalid settings target.")
        path = str(path_map[section])
        config = dict(load_toml_as_dict(path))
        # Map core.play_again (bool) → bot_config.post_match_action (string)
        if section == "core" and key == "play_again":
            config["post_match_action"] = "play_again" if payload.get("value") else "lobby"
        elif section == "core" and key == "global_pick_brawlers":
            config["global_pick_brawlers"] = bool(payload.get("value"))
        else:
            config[str(key)] = payload.get("value")
        save_dict_as_toml(config, path)
        # Reload starr_drop_detect at runtime if the flag changed
        if section == "general" and key == "starr_drop_detect":
            try:
                from starr_drop import StarrDropIntegration  # noqa
                # Find any live integration instance and reload it
                import gc as _gc
                for obj in _gc.get_objects():
                    if isinstance(obj, StarrDropIntegration):
                        obj.reload_enabled()
                        break
            except Exception:
                pass
        return {"ok": True, "settings": self.build_state()["settings"]}

    def get_onnx_providers(self):
        """Run onnxruntime provider check and return available options."""
        available = []
        try:
            import onnxruntime as ort
            providers = ort.get_available_providers()
            # CPU is always available
            available.append("cpu")
            if "CUDAExecutionProvider" in providers:
                available.append("cuda")
            if "DmlExecutionProvider" in providers:
                available.append("directml")
        except Exception:
            # onnxruntime not installed yet — fall back to running the script
            try:
                import subprocess, sys
                script = str(ROOT / "tools" / "directmltest.py")
                result = subprocess.run(
                    [sys.executable, script],
                    capture_output=True, text=True, timeout=15
                )
                out = result.stdout
                available.append("cpu")
                if "CUDAExecutionProvider" in out:
                    available.append("cuda")
                if "DmlExecutionProvider" in out:
                    available.append("directml")
            except Exception:
                available = ["cpu"]
        return {"ok": True, "providers": available}

    def check_player_tag_onboarding(self, payload):
        """Fetch player info by tag for the onboarding step (lightweight — no brawler sync)."""
        tag = str(payload.get("tag", "")).strip()
        if not tag:
            raise ValueError("Player tag is required.")
        player = fetch_brawltracker_player(tag, timeout=15)
        return {
            "ok": True,
            "name": player.get("name", ""),
            "tag": player.get("tag", ""),
            "icon": player.get("icon", ""),
            "total_trophies": player.get("total_trophies", 0),
        }

    def get_onboarding_status(self):
        """Return fsos value from general_config (0 = first run, 1 = completed)."""
        config = load_toml_as_dict(str(ROOT / "cfg" / "general_config.toml"))
        fsos = int(config.get("fsos", 0))
        return {"fsos": fsos}

    def complete_onboarding(self, payload):
        """Save onboarding choices and set fsos=1."""
        config_path = str(ROOT / "cfg" / "general_config.toml")
        config = dict(load_toml_as_dict(config_path))

        cpu_or_gpu = str(payload.get("cpu_or_gpu", "cpu")).strip().lower()
        if cpu_or_gpu not in ("cpu", "cuda", "directml"):
            cpu_or_gpu = "cpu"
        config["cpu_or_gpu"] = cpu_or_gpu

        client = str(payload.get("brawl_client", "BSD")).strip()
        package_map = {
            "BSD": "bsd.suitcase.release",
            "BSD+": "bsd.suitcase.plus",
            "Original": "com.supercell.brawlstars",
        }
        config["brawl_stars_package"] = package_map.get(client, "bsd.suitcase.release")

        emulator = str(payload.get("emulator", "LDPlayer")).strip()
        if emulator in ("LDPlayer", "MuMu"):
            config["current_emulator"] = emulator

        config["fsos"] = 1
        save_dict_as_toml(config, config_path)

        # Save player tag if provided
        player_tag = str(payload.get("player_tag", "")).strip()
        if player_tag:
            api_path = str(ROOT / "cfg" / "brawl_stars_api.toml")
            api_config = dict(load_toml_as_dict(api_path))
            api_config["player_tag"] = player_tag.lstrip("#")
            save_dict_as_toml(api_config, api_path)

        return {"ok": True}

    def update_player_tag(self, payload):
        path = str(ROOT / "cfg" / "brawl_stars_api.toml")
        config = dict(load_toml_as_dict(path))
        config["player_tag"] = str(payload.get("player_tag", "")).strip()
        save_dict_as_toml(config, path)
        return {"ok": True, "playerTag": config["player_tag"]}



    def send_telegram_test(self):
        try:
            ok = asyncio.run(async_send_test_notification())
        except Exception as exc:
            raise ValueError(str(exc))
        return {"ok": bool(ok)}

    def start_telegram_bot(self):
        try:
            import threading
            import asyncio
            from telegram.bot import main as telegram_main
            
            def run_telegram():
                try:
                    asyncio.run(telegram_main())
                except Exception as e:
                    print(f"Telegram bot error: {e}")
            
            thread = threading.Thread(target=run_telegram, daemon=True)
            thread.start()
            
            return {"ok": True, "message": "telegram bot started"}
        except Exception as exc:
            raise ValueError(str(exc))

    def send_esp_debug(self):
        """Send ESP debug screenshot to Telegram"""
        try:
            play_instance = get_play_instance()
            if not play_instance:
                raise ValueError("Bot is not running - no Play instance available")
            
            # Get current frame and detection data
            current_frame = getattr(play_instance, 'current_frame', None)
            if current_frame is None:
                raise ValueError("No current frame available - bot must be in game")
            
            # Get the last detection data
            detection_data = {
                "player": getattr(play_instance, '_last_player_data', []),
                "enemy": getattr(play_instance, '_last_enemy_data', []), 
                "teammate": getattr(play_instance, '_last_teammate_data', []),
                "wall": getattr(play_instance, '_last_wall_data', [])
            }
            
            # Create ESP debug image
            esp_image = play_instance.create_esp_debug_image(
                current_frame, 
                detection_data, 
                getattr(play_instance, 'current_brawler', None)
            )
            
            if esp_image is None:
                raise ValueError("Failed to create ESP debug image")
            
            # Check Telegram settings manually
            settings = load_telegram_settings()
            token = settings.get("bot_token", "").strip()
            chat_id = settings.get("chat_id", "").strip()
            
            if not token or not chat_id:
                raise ValueError("Telegram bot token or chat ID is not configured. Please fill these settings in Logging → Telegram section.")
            
            # Send directly to Telegram API
            image_bytes = _image_bytes(esp_image)
            if not image_bytes:
                raise ValueError("Failed to process ESP debug image")
            
            text = f"🔍 <b>ESP Debug View</b>\n\n<b>Brawler:</b> {getattr(play_instance, 'current_brawler', 'Unknown')}\n<b>State:</b> Debug screenshot with ESP visualization\n\n🟢 Player | 🔴 Enemy | 🔵 Teammate | ⬜ Walls"
            
            async def send_direct():
                async with aiohttp.ClientSession() as session:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", chat_id)
                    form.add_field("caption", text)
                    form.add_field("parse_mode", "HTML")
                    form.add_field("photo", image_bytes, filename="esp_debug.png", content_type="image/png")
                    async with session.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=form, timeout=20) as response:
                        return response.status == 200
            
            ok = asyncio.run(send_direct())
            
            if ok:
                return {"ok": True, "message": "ESP debug screenshot sent to Telegram!"}
            else:
                raise ValueError("Failed to send to Telegram - check bot token and chat ID")
            
        except Exception as exc:
            raise ValueError(str(exc))

    def load_player_trophies(self, player_tag_override=None):

        # In Multi Instance mode the browser can pass ?tag=... so each LDPlayer tab
        # can sync a different account without overwriting cfg/brawl_stars_api.toml.
        config_path = str(ROOT / "cfg" / "brawl_stars_api.toml")
        config = dict(load_toml_as_dict(config_path))
        player_tag = str(player_tag_override or config.get("player_tag", "")).strip()

        # Keep the old sync module path for normal single-instance sync only.
        if not player_tag_override:
            sync_module = load_syncbrawlers2api_module()
            if sync_module and hasattr(sync_module, "sync_from_brawltracker"):
                player = sync_module.sync_from_brawltracker(self.brawlers)
                if player.get("missed"):
                    print(f"warning brawlers from site not found in local list: {player.get('missed')}")
                return {
                    "player": player.get("player", ""),
                    "tag": player.get("tag", ""),
                    "trophies": player.get("trophies", {}),
                    "powers": player.get("powers", {}),
                    "source": player.get("source", "brawltracker+icons2"),
                }

        # Fallback / multi-instance path: built-in parser, but with stronger icons2 matching.
        timeout = int(config.get("timeout_seconds", 15) or 15)
        known = build_icons2_known_map(self.brawlers)

        player = fetch_brawltracker_player(player_tag, timeout=timeout)

        trophies = {}
        powers = {}
        missed = []
        for api_brawler in player.get("brawlers", []):
            key = local_brawler_match_key(api_brawler.get("name", ""))
            brawler = known.get(key)
            if not brawler:
                try:
                    brawler = known.get(normalize_brawler_name(api_brawler.get("name", "")))
                except Exception:
                    brawler = None
            if brawler:
                trophies[brawler] = int(api_brawler.get("trophies", 0) or 0)
                powers[brawler] = int(api_brawler.get("power", 0) or 0)
            else:
                missed.append(api_brawler.get("name", ""))
        if missed:
            print(f"warning: brawlers from site not found in local list: {missed}")
        return {
            "player": player.get("name", ""),
            "tag": player.get("tag", player_tag),
            "trophies": trophies,
            "powers": powers,
            "source": player.get("source", "brawltracker"),
        }

    @staticmethod
    def _int_value(value, default=0):
        try:
            if value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _read_json(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default


class WebRequestHandler(SimpleHTTPRequestHandler):
    web_app = None

    def translate_path(self, path):
        parsed = urlparse(path)
        requested = unquote(parsed.path)
        if requested.startswith("/assets/brawler_icons/"):
            filename = os.path.basename(requested)
            return str(BRAWLER_ICONS / filename)
        if requested.startswith("/assets/brawler_icons2/"):
            filename = os.path.basename(requested)
            return str(BRAWLER_ICONS2 / filename)
        if requested == "/":
            return str(WEB_ROOT / "index.html")
        return str(WEB_ROOT / requested.lstrip("/"))

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/onboarding":
            return self.send_json(self.web_app.get_onboarding_status())
        if parsed.path == "/api/onboarding/providers":
            return self.send_json(self.web_app.get_onnx_providers())
        if parsed.path == "/api/state":
            return self.send_json(self.web_app.build_state())
        if parsed.path == "/api/player":
            try:
                query = parse_qs(parsed.query)
                return self.send_json(self.web_app.load_player_trophies(query.get("tag", [None])[0]))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/shutdown":
            self.web_app.ready.set()
            return self.send_json({"ok": True})
        if parsed.path == "/api/queue":
            query = parse_qs(parsed.query)
            instance_id = query.get("instance_id", [None])[0]
            return self.send_json(self.web_app.get_queue(instance_id=instance_id))
        if parsed.path == "/api/online":
            return self.send_json(self.web_app.get_online())
        if parsed.path == "/api/multi/state":
            return self.send_json(self.web_app.multi.public_state())
        if parsed.path == "/api/multi/scan":
            return self.send_json({"ok": True, "devices": self.web_app.multi.scan_devices(connect=True)})
        if parsed.path == "/api/multi/logs":
            query = parse_qs(parsed.query)
            return self.send_json(self.web_app.multi.logs(query.get("id", [1])[0]))
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        payload = self.read_json_body()
        try:
            if parsed.path == "/api/online/ping":
                client_ip = self.client_address[0]
                return self.send_json(self.web_app.ping_online(client_ip))
            if parsed.path == "/api/onboarding":
                return self.send_json(self.web_app.complete_onboarding(payload))
            if parsed.path == "/api/onboarding/check-tag":
                return self.send_json(self.web_app.check_player_tag_onboarding(payload))
            if parsed.path == "/api/queue":
                return self.send_json({"queue": self.web_app.save_queue_entry(payload)})
            if parsed.path == "/api/queuecreate":
                return self.send_json(self.web_app.create_queue_entry(payload))
            if parsed.path == "/api/queuedelete":
                return self.send_json(self.web_app.delete_queue_entry(payload))
            if parsed.path == "/api/queue/reorder":
                return self.send_json(self.web_app.reorder_queue(payload))
            if parsed.path == "/api/queue/bulk":
                return self.send_json({"queue": self.web_app.save_queue_bulk(payload)})
            if parsed.path == "/api/start":
                return self.send_json(self.web_app.start_bot(payload))
            if parsed.path == "/api/stop":
                return self.send_json(self.web_app.stop_bot())
            if parsed.path == "/api/resume":
                return self.send_json(self.web_app.resume_bot())
            if parsed.path == "/api/queue/clear":
                return self.send_json(self.web_app.clear_queue())
            if parsed.path == "/api/settings":
                return self.send_json(self.web_app.update_config(payload))
            if parsed.path == "/api/player-tag":
                return self.send_json(self.web_app.update_player_tag(payload))
            if parsed.path == "/api/logging/telegram-test":
                return self.send_json(self.web_app.send_telegram_test())
            if parsed.path == "/api/logging/telegram-start":
                return self.send_json(self.web_app.start_telegram_bot())
            if parsed.path == "/api/logging/esp-debug":
                return self.send_json(self.web_app.send_esp_debug())
            if parsed.path == "/api/multi/start":
                return self.send_json(self.web_app.multi.start_instance(payload))
            if parsed.path == "/api/multi/stop":
                return self.send_json(self.web_app.multi.stop_instance(payload.get("id", 1)))
            if parsed.path == "/api/multi/pause":
                return self.send_json(self.web_app.multi.set_paused(payload.get("id", 1), True))
            if parsed.path == "/api/multi/resume":
                return self.send_json(self.web_app.multi.set_paused(payload.get("id", 1), False))
            if parsed.path == "/api/multi/stop-all":
                return self.send_json(self.web_app.multi.stop_all())
            if parsed.path == "/api/multi/pause-all":
                for row in self.web_app.multi.public_state().get("instances", []):
                    self.web_app.multi.set_paused(row.get("id"), True)
                return self.send_json(self.web_app.multi.public_state())
            if parsed.path == "/api/multi/resume-all":
                for row in self.web_app.multi.public_state().get("instances", []):
                    self.web_app.multi.set_paused(row.get("id"), False)
                return self.send_json(self.web_app.multi.public_state())
        except Exception as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
