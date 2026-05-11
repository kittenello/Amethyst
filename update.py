import os
import sys
import time
import urllib.request
import zipfile
import shutil
import tempfile

PURPLE = "\033[95m"
WHITE = "\033[97m"
GRAY = "\033[90m"
RESET = "\033[0m"

SPINNER = ["/", "-", "\\", "|"]

art = r"""
           __  __ ______ _______ _    ___     _______ _______
     /\   |  \/  |  ____|__   __| |  | \ \   / / ____|__   __|
    /  \  | \  / | |__     | |  | |__| |\ \_/ / (___    | |
   / /\ \ | |\/| |  __|    | |  |  __  | \   / \___ \   | |
  / ____ \| |  | | |____   | |  | |  | |  | |  ____) |  | |
 /_/    \_\_|  |_|______|  |_|  |_|  |_|  |_| |_____/   |_|
"""

PROTECTED_FILES = [
    "cfg/brawl_stars_api.toml",
    "cfg/telegram_config.toml",
    "cfg/discord_config.toml",
    "cfg/general_config.toml",
    "cfg/adaptive_state.json",
    "cfg/match_history.toml",
    "cfg/login.toml",
    "cfg/time_tresholds.toml",
    "cfg/lobby_config.toml",
    "cfg/bot_config.toml",
]


def clear():
    os.system("cls")


def enable_ansi():
    os.system("")


def cp(text="", color=RESET):
    try:
        width = os.get_terminal_size().columns
    except:
        width = 120

    print(color + text.center(width) + RESET)


def draw_ui(status, task="", percent=0, spin="/"):
    clear()

    bar_len = 32
    filled = int(bar_len * percent / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    for line in art.splitlines():
        cp(line, PURPLE)

    print()

    box = [
        "+------------------------------------------------------------+",
        f"| Status: {status:<51}|",
        f"| Task:   {task:<51}|",
        "+------------------------------------------------------------+",
        f"| {percent:>3}%  {bar} |",
        "+------------------------------------------------------------+",
    ]

    for line in box:
        cp(line, PURPLE)

    print()
    cp(f"----- {spin} -----", PURPLE)
    print()
    cp("Please wait...", GRAY)


def download_with_progress(url, output_path):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req) as response:
        total_size = int(response.headers.get("Content-Length", 0))

        downloaded = 0
        chunk_size = 8192

        with open(output_path, "wb") as f:
            i = 0

            while True:
                chunk = response.read(chunk_size)

                if not chunk:
                    break

                f.write(chunk)
                downloaded += len(chunk)

                percent = int(downloaded * 100 / total_size) if total_size else 0

                draw_ui(
                    status="Downloading update...",
                    task="GitHub repository",
                    percent=percent,
                    spin=SPINNER[i % len(SPINNER)]
                )

                i += 1


def download_and_update():
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = os.path.join(tmp_dir, "update.zip")

            draw_ui(
                status="Connecting...",
                task="Preparing updater",
                percent=0
            )

            time.sleep(0.5)

            download_with_progress(
                "https://github.com/kittenello/Amethyst/archive/refs/heads/main.zip",
                zip_path
            )

            draw_ui(
                status="Extracting files...",
                task="Unpacking archive",
                percent=100
            )

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)

            extracted_folders = [
                f for f in os.listdir(tmp_dir)
                if os.path.isdir(os.path.join(tmp_dir, f))
            ]

            if not extracted_folders:
                raise Exception("Unexpected zip structure.")

            root_extracted_folder = os.path.join(tmp_dir, extracted_folders[0])

            base_dir = os.path.abspath(".")

            all_files = []

            for src_dir, dirs, files in os.walk(root_extracted_folder):
                for file in files:
                    all_files.append((src_dir, file))

            total_files = len(all_files)
            current = 0

            for src_dir, file in all_files:
                current += 1

                rel_dir = os.path.relpath(src_dir, root_extracted_folder)

                dest_dir = (
                    os.path.join(base_dir, rel_dir)
                    if rel_dir != "."
                    else base_dir
                )

                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)

                src_file = os.path.join(src_dir, file)
                dest_file = os.path.join(dest_dir, file)

                rel_file_path = os.path.relpath(
                    dest_file,
                    base_dir
                ).replace("\\", "/")

                percent = int(current * 100 / total_files)

                draw_ui(
                    status="Installing update...",
                    task=rel_file_path[:45],
                    percent=percent,
                    spin=SPINNER[current % len(SPINNER)]
                )

                if (
                    rel_file_path in PROTECTED_FILES
                    and os.path.exists(dest_file)
                ):
                    continue

                try:
                    shutil.copy2(src_file, dest_file)
                except:
                    pass

            draw_ui(
                status="Update completed!",
                task="All files installed",
                percent=100,
                spin="✓"
            )

            print()
            cp("UPDATE INSTALLED SUCCESSFULLY!", WHITE)
            print()

            return True

    except Exception as e:
        clear()
        print("[ERROR]", e)
        return False


if __name__ == "__main__":
    enable_ansi()

    if download_and_update():
        print()
        cp("You can now start the bot.", WHITE)
    else:
        print()
        cp("Update failed.", WHITE)

    print()
    input("Press Enter to exit...")