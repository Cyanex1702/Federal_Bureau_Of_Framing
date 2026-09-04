from __future__ import annotations

from copy import deepcopy
from typing import Callable, MutableMapping

from profile_config import CROP_MAX, CROP_MIN

EDITOR_TUNING_KEYS = (
    "body_mode", "anchor_mode", "scale_bias", "x_nudge", "y_nudge",
    "baseline_shift", "crop_left", "crop_right", "crop_top", "crop_bottom",
)


def validate_editor_tuning(value: dict | None) -> dict:
    value = value or {}
    body = value.get("body_mode", "auto")
    anchor = value.get("anchor_mode", "auto")
    if body not in {"auto", "main", "full", "multipart"}:
        raise ValueError("Invalid body mode")
    if anchor not in {"auto", "primary", "full_center", "visual_center"}:
        raise ValueError("Invalid anchor mode")

    result = {
        "body_mode": body,
        "anchor_mode": anchor,
        "scale_bias": float(value.get("scale_bias", 0.0)),
        "x_nudge": float(value.get("x_nudge", 0.0)),
        "y_nudge": float(value.get("y_nudge", 0.0)),
        "baseline_shift": float(value.get("baseline_shift", 0.0)),
    }
    if not (-0.45 <= result["scale_bias"] <= 0.60):
        raise ValueError("Scale adjustment is outside the supported range")
    if not (-0.30 <= result["x_nudge"] <= 0.30 and -0.30 <= result["y_nudge"] <= 0.30):
        raise ValueError("Position adjustment is outside the supported range")
    if not (-0.10 <= result["baseline_shift"] <= 0.10):
        raise ValueError("Baseline adjustment is outside the supported range")
    for key in ("crop_left", "crop_right", "crop_top", "crop_bottom"):
        crop = float(value.get(key, 0.0))
        if not (CROP_MIN <= crop <= CROP_MAX):
            raise ValueError(f"{key} is outside the supported crop range")
        result[key] = crop
    return result


def atomic_apply_override(
    overrides: MutableMapping[str, dict],
    product_key: str,
    tuning: dict,
    prepare: Callable[[dict], object],
    commit: Callable[[object], None] | None = None,
):
    """Validate/compute first; commit the override only after preparation succeeds."""
    validated = validate_editor_tuning(tuning)
    prepared = prepare(deepcopy(validated))
    if commit is not None:
        commit(prepared)
    overrides[product_key] = deepcopy(validated)
    return prepared


def geometry_reset_token(
    product_key: str,
    tuning: dict,
    *,
    category: str | None,
    canvas_size: int,
    segmentation_mode: str,
    normalization_mode: str,
    revision: int = 0,
) -> str:
    t = validate_editor_tuning(tuning)
    fields = [
        product_key,
        category or "",
        str(int(canvas_size)),
        segmentation_mode or "",
        normalization_mode or "",
        t["body_mode"], t["anchor_mode"],
        f"{t['scale_bias']:.6f}", f"{t['x_nudge']:.6f}", f"{t['y_nudge']:.6f}",
        f"{t['baseline_shift']:.6f}", f"{t['crop_left']:.6f}", f"{t['crop_right']:.6f}",
        f"{t['crop_top']:.6f}", f"{t['crop_bottom']:.6f}", str(int(revision)),
    ]
    return "|".join(fields)


def history_record(history_store: MutableMapping, product_key: str, tuning: dict, max_depth: int = 60) -> None:
    snapshot = validate_editor_tuning(tuning)
    entry = history_store.setdefault(product_key, {"states": [], "index": -1})
    states = entry.setdefault("states", [])
    index = int(entry.get("index", -1))
    if 0 <= index < len(states) and states[index] == snapshot:
        return
    if index < len(states) - 1:
        del states[index + 1:]
    states.append(deepcopy(snapshot))
    if len(states) > max_depth:
        del states[:-max_depth]
    entry["index"] = len(states) - 1


def history_step(history_store: MutableMapping, product_key: str, delta: int) -> dict | None:
    entry = history_store.get(product_key)
    if not entry:
        return None
    states = entry.get("states", [])
    target = int(entry.get("index", -1)) + int(delta)
    if not 0 <= target < len(states):
        return None
    entry["index"] = target
    return deepcopy(states[target])
