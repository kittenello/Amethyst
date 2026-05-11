import sys
import platform
import subprocess
import os
import time

PURPLE = "\033[95m"
WHITE = "\033[97m"
GRAY = "\033[90m"
RESET = "\033[0m"

SPINNER = ["/", "-", "\\", "|"]

art = """
           __  __ ______ _______ _    ___     _______ _______  
     /\   |  \/  |  ____|__   __| |  | \ \   / / ____|__   __| 
    /  \  | \  / | |__     | |  | |__| |\ \_/ / (___    | |    
   / /\ \ | |\/| |  __|    | |  |  __  | \   / \___ \   | |    
  / ____ \| |  | | |____   | |  | |  | |  | |  ____) |  | |    
 /_/    \_\_|  |_|______|  |_|  |_|  |_|  |_| |_____/   |_|    
                                                               
                                                               
"""

amethyst = """
  .     '     ,
    _________
 _ /_|_____|_\ _
   '. \   / .'
     '.\ /.'
       '.'
"""

if platform.system() != "Windows" or "microsoft" in platform.uname()[3].lower():
    print("ERROR: This version of Amethyst is for WINDOWS ONLY.")
    print("Mac or Linux detected. Please use the Universal branch.")
    sys.exit(1)


def bootstrap():
    if os.environ.get("PYLAAI_BOOTSTRAP") == "1":
        return

    try:
        import setuptools
    except ImportError:
        print("\nDetected missing core tools. Stabilizing environment...")
        os.environ["PYLAAI_BOOTSTRAP"] = "1"
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel"
        ])
        subprocess.run([sys.executable] + sys.argv)
        sys.exit(0)


if any(cmd in sys.argv for cmd in ["install", "develop"]):
    bootstrap()

from setuptools import setup, find_packages


def clear():
    os.system("cls")


def enable_ansi():
    os.system("")

def center_text(text):
    try:
        width = os.get_terminal_size().columns
    except:
        width = 120

    lines = text.splitlines()
    centered = []

    for line in lines:
        centered.append(line.center(width))

    return "\n".join(centered)

def draw_ui(
    percent=0,
    step=0,
    total=1,
    gpu="Detecting...",
    setup_name="Auto",
    status="Starting...",
    current_task="Preparing...",
    spin="/"
):
    clear()

    try:
        width = os.get_terminal_size().columns
    except:
        width = 120

    box_width = 64
    inner = box_width - 2

    bar_len = 32
    filled = int(bar_len * percent / 100)
    bar = "#" * filled + "." * (bar_len - filled)

    def c(line=""):
        print(line.center(width))

    def cp(line="", color=RESET):
        print(color + line.center(width) + RESET)

    def row(text=""):
        text = text[:inner - 2]
        return "| " + text.ljust(inner - 2) + " |"

    cp("", PURPLE)

    for line in art.splitlines():
        cp(line, PURPLE)

    print()

    box = [
        "+" + "-" * inner + "+",
        row(f"Detected GPU:   {gpu}"),
        row(f"Selected Setup: {setup_name}"),
        row(f"Status:         {status}"),
        "+" + "-" * inner + "+",
        row(f"{percent:>3}%  {bar}  {step}/{total}"),
        "+" + "-" * inner + "+",
    ]

    for line in box:
        cp(line, PURPLE)

    print()
    cp(f"Installing: {current_task}", WHITE)
    print()
    cp(f"----- {spin} -----", PURPLE)
    print()
    cp("Please wait...", GRAY)


def run_command_silent(cmd, task_name, step, total, gpu, setup_name):
    stderr_lines = []

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    i = 0
    while process.poll() is None:
        percent = min(99, int(step / total * 100))
        draw_ui(
            percent=percent,
            step=step,
            total=total,
            gpu=gpu,
            setup_name=setup_name,
            status="Installing dependencies...",
            current_task=task_name,
            spin=SPINNER[i % len(SPINNER)]
        )
        i += 1
        time.sleep(0.15)

    if process.stderr:
        stderr_lines = process.stderr.read().splitlines()

    if process.returncode != 0:
        clear()
        print("[ERROR] Failed installing:", task_name)
        print()
        print("\n".join(stderr_lines[-40:]))
        raise RuntimeError(f"pip install failed: {task_name}")


