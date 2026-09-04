from __future__ import annotations

import io
from typing import Dict, Optional, Tuple, List

import numpy as np
from PIL import Image, ImageOps

from profile_config import (
    AMAZON_A_PROFILES,
    CATEGORY_ANCHOR_POLICY,
    CATEGORY_PROFILE,
    CATEGORY_STANDARDS,
    CROP_MAX,
    CROP_MIN,
    PROFILES,
)

try:
    import cv2
except Exception:
    cv2 = None

try:
    from rembg import remove as rembg_remove
    _REMBG_AVAILABLE = True
except Exception:
    rembg_remove = None
    _REMBG_AVAILABLE = False


def normalization_mode_key(value: Optional[str]) -> str:
    text = (value or "standard").strip().casefold()
    if text.startswith("amazon"):
        return "amazon_a"
    if text.startswith("strict"):
        return "strict"
    return "standard"


def profile_table_for_mode(normalization_mode: Optional[str]) -> Dict[str, dict]:
    if normalization_mode_key(normalization_mode) == "amazon_a":
        return AMAZON_A_PROFILES
    return PROFILES


def resolve_anchor_mode(
    category: Optional[str],
    subtype: str,
    accessory_area_ratio: float,
    requested_anchor_mode: str = "auto",
) -> str:
    """
    Return one of:
      primary       -> align the structural / primary product body
      full_center   -> center the complete visible foreground bbox
      visual_center -> center foreground visual mass / mask centroid

    Manual override always wins.

    V6.6 treats true multi-part product compositions as a whole scene. This is
    important for home-appliance bundles such as a juicer + jug or a floor-care
    product shown with several included attachments.
    """
    if requested_anchor_mode in {
        "primary",
        "full_center",
        "visual_center",
    }:
        return requested_anchor_mode

    # Multi-module/bundle compositions should be evaluated as one sold visual
    # composition rather than centering only the largest object.
    if subtype in {"multipart", "bundle_layout"}:
        return "full_center"

    # Detached minor props should not drag the main appliance around.
    if accessory_area_ratio >= 0.08 or subtype == "accessory_heavy":
        return "primary"

    policy = CATEGORY_ANCHOR_POLICY.get(category or "")
    if policy:
        return policy

    if subtype in {"flat_horizontal", "wide"}:
        return "full_center"

    return "primary"


def resolve_scale_basis(subtype: str) -> str:
    """
    primary -> scale from the main appliance body
    full    -> scale from the complete sold composition

    Multipart/bundle layouts are where older builds could make an already-good
    composition much too small by scaling only from the primary component.
    """
    if subtype in {"multipart", "bundle_layout"}:
        return "full"
    return "primary"


# ------------------------------------------------------------------
# Background / segmentation
# ------------------------------------------------------------------
def rembg_available() -> bool:
    return _REMBG_AVAILABLE


def _meaningful_alpha(img: Image.Image) -> bool:
    if img.mode != "RGBA":
        return False
    alpha = np.asarray(img.getchannel("A"))
    return np.any(alpha < 250)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    mask = (mask > 0).astype(np.uint8)
    if cv2 is None:
        return mask * 255

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num_labels <= 1:
        return mask * 255

    areas = stats[1:, cv2.CC_STAT_AREA]
    best_label = 1 + int(np.argmax(areas))
    return (labels == best_label).astype(np.uint8) * 255


def _cleanup_mask(mask: np.ndarray) -> np.ndarray:
    """
    Keep meaningful detached components.

    V5 kept detached foreground but still scaled using its full bounding box.
    V6 intentionally preserves those components because we later decide whether
    they are primary structure, a second module, or an accessory.
    """
    mask = (mask > 0).astype(np.uint8) * 255
    if cv2 is None:
        return mask

    h, w = mask.shape
    k = max(3, int(round(min(h, w) * 0.0035)))
    if k % 2 == 0:
        k += 1
    kernel = np.ones((k, k), np.uint8)

    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8
    )
    if num_labels <= 1:
        return mask

    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = max(1, int(np.max(areas)))
    keep = np.zeros_like(mask)

    # Low threshold keeps legitimate cups, milk tanks, handles and side modules.
    for label_idx in range(1, num_labels):
        area = int(stats[label_idx, cv2.CC_STAT_AREA])
        if area >= max(18, largest * 0.0015):
            keep[labels == label_idx] = 255

    return keep


