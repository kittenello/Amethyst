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
from telegram_notifier import async_send_test_message


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "webapp" / "static"
BRAWLER_ICONS = ROOT / "api" / "assets" / "brawler_icons"
BRAWLER_ICONS2 = ROOT / "api" / "assets" / "brawler_icons2"
SYNCBRAWLERS2API = ROOT / "api" / "syncbrawlers2api.py"


def title_from_slug(value):
    parts = re.split(r"[^a-zA-Z0-9]+", str(value).replace("8bit", "8 bit"))
    name = " ".join(part.capitalize() for part in parts if part)
    return name.replace("8 Bit", "8bit") or str(value)


def read_playstyle_meta(path):
    fallback = {
        "file": path.name,
        "name": path.stem.replace("_", " ").title(),
        "description": "Custom PylaAI playstyle",
        "author": "Local",
        "date": "",
        "brawlers": ["all"],
        "gamemodes": ["all"],
    }
    try:
        first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        meta = json.loads(first_line)
        if isinstance(meta, dict):
            fallback.update(meta)
    except Exception:
        pass
    fallback["file"] = path.name
    return fallback


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
    print(f"[WEBAPP][BRAWLTRACKER] Status: {response.status_code}, bytes: {len(response.text)}, mode={mode}")
    response.raise_for_status()
    return response.text


