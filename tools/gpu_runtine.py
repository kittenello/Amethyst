import subprocess

def install_gpu_packages():
    subprocess.check_call([
        "py", "-3.11-64", "-m", "pip", "install",
        "torch", "torchvision",
        "--index-url", "https://download.pytorch.org/whl/cu124"
    ])
    
    subprocess.check_call([
        "py", "-3.11-64", "-m", "pip", "install",
        "onnxruntime-gpu"
    ])

if __name__ == "__main__":
    install_gpu_packages()