def force_install(reqs, no_deps=False, task_name=None, step=1, total=1, gpu="Detecting...", setup_name="Auto"):
    task_name = task_name or " ".join(reqs)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-q"
    ]

    if no_deps:
        cmd += ["--force-reinstall", "--no-deps"]

    cmd += reqs

    run_command_silent(cmd, task_name, step, total, gpu, setup_name)


def remove_onnxruntime_variants(step, total, gpu, setup_name):
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "uninstall",
        "-y",
        "onnxruntime",
        "onnxruntime-gpu",
        "onnxruntime-directml",
        "onnxruntime-openvino"
    ]

    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False
    )


def install_onnxruntime_variant(req, step, total, gpu, setup_name):
    remove_onnxruntime_variants(step, total, gpu, setup_name)
    force_install(
        [req],
        task_name=req,
        step=step,
        total=total,
        gpu=gpu,
        setup_name=setup_name
    )


def get_gpu_data():
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,compute_cap",
                "--format=csv,noheader,nounits"
            ],
            encoding="utf-8",
            stderr=subprocess.DEVNULL
        ).strip()

        first_line = output.splitlines()[0]
        name, cc = first_line.split(", ")
        return "nvidia", float(cc), name

    except Exception:
        pass

    try:
        wmic = subprocess.check_output(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            encoding="utf-8",
            stderr=subprocess.DEVNULL
        )

        if "AMD" in wmic or "Radeon" in wmic:
            return "amd_windows", 0.0, "AMD Radeon"

        if "Intel" in wmic:
            return "intel", 0.0, "Intel HD/Arc Graphics"

    except Exception:
        pass

    return "cpu", 0.0, "Generic CPU"


def ask_user(prompt_text):
    if os.environ.get("PYLAAI_SETUP_AUTO", "").strip().lower() in ("1", "true", "yes"):
        return True

    print()
    response = input(prompt_text + " (Y/N): ").strip().lower()
    return response in ["y", "yes", "д", "да"]


