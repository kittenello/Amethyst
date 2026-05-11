#!/usr/bin/env python
"""Экспортировать обученную wall model в ONNX формат."""

import shutil
from pathlib import Path

try:
    from ultralytics import YOLO
except ImportError as exc:
    raise SystemExit("ultralytics не установлена") from exc

ROOT = Path(__file__).resolve().parent
runs_dir = ROOT / "runs" / "wall_train"

# Найти последний обученный проект
if not runs_dir.exists():
    raise SystemExit(f"Папка {runs_dir} не существует. Обучение не завершилось?")

# Поиск best.pt
best_pt_files = list(runs_dir.rglob("best.pt"))
if not best_pt_files:
    # Если нет best.pt, проверим last.pt
    last_pt_files = list(runs_dir.rglob("last.pt"))
    if not last_pt_files:
        raise SystemExit(f"Не найдено best.pt или last.pt в {runs_dir}")
    best_pt = last_pt_files[0]
    print(f"Найден last.pt: {best_pt}")
else:
    best_pt = best_pt_files[0]
    print(f"Найден best.pt: {best_pt}")

# Загрузить модель
print(f"Загружаем модель: {best_pt}")
model = YOLO(str(best_pt))

# Экспортировать в ONNX
print("Экспортируем в ONNX...")
exported = Path(model.export(format="onnx", imgsz=640, opset=12, simplify=True))
print(f"Экспортирована модель: {exported}")

# Заменить старую модель
target = ROOT / "models" / "tileDetector.onnx"
backup = target.with_suffix(".onnx.bak")

if target.exists() and not backup.exists():
    shutil.copy2(target, backup)
    print(f"Создана резервная копия: {backup}")

shutil.copy2(exported, target)
print(f"Новая модель установлена: {target}")
print("✓ Готово!")
