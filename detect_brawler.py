import os
import cv2
import glob
import time
import argparse
import subprocess
import numpy as np

ASSETS_DIR = os.path.join("api", "assets", "brawler_icons2")
DEBUG_DIR = "debug_brawlers"
os.makedirs(DEBUG_DIR, exist_ok=True)

parser = argparse.ArgumentParser()
parser.add_argument("--brawler", type=str, help="Искать конкретного бойца (например: gray)")
parser.add_argument("--once", action="store_true", help="Сделать один скан и выйти")
parser.add_argument("--interval", type=float, default=2.0, help="Интервал между сканами")
args = parser.parse_args()

if args.brawler:
    pattern = os.path.join(ASSETS_DIR, f"*{args.brawler}*.png")
else:
    pattern = os.path.join(ASSETS_DIR, "*.png")
icon_files = glob.glob(pattern)
print(f"Icons: {ASSETS_DIR} | loaded={len(icon_files)}")

icons = []
for f in icon_files:
    img = cv2.imread(f, cv2.IMREAD_UNCHANGED)
    if img is not None:
        icons.append((os.path.basename(f), img))
    else:
        print(f"не удалось загрузить {f}")

def adb_check():
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    devices = [l.split()[0] for l in lines[1:] if l.strip() and "device" in l]
    if not devices:
        raise RuntimeError("ADB устройства не найдены")
    print(f"ADB device: {devices[0]}")
    return devices[0]

device = adb_check()

def get_screenshot():
    remote_path = "/sdcard/tmp_screen.png"
    local_path = os.path.join(DEBUG_DIR, "screen.png")
    subprocess.run(["adb", "shell", f"screencap -p {remote_path}"])
    subprocess.run(["adb", "pull", remote_path, local_path])
    subprocess.run(["adb", "shell", f"rm {remote_path}"])
    frame = cv2.imread(local_path)
    if frame is None:
        print(f"не удалось прочитать скрин {local_path}")
    return frame

def detect_brawlers(frame):
    found = []

    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    for name, icon in icons:

        if icon.shape[2] == 4:
            icon = cv2.cvtColor(icon, cv2.COLOR_BGRA2BGR)

        icon_gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY)

        best_score = 0
        best_loc = None
        best_size = None

        for scale in np.arange(0.5, 1.6, 0.1):

            resized = cv2.resize(
                icon_gray,
                (0, 0),
                fx=scale,
                fy=scale
            )

            h, w = resized.shape[:2]

            if h >= frame_gray.shape[0] or w >= frame_gray.shape[1]:
                continue

            result = cv2.matchTemplate(
                frame_gray,
                resized,
                cv2.TM_CCOEFF_NORMED
            )

            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val > best_score:
                best_score = max_val
                best_loc = max_loc
                best_size = (w, h)

        if best_score > 0.45:

            x, y = best_loc
            w, h = best_size

            found.append((name, x, y, best_score))

            overlay = frame.copy()

            cv2.rectangle(
                overlay,
                (x, y),
                (x + w, y + h),
                (0, 0, 255),
                -1
            )

            cv2.addWeighted(
                overlay,
                0.35,
                frame,
                0.65,
                0,
                frame
            )

            cv2.rectangle(
                frame,
                (x, y),
                (x + w, y + h),
                (0, 0, 255),
                4
            )

            cv2.putText(
                frame,
                f"{name} {best_score:.2f}",
                (x, y - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 255, 255),
                3
            )

            print(f"found {name} score={best_score:.2f}")

    return found, frame

while True:
    frame = get_screenshot()
    if frame is None:
        time.sleep(args.interval)
        continue
    found, debug_frame = detect_brawlers(frame)
    for name, x, y, score in found:
        print(f"[FOUND] {name} at ({x},{y}) score={score:.2f}")

    debug_path = os.path.join(DEBUG_DIR, f"debug_{int(time.time())}.png")
    cv2.imwrite(debug_path, debug_frame)

    if args.once:
        break
    time.sleep(args.interval)