import json
import os
import platform
import warnings

import cv2
import numpy as np
import onnxruntime as ort

from utils import load_toml_as_dict

warnings.filterwarnings(
    "ignore",
    message=".*'pin_memory' argument is set as true but no accelerator is found.*",
    category=UserWarning,
)

debug = load_toml_as_dict("cfg/general_config.toml")["super_debug"] == "yes"


def get_optimal_threads(max_limit=4):
    general_config = load_toml_as_dict("cfg/general_config.toml")
    configured_threads = general_config.get("used_threads", general_config.get("onnx_cpu_threads", "auto"))
    if str(configured_threads).strip().lower() != "auto":
        try:
            threads_amount = max(1, int(configured_threads))
            print(f"Using configured ONNX CPU threads: {threads_amount}.")
            return threads_amount
        except (TypeError, ValueError):
            print(f"Ignoring invalid used_threads={configured_threads!r}; falling back to auto.")

    threads = os.cpu_count() or 2
    threads_amount = min(max(2, threads // 2), max_limit)
    print(f"Detected {threads} CPU threads, using {threads_amount} threads.")
    return threads_amount


_provider_message_printed = False
_optimal_threads_amount = None


def _get_cached_optimal_threads():
    global _optimal_threads_amount
    if _optimal_threads_amount is None:
        _optimal_threads_amount = get_optimal_threads()
    return _optimal_threads_amount


def _directml_provider():
    device_id = load_toml_as_dict("cfg/general_config.toml").get("directml_device_id", "auto")
    if str(device_id).strip().lower() in ("", "auto", "none"):
        return "DmlExecutionProvider"
    try:
        return ("DmlExecutionProvider", {"device_id": int(device_id)})
    except (TypeError, ValueError):
        print(f"Ignoring invalid directml_device_id={device_id!r}; using default DirectML adapter.")
        return "DmlExecutionProvider"


def _config_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _tensorrt_provider():
    general_config = load_toml_as_dict("cfg/general_config.toml")
    cache_dir = os.path.join("logs", "tensorrt_engine_cache")
    os.makedirs(cache_dir, exist_ok=True)
    options = {
        "trt_fp16_enable": _config_bool(general_config, "tensorrt_fp16", True),
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": cache_dir,
        "trt_timing_cache_enable": True,
        "trt_timing_cache_path": cache_dir,
    }

    device_id = general_config.get("tensorrt_device_id", general_config.get("cuda_device_id", "auto"))
    if str(device_id).strip().lower() not in ("", "auto", "none"):
        try:
            options["device_id"] = int(device_id)
        except (TypeError, ValueError):
            print(f"Ignoring invalid tensorrt_device_id={device_id!r}; using default TensorRT adapter.")

    workspace = general_config.get("tensorrt_workspace_size")
    if str(workspace).strip().lower() not in ("", "auto", "none"):
        try:
            options["trt_max_workspace_size"] = int(workspace)
        except (TypeError, ValueError):
            print(f"Ignoring invalid tensorrt_workspace_size={workspace!r}; using TensorRT default workspace.")

    return ("TensorrtExecutionProvider", options)


def _build_providers(preferred_device):
    global _provider_message_printed
    preferred_device = str(preferred_device or "auto").strip().lower()
    available_providers = set(ort.get_available_providers())
    providers = []

    if preferred_device in ("tensorrt", "trt"):
        if "TensorrtExecutionProvider" in available_providers:
            providers.append(_tensorrt_provider())
        else:
            print(
                "TensorRT was requested but TensorrtExecutionProvider is not available. "
                f"Available ONNX providers: {', '.join(ort.get_available_providers())}. Falling back to CUDA/CPU."
            )

    if preferred_device in ("gpu", "auto", "cuda", "tensorrt", "trt"):
        if "CUDAExecutionProvider" in available_providers:
            providers.append((
                "CUDAExecutionProvider",
                {
                    "cudnn_conv_algo_search": "EXHAUSTIVE",
                },
            ))

    if preferred_device in ("gpu", "auto", "directml", "dml"):
        if "DmlExecutionProvider" in available_providers and not providers:
            providers.append(_directml_provider())

    if preferred_device in ("gpu", "auto", "openvino"):
        if "OpenVINOExecutionProvider" in available_providers and not providers:
            providers.append("OpenVINOExecutionProvider")

    if preferred_device in ("gpu", "auto"):
        if "AzureExecutionProvider" in available_providers and not providers:
            providers.append("AzureExecutionProvider")

    providers.append("CPUExecutionProvider")
    if not _provider_message_printed:
        selected = providers[0][0] if isinstance(providers[0], tuple) else providers[0]
        if selected == "CPUExecutionProvider":
            print(
                f"Using CPU inference. Available ONNX providers: {', '.join(ort.get_available_providers())}. "
                f"Python={platform.python_version()} {platform.architecture()[0]}."
            )
        else:
            print(
                f"Using {selected} for ONNX inference with CPU fallback. "
                f"Available ONNX providers: {', '.join(ort.get_available_providers())}. "
                f"Python={platform.python_version()} {platform.architecture()[0]}."
            )
        _provider_message_printed = True
    return providers


def _provider_name(provider):
    return provider[0] if isinstance(provider, tuple) else provider


def format_onnx_backend(provider_name):
    provider_name = str(provider_name or "").strip()
    provider_labels = {
        "TensorrtExecutionProvider": "TensorrtExecutionProvider",
        "CUDAExecutionProvider": "CUDAExecutionProvider",
        "CPUExecutionProvider": "CPUExecutionProvider",
        "DmlExecutionProvider": "DirectML",
        "OpenVINOExecutionProvider": "OpenVINO",
    }
    return provider_labels.get(provider_name, "unknown")


def _preferred_profile_provider(provider_counts):
    for provider_name in (
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "DmlExecutionProvider",
            "OpenVINOExecutionProvider",
            "CPUExecutionProvider",
    ):
        if provider_counts.get(provider_name, 0) > 0:
            return provider_name
    return None


def _configure_session_options_for_provider(session_options, provider_name):
    if provider_name == "DmlExecutionProvider":
        session_options.enable_mem_pattern = False
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL


def _numpy_nms(boxes, scores, iou_threshold=0.6):
    if len(boxes) == 0:
        return np.array([], dtype=np.int32)

    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []

    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[np.where(iou <= iou_threshold)[0] + 1]

    return np.array(keep, dtype=np.int32)


def _postprocess_raw(raw_output, conf_thresh=0.6, iou_thresh=0.6):
    prediction = raw_output[0]

    if prediction.ndim == 3:
        prediction = prediction[0]
        if prediction.shape[0] < prediction.shape[1]:
            prediction = prediction.T

    if prediction.shape[1] <= 6:
        boxes_xyxy = prediction[:, :4]
        confidences = prediction[:, 4]
        class_ids = prediction[:, 5].astype(np.int32)
    else:
        boxes_cxcywh = prediction[:, :4]
        class_scores = prediction[:, 4:]
        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(prediction.shape[0]), class_ids]

        x1 = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        y1 = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        x2 = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        y2 = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
        boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

    mask = confidences >= conf_thresh
    if not np.any(mask):
        return []

    boxes_xyxy = boxes_xyxy[mask]
    confidences = confidences[mask]
    class_ids = class_ids[mask]

    # batched NMS via cv2 (C++ backend, ~3-5x faster than pure numpy loop)
    xywh = np.empty_like(boxes_xyxy)
    xywh[:, 0] = boxes_xyxy[:, 0]
    xywh[:, 1] = boxes_xyxy[:, 1]
    xywh[:, 2] = boxes_xyxy[:, 2] - boxes_xyxy[:, 0]
    xywh[:, 3] = boxes_xyxy[:, 3] - boxes_xyxy[:, 1]

    keep_idx = cv2.dnn.NMSBoxesBatched(
        xywh.tolist(),
        confidences.tolist(),
        class_ids.tolist(),
        score_threshold=conf_thresh,
        nms_threshold=iou_thresh,
    )

    if len(keep_idx) == 0:
        return []

    keep_idx = np.array(keep_idx, dtype=np.int32)
    kept_boxes   = boxes_xyxy[keep_idx]
    kept_scores  = confidences[keep_idx].reshape(-1, 1)
    kept_classes = class_ids[keep_idx].reshape(-1, 1).astype(np.float32)
    return [np.hstack([kept_boxes, kept_scores, kept_classes])]


class Detect:
    def __init__(self, model_path, ignore_classes=None, classes=None, input_size=(640, 640)):
        self.preferred_device = load_toml_as_dict("cfg/general_config.toml")["cpu_or_gpu"]
        self.model_path = model_path
        self.classes = classes
        self.ignore_classes = set(ignore_classes) if ignore_classes else set()
        self.input_size = input_size

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Model file not found: {model_path}")

        self.model, self.device = self.load_model()
        self.verified_device = self.device if self.device == "CPUExecutionProvider" else None
        self.profile_provider_counts = {}
        self._profile_checked = self.device == "CPUExecutionProvider"
        self.input_name = self.model.get_inputs()[0].name
        self.output_names = [output.name for output in self.model.get_outputs()]
        self._padded_img_buffer = np.full(
            (1, 3, self.input_size[0], self.input_size[1]),
            128.0 / 255.0,
            dtype=np.float32,
        )
        self._last_resized_w = 0
        self._last_resized_h = 0

    def load_model(self):
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = _build_providers(self.preferred_device)
        first_provider = _provider_name(providers[0])
        if first_provider != "CPUExecutionProvider":
            so.enable_profiling = True
            os.makedirs("logs", exist_ok=True)
            model_name = os.path.splitext(os.path.basename(self.model_path))[0]
            so.profile_file_prefix = os.path.join("logs", f"ort_profile_{model_name}")
        _configure_session_options_for_provider(so, first_provider)
        optimal_threads_amount = _get_cached_optimal_threads()
        if first_provider == "CPUExecutionProvider":
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            so.intra_op_num_threads = optimal_threads_amount
            so.inter_op_num_threads = max(1, min(2, optimal_threads_amount))
        else:
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
        model = ort.InferenceSession(self.model_path, sess_options=so, providers=providers)
        return model, model.get_providers()[0]

    def get_backend_provider(self):
        if self.verified_device:
            return self.verified_device
        if self.device == "CPUExecutionProvider":
            return self.device
        return None

    def _record_profiled_provider(self):
        if self._profile_checked:
            return
        self._profile_checked = True
        profile_path = None
        try:
            profile_path = self.model.end_profiling()
            if not profile_path or not os.path.exists(profile_path):
                print(
                    f"Could not verify ONNX execution provider for {os.path.basename(self.model_path)}: "
                    "ONNX Runtime did not produce a profile file."
                )
                return

            with open(profile_path, "r", encoding="utf-8") as profile_file:
                events = json.load(profile_file)

            provider_counts = {}
            for event in events:
                provider_name = event.get("args", {}).get("provider")
                if provider_name:
                    provider_counts[provider_name] = provider_counts.get(provider_name, 0) + 1

            self.profile_provider_counts = provider_counts
            self.verified_device = _preferred_profile_provider(provider_counts)
            if self.verified_device:
                counts = ", ".join(
                    f"{provider}={count}" for provider, count in sorted(provider_counts.items())
                )
                print(
                    f"Verified ONNX execution for {os.path.basename(self.model_path)}: "
                    f"{format_onnx_backend(self.verified_device)} ({counts})."
                )
            else:
                print(
                    f"Could not verify ONNX execution provider for {os.path.basename(self.model_path)} "
                    "from the runtime profile; reporting unknown until a provider is observed."
                )
        except Exception as e:
            self.verified_device = None
            print(
                f"Could not verify ONNX execution provider for {os.path.basename(self.model_path)}: {e}. "
                "Keeping ONNX backend as unknown instead of stopping the bot."
            )
        finally:
            if profile_path:
                try:
                    os.remove(profile_path)
                except OSError:
                    pass

    def preprocess_image(self, img):
        h, w = img.shape[:2]
        scale = min(self.input_size[0] / h, self.input_size[1] / w)
        new_w = int(w * scale)
        new_h = int(h * scale)

        if new_w != self._last_resized_w or new_h != self._last_resized_h:
            self._padded_img_buffer[:] = 128.0 / 255.0
            self._last_resized_w = new_w
            self._last_resized_h = new_h

        # INTER_AREA is faster and sharper when downscaling
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized_img = cv2.resize(img, (new_w, new_h), interpolation=interp)

        # single float32 conversion + one-shot CHW copy avoids 3 extra passes
        img_float = resized_img.astype(np.float32)
        img_float *= 1.0 / 255.0
        np.copyto(self._padded_img_buffer[0, :, :new_h, :new_w],
                  img_float.transpose(2, 0, 1))

        return self._padded_img_buffer, new_w, new_h

    def postprocess(self, raw_output, orig_img_shape, resized_shape, conf_tresh=0.6):
        detections = _postprocess_raw(raw_output, conf_thresh=conf_tresh, iou_thresh=0.6)

        orig_h, orig_w = orig_img_shape
        resized_w, resized_h = resized_shape
        scale_w = orig_w / resized_w
        scale_h = orig_h / resized_h

        results = []
        for detection in detections:
            if len(detection):
                detection[:, 0] *= scale_w
                detection[:, 1] *= scale_h
                detection[:, 2] *= scale_w
                detection[:, 3] *= scale_h
                results.append(detection)
        return results

    def detect_objects(self, img, conf_tresh=0.6):
        orig_h, orig_w = img.shape[:2]
        preprocessed_img, resized_w, resized_h = self.preprocess_image(img)
        outputs = self.model.run(self.output_names, {self.input_name: preprocessed_img})
        self._record_profiled_provider()
        detections = self.postprocess(outputs, (orig_h, orig_w), (resized_w, resized_h), conf_tresh)

        results = {}
        for detection in detections:
            for row in detection:
                x1, y1, x2, y2 = int(row[0]), int(row[1]), int(row[2]), int(row[3])
                class_id = int(row[5])
                class_name = self.classes[class_id]

                if class_id in self.ignore_classes or class_name in self.ignore_classes:
                    continue
                results.setdefault(class_name, []).append([x1, y1, x2, y2])

        return results