def setup_pyla():
    enable_ansi()

    target, ver, name = get_gpu_data()

    total_steps = 7
    step = 1

    setup_name = "CPU Edition"

    draw_ui(
        percent=0,
        step=0,
        total=total_steps,
        gpu=name,
        setup_name=setup_name,
        status="Starting...",
        current_task="Preparing installer..."
    )

    time.sleep(0.5)

    force_install(
        ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"],
        task_name="PyTorch CPU",
        step=step,
        total=total_steps,
        gpu=name,
        setup_name=setup_name
    )
    step += 1

    base_reqs = [
        "customtkinter>=5.2.0",
        "toml>=0.10.2",
        "Pillow>=10.0.0",
        "discord.py>=2.3.2",
        "opencv-python==4.8.0.76",
        "requests",
        "ultralytics",
        "aiohttp",
        "easyocr",
        "google-play-scraper",
        "pyautogui>=0.9.54",
        "packaging>=23.0"
    ]

    force_install(
        base_reqs,
        task_name="Core dependencies",
        step=step,
        total=total_steps,
        gpu=name,
        setup_name=setup_name
    )
    step += 1

    if target == "nvidia":
        setup_name = "NVIDIA DirectML"

        if os.environ.get("PYLAAI_SETUP_AUTO", "").strip().lower() in ("1", "true", "yes"):
            install_onnxruntime_variant(
                "onnxruntime-directml",
                step,
                total_steps,
                name,
                setup_name
            )

        elif ask_user("Install NVIDIA CUDA acceleration?"):
            if ver >= 10.0:
                torch_cmd = [
                    "--pre",
                    "torch",
                    "torchvision",
                    "--index-url",
                    "https://download.pytorch.org/whl/nightly/cu128"
                ]
                setup_name = "CUDA 12.8 Blackwell"
            elif ver >= 8.9:
                torch_cmd = [
                    "torch",
                    "torchvision",
                    "--index-url",
                    "https://download.pytorch.org/whl/cu124"
                ]
                setup_name = "CUDA 12.4 Ada"
            else:
                torch_cmd = [
                    "torch",
                    "torchvision",
                    "--index-url",
                    "https://download.pytorch.org/whl/cu121"
                ]
                setup_name = "CUDA 12.1 Standard"

            force_install(
                torch_cmd,
                task_name="PyTorch CUDA",
                step=step,
                total=total_steps,
                gpu=name,
                setup_name=setup_name
            )

            install_onnxruntime_variant(
                "onnxruntime-gpu",
                step,
                total_steps,
                name,
                setup_name
            )

        elif ask_user("Install DirectML GPU acceleration instead?"):
            setup_name = "DirectML Recommended"
            install_onnxruntime_variant(
                "onnxruntime-directml",
                step,
                total_steps,
                name,
                setup_name
            )

        else:
            setup_name = "CPU ONNX Runtime"
            install_onnxruntime_variant(
                "onnxruntime",
                step,
                total_steps,
                name,
                setup_name
            )

    elif target == "intel":
        setup_name = "Intel GPU"

        if ask_user("Install DirectML GPU acceleration?"):
            setup_name = "Intel DirectML"
            install_onnxruntime_variant(
                "onnxruntime-directml",
                step,
                total_steps,
                name,
                setup_name
            )

        elif ask_user("Install Intel OpenVINO acceleration instead?"):
            setup_name = "Intel OpenVINO"
            install_onnxruntime_variant(
                "onnxruntime-openvino",
                step,
                total_steps,
                name,
                setup_name
            )

        else:
            setup_name = "CPU ONNX Runtime"
            install_onnxruntime_variant(
                "onnxruntime",
                step,
                total_steps,
                name,
                setup_name
            )

    elif "amd" in target:
        setup_name = "AMD GPU"

        if ask_user("Install AMD DirectML acceleration?"):
            setup_name = "AMD DirectML"
            install_onnxruntime_variant(
                "onnxruntime-directml",
                step,
                total_steps,
                name,
                setup_name
            )
        else:
            setup_name = "CPU ONNX Runtime"
            install_onnxruntime_variant(
                "onnxruntime",
                step,
                total_steps,
                name,
                setup_name
            )

    else:
        if ask_user("Install DirectML GPU acceleration?"):
            setup_name = "DirectML"
            install_onnxruntime_variant(
                "onnxruntime-directml",
                step,
                total_steps,
                name,
                setup_name
            )
        else:
            setup_name = "CPU ONNX Runtime"
            install_onnxruntime_variant(
                "onnxruntime",
                step,
                total_steps,
                name,
                setup_name
            )

    step += 1

    force_install(
        ["numpy<2.0.0"],
        no_deps=True,
        task_name="Repairing numpy",
        step=step,
        total=total_steps,
        gpu=name,
        setup_name=setup_name
    )
    step += 1

    force_install(
        ["adbutils==2.12.0", "av==12.3.0"],
        task_name="ADB / AV dependencies",
        step=step,
        total=total_steps,
        gpu=name,
        setup_name=setup_name
    )
    step += 1

    force_install(
        ["https://github.com/leng-yue/py-scrcpy-client/archive/refs/tags/v0.5.0.zip"],
        no_deps=True,
        task_name="py-scrcpy-client",
        step=step,
        total=total_steps,
        gpu=name,
        setup_name=setup_name
    )
    step += 1

    draw_ui(
        percent=100,
        step=total_steps,
        total=total_steps,
        gpu=name,
        setup_name=setup_name,
        status="Setup completed!",
        current_task="All dependencies installed",
        spin="✓"
    )

    run_bat = r"""@echo off
    cd /d %~dp0
    set OMP_NUM_THREADS=2
    set OPENBLAS_NUM_THREADS=2
    set MKL_NUM_THREADS=2
    set NUMEXPR_NUM_THREADS=2
    py -3.11-64 main.py
    pause
    """

    with open("run.bat", "w", encoding="utf-8") as f:
        f.write(run_bat)

    print()
    print(PURPLE + "=" * 50 + RESET)
    print(WHITE + "            SETUP COMPLETED!" + RESET)
    print(PURPLE + "=" * 50 + RESET)
    print(f"  GPU Detected: {name}")
    print(f"  Setup:        {setup_name}")
    print(PURPLE + "=" * 50 + RESET)
    print()
    print(f"  Start run.bat for start a bot")


if len(sys.argv) == 1:
    sys.argv.append("--pyla-install")


if "--pyla-install" in sys.argv:
    try:
        setup_pyla()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)

    sys.exit(0)


setup(
    name="Pyla-Biomistik",
    version="1.0.0",
    packages=find_packages(exclude=["api", "cfg", "models", "typization"]),
    install_requires=[]
)


if any(cmd in sys.argv for cmd in ["install", "develop"]):
    print(
        "\nWARNING: 'setup.py install' is deprecated. "
        "Run 'python setup.py --pyla-install' or setup.exe instead."
    )

    try:
        setup_pyla()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)