def _urllib_get_text(url, timeout):
    req = Request(url, headers=BRAWLTRACKER_HEADERS)
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
    html = data.decode("utf-8", errors="replace")
    print(f"[WEBAPP][BRAWLTRACKER] Status: urllib, bytes: {len(html)}")
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
    print(f"[WEBAPP][BRAWLTRACKER] Status: curl, bytes: {len(result.stdout)}")
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
            print(f"[WEBAPP][BRAWLTRACKER] Attempt {index}/{len(attempts)}: {name}")
            html = getter()
            if html and "brawltracker" in html.lower() or "BRAWLER" in html.upper() or "Player Icon" in html:
                return html
            if html:
                return html
            raise RuntimeError("empty response")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"[WEBAPP][BRAWLTRACKER][WARN] {errors[-1]}")
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
    print(f"[WEBAPP][BRAWLTRACKER] Request: {url}")
    html = fetch_url_text_robust(url, timeout=timeout)

    debug_dir = ROOT / "logs"
    debug_dir.mkdir(exist_ok=True)
    try:
        (debug_dir / "debug_brawltracker.html").write_text(html, encoding="utf-8")
        print(f"[WEBAPP][BRAWLTRACKER] HTML saved: {debug_dir / 'debug_brawltracker.html'}")
    except OSError as exc:
        print(f"[WEBAPP][BRAWLTRACKER][WARN] Could not save debug html: {exc}")

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
            print(f"[WEBAPP][BRAWLTRACKER][WARN] No trophies found for {name}")
            continue
        brawlers.append({
            "name": name,
            "trophies": int(trophy_match.group(1)),
            "power": int(power_match.group(1)) if power_match else 0,
        })

    if not brawlers:
        raise RuntimeError("Brawltracker HTML loaded, but no brawler cards were parsed. Check logs/debug_brawltracker.html")

    print(f"[WEBAPP][BRAWLTRACKER] Parsed player={player_name or 'Player'} brawlers={len(brawlers)}")
    return {"name": player_name or "Player", "tag": f"#{tag}", "brawlers": brawlers, "source": "brawltracker"}



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
        print(f"[WEBAPP][PLAYER][WARN] syncbrawlers2api load failed: {exc}")
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
                if port not in self.default_ports and port not in (5563, 5554, 5556):
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
        save_dict_as_toml(config, str(cfg_path))

    def _queue_for_instance(self, payload, instance_id):
        queue = payload.get("queue")
        if not isinstance(queue, list) or not queue:
            queue = []
        # Optional per-instance override from UI can be added later; default: use current queue.
        if not queue:
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
            # Fast path after first prebuild: only refresh selected cfg and port.
            self._copy_project_runtime(runtime_dir, cfg_source)
            self._patch_instance_config(runtime_dir, port)
            (runtime_dir / "latest_brawler_data.json").write_text(json.dumps(queue, indent=2), encoding="utf-8")

            log_path = self.logs_dir / f"multi_instance_{instance_id}.log"
            log_file = open(log_path, "a", encoding="utf-8", errors="replace")
            env = os.environ.copy()
            env["PYLA_MULTI_INSTANCE_ID"] = str(instance_id)
            env["PYLA_MULTI_INSTANCE_PORT"] = str(port)
            env["PYLA_MULTI_INSTANCE_SERIAL"] = f"127.0.0.1:{port}"
            env["PYLA_MULTI_INSTANCE_NAME"] = self._emulator_name(port)
            env["PYLA_MULTI_INSTANCE_CFG"] = cfg_source.name
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
        self.server = None
        self.thread = None
        self.selected_data = None
        self.multi = MultiInstanceManager(ROOT)

    def start(self):
        app = self

        class Handler(WebRequestHandler):
            web_app = app

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = f"http://{self.host}:{self.port}"
        print(f"Amethyst webapp: {url}")
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
            "currentPlaystyle": bot_config.get("current_playstyle", ""),
            "brawlers": self.get_brawlers(),
            "queue": queue if isinstance(queue, list) else [],
            "playstyles": self.get_playstyles(),
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

    def get_playstyles(self):
        playstyles_dir = ROOT / "playstyles"
        rows = []
        for path in sorted(playstyles_dir.glob("*.pyla")):
            rows.append(read_playstyle_meta(path))
        return rows

    def get_runtime_state(self, queue):
        state = "idle"
        ips = 0.0
        current_brawler = None
        runtime_file = ROOT / "logs" / "web_runtime.json"
        try:
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            state = str(runtime.get("state") or state)
            ips = runtime.get("ips", ips)
            current_brawler = runtime.get("currentBrawler")
        except Exception:
            runtime = {}
        for path in (ROOT / "logs").glob("runtime_control_*.state"):
            try:
                control_state = path.read_text(encoding="utf-8", errors="ignore").strip()
                if control_state == "stopped":
                    state = "idle"
                    break
                if control_state == "running" and state == "idle":
                    state = "running"
                    break
            except OSError:
                pass
        current = queue[0] if queue else {}
        return {
            "state": state,
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
        return {
            "general": {
                "trophies_multiplier": general_config.get("trophies_multiplier", 1),
                "emulator_port": general_config.get("emulator_port", 5555),
                "brawl_stars_package": general_config.get("brawl_stars_package", "com.supercell.brawlstars"),
                "super_debug": general_config.get("super_debug", "no"),
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

    def save_queue_entry(self, payload):
        queue = self._read_json(ROOT / "latest_brawler_data.json", [])
        if not isinstance(queue, list):
            queue = []
        brawler = str(payload.get("brawler", "")).strip()
        push_type = payload.get("type", "trophies")
        data = {
            "brawler": brawler,
            "push_until": self._int_value(payload.get("push_until"), 1000 if push_type == "trophies" else 300),
            "trophies": self._int_value(payload.get("trophies"), 0),
            "wins": self._int_value(payload.get("wins"), 0),
            "type": push_type if push_type in ("trophies", "wins") else "trophies",
            "automatically_pick": bool(payload.get("automatically_pick", bool(queue))),
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
        playstyle = str(payload.get("playstyle", "")).strip()
        if not queue or not playstyle:
            raise ValueError("Queue and playstyle are required before starting.")
        bot_config_path = str(ROOT / "cfg" / "bot_config.toml")
        bot_config = dict(load_toml_as_dict(bot_config_path))
        bot_config["current_playstyle"] = playstyle
        save_dict_as_toml(bot_config, bot_config_path)
        save_brawler_data(queue)
        self.selected_data = queue
        self.data_setter(queue)
        self.ready.set()
        return {"ok": True}

    def stop_bot(self):
        logs_dir = ROOT / "logs"
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / "web_stop_requested.flag").write_text("stopped", encoding="utf-8")
        stopped_any = False
        for path in logs_dir.glob("runtime_control_*.state"):
            try:
                path.write_text("stopped", encoding="utf-8")
                stopped_any = True
            except OSError:
                pass
        return {"ok": True, "stopped": stopped_any}

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
                "selection_method": row.get("selection_method", "lowest_trophies"),
                "win_streak": self._int_value(row.get("win_streak"), 0),
            })
        save_brawler_data(cleaned)
        return cleaned

    def clear_queue(self):
        save_brawler_data([])
        return {"queue": []}

    def update_config(self, payload):
        path_map = {
            "general": ROOT / "cfg" / "general_config.toml",
            "bot": ROOT / "cfg" / "bot_config.toml",
            "timers": ROOT / "cfg" / "time_tresholds.toml",
            "discord": ROOT / "cfg" / "discord_config.toml",
            "telegram": ROOT / "cfg" / "telegram_config.toml",
        }
        section = payload.get("section")
        key = payload.get("key")
        if section not in path_map or not key:
            raise ValueError("Invalid settings target.")
        path = str(path_map[section])
        config = dict(load_toml_as_dict(path))
        config[str(key)] = payload.get("value")
        save_dict_as_toml(config, path)
        return {"ok": True, "settings": self.build_state()["settings"]}

    def update_player_tag(self, payload):
        path = str(ROOT / "cfg" / "brawl_stars_api.toml")
        config = dict(load_toml_as_dict(path))
        config["player_tag"] = str(payload.get("player_tag", "")).strip()
        save_dict_as_toml(config, path)
        return {"ok": True, "playerTag": config["player_tag"]}

    def import_playstyle(self, payload):
        filename = os.path.basename(str(payload.get("filename", "")).strip())
        content = str(payload.get("content", ""))
        if not filename.lower().endswith(".pyla"):
            raise ValueError("Only .pyla files can be imported.")
        if not filename:
            raise ValueError("Missing playstyle filename.")
        target_dir = ROOT / "playstyles"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / filename
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            index = 2
            while target.exists():
                target = target_dir / f"{stem}_{index}{suffix}"
                index += 1
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "playstyles": self.get_playstyles(), "file": target.name}

    def delete_playstyle(self, payload):
        filename = os.path.basename(str(payload.get("filename", "")).strip())
        if not filename.lower().endswith(".pyla"):
            raise ValueError("Only .pyla playstyles can be deleted.")
        target = ROOT / "playstyles" / filename
        if not target.exists():
            raise ValueError("Playstyle not found.")
        target.unlink()
        bot_config_path = str(ROOT / "cfg" / "bot_config.toml")
        bot_config = dict(load_toml_as_dict(bot_config_path))
        if bot_config.get("current_playstyle") == filename:
            bot_config["current_playstyle"] = ""
            save_dict_as_toml(bot_config, bot_config_path)
        return {"ok": True, "playstyles": self.get_playstyles()}

    def send_telegram_test(self):
        try:
            ok = asyncio.run(async_send_test_message())
        except Exception as exc:
            raise ValueError(str(exc))
        return {"ok": bool(ok)}

    def load_player_trophies(self, player_tag_override=None):
        print("[WEBAPP][PLAYER] Loading player data via brawltracker + api/assets/brawler_icons2")

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
                print(f"[WEBAPP][PLAYER] Result: player={player.get('player', '')}, trophies={len(player.get('trophies', {}))}, source={player.get('source')}")
                if player.get("missed"):
                    print(f"[WEBAPP][PLAYER][WARN] Brawlers from site not found in local list: {player.get('missed')}")
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

        print(f"[WEBAPP][PLAYER] Loading player data for tag={player_tag or '<empty>'}")
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
            print(f"[WEBAPP][PLAYER][WARN] Brawlers from site not found in local list: {missed}")
        print(f"[WEBAPP][PLAYER] Result: player={player.get('name', '')}, trophies={len(trophies)}, source={player.get('source')}")
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
        if requested == "/":
            return str(WEB_ROOT / "index.html")
        return str(WEB_ROOT / requested.lstrip("/"))

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
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
            if parsed.path == "/api/queue":
                return self.send_json({"queue": self.web_app.save_queue_entry(payload)})
            if parsed.path == "/api/queue/bulk":
                return self.send_json({"queue": self.web_app.save_queue_bulk(payload)})
            if parsed.path == "/api/start":
                return self.send_json(self.web_app.start_bot(payload))
            if parsed.path == "/api/stop":
                return self.send_json(self.web_app.stop_bot())
            if parsed.path == "/api/queue/clear":
                return self.send_json(self.web_app.clear_queue())
            if parsed.path == "/api/settings":
                return self.send_json(self.web_app.update_config(payload))
            if parsed.path == "/api/player-tag":
                return self.send_json(self.web_app.update_player_tag(payload))
            if parsed.path == "/api/playstyles/import":
                return self.send_json(self.web_app.import_playstyle(payload))
            if parsed.path == "/api/playstyles/delete":
                return self.send_json(self.web_app.delete_playstyle(payload))
            if parsed.path == "/api/logging/telegram-test":
                return self.send_json(self.web_app.send_telegram_test())
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
