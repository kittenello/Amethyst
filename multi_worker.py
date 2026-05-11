import json
import os
import sys
import traceback
from pathlib import Path

# This file is launched from .multi_instances/instance_N.
# It intentionally runs only the bot core, without starting another WebApp.
ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    from main import pyla_main

    queue_path = ROOT / "latest_brawler_data.json"
    if not queue_path.exists():
        raise RuntimeError("latest_brawler_data.json is missing for this instance")
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if not isinstance(queue, list) or not queue:
        raise RuntimeError("Instance queue is empty")

    print(f"[MULTI-WORKER] Starting instance in {ROOT}", flush=True)
    print(f"[MULTI-WORKER] Queue: {[row.get('brawler') for row in queue]}", flush=True)
    pyla_main(queue)
    print("[MULTI-WORKER] Bot loop finished", flush=True)
except Exception as exc:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    crash = log_dir / "multi_worker_crash.log"
    crash.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
    print(f"[MULTI-WORKER][CRASH] {exc}", flush=True)
    print(traceback.format_exc(), flush=True)
    raise