def _border_background_color(rgb: np.ndarray) -> np.ndarray:
    h, w, _ = rgb.shape
    band = max(2, int(round(min(h, w) * 0.025)))
    samples = np.concatenate(
        [
            rgb[:band, :, :].reshape(-1, 3),
            rgb[-band:, :, :].reshape(-1, 3),
            rgb[:, :band, :].reshape(-1, 3),
            rgb[:, -band:, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    return np.median(samples, axis=0)


def simple_foreground_mask(image: Image.Image) -> np.ndarray:
    rgba = image.convert("RGBA")

    if _meaningful_alpha(rgba):
        alpha = np.asarray(rgba.getchannel("A"))
        mask = np.where(alpha > 12, 255, 0).astype(np.uint8)
        return _cleanup_mask(mask)

    rgb = np.asarray(rgba.convert("RGB")).astype(np.float32)
    bg = _border_background_color(rgb)
    distance = np.linalg.norm(rgb - bg[None, None, :], axis=2)

    border_distance = np.concatenate(
        [
            distance[0, :],
            distance[-1, :],
            distance[:, 0],
            distance[:, -1],
        ]
    )
    noise = float(np.percentile(border_distance, 95))
    threshold = max(18.0, min(55.0, noise * 2.5 + 12.0))

    mask = np.where(distance > threshold, 255, 0).astype(np.uint8)
    ratio = float(np.count_nonzero(mask)) / max(mask.size, 1)

    if ratio < 0.005:
        threshold = max(10.0, threshold * 0.55)
        mask = np.where(distance > threshold, 255, 0).astype(np.uint8)

    return _cleanup_mask(mask)


def ai_foreground_mask(image: Image.Image) -> np.ndarray:
    if not _REMBG_AVAILABLE:
        raise RuntimeError(
            "rembg is not installed. Install the optional AI dependencies or use Auto / white-background mode."
        )

    inp = io.BytesIO()
    image.convert("RGBA").save(inp, format="PNG")
    out = rembg_remove(inp.getvalue())
    rgba = Image.open(io.BytesIO(out)).convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"))
    return _cleanup_mask(np.where(alpha > 12, 255, 0).astype(np.uint8))


# ------------------------------------------------------------------
# Geometry helpers
# ------------------------------------------------------------------
def foreground_bbox(mask: np.ndarray) -> Tuple[int, int, int, int]:
    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        raise ValueError(
            "No foreground product was detected. Try a cleaner source image or AI background removal."
        )
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _mask_centroid(mask: np.ndarray) -> Tuple[float, float]:
    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        box = foreground_bbox(mask)
        return (
            (box[0] + box[2]) / 2.0,
            (box[1] + box[3]) / 2.0,
        )
    return float(xs.mean()), float(ys.mean())


def _bbox_union(boxes: List[Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
    if not boxes:
        raise ValueError("Cannot union an empty list of bounding boxes.")
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _bbox_distance(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(bx1 - ax2, ax1 - bx2, 0)
    dy = max(by1 - ay2, ay1 - by2, 0)
    return float((dx * dx + dy * dy) ** 0.5)


def component_regions(mask: np.ndarray) -> List[dict]:
    """
    Return meaningful connected components, largest first.

    Each component includes area, bbox, dimensions and normalized area share.
    """
    mask_u8 = (mask > 0).astype(np.uint8)
    full_area = max(1, int(np.count_nonzero(mask_u8)))

    if cv2 is None:
        box = foreground_bbox(mask)
        x1, y1, x2, y2 = box
        return [{
            "label": 1,
            "area": full_area,
            "area_share": 1.0,
            "bbox": box,
            "width": x2 - x1,
            "height": y2 - y1,
            "cx": (x1 + x2) / 2,
            "cy": (y1 + y2) / 2,
        }]

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask_u8, connectivity=8
    )
    if num_labels <= 1:
        return []

    raw = []
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = max(1, int(np.max(areas)))

    for i in range(1, num_labels):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < max(18, largest * 0.0015):
            continue
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        cx, cy = centroids[i]
        raw.append({
            "label": i,
            "area": area,
            "area_share": float(area / full_area),
            "bbox": (x, y, x + w, y + h),
            "width": w,
            "height": h,
            "cx": float(cx),
            "cy": float(cy),
        })

    return sorted(raw, key=lambda x: x["area"], reverse=True)


def infer_subtype(
    primary_bbox: Tuple[int, int, int, int],
    components: List[dict],
    structural_count: int,
    accessory_area_ratio: float,
) -> str:
    x1, y1, x2, y2 = primary_bbox
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    aspect = w / h

    if structural_count >= 2:
        return "multipart"
    if accessory_area_ratio >= 0.10 and len(components) >= 2:
        return "accessory_heavy"
    if aspect < 0.62:
        return "tall_standing"
    if aspect > 3.0:
        return "flat_horizontal"
    if aspect > 1.55:
        return "wide"
    if aspect < 0.82:
        return "portrait_boxy"
    return "boxy"


def analyze_components(mask: np.ndarray, body_mode: str = "auto") -> dict:
    """
    V6's key change.

    We keep TWO concepts:
      - full_bbox: all meaningful visible product/props, used for crop safety.
      - primary_bbox: the appliance body / structural group, used for scaling.

    body_mode:
      auto      -> smart component roles
      main      -> force the single largest component as primary
      full      -> use all foreground as the scaling body (V5-like behavior)
      multipart -> merge all major structural components
    """
    full_bbox = foreground_bbox(mask)
    components = component_regions(mask)

    if not components:
        components = [{
            "label": 1,
            "area": int(np.count_nonzero(mask)),
            "area_share": 1.0,
            "bbox": full_bbox,
            "width": full_bbox[2] - full_bbox[0],
            "height": full_bbox[3] - full_bbox[1],
            "cx": (full_bbox[0] + full_bbox[2]) / 2,
            "cy": (full_bbox[1] + full_bbox[3]) / 2,
        }]

    largest = components[0]
    largest_area = max(1, largest["area"])
    total_area = max(1, sum(c["area"] for c in components))

    if body_mode == "full":
        primary_components = components
        primary_bbox = full_bbox
    elif body_mode == "main":
        primary_components = [largest]
        primary_bbox = largest["bbox"]
    else:
        major = [c for c in components if c["area"] >= largest_area * 0.42]

        if body_mode == "multipart":
            structural = [c for c in components if c["area"] >= largest_area * 0.20]
            primary_components = structural or [largest]
            primary_bbox = _bbox_union([c["bbox"] for c in primary_components])
        elif len(major) >= 2:
            # Two similarly large components are much more likely to be a sold
            # multi-module product than a decorative cup/prop.
            primary_components = major
            primary_bbox = _bbox_union([c["bbox"] for c in major])
        else:
            primary_components = [largest]
            current_bbox = largest["bbox"]

            # Merge substantial components that are physically close to the
            # main body; this recovers separated hoppers/feet/structural parts.
            fx1, fy1, fx2, fy2 = full_bbox
            diag = max(1.0, ((fx2 - fx1) ** 2 + (fy2 - fy1) ** 2) ** 0.5)
            for comp in components[1:]:
                if comp["area"] < largest_area * 0.10:
                    continue
                if _bbox_distance(current_bbox, comp["bbox"]) <= diag * 0.035:
                    primary_components.append(comp)
                    current_bbox = _bbox_union([c["bbox"] for c in primary_components])
            primary_bbox = current_bbox

    primary_labels = {c["label"] for c in primary_components}
    primary_area = sum(c["area"] for c in primary_components)
    accessory_area = max(0, total_area - primary_area)
    accessory_area_ratio = float(accessory_area / total_area)
    structural_count = len(primary_components)

    subtype = infer_subtype(
        primary_bbox=primary_bbox,
        components=components,
        structural_count=structural_count,
        accessory_area_ratio=accessory_area_ratio,
    )

    # V6.6: detect deliberately laid-out accessory bundles.
    # Example: upright floor cleaner + phone + several heads/tools.
    # These should be centered/scaled as a composition, unlike a coffee cup
    # beside a coffee machine.
    p_x1, p_y1, p_x2, p_y2 = primary_bbox
    f_x1, f_y1, f_x2, f_y2 = full_bbox
    primary_w = max(1, p_x2 - p_x1)
    primary_h = max(1, p_y2 - p_y1)
    full_w = max(1, f_x2 - f_x1)
    full_h = max(1, f_y2 - f_y1)
    spread_x = full_w / primary_w
    spread_y = full_h / primary_h

    significant_secondaries = [
        c for c in components
        if c["label"] not in primary_labels
        and c["area"] >= largest_area * 0.035
    ]

    if (
        subtype == "accessory_heavy"
        and accessory_area_ratio >= 0.14
        and len(significant_secondaries) >= 2
        and (spread_x >= 1.50 or spread_y >= 1.45)
    ):
        subtype = "bundle_layout"

    # Create a primary-body mask for diagnostics and final metrics.
    if cv2 is not None:
        num_labels, labels, _, _ = cv2.connectedComponentsWithStats(
            (mask > 0).astype(np.uint8), connectivity=8
        )
        primary_mask = np.zeros_like(mask, dtype=np.uint8)
        for label in primary_labels:
            if 0 < label < num_labels:
                primary_mask[labels == label] = 255
    else:
        primary_mask = np.zeros_like(mask, dtype=np.uint8)
        x1, y1, x2, y2 = primary_bbox
        primary_mask[y1:y2, x1:x2] = mask[y1:y2, x1:x2]

    return {
        "full_bbox": full_bbox,
        "primary_bbox": primary_bbox,
        "components": components,
        "component_count": len(components),
        "primary_component_count": structural_count,
        "accessory_area_ratio": accessory_area_ratio,
        "composition_spread_x": float(spread_x),
        "composition_spread_y": float(spread_y),
        "significant_secondary_count": len(significant_secondaries),
        "subtype": subtype,
        "primary_mask": primary_mask,
        "body_mode": body_mode,
    }


def infer_profile_from_shape(width: int, height: int) -> str:
    aspect = width / max(height, 1)
    if aspect < 0.68:
        return "tall"
    if aspect < 1.30:
        return "boxy"
    if aspect < 3.10:
        return "wide"
    return "flat"


def resolve_profile(
    category: Optional[str],
    primary_bbox: Tuple[int, int, int, int],
    subtype: str,
    full_bbox: Optional[Tuple[int, int, int, int]] = None,
) -> str:
    """
    Use catalog category as a prior, then correct obvious geometric mismatches.

    Multi-part / bundle compositions resolve their shape from the complete sold
    composition, not only the largest component.
    """
    shape_bbox = (
        full_bbox
        if (
            full_bbox is not None
            and subtype in {"multipart", "bundle_layout"}
        )
        else primary_bbox
    )

    x1, y1, x2, y2 = shape_bbox
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    aspect_profile = infer_profile_from_shape(w, h)

    # V6.6.1: vacuum-cleaner catalog images need a stable visual ruler.
    # Robot-vacuum + dock compositions can have tiny aspect-ratio differences
    # because one source contains a larger white shadow or slightly different
    # crop. Letting those differences flip the resolved profile creates visibly
    # inconsistent output sizes even for the same product archetype.
    if (
        category == "Vacuum cleaner"
        and subtype in {
            "boxy",
            "portrait_boxy",
            "accessory_heavy",
            "multipart",
        }
    ):
        return "boxy"

    if subtype == "tall_standing":
        return "tall"
    if subtype == "flat_horizontal":
        return "flat"
    if subtype == "wide":
        return "wide"
    if subtype in {"multipart", "bundle_layout"}:
        return aspect_profile

    if category and category in CATEGORY_PROFILE:
        return CATEGORY_PROFILE[category]
    return aspect_profile


def _profile_for(
    profile_name: str,
    category: Optional[str],
    subtype: str,
    profile_overrides: Optional[Dict[str, float | str]],
    normalization_mode: str = "standard",
) -> dict:
    mode_key = normalization_mode_key(normalization_mode)
    source_table = profile_table_for_mode(mode_key)
    profile = dict(source_table[profile_name])

    # Amazon A profile targets are geometric, not category-specific.
    if mode_key != "amazon_a":
        cat = CATEGORY_STANDARDS.get(category or "")
        if cat:
            if "target_height" in profile and "target_height" in cat:
                profile.update(cat)
            elif "target_width" in profile and "target_width" in cat:
                profile.update(cat)

    if subtype == "accessory_heavy":
        if "target_height" in profile:
            profile["max_width"] = max(
                float(profile.get("max_width", 0.74)),
                0.84,
            )

    elif subtype in {"multipart", "bundle_layout"}:
        if mode_key == "amazon_a":
            # Full composition already controls scale; keep the family ruler.
            profile["alignment"] = "center"
        elif "target_height" in profile:
            profile["max_width"] = max(
                float(profile.get("max_width", 0.74)),
                0.86,
            )
            profile["target_height"] = 0.82
            profile["alignment"] = "center"
        else:
            profile["max_height"] = max(
                float(profile.get("max_height", 0.62)),
                0.80,
            )
            profile["target_width"] = 0.84
            profile["alignment"] = "center"

    elif subtype == "tall_standing":
        profile = dict(source_table["tall"])

    elif subtype == "flat_horizontal":
        profile = dict(source_table["flat"])

    elif subtype == "wide":
        profile = dict(source_table["wide"])

    if profile_overrides:
        profile.update(profile_overrides)

    return profile


def _resample_mask(mask_img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    return mask_img.resize(size, Image.Resampling.NEAREST)


# ------------------------------------------------------------------
# Main normalizer
# ------------------------------------------------------------------
def normalize_product_image(
    image: Image.Image,
    category: Optional[str] = None,
    canvas_size: int = 1000,
    segmentation_mode: str = "auto",
    min_padding: float = 0.06,
    background=(255, 255, 255),
    profile_overrides: Optional[Dict[str, float | str]] = None,
    adjustments: Optional[Dict[str, float]] = None,
    body_mode: str = "auto",
    anchor_mode: str = "auto",
    normalization_mode: str = "standard",
):
    image = ImageOps.exif_transpose(image).convert("RGBA")
    adjustments = adjustments or {}

    if segmentation_mode == "rembg":
        mask = ai_foreground_mask(image)
    else:
        mask = simple_foreground_mask(image)

    # --------------------------------------------------------------
    # V7.2 foreground trim / crop
    #
    # Crop values are percentages of the detected foreground envelope, not
    # arbitrary source-image whitespace. This makes the editor useful for
    # removing an unwanted prop / clipped label / damaged edge while keeping
    # the normalizer's standard square-canvas behavior.
    # --------------------------------------------------------------
    def _crop_value(name: str) -> float:
        value = float(adjustments.get(name, 0.0))
        if not (CROP_MIN <= value <= CROP_MAX):
            raise ValueError(
                f"{name} must be between {CROP_MIN:.0%} and {CROP_MAX:.0%}."
            )
        return value

    crop_left = _crop_value("crop_left")
    crop_right = _crop_value("crop_right")
    crop_top = _crop_value("crop_top")
    crop_bottom = _crop_value("crop_bottom")
    manual_crop_applied = any(
        value > 1e-6
        for value in (
            crop_left,
            crop_right,
            crop_top,
            crop_bottom,
        )
    )

    if manual_crop_applied:
        raw_x1, raw_y1, raw_x2, raw_y2 = foreground_bbox(mask)
        raw_w = max(1, raw_x2 - raw_x1)
        raw_h = max(1, raw_y2 - raw_y1)

        keep_x1 = raw_x1 + int(round(raw_w * crop_left))
        keep_x2 = raw_x2 - int(round(raw_w * crop_right))
        keep_y1 = raw_y1 + int(round(raw_h * crop_top))
        keep_y2 = raw_y2 - int(round(raw_h * crop_bottom))

        if keep_x2 - keep_x1 < 4 or keep_y2 - keep_y1 < 4:
            raise ValueError(
                "Manual crop removed too much of the detected product. "
                "Reduce the crop values or use Reset Crop."
            )

        cropped_mask = mask.copy()
        cropped_mask[:keep_y1, :] = 0
        cropped_mask[keep_y2:, :] = 0
        cropped_mask[:, :keep_x1] = 0
        cropped_mask[:, keep_x2:] = 0

        original_pixels = int(np.count_nonzero(mask))
        remaining_pixels = int(np.count_nonzero(cropped_mask))
        if remaining_pixels < max(20, int(original_pixels * 0.04)):
            raise ValueError(
                "Manual crop removed almost all detected foreground. "
                "Reduce the crop values."
            )

        mask = cropped_mask

    comp = analyze_components(mask, body_mode=body_mode)
    fx1, fy1, fx2, fy2 = comp["full_bbox"]
    px1, py1, px2, py2 = comp["primary_bbox"]

    full_w = max(1, fx2 - fx1)
    full_h = max(1, fy2 - fy1)
    body_w = max(1, px2 - px1)
    body_h = max(1, py2 - py1)

    scale_basis = resolve_scale_basis(comp["subtype"])
    scale_ref_w = full_w if scale_basis == "full" else body_w
    scale_ref_h = full_h if scale_basis == "full" else body_h

    profile_name = resolve_profile(
        category,
        comp["primary_bbox"],
        comp["subtype"],
        full_bbox=comp["full_bbox"],
    )
    mode_key = normalization_mode_key(
        normalization_mode
    )

    profile = _profile_for(
        profile_name=profile_name,
        category=category,
        subtype=comp["subtype"],
        profile_overrides=profile_overrides,
        normalization_mode=mode_key,
    )

    resolved_anchor_mode = resolve_anchor_mode(
        category=category,
        subtype=comp["subtype"],
        accessory_area_ratio=comp["accessory_area_ratio"],
        requested_anchor_mode=anchor_mode,
    )

    scale_bias = float(adjustments.get("scale_bias", 0.0))
    x_nudge = float(adjustments.get("x_nudge", 0.0))
    y_nudge = float(adjustments.get("y_nudge", 0.0))
    baseline_shift = float(adjustments.get("baseline_shift", 0.0))

    if not (-0.45 <= scale_bias <= 0.60):
        raise ValueError("Scale adjustment is outside the supported range.")
    if not (-0.30 <= x_nudge <= 0.30 and -0.30 <= y_nudge <= 0.30):
        raise ValueError("Position adjustment is outside the supported range.")
    if not (-0.10 <= baseline_shift <= 0.10):
        raise ValueError("Baseline adjustment is outside the supported range.")
    if body_mode not in {"auto", "main", "full", "multipart"}:
        raise ValueError("Unsupported body mode.")
    if anchor_mode not in {"auto", "primary", "full_center", "visual_center"}:
        raise ValueError("Unsupported anchor mode.")

    # Crop the *full* foreground extent so accessories/structural secondaries
    # are preserved. Scaling is based on primary body geometry.
    crop = image.crop((fx1, fy1, fx2, fy2))
    crop_mask = Image.fromarray(mask[fy1:fy2, fx1:fx2], mode="L")
    crop_body_mask = Image.fromarray(
        comp["primary_mask"][fy1:fy2, fx1:fx2], mode="L"
    )

    # Primary box relative to full crop.
    rpx1, rpy1 = px1 - fx1, py1 - fy1
    rpx2, rpy2 = px2 - fx1, py2 - fy1

    W = H = int(canvas_size)
    min_px = int(round(min_padding * W))
    safe_w = W - 2 * min_px
    safe_h = H - 2 * min_px

    # --------------------------------------------------------------
    # V6.1 PIXEL-LOCKED NORMALIZATION
    #
    # The V6 code positioned the crop using floating-point scaled
    # source coordinates and then clamped the full extent. Depending on
    # source dimensions, rounding could move otherwise identical products
    # by 1–2 output pixels. Worse, a detached accessory could trigger the
    # clamp and shift the primary appliance body off its canonical anchor.
    #
    # V6.1 fixes both:
    #   1) choose integer canonical targets,
    #   2) limit scale so the complete visible extent fits AROUND the
    #      canonical body anchor (instead of shifting it afterwards),
    #   3) resize the body mask,
    #   4) measure the ACTUAL resized body bbox,
    #   5) place that integer bbox exactly on the canonical pixel anchor.
    # --------------------------------------------------------------

    if "target_height" in profile:
        target_body_h_px = min(
            int(round(H * float(profile["target_height"]))),
            safe_h,
        )
        max_body_w_px = min(
            int(round(W * float(profile["max_width"]))),
            safe_w,
        )
        scale_h = target_body_h_px / scale_ref_h
        scale_w = max_body_w_px / scale_ref_w
        scale = min(scale_h, scale_w)
        controlling_axis = "height" if scale_h <= scale_w else "width"
        controlling_target_px = (
            target_body_h_px if controlling_axis == "height"
            else max_body_w_px
        )
    else:
        target_body_w_px = min(
            int(round(W * float(profile["target_width"]))),
            safe_w,
        )
        max_body_h_px = min(
            int(round(H * float(profile["max_height"]))),
            safe_h,
        )
        scale_w = target_body_w_px / scale_ref_w
        scale_h = max_body_h_px / scale_ref_h
        scale = min(scale_w, scale_h)
        controlling_axis = "width" if scale_w <= scale_h else "height"
        controlling_target_px = (
            target_body_w_px if controlling_axis == "width"
            else max_body_h_px
        )

    scale *= (1.0 + scale_bias)

    desired_center_x_px = int(round(W / 2 + x_nudge * W))

    # Full-envelope anchoring is always geometric centering. Primary anchoring
    # continues to use the product/profile baseline when appropriate.
    if resolved_anchor_mode == "full_center":
        desired_anchor_y_px = int(round(H / 2 + y_nudge * H))
        vertical_anchor_mode = "center"
        anchor_center_x_src = full_w / 2.0
        anchor_center_y_src = full_h / 2.0
    elif resolved_anchor_mode == "visual_center":
        desired_anchor_y_px = int(round(H / 2 + y_nudge * H))
        vertical_anchor_mode = "visual_center"
        anchor_center_x_src, anchor_center_y_src = _mask_centroid(
            np.asarray(crop_mask)
        )
    else:
        if profile.get("alignment") == "bottom_center":
            desired_anchor_y_px = int(round(
                H * (
                    float(profile.get("baseline", 0.89))
                    + baseline_shift
                    + y_nudge
                )
            ))
            vertical_anchor_mode = "baseline"
        else:
            desired_anchor_y_px = int(round(H / 2 + y_nudge * H))
            vertical_anchor_mode = "center"

        anchor_center_x_src = (rpx1 + rpx2) / 2.0
        anchor_center_y_src = (rpy1 + rpy2) / 2.0

    # Keep a one-pixel rounding reserve so exact placement never needs
    # to be corrected by a later clamp.
    safe_left = min_px + 1
    safe_top = min_px + 1
    safe_right = W - min_px - 1
    safe_bottom = H - min_px - 1

    anchor_scale_limits = [
        safe_w / full_w,
        safe_h / full_h,
    ]

    # Horizontal fit around whichever reference envelope controls alignment.
    if anchor_center_x_src > 0:
        anchor_scale_limits.append(
            (desired_center_x_px - safe_left) / anchor_center_x_src
        )
    right_extent_from_anchor = full_w - anchor_center_x_src
    if right_extent_from_anchor > 0:
        anchor_scale_limits.append(
            (safe_right - desired_center_x_px)
            / right_extent_from_anchor
        )

    # Vertical fit around the selected reference.
    if resolved_anchor_mode in {"full_center", "visual_center"}:
        if anchor_center_y_src > 0:
            anchor_scale_limits.append(
                (desired_anchor_y_px - safe_top) / anchor_center_y_src
            )
        below_anchor = full_h - anchor_center_y_src
        if below_anchor > 0:
            anchor_scale_limits.append(
                (safe_bottom - desired_anchor_y_px) / below_anchor
            )
    elif vertical_anchor_mode == "baseline":
        body_anchor_y_src = float(rpy2)
        if body_anchor_y_src > 0:
            anchor_scale_limits.append(
                (desired_anchor_y_px - safe_top) / body_anchor_y_src
            )
        below_body = full_h - body_anchor_y_src
        if below_body > 0:
            anchor_scale_limits.append(
                (safe_bottom - desired_anchor_y_px) / below_body
            )
    else:
        body_anchor_y_src = anchor_center_y_src
        if body_anchor_y_src > 0:
            anchor_scale_limits.append(
                (desired_anchor_y_px - safe_top) / body_anchor_y_src
            )
        below_body_center = full_h - body_anchor_y_src
        if below_body_center > 0:
            anchor_scale_limits.append(
                (safe_bottom - desired_anchor_y_px) / below_body_center
            )

    positive_limits = [v for v in anchor_scale_limits if v > 0]
    anchor_safe_scale = min(positive_limits) if positive_limits else scale
    scale = max(0.05, min(scale, anchor_safe_scale))

    def _resize_masks_at(scale_value: float):
        rw = max(1, int(round(full_w * scale_value)))
        rh = max(1, int(round(full_h * scale_value)))
        rcrop = crop.resize((rw, rh), Image.Resampling.LANCZOS)
        rmask = _resample_mask(crop_mask, (rw, rh))
        rbmask = _resample_mask(crop_body_mask, (rw, rh))
        return rw, rh, rcrop, rmask, rbmask

    # First resize.
    new_w, new_h, crop, crop_mask, crop_body_mask = _resize_masks_at(scale)

    # Snap the controlling scale-basis dimension to the SAME integer pixel
    # target across equivalent products.
    #
    # Earlier builds used two correction passes. That normally landed within
    # 1–2 px, but visually similar products (for example two ECOVACS robot
    # vacuums with stations) could still end up at 818 px vs 820 px because
    # mask-resampling is discrete.
    #
    # V6.6.1 does:
    #   1) iterative proportional correction,
    #   2) a tiny discrete neighborhood search,
    #   3) choose the candidate with the smallest integer pixel error.
    #
    # No stretching is used; only uniform scale changes are allowed.
    scale_basis_source_dimension = (
        scale_ref_h
        if controlling_axis == "height"
        else scale_ref_w
    )

    def _control_px_at_current_masks(
        current_mask: Image.Image,
        current_body_mask: Image.Image,
    ) -> int:
        local_scale_box = foreground_bbox(
            np.asarray(
                current_mask
                if scale_basis == "full"
                else current_body_mask
            )
        )
        sx1, sy1, sx2, sy2 = local_scale_box
        return int(
            (sy2 - sy1)
            if controlling_axis == "height"
            else (sx2 - sx1)
        )

    for _ in range(6):
        actual_control_px = _control_px_at_current_masks(
            crop_mask,
            crop_body_mask,
        )

        if actual_control_px <= 0:
            break

        if actual_control_px == controlling_target_px:
            break

        correction = controlling_target_px / actual_control_px
        candidate_scale = min(
            scale * correction,
            anchor_safe_scale,
        )

        if abs(candidate_scale - scale) < 1e-9:
            break

        scale = max(0.05, candidate_scale)
        new_w, new_h, crop, crop_mask, crop_body_mask = _resize_masks_at(
            scale
        )

    # Discrete pixel search around the best scale. The natural step for one
    # output pixel is about 1/source_dimension. Search a small neighborhood so
    # +/-1 px mask rounding does not survive.
    best_scale = scale
    best_payload = (
        new_w,
        new_h,
        crop,
        crop_mask,
        crop_body_mask,
    )
    best_control_px = _control_px_at_current_masks(
        crop_mask,
        crop_body_mask,
    )
    best_error = abs(
        best_control_px - controlling_target_px
    )

    natural_step = 1.0 / max(
        float(scale_basis_source_dimension),
        1.0,
    )

    for delta in range(-6, 7):
        if delta == 0:
            continue

        candidate_scale = scale + delta * natural_step

        if candidate_scale <= 0:
            continue

        candidate_scale = min(
            candidate_scale,
            anchor_safe_scale,
        )

        if candidate_scale <= 0:
            continue

        payload = _resize_masks_at(
            candidate_scale
        )
        _, _, _, candidate_mask, candidate_body_mask = payload
        candidate_px = _control_px_at_current_masks(
            candidate_mask,
            candidate_body_mask,
        )
        candidate_error = abs(
            candidate_px - controlling_target_px
        )

        if (
            candidate_error < best_error
            or (
                candidate_error == best_error
                and abs(candidate_scale - scale)
                < abs(best_scale - scale)
            )
        ):
            best_error = candidate_error
            best_scale = candidate_scale
            best_control_px = candidate_px
            best_payload = payload

        if best_error == 0:
            break

    scale = best_scale
    new_w, new_h, crop, crop_mask, crop_body_mask = best_payload
    size_lock_error_px = int(best_error)

    crop.putalpha(crop_mask)

    # IMPORTANT: place from the ACTUAL resized mask of the selected anchor
    # envelope. This is the V6.2 fix for facade-style appliances where the
    # detected primary body is not visually centered inside the complete outer
    # frame.
    local_body_box = foreground_bbox(np.asarray(crop_body_mask))
    lbx1, lby1, lbx2, lby2 = local_body_box

    if resolved_anchor_mode == "full_center":
        local_anchor_box = foreground_bbox(np.asarray(crop_mask))
    else:
        local_anchor_box = local_body_box

    lax1, lay1, lax2, lay2 = local_anchor_box

    if resolved_anchor_mode == "visual_center":
        local_visual_cx, local_visual_cy = _mask_centroid(
            np.asarray(crop_mask)
        )
        x = int(round(desired_center_x_px - local_visual_cx))
        y = int(round(desired_anchor_y_px - local_visual_cy))
    else:
        # Exact integer horizontal anchor-center lock.
        x = int(round(
            (2 * desired_center_x_px - (lax1 + lax2)) / 2.0
        ))

    if resolved_anchor_mode == "full_center":
        # Exact center of the complete visible appliance envelope.
        y = int(round(
            (2 * desired_anchor_y_px - (lay1 + lay2)) / 2.0
        ))
    elif resolved_anchor_mode == "visual_center":
        pass
    elif vertical_anchor_mode == "baseline":
        y = desired_anchor_y_px - lby2
    else:
        y = int(round(
            (2 * desired_anchor_y_px - (lby1 + lby2)) / 2.0
        ))

    placement_constrained = False

    # Anchor-safe scaling should make this unnecessary. Keep a defensive
    # fallback for pathological masks and record it in metrics instead of
    # silently shifting ordinary products.
    if (
        x < min_px
        or y < min_px
        or x + new_w > W - min_px
        or y + new_h > H - min_px
    ):
        placement_constrained = True
        x = min(max(x, min_px), W - min_px - new_w)
        y = min(max(y, min_px), H - min_px - new_h)

    canvas = Image.new("RGBA", (W, H), tuple(background) + (255,))
    canvas.alpha_composite(crop, (x, y))
    final_rgb = canvas.convert("RGB")

    final_mask = Image.new("L", (W, H), 0)
    final_mask.paste(crop_mask, (x, y))
    final_body_mask = Image.new("L", (W, H), 0)
    final_body_mask.paste(crop_body_mask, (x, y))

    fm = np.asarray(final_mask)
    bm = np.asarray(final_body_mask)

    fbx = foreground_bbox(fm)
    bbx = foreground_bbox(bm)
    bx1, by1, bx2, by2 = bbx
    gx1, gy1, gx2, gy2 = fbx

    body_height_occ = (by2 - by1) / H
    body_width_occ = (bx2 - bx1) / W
    full_height_occ = (gy2 - gy1) / H
    full_width_occ = (gx2 - gx1) / W
    body_center_x = ((bx1 + bx2) / 2) / W
    body_center_y = ((by1 + by2) / 2) / H

    body_center_x_px = (bx1 + bx2) / 2.0
    body_center_y_px = (by1 + by2) / 2.0
    full_center_x_px = (gx1 + gx2) / 2.0
    full_center_y_px = (gy1 + gy2) / 2.0

    if resolved_anchor_mode == "full_center":
        actual_anchor_x_px = full_center_x_px
        actual_anchor_y_px = full_center_y_px
    elif resolved_anchor_mode == "visual_center":
        actual_anchor_x_px, actual_anchor_y_px = _mask_centroid(fm)
    else:
        actual_anchor_x_px = body_center_x_px
        if vertical_anchor_mode == "baseline":
            actual_anchor_y_px = float(by2)
        else:
            actual_anchor_y_px = body_center_y_px

    anchor_error_x_px = abs(actual_anchor_x_px - desired_center_x_px)
    anchor_error_y_px = abs(actual_anchor_y_px - desired_anchor_y_px)

    full_edge_padding = min(gx1, W - gx2, gy1, H - gy2) / W
    cropped = gx1 <= 0 or gy1 <= 0 or gx2 >= W or gy2 >= H

    amazon_a_mode = mode_key == "amazon_a"
    amazon_a_compliant = bool(
        amazon_a_mode
        and size_lock_error_px == 0
        and anchor_error_x_px <= 0.5
        and anchor_error_y_px <= 0.5
        and not cropped
        and not placement_constrained
        and full_edge_padding >= min_padding * 0.72
    )

    warnings = []
    if manual_crop_applied:
        warnings.append("Manual foreground crop / trim applied")
    if cropped:
        warnings.append("Full product extent may be cropped")
    if abs(body_center_x - 0.5) > 0.035:
        warnings.append("Primary product body is not horizontally centered")
    if full_edge_padding < min_padding * 0.80:
        warnings.append("Full product safe padding is below target")
    if placement_constrained:
        warnings.append("Full visible extent constrained exact canonical placement")

    # Tighter QA focuses on the primary body.
    if "target_height" in profile:
        target = float(profile["target_height"])
        if body_height_occ > target + 0.055:
            warnings.append("Primary body looks too large vertically")
        if body_height_occ < min(0.50, target - 0.17):
            warnings.append("Primary body may look too zoomed out")
    else:
        target = float(profile["target_width"])
        if body_width_occ > target + 0.055:
            warnings.append("Primary body looks too large horizontally")
        if body_width_occ < min(0.56, target - 0.18):
            warnings.append("Primary body may look too zoomed out")

    metrics = {
        # Backward-compatible names now intentionally refer to primary body.
        "height_occupancy": float(body_height_occ),
        "width_occupancy": float(body_width_occ),
        "center_offset": float(abs(body_center_x - 0.5)),

        "body_height_occupancy": float(body_height_occ),
        "body_width_occupancy": float(body_width_occ),
        "full_height_occupancy": float(full_height_occ),
        "full_width_occupancy": float(full_width_occ),
        "body_center_x": float(body_center_x),
        "body_center_y": float(body_center_y),
        "body_baseline_y": float(by2 / H),
        "full_edge_padding": float(full_edge_padding),
        "anchor_error_x_px": float(anchor_error_x_px),
        "anchor_error_y_px": float(anchor_error_y_px),
        "pixel_lock_ok": bool(
            anchor_error_x_px <= 0.5
            and anchor_error_y_px <= 0.5
            and not placement_constrained
        ),
        "size_lock_target_px": int(controlling_target_px),
        "size_lock_actual_px": int(
            (gy2 - gy1)
            if scale_basis == "full" and controlling_axis == "height"
            else (gx2 - gx1)
            if scale_basis == "full" and controlling_axis == "width"
            else (by2 - by1)
            if controlling_axis == "height"
            else (bx2 - bx1)
        ),
        "size_lock_error_px": int(size_lock_error_px),
        "size_lock_ok": bool(
            size_lock_error_px == 0
            and not placement_constrained
        ),
        "normalization_mode": mode_key,
        "amazon_a_mode": bool(amazon_a_mode),
        "amazon_a_compliant": bool(amazon_a_compliant),
        "amazon_a_profile": profile_name if amazon_a_mode else "",
        "amazon_a_target_axis": profile.get("amazon_axis", "") if amazon_a_mode else "",
        "placement_constrained": bool(placement_constrained),
        "anchor_mode": resolved_anchor_mode,
        "manual_crop_applied": bool(manual_crop_applied),
        "crop_left": float(crop_left),
        "crop_right": float(crop_right),
        "crop_top": float(crop_top),
        "crop_bottom": float(crop_bottom),
        "visible_center_offset_x_px": float(abs(full_center_x_px - W / 2.0)),
        "visible_center_offset_y_px": float(abs(full_center_y_px - H / 2.0)),
        "body_center_offset_x_px": float(abs(body_center_x_px - W / 2.0)),
        "body_center_offset_y_px": float(abs(body_center_y_px - H / 2.0)),

        "accessory_area_ratio": float(comp["accessory_area_ratio"]),
        "component_count": int(comp["component_count"]),
        "primary_component_count": int(comp["primary_component_count"]),
        "cropped": bool(cropped),
        "qa_pass": len(warnings) == 0,
        "warnings": warnings,
    }

    return {
        "image": final_rgb,
        "mask": final_mask,
        "body_mask": final_body_mask,
        "profile": profile_name,
        "subtype": comp["subtype"],
        "normalization_mode": mode_key,
        "amazon_a_compliant": bool(amazon_a_compliant),
        "amazon_a_target_profile": dict(profile) if amazon_a_mode else None,
        "anchor_mode": resolved_anchor_mode,
        "scale_basis": scale_basis,
        "metrics": metrics,
        "source_bbox": comp["full_bbox"],
        "primary_bbox": comp["primary_bbox"],
        "component_analysis": {
            "component_count": comp["component_count"],
            "primary_component_count": comp["primary_component_count"],
            "accessory_area_ratio": comp["accessory_area_ratio"],
            "composition_spread_x": comp.get("composition_spread_x", 1.0),
            "composition_spread_y": comp.get("composition_spread_y", 1.0),
            "significant_secondary_count": comp.get("significant_secondary_count", 0),
            "scale_basis": scale_basis,
            "body_mode": body_mode,
        },
    }


def process_image_bytes(
    data: bytes,
    category: Optional[str] = None,
    canvas_size: int = 1000,
    segmentation_mode: str = "auto",
    min_padding: float = 0.06,
    profile_overrides: Optional[Dict[str, float | str]] = None,
    adjustments: Optional[Dict[str, float]] = None,
    body_mode: str = "auto",
    anchor_mode: str = "auto",
    normalization_mode: str = "standard",
):
    image = Image.open(io.BytesIO(data))
    return normalize_product_image(
        image=image,
        category=category,
        canvas_size=canvas_size,
        segmentation_mode=segmentation_mode,
        min_padding=min_padding,
        profile_overrides=profile_overrides,
        adjustments=adjustments,
        body_mode=body_mode,
        anchor_mode=anchor_mode,
        normalization_mode=normalization_mode,
    )


# ------------------------------------------------------------------
# Source-image analysis used by Detection Test.
# ------------------------------------------------------------------
def analyze_source_image(
    image: Image.Image,
    category: Optional[str] = None,
    segmentation_mode: str = "auto",
    body_mode: str = "auto",
):
    image = ImageOps.exif_transpose(image).convert("RGBA")

    if segmentation_mode == "rembg":
        mask = ai_foreground_mask(image)
    else:
        mask = simple_foreground_mask(image)

    comp = analyze_components(mask, body_mode=body_mode)
    fx1, fy1, fx2, fy2 = comp["full_bbox"]
    px1, py1, px2, py2 = comp["primary_bbox"]

    W, H = image.size
    body_w = max(1, px2 - px1)
    body_h = max(1, py2 - py1)
    full_w = max(1, fx2 - fx1)
    full_h = max(1, fy2 - fy1)

    profile_name = resolve_profile(
        category,
        comp["primary_bbox"],
        comp["subtype"],
        full_bbox=comp["full_bbox"],
    )

    body_center_x = ((px1 + px2) / 2) / max(W, 1)
    body_center_y = ((py1 + py2) / 2) / max(H, 1)

    left_padding = fx1 / max(W, 1)
    right_padding = (W - fx2) / max(W, 1)
    top_padding = fy1 / max(H, 1)
    bottom_padding = (H - fy2) / max(H, 1)

    body_height_occ = body_h / max(H, 1)
    body_width_occ = body_w / max(W, 1)
    full_height_occ = full_h / max(H, 1)
    full_width_occ = full_w / max(W, 1)
    area_ratio = float(np.count_nonzero(mask)) / max(mask.size, 1)

    full_centroid_px = _mask_centroid(mask)
    primary_centroid_px = _mask_centroid(comp["primary_mask"])
    scale_basis = resolve_scale_basis(comp["subtype"])

    segmentation_suspect = (
        (full_height_occ > 0.985 and full_width_occ > 0.985)
        or area_ratio > 0.93
        or area_ratio < 0.002
    )

    return {
        "profile": profile_name,
        "subtype": comp["subtype"],
        "mask": Image.fromarray(mask, mode="L"),
        "body_mask": Image.fromarray(comp["primary_mask"], mode="L"),
        "bbox": comp["full_bbox"],
        "primary_bbox": comp["primary_bbox"],
        "metrics": {
            "image_width": int(W),
            "image_height": int(H),

            # Detection comparisons now use primary body occupancy.
            "height_occupancy": float(body_height_occ),
            "width_occupancy": float(body_width_occ),
            "body_height_occupancy": float(body_height_occ),
            "body_width_occupancy": float(body_width_occ),
            "full_height_occupancy": float(full_height_occ),
            "full_width_occupancy": float(full_width_occ),

            "center_x": float(body_center_x),
            "center_y": float(body_center_y),
            "center_offset_x": float(abs(body_center_x - 0.5)),
            "center_offset_y": float(abs(body_center_y - 0.5)),

            # Full extent controls crop/edge safety.
            "left_padding": float(left_padding),
            "right_padding": float(right_padding),
            "top_padding": float(top_padding),
            "bottom_padding": float(bottom_padding),
            "min_edge_padding": float(
                min(left_padding, right_padding, top_padding, bottom_padding)
            ),

            # Alignment uses primary body.
            "baseline_y": float(py2 / max(H, 1)),
            "body_baseline_y": float(py2 / max(H, 1)),

            "foreground_area_ratio": float(area_ratio),
            "accessory_area_ratio": float(comp["accessory_area_ratio"]),
            "composition_spread_x": float(comp.get("composition_spread_x", 1.0)),
            "composition_spread_y": float(comp.get("composition_spread_y", 1.0)),
            "significant_secondary_count": int(comp.get("significant_secondary_count", 0)),
            "scale_basis": scale_basis,
            "component_count": int(comp["component_count"]),
            "primary_component_count": int(comp["primary_component_count"]),
            "segmentation_suspect": bool(segmentation_suspect),
        },
    }


def analyze_source_bytes(
    data: bytes,
    category: Optional[str] = None,
    segmentation_mode: str = "auto",
    body_mode: str = "auto",
):
    image = Image.open(io.BytesIO(data))
    return analyze_source_image(
        image=image,
        category=category,
        segmentation_mode=segmentation_mode,
        body_mode=body_mode,
    )
