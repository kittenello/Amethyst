from __future__ import annotations

from copy import deepcopy

from utils import load_toml_as_dict, save_dict_as_toml


PERFORMANCE_PROFILES = {
    "balanced": {
        "description": "Best default for one emulator: smooth enough, but avoids overloading Brawl Stars.",
        "general_config": {
            "cpu_or_gpu": "auto",
            "max_ips": 24,
            "scrcpy_max_fps": 30,
            "scrcpy_max_width": 854,
            "scrcpy_bitrate": 1500000,
            "onnx_cpu_threads": 2,
            "used_threads": 2,
            "visual_debug": "no",
            "terminal_logging": "no",
            "capture_bad_vision_frames": "no",
            "web_runtime_update_interval": 1.0,
        },
        "bot_config": {
            "entity_detection_confidence": 0.55,
            "entity_detection_retry_confidence": 0.35,
        },
    },

    "multi_instance": {
        "description": "Best for running 2-4 LDPlayer windows: lower CPU/GPU and less emulator lag.",
        "general_config": {
            "cpu_or_gpu": "auto",
            "max_ips": 24,
            "scrcpy_max_fps": 24,
            "scrcpy_max_width": 854,
            "scrcpy_bitrate": 1500000,
            "onnx_cpu_threads": 1,
            "used_threads": 1,
            "visual_debug": "no",
            "terminal_logging": "no",
            "capture_bad_vision_frames": "no",
            "web_runtime_update_interval": 1.5,
        },
        "bot_config": {
            "entity_detection_confidence": 0.55,
            "entity_detection_retry_confidence": 0.35,
        },
    },
    "low_end": {
        "description": "Lower heat/CPU profile for older laptops or thermal throttling.",
        "general_config": {
            "cpu_or_gpu": "auto",
            "max_ips": 20,
            "scrcpy_max_fps": 24,
            "scrcpy_max_width": 854,
            "scrcpy_bitrate": 2000000,
            "onnx_cpu_threads": 2,
            "used_threads": 2,
        },
        "bot_config": {
            "entity_detection_confidence": 0.55,
            "entity_detection_retry_confidence": 0.35,
        },
    },
    "smooth_single": {
        "description": "For users who report emulator/Brawl Stars lag on one LDPlayer window.",
        "general_config": {
            "cpu_or_gpu": "auto",
            "max_ips": 20,
            "scrcpy_max_fps": 24,
            "scrcpy_max_width": 854,
            "scrcpy_bitrate": 1200000,
            "onnx_cpu_threads": 2,
            "used_threads": 2,
            "visual_debug": "no",
            "terminal_logging": "no",
            "capture_bad_vision_frames": "no",
            "web_runtime_update_interval": 1.5,
        },
        "bot_config": {
            "entity_detection_confidence": 0.55,
            "entity_detection_retry_confidence": 0.35,
        },
    },
    "quality": {
        "description": "Sharper capture for strong PCs; uncapped bot loop with a 60 FPS emulator feed.",
        "general_config": {
            "cpu_or_gpu": "auto",
            "max_ips": 0,
            "scrcpy_max_fps": 60,
            "scrcpy_max_width": 1280,
            "scrcpy_bitrate": 5000000,
            "onnx_cpu_threads": 4,
            "used_threads": 4,
        },
        "bot_config": {
            "entity_detection_confidence": 0.55,
            "entity_detection_retry_confidence": 0.35,
        },
    },
}


def apply_performance_profile(
    profile_name: str = "balanced",
    general_config_path: str = "cfg/general_config.toml",
    bot_config_path: str = "cfg/bot_config.toml",
    save: bool = True,
) -> dict:
    profile_key = str(profile_name or "balanced").strip().lower().replace("-", "_")
    if profile_key not in PERFORMANCE_PROFILES:
        available = ", ".join(sorted(PERFORMANCE_PROFILES))
        raise ValueError(f"Unknown performance profile '{profile_name}'. Available profiles: {available}")

    profile = PERFORMANCE_PROFILES[profile_key]
    general_config = deepcopy(load_toml_as_dict(general_config_path))
    bot_config = deepcopy(load_toml_as_dict(bot_config_path))

    general_config.update(profile["general_config"])
    bot_config.update(profile["bot_config"])

    if save:
        save_dict_as_toml(general_config, general_config_path)
        save_dict_as_toml(bot_config, bot_config_path)

    return {
        "profile": profile_key,
        "description": profile["description"],
        "general_config": general_config,
        "bot_config": bot_config,
        "changed_general_keys": sorted(profile["general_config"]),
        "changed_bot_keys": sorted(profile["bot_config"]),
    }


def get_performance_profile_summary(profile_name: str = "balanced") -> str:
    profile_key = str(profile_name or "balanced").strip().lower().replace("-", "_")
    profile = PERFORMANCE_PROFILES[profile_key]
    settings = []
    for key, value in profile["general_config"].items():
        settings.append(f"{key}={value}")
    for key, value in profile["bot_config"].items():
        settings.append(f"{key}={value}")
    return f"{profile_key}: {profile['description']} ({', '.join(settings)})"
