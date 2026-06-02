import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

MAX_RETRIES = 5  
RETRY_DELAY = 15 

log_dir = ROOT / "logs"
log_dir.mkdir(exist_ok=True)
crash_log = log_dir / "multi_worker_crash.log"
instance_id = os.environ.get("PYLA_MULTI_INSTANCE_ID", "?")
instance_serial = os.environ.get("PYLA_MULTI_INSTANCE_SERIAL", "")


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} multi instance #{instance_id}] {msg}"
    print(line, flush=True)


def _load_queue():
    queue_path = ROOT / "latest_brawler_data.json"
    if not queue_path.exists():
        raise RuntimeError("latest_brawler_data.json is missing for this instance")
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if not isinstance(queue, list) or not queue:
        raise RuntimeError("Instance queue is empty")
    return queue


def _run_once():
    queue = _load_queue()
    _log(f"Starting in {ROOT}  serial={instance_serial}")
    _log(f"Queue: {[row.get('brawler') for row in queue]}")

    from main import pyla_main  # noqa: PLC0415
    pyla_main(queue)
    _log("Bot loop finished cleanly.")
    return True


def main():
    attempt = 0
    last_exc = None

    while attempt <= MAX_RETRIES:
        if attempt > 0:
            _log(f"Restarting after crash (attempt {attempt}/{MAX_RETRIES}), "
                 f"waiting {RETRY_DELAY}s …")
            stop_flag = log_dir / "web_stop_requested.flag"
            for _ in range(RETRY_DELAY * 2):
                if stop_flag.exists():
                    _log("stop flag detected during retry delay – exiting.")
                    return
                time.sleep(0.5)

        stop_flag = log_dir / "web_stop_requested.flag"
        if stop_flag.exists():
            _log("stop flag present – not starting.")
            return

        try:
            _run_once()
            return
        except Exception as exc:
            last_exc = exc
            tb = traceback.format_exc()
            _log(f"CRASH: {exc}")
            print(tb, flush=True)
            with crash_log.open("a", encoding="utf-8") as fh:
                fh.write(f"attempt {attempt + 1} crashed at "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                fh.write(tb)
            attempt += 1

    _log(f"all {MAX_RETRIES} restart attempts failed. Giving up.")
    if last_exc:
        raise last_exc


main()
