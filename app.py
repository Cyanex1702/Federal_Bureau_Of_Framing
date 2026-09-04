import copy
import csv
import html
import io
import zipfile
import re
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps

from normalizer import (
    AMAZON_A_PROFILES,
    CATEGORY_PROFILE,
    PROFILES,
    normalize_product_image,
    process_image_bytes,
    rembg_available,
)
from page_detector import candidate_override_key, discover_pagination_urls, scan_page, scan_single_product
from product_transform_component import product_transform_editor
from editor_state import geometry_reset_token, validate_editor_tuning
from safe_io import sanitize_path_component
from exception_store import (
    ExceptionLibraryCorruptError,
    delete_exception,
    exception_identity,
    find_exception_suggestions,
    load_exception_library,
    save_exception,
)
from audit_store import (
    AUDIT_FIELDS,
    MASTER_AUDIT_PATH,
    build_audit_rows,
    clear_master_rows,
    advanced_csv_bytes,
    build_boss_excel_report,
    build_master_visual_package,
    clear_page_media,
    hierarchy_summary,
    load_master_rows,
    save_audit_media,
    separate_page_csv_zip,
    simple_csv_bytes,
    suggested_fix_from_issues,
    update_master_rows,
)



def product_override_key(item: dict) -> str:
    return candidate_override_key(item)


def selected_normalization_mode(label: str) -> str:
    if (label or "").startswith("Amazon"):
        return "amazon_a"
    if (label or "").startswith("Strict"):
        return "strict"
    return "standard"


def merged_adjustments(global_adjustments: dict | None, manual: dict | None) -> dict:
    global_adjustments = global_adjustments or {}
    manual = manual or {}
    return {
        "scale_bias": float(global_adjustments.get("scale_bias", 0.0))
            + float(manual.get("scale_bias", 0.0)),
        "x_nudge": float(global_adjustments.get("x_nudge", 0.0))
            + float(manual.get("x_nudge", 0.0)),
        "y_nudge": float(global_adjustments.get("y_nudge", 0.0))
            + float(manual.get("y_nudge", 0.0)),
        "baseline_shift": float(global_adjustments.get("baseline_shift", 0.0))
            + float(manual.get("baseline_shift", 0.0)),
        # Crop/trim is product-specific. It is intentionally not added to the
        # global fine-tune sliders.
        "crop_left": float(manual.get("crop_left", 0.0)),
        "crop_right": float(manual.get("crop_right", 0.0)),
        "crop_top": float(manual.get("crop_top", 0.0)),
        "crop_bottom": float(manual.get("crop_bottom", 0.0)),
    }


def manual_override_for(item: dict, manual_overrides: dict | None) -> dict:
    if not manual_overrides:
        return {}
    return manual_overrides.get(product_override_key(item), {})



EDITOR_TUNING_KEYS = (
    "body_mode",
    "anchor_mode",
    "scale_bias",
    "x_nudge",
    "y_nudge",
    "baseline_shift",
    "crop_left",
    "crop_right",
    "crop_top",
    "crop_bottom",
)


def canonical_editor_tuning(value: dict | None) -> dict:
    value = value or {}
    return {
        "body_mode": value.get("body_mode", "auto"),
        "anchor_mode": value.get("anchor_mode", "auto"),
        "scale_bias": round(float(value.get("scale_bias", 0.0)), 6),
        "x_nudge": round(float(value.get("x_nudge", 0.0)), 6),
        "y_nudge": round(float(value.get("y_nudge", 0.0)), 6),
        "baseline_shift": round(float(value.get("baseline_shift", 0.0)), 6),
        "crop_left": round(float(value.get("crop_left", 0.0)), 6),
        "crop_right": round(float(value.get("crop_right", 0.0)), 6),
        "crop_top": round(float(value.get("crop_top", 0.0)), 6),
        "crop_bottom": round(float(value.get("crop_bottom", 0.0)), 6),
    }


def record_editor_history(product_key: str, tuning: dict) -> None:
    history_store = st.session_state.setdefault(
        "editor_history",
        {},
    )
    entry = history_store.setdefault(
        product_key,
        {
            "states": [],
            "index": -1,
        },
    )
    snapshot = canonical_editor_tuning(
        tuning
    )
    states = entry["states"]
    index = int(entry.get("index", -1))

    if (
        0 <= index < len(states)
        and states[index] == snapshot
    ):
        return

    # A new edit after Undo starts a new branch and drops the old redo path.
    if index < len(states) - 1:
        del states[index + 1:]

    states.append(snapshot)
    if len(states) > 60:
        del states[:-60]
    entry["index"] = len(states) - 1


def editor_history_can_step(
    product_key: str,
    delta: int,
) -> bool:
    entry = st.session_state.get(
        "editor_history",
        {},
    ).get(product_key)
    if not entry:
        return False
    target = int(entry.get("index", -1)) + int(delta)
    return 0 <= target < len(entry.get("states", []))


def restore_editor_history(
    product_key: str,
    delta: int,
) -> bool:
    entry = st.session_state.get(
        "editor_history",
        {},
    ).get(product_key)
    if not entry:
        return False

    states = entry.get("states", [])
    target = int(entry.get("index", -1)) + int(delta)
    if not (0 <= target < len(states)):
        return False

    entry["index"] = target
    st.session_state.setdefault(
        "editor_draft_overrides",
        {},
    )[product_key] = dict(states[target])
    clear_editor_widget_state_for_product(
        product_key
    )
    revisions = st.session_state.setdefault(
        "editor_reset_revisions",
        {},
    )
    revisions[product_key] = int(
        revisions.get(product_key, 0)
    ) + 1
    return True


def render_showcase_scroll_guard(
    page_identity: str,
) -> None:
    """Keep the viewer on the same product when the Showcase fragment reruns."""
    token = str(
        abs(
            hash(
                page_identity
            )
        )
    )
    st.iframe(
        f"""
        <script>
        (() => {{
          const p = window.parent;
          const key = 'fbf_showcase_scroll_{token}';
          p.__fbfActiveShowcaseScrollKey = key;

          if (!p.__fbfShowcaseScrollListenerInstalled) {{
            p.__fbfShowcaseScrollListenerInstalled = true;
            p.addEventListener('scroll', () => {{
              const active = p.__fbfActiveShowcaseScrollKey;
              if (!active) return;
              try {{
                p.sessionStorage.setItem(active, String(p.scrollY || 0));
              }} catch (e) {{}}
            }}, {{passive: true}});
          }}

          let saved = null;
          try {{ saved = p.sessionStorage.getItem(key); }} catch (e) {{}}
          if (saved !== null) {{
            const y = Number(saved) || 0;
            [30, 100, 240, 500].forEach(delay => {{
              setTimeout(() => p.scrollTo(0, y), delay);
            }});
          }}
        }})();
        </script>
        """,
        width=1,
        height=1,
        tab_index=-1,
    )


def safe_product_name(name: str, fallback: str = "Unnamed product") -> str:
    """Shared cross-platform filename policy used by audit and export paths."""
    return sanitize_path_component(
        name,
        fallback,
        max_length=120,
    )


def unique_export_name(name: str, used: set[str]) -> str:
    base_name = safe_product_name(name)
    candidate = base_name
    i = 2
    while candidate.casefold() in used:
        candidate = f"{base_name}__{i}"
        i += 1
    used.add(candidate.casefold())
    return candidate


def image_to_bytes(image: Image.Image, output_format: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if output_format == "WEBP":
        image.convert("RGB").save(buf, format="WEBP", quality=95, method=6)
        return buf.getvalue(), ".webp"
    image.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), ".png"



@st.cache_data(
    show_spinner=False,
    max_entries=512,
)
def square_card_preview_bytes(
    data: bytes,
    size: int = 900,
) -> bytes:
    """
    Put every showcase image inside the same square preview frame.

    The source image is contained without stretching. This prevents a portrait,
    wide, transparent, or corrected image from changing the physical height of
    its Streamlit product card.
    """
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image).convert("RGBA")

    fitted = ImageOps.contain(
        image,
        (size, size),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new(
        "RGBA",
        (size, size),
        (255, 255, 255, 255),
    )
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    canvas.alpha_composite(fitted, (x, y))

    buf = io.BytesIO()
    canvas.convert("RGB").save(
        buf,
        format="PNG",
        optimize=False,
        compress_level=1,
    )
    return buf.getvalue()



def applied_edit_record_for(
    item_or_key,
) -> dict | None:
    """Return the latest explicitly-applied Product Editor result."""
    if isinstance(
        item_or_key,
        str,
    ):
        product_key = item_or_key
    else:
        product_key = product_override_key(
            item_or_key
        )

    return st.session_state.get(
        "applied_edit_registry",
        {},
    ).get(
        product_key
    )


def latest_fixed_bytes_for_item(
    item: dict,
) -> bytes:
    """
    Single source of truth for the edited/fixed image.

    A manually-applied Product Editor image wins over an older Showcase cache.
    This prevents stale cards after navigating away from the editor.
    """
    applied = applied_edit_record_for(
        item
    )

    if applied and applied.get(
        "image_bytes"
    ):
        return applied[
            "image_bytes"
        ]

    return (
        item.get(
            "showcase_bytes"
        )
        or item.get(
            "image_bytes",
            b"",
        )
    )


def store_applied_edit_record(
    product_key: str,
    normalized_result: dict,
    normalized_bytes: bytes,
    tuning: dict,
) -> None:
    st.session_state.setdefault(
        "applied_edit_registry",
        {},
    )[
        product_key
    ] = {
        "image_bytes": normalized_bytes,
        "metrics": normalized_result.get(
            "metrics"
        ),
        "subtype": normalized_result.get(
            "subtype"
        ),
        "tuning": canonical_editor_tuning(
            tuning
        ),
    }


def showcase_display_bytes(
    item: dict,
    showcase_mode: str = "Fixed page",
) -> bytes:
    """
    Resolve the image to show/export without rebuilding the showcase.
    """
    original = item.get("image_bytes", b"")
    fixed = latest_fixed_bytes_for_item(
        item
    ) or original
    changed = bool(item.get("changed"))
    decision = (
        item.get(
            "operator_decision"
        )
        or ""
    )

    if showcase_mode == "Current page":
        return original

    if decision in {
        "keep_original",
        "complete_rework",
    }:
        return original

    if decision == "use_fixed":
        return fixed

    if showcase_mode == "Changed only":
        return fixed if changed else original

    return fixed if changed else original



def automatic_issues_for_item(
    item: dict,
) -> list[str]:
    return list(
        item.get(
            "_automatic_issues",
            item.get(
                "issues",
                [],
            ),
        )
        or []
    )


def operator_decision_label(
    item: dict,
) -> str:
    decision = (
        item.get(
            "operator_decision"
        )
        or ""
    )

    return {
        "keep_original": "KEEP ORIGINAL",
        "use_fixed": "USE FIXED",
        "complete_rework": "COMPLETE REWORK",
    }.get(
        decision,
        "",
    )


def _snapshot_automatic_review_state(
    item: dict,
) -> None:
    if "_automatic_status" not in item:
        item[
            "_automatic_status"
        ] = item.get(
            "status",
            "pass",
        )

    if "_automatic_issues" not in item:
        item[
            "_automatic_issues"
        ] = list(
            item.get(
                "issues",
                [],
            )
            or []
        )


def _apply_operator_decision_to_item(
    item: dict,
    decision: str,
) -> None:
    _snapshot_automatic_review_state(
        item
    )

    if decision == "keep_original":
        item[
            "operator_decision"
        ] = "keep_original"
        item[
            "manual_review_note"
        ] = (
            "Operator approved the original website image "
            "and rejected the generated replacement."
        )
        item["status"] = "pass"
        item["issues"] = []
        item[
            "showcase_force_original"
        ] = True

    elif decision == "use_fixed":
        item[
            "operator_decision"
        ] = "use_fixed"
        item[
            "manual_review_note"
        ] = (
            "Operator approved the generated normalized replacement."
        )
        item["status"] = "pass"
        item["issues"] = []
        item[
            "showcase_force_original"
        ] = False

    elif decision == "complete_rework":
        item[
            "operator_decision"
        ] = "complete_rework"
        item[
            "manual_review_note"
        ] = (
            "Operator marked this source image as unusable. "
            "A complete image rework/replacement is required."
        )
        item["status"] = "review"
        item["issues"] = [
            "MANUAL REVIEW — complete image rework required."
        ]
        item[
            "showcase_force_original"
        ] = True

    elif decision == "reset":
        item["status"] = item.get(
            "_automatic_status",
            item.get(
                "status",
                "pass",
            ),
        )
        item["issues"] = list(
            item.get(
                "_automatic_issues",
                item.get(
                    "issues",
                    [],
                ),
            )
            or []
        )
        item.pop(
            "operator_decision",
            None,
        )
        item.pop(
            "manual_review_note",
            None,
        )
        item.pop(
            "showcase_force_original",
            None,
        )


def clear_generated_review_caches(
    preserve_showcase: bool = False,
) -> None:
    keys = [
        "page_export_zip",
        "page_export_summary",
        "advanced_image_review_zip",
        "advanced_image_review_summary",
        "showcase_download_zip",
        "advanced_simple_csv_snapshot",
        "advanced_page_csv_snapshot",
        "advanced_csv_snapshot_summary",
    ]

    if not preserve_showcase:
        current_key = st.session_state.get(
            "showcase_cache_key"
        )
        page_cache = st.session_state.get(
            "showcase_page_cache",
            {},
        )

        if current_key is not None:
            page_cache.pop(
                current_key,
                None,
            )

        keys.extend(
            [
                "showcase_cache_key",
                "showcase_items",
            ]
        )

    for key in keys:
        st.session_state.pop(
            key,
            None,
        )


def apply_operator_decision_everywhere(
    product_key: str,
    decision: str,
) -> int:
    """
    Update only the matching product records.

    Original/Edited is a presentation/export choice and does not require
    re-running normalization for the complete showcase page.
    """
    changed = 0
    seen_item_ids = set()

    scans = []

    detection = st.session_state.get(
        "detection_result"
    )
    if detection:
        scans.append(
            detection
        )

    for page in st.session_state.get(
        "advanced_audit_pages",
        [],
    ):
        scan = page.get(
            "scan"
        )
        if scan:
            scans.append(
                scan
            )

    for scan in scans:
        for item in scan.get(
            "items",
            [],
        ):
            if (
                product_override_key(
                    item
                )
                != product_key
            ):
                continue

            object_id = id(
                item
            )
            if object_id in seen_item_ids:
                continue

            seen_item_ids.add(
                object_id
            )
            _apply_operator_decision_to_item(
                item,
                decision,
            )
            changed += 1

    # Synchronize the already-built showcase copy in place.
    for item in st.session_state.get(
        "showcase_items",
        [],
    ):
        if (
            product_override_key(
                item
            )
            == product_key
        ):
            _apply_operator_decision_to_item(
                item,
                decision,
            )

    # Also synchronize cached showcase pages, so a decision made in Advanced
    # Audit remains visible when that page is reopened later.
    for cached_items in st.session_state.get(
        "showcase_page_cache",
        {},
    ).values():
        for item in cached_items:
            if (
                product_override_key(
                    item
                )
                == product_key
            ):
                _apply_operator_decision_to_item(
                    item,
                    decision,
                )

    # Only generated reports/packages become stale.
    clear_generated_review_caches(
        preserve_showcase=True,
    )
    return changed


def _set_showcase_flip(
    product_key: str,
    variant: str,
    preserve_scroll: bool = False,
) -> None:
    st.session_state.setdefault(
        "showcase_flip_states",
        {},
    )[
        product_key
    ] = variant
    if preserve_scroll:
        st.session_state["showcase_local_action"] = True


def _keep_original_from_showcase(
    product_key: str,
) -> None:
    st.session_state["showcase_local_action"] = True
    apply_operator_decision_everywhere(
        product_key,
        "keep_original",
    )
    st.session_state.setdefault(
        "showcase_flip_states",
        {},
    )[
        product_key
    ] = "original"


# Accidental click recovery for Showcase cards.
def _undo_keep_original_from_showcase(
    product_key: str,
) -> None:
    st.session_state["showcase_local_action"] = True
    apply_operator_decision_everywhere(
        product_key,
        "reset",
    )
    st.session_state.setdefault(
        "showcase_flip_states",
        {},
    )[
        product_key
    ] = "fixed"


def _mark_complete_rework(
    product_key: str,
) -> None:
    st.session_state["showcase_local_action"] = True
    apply_operator_decision_everywhere(
        product_key,
        "complete_rework",
    )
    st.session_state.setdefault(
        "showcase_flip_states",
        {},
    )[
        product_key
    ] = "original"


def _undo_complete_rework(
    product_key: str,
) -> None:
    st.session_state["showcase_local_action"] = True
    apply_operator_decision_everywhere(
        product_key,
        "reset",
    )
    st.session_state.setdefault(
        "showcase_flip_states",
        {},
    )[
        product_key
    ] = "fixed"


def _open_product_editor(
    product_key: str,
    *,
    as_stencil: bool = False,
) -> None:
    if as_stencil:
        st.session_state[
            "showcase_pending_stencil_key"
        ] = product_key
    else:
        st.session_state[
            "showcase_pending_tune_key"
        ] = product_key

    st.session_state[
        "showcase_editor_open"
    ] = True

    # Use the same pre-widget pending-state pattern as the tuner itself.
    # The next app run changes only the lightweight workspace router; it does
    # not execute Detection / Advanced Audit / Showcase first.
    st.session_state[
        "pending_fbf_workspace"
    ] = "🎛️ Product Editor"


def sync_applied_edit_to_showcase_cache(
    product_key: str,
    normalized_result: dict,
    normalized_bytes: bytes,
    body_mode: str = "auto",
    tuning: dict | None = None,
) -> int:
    """
    Patch only the currently prepared showcase product after Product Editor
    Apply. This avoids rebuilding the whole page and makes the edited image
    visible immediately when the operator returns to Page Showcase.
    """
    changed = 0
    seen = set()

    store_applied_edit_record(
        product_key,
        normalized_result,
        normalized_bytes,
        tuning or {},
    )

    collections = []
    current_items = st.session_state.get(
        "showcase_items",
        [],
    )
    if current_items:
        collections.append(current_items)

    page_cache = st.session_state.get(
        "showcase_page_cache",
        {},
    )
    # Patch every prepared page/configuration that contains the same product.
    # This prevents an older cached card from resurfacing when the operator
    # returns to another already-visited page.
    collections.extend(
        page_cache.values()
    )

    for items in collections:
        for item in items:
            if (
                product_override_key(item)
                != product_key
            ):
                continue

            object_id = id(item)
            if object_id in seen:
                continue
            seen.add(object_id)

            item["showcase_bytes"] = normalized_bytes
            item["changed"] = True
            item["normalization_metrics"] = normalized_result.get(
                "metrics"
            )
            item["normalization_error"] = None
            item["normalized_subtype"] = normalized_result.get(
                "subtype",
                item.get("subtype"),
            )
            item["body_mode"] = body_mode
            changed += 1

    # The cached showcase download is now stale, but the page image cache is
    # intentionally preserved because we patched it in place.
    st.session_state.pop(
        "showcase_download_zip",
        None,
    )
    st.session_state.pop(
        "showcase_download_signature",
        None,
    )
    return changed



def render_editor_normalized_result(
    *,
    selected_item: dict,
    current_tuning: dict,
    tuner_category: str | None,
    tuner_profile_overrides: dict,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    fine_tune: dict,
    normalization_mode: str,
) -> dict:
    """Normalize exactly the draft currently visible in Product Editor."""
    return process_image_bytes(
        selected_item[
            "image_bytes"
        ],
        category=tuner_category,
        canvas_size=canvas_size,
        segmentation_mode=segmentation_mode,
        min_padding=min_padding,
        profile_overrides=get_profile_override_for_item(
            selected_item,
            tuner_profile_overrides,
        ),
        adjustments=merged_adjustments(
            fine_tune,
            current_tuning,
        ),
        body_mode=current_tuning.get(
            "body_mode",
            "auto",
        ),
        anchor_mode=current_tuning.get(
            "anchor_mode",
            "auto",
        ),
        normalization_mode=normalization_mode,
    )


def _snapshot_product_apply_state(product_key: str) -> dict:
    """Capture the product-scoped state touched by Apply so commit can roll back."""
    item_snapshots = []
    seen = set()
    collections = []
    detection = st.session_state.get("detection_result")
    if detection:
        collections.append(detection.get("items", []))
    for page in st.session_state.get("advanced_audit_pages", []):
        scan = page.get("scan")
        if scan:
            collections.append(scan.get("items", []))
    collections.append(st.session_state.get("showcase_items", []))
    collections.extend(st.session_state.get("showcase_page_cache", {}).values())
    for items in collections:
        for item in items:
            if product_override_key(item) != product_key or id(item) in seen:
                continue
            seen.add(id(item))
            item_snapshots.append((item, copy.deepcopy(item)))

    map_keys = ("manual_overrides", "applied_edit_registry", "showcase_flip_states")
    map_snapshots = {}
    for map_key in map_keys:
        mapping = st.session_state.get(map_key, {})
        if product_key in mapping:
            map_snapshots[map_key] = (True, copy.deepcopy(mapping[product_key]))
        else:
            map_snapshots[map_key] = (False, None)

    cache_keys = (
        "page_export_zip", "page_export_summary", "advanced_image_review_zip",
        "advanced_image_review_summary", "showcase_download_zip",
        "advanced_simple_csv_snapshot", "advanced_page_csv_snapshot",
        "advanced_csv_snapshot_summary",
    )
    cache_snapshots = {
        key: (key in st.session_state, st.session_state.get(key))
        for key in cache_keys
    }
    return {
        "items": item_snapshots,
        "maps": map_snapshots,
        "caches": cache_snapshots,
    }


def _restore_product_apply_state(product_key: str, snapshot: dict) -> None:
    for item, previous in snapshot.get("items", []):
        item.clear()
        item.update(previous)
    for map_key, (existed, previous) in snapshot.get("maps", {}).items():
        mapping = st.session_state.setdefault(map_key, {})
        if existed:
            mapping[product_key] = previous
        else:
            mapping.pop(product_key, None)
    for key, (existed, previous) in snapshot.get("caches", {}).items():
        if existed:
            st.session_state[key] = previous
        else:
            st.session_state.pop(key, None)


def apply_editor_tuning_and_sync(
    *,
    selected_item: dict,
    override_key: str,
    current_tuning: dict,
    tuner_category: str | None,
    tuner_profile_overrides: dict,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    fine_tune: dict,
    normalization_mode: str,
) -> dict:

    """Atomically apply one Product Editor draft after normalization succeeds."""
    validated_tuning = validate_editor_tuning(current_tuning)

    # PREPARE PHASE — no persistent/session mutation is allowed above this line.
    normalized = render_editor_normalized_result(
        selected_item=selected_item,
        current_tuning=validated_tuning,
        tuner_category=tuner_category,
        tuner_profile_overrides=tuner_profile_overrides,
        segmentation_mode=segmentation_mode,
        canvas_size=canvas_size,
        min_padding=min_padding,
        fine_tune=fine_tune,
        normalization_mode=normalization_mode,
    )
    normalized_bytes, _ = image_to_bytes(
        normalized["image"],
        "PNG",
    )

    # COMMIT PHASE — snapshot all product-scoped state so an unexpected commit
    # error cannot leave a half-applied edit behind.
    snapshot = _snapshot_product_apply_state(override_key)
    try:
        sync_applied_edit_to_showcase_cache(
            override_key,
            normalized,
            normalized_bytes,
            body_mode=validated_tuning.get("body_mode", "auto"),
            tuning=validated_tuning,
        )
        apply_operator_decision_everywhere(
            override_key,
            "use_fixed",
        )
        _set_showcase_flip(
            override_key,
            "fixed",
        )
        st.session_state.setdefault(
            "manual_overrides",
            {},
        )[override_key] = dict(validated_tuning)
    except Exception:
        _restore_product_apply_state(override_key, snapshot)
        raise

    return normalized


def clear_editor_widget_state_for_product(
    product_key: str,
) -> None:
    token = str(
        abs(
            hash(
                product_key
            )
        )
    )

    prefixes = (
        f"v710_body_{token}",
        f"v710_anchor_{token}",
        f"v710_scale_{token}",
        f"v710_x_{token}",
        f"v710_y_{token}",
        f"v710_baseline_{token}",
        f"v710_overlay_{token}",
        f"v711_body_{token}",
        f"v711_anchor_{token}",
        f"v711_zoom_{token}",
        f"v711_x_{token}",
        f"v711_y_{token}",
        f"v711_baseline_{token}",
        f"v711_stencil_opacity_{token}",
        f"v714_body_{token}",
        f"v714_anchor_{token}",
        f"v714_baseline_{token}",
        f"v714_final_choice_{token}",
    )

    for key in list(
        st.session_state.keys()
    ):
        if key in prefixes:
            st.session_state.pop(
                key,
                None,
            )



def draggable_cutout_and_box(
    normalized_result: dict,
) -> tuple[bytes, dict]:
    """
    Build a transparent foreground cutout from a normalized result plus its
    normalized canvas bounding box.
    """
    image = normalized_result[
        "image"
    ].convert("RGBA")
    mask = normalized_result[
        "mask"
    ].convert("L")

    bbox = mask.getbbox()

    if not bbox:
        bbox = (
            0,
            0,
            image.width,
            image.height,
        )

    left, top, right, bottom = bbox
    cutout = image.crop(
        bbox
    )
    cutout_mask = mask.crop(
        bbox
    )
    cutout.putalpha(
        cutout_mask
    )

    buffer = io.BytesIO()
    cutout.save(
        buffer,
        format="PNG",
        optimize=True,
    )

    return (
        buffer.getvalue(),
        {
            "x": left
            / max(
                image.width,
                1,
            ),
            "y": top
            / max(
                image.height,
                1,
            ),
            "w": (
                right
                - left
            )
            / max(
                image.width,
                1,
            ),
            "h": (
                bottom
                - top
            )
            / max(
                image.height,
                1,
            ),
        },
    )


def tuning_after_drag_transform(
    *,
    current_tuning: dict,
    global_tuning: dict,
    base_box: dict,
    dragged_box: dict,
) -> dict:
    """
    Convert a browser drag/resize gesture back into the normalizer's manual
    scale and position variables.

    The browser box is a direct visual editor over the current normalized
    result. Corner resize is uniform, so width/height scale should agree.
    """
    base_w = max(
        float(
            base_box.get(
                "w",
                0.0,
            )
        ),
        1e-6,
    )
    base_h = max(
        float(
            base_box.get(
                "h",
                0.0,
            )
        ),
        1e-6,
    )

    scale_x = (
        float(
            dragged_box.get(
                "w",
                base_w,
            )
        )
        / base_w
    )
    scale_y = (
        float(
            dragged_box.get(
                "h",
                base_h,
            )
        )
        / base_h
    )

    scale_factor = max(
        0.25,
        min(
            4.0,
            (
                scale_x
                + scale_y
            )
            / 2.0,
        ),
    )

    base_center_x = (
        float(
            base_box.get(
                "x",
                0.0,
            )
        )
        + base_w
        / 2.0
    )
    base_center_y = (
        float(
            base_box.get(
                "y",
                0.0,
            )
        )
        + base_h
        / 2.0
    )

    new_center_x = (
        float(
            dragged_box.get(
                "x",
                base_box.get(
                    "x",
                    0.0,
                ),
            )
        )
        + float(
            dragged_box.get(
                "w",
                base_w,
            )
        )
        / 2.0
    )
    new_center_y = (
        float(
            dragged_box.get(
                "y",
                base_box.get(
                    "y",
                    0.0,
                ),
            )
        )
        + float(
            dragged_box.get(
                "h",
                base_h,
            )
        )
        / 2.0
    )

    manual_scale = float(
        current_tuning.get(
            "scale_bias",
            0.0,
        )
    )
    global_scale = float(
        global_tuning.get(
            "scale_bias",
            0.0,
        )
    )

    total_scale_before = (
        global_scale
        + manual_scale
    )

    total_scale_after = (
        (
            1.0
            + total_scale_before
        )
        * scale_factor
        - 1.0
    )

    new_manual_scale = (
        total_scale_after
        - global_scale
    )

    new_manual_x = (
        float(
            current_tuning.get(
                "x_nudge",
                0.0,
            )
        )
        + (
            new_center_x
            - base_center_x
        )
    )
    new_manual_y = (
        float(
            current_tuning.get(
                "y_nudge",
                0.0,
            )
        )
        + (
            new_center_y
            - base_center_y
        )
    )

    return {
        **current_tuning,
        "scale_bias": max(
            -0.45,
            min(
                0.60,
                new_manual_scale,
            ),
        ),
        "x_nudge": max(
            -0.30,
            min(
                0.30,
                new_manual_x,
            ),
        ),
        "y_nudge": max(
            -0.30,
            min(
                0.30,
                new_manual_y,
            ),
        ),
    }




def _find_current_showcase_item(
    product_key: str,
) -> dict | None:
    for item in st.session_state.get(
        "showcase_items",
        [],
    ):
        if (
            product_override_key(
                item
            )
            == product_key
        ):
            return item
    return None


def render_showcase_card_fragment(
    product_key: str,
    showcase_mode: str,
    show_details: bool,
) -> None:
    """
    Lightweight showcase card rendered inside the Page Showcase fragment.
    """
    item = _find_current_showcase_item(
        product_key
    )

    if not item:
        st.warning(
            "Card unavailable. Refresh the showcase."
        )
        return

    with st.container(
        border=True
    ):
        decision = (
            item.get(
                "operator_decision"
            )
            or ""
        )
        original_bytes = item[
            "image_bytes"
        ]
        fixed_bytes = (
            latest_fixed_bytes_for_item(
                item
            )
            or original_bytes
        )

        flip_states = (
            st.session_state.setdefault(
                "showcase_flip_states",
                {},
            )
        )

        if showcase_mode == "Current page":
            default_variant = "original"
        elif (
            showcase_mode
            == "Changed only"
            or item.get(
                "changed"
            )
        ):
            default_variant = (
                flip_states.get(
                    product_key
                )
            )

            if (
                default_variant
                not in {
                    "original",
                    "fixed",
                }
            ):
                default_variant = (
                    "original"
                    if decision
                    in {
                        "keep_original",
                        "complete_rework",
                    }
                    else "fixed"
                )
        else:
            default_variant = "original"

        display_bytes = (
            original_bytes
            if default_variant
            == "original"
            else fixed_bytes
        )
        badge = (
            "ORIGINAL"
            if default_variant
            == "original"
            else "FIXED"
        )

        st.image(
            square_card_preview_bytes(
                display_bytes
            ),
            use_container_width=True,
        )

        if (
            showcase_mode
            != "Current page"
            and item.get(
                "changed"
            )
        ):
            target_variant = (
                "fixed"
                if default_variant
                == "original"
                else "original"
            )

            st.button(
                (
                    "🔄 Flip to fixed"
                    if target_variant
                    == "fixed"
                    else "🔄 Flip to original"
                ),
                key=(
                    "showcase_flip_"
                    + str(
                        abs(
                            hash(
                                product_key
                            )
                        )
                    )
                ),
                on_click=_set_showcase_flip,
                args=(
                    product_key,
                    target_variant,
                    True,
                ),
                use_container_width=True,
            )

        render_showcase_title(
            item[
                "name"
            ]
        )

        if (
            showcase_mode
            != "Current page"
        ):
            render_showcase_status(
                changed=bool(
                    item.get(
                        "changed"
                    )
                ),
                error=item.get(
                    "normalization_error"
                ),
            )

        if show_details:
            st.caption(
                f"{badge} · {item['profile'].title()} / "
                f"{item.get('subtype','—').replace('_',' ').title()} · "
                f"score {item['consistency_score']:.0f}/100"
            )

            issues = automatic_issues_for_item(
                item
            )
            if issues:
                st.caption(
                    " · ".join(
                        issues
                    )
                )

            if item.get(
                "normalization_metrics"
            ):
                nm = item[
                    "normalization_metrics"
                ]
                st.caption(
                    "Pixel lock: "
                    + (
                        "exact"
                        if nm.get(
                            "pixel_lock_ok"
                        )
                        else (
                            "anchor drift "
                            f"{nm.get('anchor_error_y_px',0):.1f}px"
                        )
                    )
                )

            if item.get(
                "normalization_error"
            ):
                st.caption(
                    item[
                        "normalization_error"
                    ]
                )

        if decision == "keep_original":
            st.caption(
                "✅ Final image: **Original**"
            )
        elif decision == "use_fixed":
            st.caption(
                "✨ Final image: **Edited**"
            )
        elif decision == "complete_rework":
            st.error(
                "🛑 **COMPLETE REWORK REQUIRED** — do not use the original or generated fix."
            )

        if showcase_mode != "Current page":
            decision_left, decision_right = st.columns(
                2
            )

            with decision_left:
                if item.get("changed") and decision != "complete_rework":
                    is_kept_original = (
                        decision
                        == "keep_original"
                    )

                    st.button(
                        (
                            "↺ Undo keep original"
                            if is_kept_original
                            else "↩️ Keep original"
                        ),
                        key=(
                            "showcase_keep_original_"
                            + str(
                                abs(
                                    hash(
                                        product_key
                                    )
                                )
                            )
                        ),
                        on_click=(
                            _undo_keep_original_from_showcase
                            if is_kept_original
                            else _keep_original_from_showcase
                        ),
                        args=(
                            product_key,
                        ),
                        use_container_width=True,
                    )

            with decision_right:
                is_complete_rework = (
                    decision
                    == "complete_rework"
                )
                st.button(
                    (
                        "↺ Undo rework flag"
                        if is_complete_rework
                        else "🛑 Complete rework"
                    ),
                    key=(
                        "showcase_complete_rework_"
                        + str(
                            abs(
                                hash(
                                    product_key
                                )
                            )
                        )
                    ),
                    on_click=(
                        _undo_complete_rework
                        if is_complete_rework
                        else _mark_complete_rework
                    ),
                    args=(
                        product_key,
                    ),
                    help=(
                        "Use this when both the source image and generated fix are unusable "
                        "and the product needs a completely new/reworked image."
                    ),
                    use_container_width=True,
                )

        action_a, action_b = st.columns(
            2
        )

        with action_a:
            if st.button(
                "🎛️ Tune this product",
                key=(
                    "showcase_tune_"
                    + str(
                        abs(
                            hash(
                                product_key
                            )
                        )
                    )
                ),
                type="secondary",
                use_container_width=True,
            ):
                _open_product_editor(
                    product_key,
                    as_stencil=False,
                )
                st.rerun(
                    scope="app"
                )

        with action_b:
            if st.button(
                "🧷 Use as stencil",
                key=(
                    "showcase_stencil_"
                    + str(
                        abs(
                            hash(
                                product_key
                            )
                        )
                    )
                ),
                type="secondary",
                use_container_width=True,
            ):
                _open_product_editor(
                    product_key,
                    as_stencil=True,
                )
                st.rerun(
                    scope="app"
                )

        if item.get(
            "product_url"
        ):
            st.link_button(
                "Open product",
                item[
                    "product_url"
                ],
                use_container_width=True,
            )



def build_stencil_overlay_bytes(
    reference_bytes: bytes,
    target_bytes: bytes,
    opacity: float = 0.45,
    size: int = 900,
) -> bytes:
    """
    Blend a semi-transparent tuned preview over a reference preview so the
    operator can compare scale, padding and centering more precisely.
    """
    reference = Image.open(
        io.BytesIO(
            square_card_preview_bytes(reference_bytes, size=size)
        )
    ).convert("RGBA")

    target = Image.open(
        io.BytesIO(
            square_card_preview_bytes(target_bytes, size=size)
        )
    ).convert("RGBA")

    opacity = max(0.05, min(float(opacity), 0.95))
    overlay = Image.blend(reference, target, opacity)

    buf = io.BytesIO()
    overlay.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_showcase_download_zip(
    showcase_items: list[dict],
    showcase_mode: str = "Fixed page",
    changed_only: bool = False,
) -> bytes:
    """
    Download exactly what the showcase is showing as a simple ZIP.
    This is lighter/faster than the full page export package.
    """
    selected = [
        item
        for item in showcase_items
        if (not changed_only) or item.get("changed")
    ]

    zip_buffer = io.BytesIO()
    manifest_buffer = io.StringIO()
    writer = csv.writer(manifest_buffer)
    writer.writerow(
        [
            "product_name",
            "status_in_showcase",
            "profile",
            "subtype",
            "source_image_url",
            "product_url",
            "showcase_file",
        ]
    )

    used_names = set()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if not selected:
            zf.writestr(
                "README.txt",
                "No products matched the current showcase filter.",
            )

        for item in selected:
            export_name = unique_export_name(item["name"], used_names)
            image_bytes = showcase_display_bytes(
                item,
                showcase_mode=showcase_mode,
            )

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            buf = io.BytesIO()
            image.save(buf, format="PNG", optimize=True)

            rel = f"showcase_images/{export_name}.png"
            zf.writestr(rel, buf.getvalue())

            if item.get("normalization_error"):
                status = "replacement_failed"
            elif item.get("changed"):
                status = "corrected"
            else:
                status = "original_kept"

            writer.writerow(
                [
                    item["name"],
                    status,
                    item.get("profile", ""),
                    item.get("subtype", ""),
                    item.get("image_url", ""),
                    item.get("product_url", ""),
                    rel,
                ]
            )

        zf.writestr(
            "showcase_manifest.csv",
            manifest_buffer.getvalue(),
        )

    return zip_buffer.getvalue()




def render_showcase_title(name: str) -> None:
    safe = html.escape(name)
    st.markdown(
        (
            "<div title='"
            + safe.replace("'", "&#39;")
            + "' style='"
            "font-weight:700;"
            "line-height:1.35;"
            "height:4.15em;"
            "overflow:hidden;"
            "display:-webkit-box;"
            "-webkit-line-clamp:3;"
            "-webkit-box-orient:vertical;"
            "margin:0.35rem 0 0.55rem 0;"
            "'>"
            + safe
            + "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_showcase_status(
    changed: bool,
    error: str | None,
) -> None:
    if error:
        label = "⚠️ Could not replace"
        bg = "rgba(127,29,29,0.45)"
        fg = "#fecaca"
    elif changed:
        label = "✨ Corrected"
        bg = "rgba(20,83,45,0.65)"
        fg = "#86efac"
    else:
        label = "✓ Original kept"
        bg = "rgba(63,63,70,0.32)"
        fg = "#a1a1aa"

    st.markdown(
        (
            "<div style='"
            "height:3.35rem;"
            "box-sizing:border-box;"
            "display:flex;"
            "align-items:center;"
            "padding:0 0.85rem;"
            "border-radius:0.45rem;"
            f"background:{bg};"
            f"color:{fg};"
            "margin:0 0 0.75rem 0;"
            "'>"
            + label
            + "</div>"
        ),
        unsafe_allow_html=True,
    )


def normalization_category_for_item(
    item: dict,
    scan: dict,
    detection_category: str,
) -> str | None:
    if detection_category != "Auto by page / shape":
        return detection_category

    return (
        item.get("item_category")
        or scan.get("effective_category")
    )




def _filter_override_store() -> dict:
    return st.session_state.setdefault(
        "filter_manual_overrides",
        {},
    )


def _filter_override_page_key(scan: dict) -> str:
    return (
        scan.get("url")
        or scan.get("final_url")
        or "unknown-page"
    )


def get_filter_overrides_for_scan(
    scan: dict,
) -> dict:
    store = _filter_override_store()
    page_key = _filter_override_page_key(scan)

    return store.setdefault(
        page_key,
        {
            "include_keys": set(),
            "category_overrides": {},
            "labels": {},
        },
    )


def rerun_scan_with_filter_overrides(
    scan: dict,
    scan_config: dict,
) -> dict:
    overrides = get_filter_overrides_for_scan(
        scan
    )

    return scan_page(
        scan_config["url"],
        category=scan_config.get("category"),
        segmentation_mode=scan_config.get(
            "segmentation_mode",
            "auto",
        ),
        max_items=int(
            scan_config.get(
                "max_items",
                40,
            )
        ),
        scale_tolerance=float(
            scan_config.get(
                "scale_tolerance",
                0.18,
            )
        ),
        alignment_tolerance=float(
            scan_config.get(
                "alignment_tolerance",
                0.055,
            )
        ),
        forced_include_keys=set(
            overrides["include_keys"]
        ),
        item_category_overrides=dict(
            overrides["category_overrides"]
        ),
        normalization_mode=scan_config.get(
            "normalization_mode",
            "amazon_a",
        ),
    )


def filtered_items_csv_bytes(items: list[dict]) -> bytes:
    fields = [
        "product_name",
        "expected_category",
        "detected_category",
        "filter_confidence",
        "detected_category_score",
        "matched_evidence",
        "filter_reason",
        "product_url",
        "image_url",
        "source",
        "page_membership_method",
    ]

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=fields,
    )
    writer.writeheader()

    for item in items:
        writer.writerow(
            {
                "product_name": item.get(
                    "name",
                    "Unnamed product",
                ),
                "expected_category": item.get(
                    "expected_category",
                    "",
                ),
                "detected_category": item.get(
                    "detected_category",
                    "",
                ),
                "filter_confidence": item.get(
                    "filter_confidence",
                    "",
                ),
                "detected_category_score": item.get(
                    "detected_category_score",
                    "",
                ),
                "matched_evidence": ", ".join(
                    item.get(
                        "detected_category_evidence",
                        [],
                    )
                ),
                "filter_reason": item.get(
                    "filter_reason",
                    "",
                ),
                "product_url": item.get(
                    "product_url",
                    "",
                ),
                "image_url": item.get(
                    "image_url",
                    "",
                ),
                "source": item.get(
                    "source",
                    "",
                ),
                "page_membership_method": item.get(
                    "page_membership_method",
                    "",
                ),
            }
        )

    return buf.getvalue().encode("utf-8-sig")


def render_filtered_product_inspector(
    scan: dict,
    key_prefix: str,
    expanded: bool = False,
    max_items: int = 100,
    scan_config: dict | None = None,
    result_target: str | None = None,
    advanced_page_index: int | None = None,
) -> None:
    excluded = scan.get("excluded_items") or []
    overrides = get_filter_overrides_for_scan(
        scan
    )

    # ----------------------------------------------------------
    # Prominent actions OUTSIDE the details expander.
    # ----------------------------------------------------------
    top1, top2, top3 = st.columns(
        [1.35, 1.35, 1]
    )

    with top1:
        if excluded:
            st.download_button(
                (
                    "⬇️ Filtered Products CSV "
                    f"({len(excluded)})"
                ),
                data=filtered_items_csv_bytes(
                    excluded
                ),
                file_name=(
                    f"{key_prefix}_filtered_products.csv"
                ),
                mime="text/csv",
                key=(
                    f"{key_prefix}_filtered_csv_top"
                ),
                help=(
                    "Separate CSV containing every product "
                    "that the category gate skipped and the reason."
                ),
            )
        else:
            st.button(
                "⬇️ Filtered Products CSV (0)",
                disabled=True,
                key=(
                    f"{key_prefix}_filtered_csv_empty"
                ),
            )

    with top2:
        st.caption(
            (
                f"🧹 {len(excluded)} currently filtered"
                if excluded
                else "✅ No currently filtered products"
            )
        )

    with top3:
        if overrides["include_keys"]:
            if st.button(
                "↩️ Reset manual includes",
                key=(
                    f"{key_prefix}_reset_manual_filters"
                ),
                help=(
                    "Remove manual include/category overrides "
                    "for this page and rerun it."
                ),
            ):
                overrides["include_keys"].clear()
                overrides["category_overrides"].clear()
                overrides["labels"].clear()

                if scan_config:
                    with st.spinner(
                        "Restoring automatic category filtering..."
                    ):
                        refreshed = rerun_scan_with_filter_overrides(
                            scan,
                            scan_config,
                        )

                    if (
                        result_target
                        == "detection"
                    ):
                        st.session_state[
                            "detection_result"
                        ] = refreshed
                        st.session_state.pop(
                            "showcase_items",
                            None,
                        )
                        st.session_state.pop(
                            "showcase_cache_key",
                            None,
                        )
                    elif (
                        result_target
                        == "advanced"
                        and advanced_page_index
                        is not None
                    ):
                        pages = st.session_state.get(
                            "advanced_audit_pages",
                            [],
                        )
                        if (
                            0
                            <= advanced_page_index
                            < len(pages)
                        ):
                            pages[
                                advanced_page_index
                            ]["scan"] = refreshed
                            st.session_state[
                                "advanced_audit_pages"
                            ] = pages

                    st.rerun()

    if overrides["include_keys"]:
        with st.expander(
            (
                "✏️ Active manual filter overrides "
                f"({len(overrides['include_keys'])})"
            ),
            expanded=False,
        ):
            for override_key in sorted(
                overrides["include_keys"]
            ):
                label = overrides[
                    "labels"
                ].get(
                    override_key,
                    override_key,
                )
                category_override = overrides[
                    "category_overrides"
                ].get(
                    override_key,
                    "Use automatic/page category",
                )

                st.write(
                    f"**{label}** → included manually · "
                    f"category: `{category_override}`"
                )

    if not excluded:
        return

    counts_by_category = {}
    counts_by_confidence = {}

    for item in excluded:
        detected = item.get(
            "detected_category",
            "Unknown",
        )
        confidence = item.get(
            "filter_confidence",
            "unknown",
        )

        counts_by_category[detected] = (
            counts_by_category.get(
                detected,
                0,
            )
            + 1
        )
        counts_by_confidence[confidence] = (
            counts_by_confidence.get(
                confidence,
                0,
            )
            + 1
        )

    with st.expander(
        (
            "🧹 Review / Edit Filtered Products — "
            f"{len(excluded)} skipped"
        ),
        expanded=expanded,
    ):
        st.write(
            "These products were scouted but removed before "
            "image-quality analysis. You can now **override the "
            "filter and include a product directly from here**."
        )

        expected_category = (
            scan.get("effective_category")
            or excluded[0].get(
                "expected_category",
                "Unknown",
            )
        )

        s1, s2, s3 = st.columns(3)
        s1.metric(
            "Audit category gate",
            expected_category,
        )
        s2.metric(
            "Filtered products",
            len(excluded),
        )
        s3.metric(
            "High-confidence filters",
            counts_by_confidence.get(
                "high",
                0,
            ),
        )

        if counts_by_category:
            st.markdown(
                "**What categories were filtered out?**"
            )
            st.dataframe(
                [
                    {
                        "Detected category": category,
                        "Skipped products": count,
                    }
                    for category, count in sorted(
                        counts_by_category.items(),
                        key=lambda kv: (
                            -kv[1],
                            kv[0],
                        ),
                    )
                ],
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "⬇️ Download detailed filtered CSV",
            data=filtered_items_csv_bytes(
                excluded
            ),
            file_name=(
                f"{key_prefix}_filtered_products_detailed.csv"
            ),
            mime="text/csv",
            key=f"{key_prefix}_filtered_csv_inside",
        )

        st.markdown(
            "### Skipped product details"
        )

        manual_category_options = [
            "Use page / automatic category"
        ] + [
            category
            for category in categories
            if category
            != "Auto by page / shape"
        ]

        for index, item in enumerate(
            excluded[:max_items],
            start=1,
        ):
            item_key = candidate_override_key(
                item
            )

            with st.container(border=True):
                image_col, detail_col = st.columns(
                    [1, 2.5]
                )

                with image_col:
                    image_url = item.get(
                        "image_url"
                    )

                    if image_url:
                        try:
                            st.image(
                                image_url,
                                use_container_width=True,
                            )
                        except Exception:
                            st.caption(
                                "Preview unavailable"
                            )
                    else:
                        st.caption(
                            "No image URL"
                        )

                with detail_col:
                    st.markdown(
                        f"**{index}. "
                        f"{item.get('name','Unnamed product')}**"
                    )

                    r1, r2, r3 = st.columns(3)
                    r1.metric(
                        "Expected",
                        item.get(
                            "expected_category",
                            expected_category,
                        ),
                    )
                    r2.metric(
                        "Detected",
                        item.get(
                            "detected_category",
                            "Unknown",
                        ),
                    )
                    r3.metric(
                        "Filter confidence",
                        str(
                            item.get(
                                "filter_confidence",
                                "unknown",
                            )
                        ).title(),
                    )

                    st.warning(
                        item.get(
                            "filter_reason",
                            "Off-category product",
                        )
                    )

                    evidence = item.get(
                        "detected_category_evidence",
                        [],
                    )

                    if evidence:
                        st.markdown(
                            "**Evidence that triggered the filter:** "
                            + ", ".join(
                                f"`{term}`"
                                for term in evidence
                            )
                        )

                    scores = item.get(
                        "category_scores",
                        {},
                    )

                    if scores:
                        st.caption(
                            "Category scores: "
                            + " · ".join(
                                f"{category}={score}"
                                for category, score
                                in scores.items()
                            )
                        )

                    edit1, edit2 = st.columns(
                        [1.4, 1]
                    )

                    with edit1:
                        selected_override = st.selectbox(
                            "If included, classify as",
                            manual_category_options,
                            index=0,
                            key=(
                                f"{key_prefix}_category_override_"
                                f"{index}"
                            ),
                            help=(
                                "Usually leave this on automatic/page category. "
                                "Choose a category only when the product itself "
                                "was classified incorrectly."
                            ),
                        )

                    with edit2:
                        st.write("")
                        st.write("")

                        can_apply = bool(
                            scan_config
                            and result_target
                        )

                        if st.button(
                            "✅ Include in audit",
                            key=(
                                f"{key_prefix}_include_"
                                f"{index}"
                            ),
                            type="primary",
                            disabled=not can_apply,
                            help=(
                                "Overrides the category filter for this product "
                                "and reruns this page immediately."
                                if can_apply
                                else (
                                    "This result does not have enough scan "
                                    "configuration to rerun automatically."
                                )
                            ),
                        ):
                            overrides[
                                "include_keys"
                            ].add(
                                item_key
                            )
                            overrides[
                                "labels"
                            ][
                                item_key
                            ] = item.get(
                                "name",
                                "Unnamed product",
                            )

                            if (
                                selected_override
                                == "Use page / automatic category"
                            ):
                                overrides[
                                    "category_overrides"
                                ].pop(
                                    item_key,
                                    None,
                                )
                            else:
                                overrides[
                                    "category_overrides"
                                ][
                                    item_key
                                ] = selected_override

                            with st.spinner(
                                "Including product and rerunning this page..."
                            ):
                                refreshed = rerun_scan_with_filter_overrides(
                                    scan,
                                    scan_config,
                                )

                            if result_target == "detection":
                                st.session_state[
                                    "detection_result"
                                ] = refreshed
                                st.session_state.pop(
                                    "showcase_items",
                                    None,
                                )
                                st.session_state.pop(
                                    "showcase_cache_key",
                                    None,
                                )

                            elif (
                                result_target
                                == "advanced"
                                and advanced_page_index
                                is not None
                            ):
                                pages = st.session_state.get(
                                    "advanced_audit_pages",
                                    [],
                                )

                                if (
                                    0
                                    <= advanced_page_index
                                    < len(pages)
                                ):
                                    pages[
                                        advanced_page_index
                                    ]["scan"] = refreshed
                                    st.session_state[
                                        "advanced_audit_pages"
                                    ] = pages

                            # This inspector is rendered from normal workspace code,
                            # not from an @st.fragment callback. Including a filtered
                            # product changes the underlying scan and surrounding audit UI,
                            # so a full app rerun is both valid and required here.
                            st.rerun()

                    source = item.get(
                        "source",
                        "unknown",
                    )
                    membership = item.get(
                        "page_membership_method",
                        "—",
                    )

                    st.caption(
                        f"Scout source: {source} · "
                        f"Page-membership method: {membership}"
                    )

                    if item.get(
                        "product_url"
                    ):
                        st.link_button(
                            "🔗 Open skipped product",
                            item[
                                "product_url"
                            ],
                            key=(
                                f"{key_prefix}_open_"
                                f"{index}"
                            ),
                        )

        if len(excluded) > max_items:
            st.caption(
                f"Showing first {max_items} of "
                f"{len(excluded)} filtered products."
            )



def filtered_items_csv_with_page_bytes(
    rows: list[dict],
) -> bytes:
    """
    Aggregate filtered products from one or more pages into one dedicated CSV.
    """
    fields = [
        "page_label",
        "page_url",
        "product_name",
        "expected_category",
        "detected_category",
        "filter_confidence",
        "detected_category_score",
        "matched_evidence",
        "filter_reason",
        "product_url",
        "image_url",
        "source",
        "page_membership_method",
    ]

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=fields,
    )
    writer.writeheader()

    for row in rows:
        item = row["item"]

        writer.writerow(
            {
                "page_label": row.get(
                    "page_label",
                    "",
                ),
                "page_url": row.get(
                    "page_url",
                    "",
                ),
                "product_name": item.get(
                    "name",
                    "Unnamed product",
                ),
                "expected_category": item.get(
                    "expected_category",
                    "",
                ),
                "detected_category": item.get(
                    "detected_category",
                    "",
                ),
                "filter_confidence": item.get(
                    "filter_confidence",
                    "",
                ),
                "detected_category_score": item.get(
                    "detected_category_score",
                    "",
                ),
                "matched_evidence": ", ".join(
                    item.get(
                        "detected_category_evidence",
                        [],
                    )
                ),
                "filter_reason": item.get(
                    "filter_reason",
                    "",
                ),
                "product_url": item.get(
                    "product_url",
                    "",
                ),
                "image_url": item.get(
                    "image_url",
                    "",
                ),
                "source": item.get(
                    "source",
                    "",
                ),
                "page_membership_method": item.get(
                    "page_membership_method",
                    "",
                ),
            }
        )

    return buf.getvalue().encode(
        "utf-8-sig"
    )


def collect_filtered_rows(
    detection_scan: dict | None,
    advanced_pages: list[dict],
) -> dict:
    """
    Return filtered rows grouped by source.
    """
    detection_rows = []
    advanced_rows = []

    if detection_scan:
        for item in (
            detection_scan.get(
                "excluded_items",
                [],
            )
            or []
        ):
            detection_rows.append(
                {
                    "source_kind": "Detection Test",
                    "page_label": "Latest Detection Test",
                    "page_url": (
                        detection_scan.get("url")
                        or detection_scan.get(
                            "final_url",
                            "",
                        )
                    ),
                    "item": item,
                }
            )

    for page in advanced_pages:
        scan = page.get(
            "scan",
            {},
        )

        for item in (
            scan.get(
                "excluded_items",
                [],
            )
            or []
        ):
            advanced_rows.append(
                {
                    "source_kind": "Advanced Audit",
                    "page_label": page.get(
                        "page_label",
                        "Page",
                    ),
                    "page_url": page.get(
                        "url",
                        scan.get(
                            "url",
                            "",
                        ),
                    ),
                    "item": item,
                }
            )

    return {
        "detection": detection_rows,
        "advanced": advanced_rows,
    }


def render_filtered_tab_card(
    row: dict,
    index: int,
) -> None:
    """
    Read-only compact card used by the all-pages filtered gallery.
    Editing stays inside the page-level inspector below.
    """
    item = row["item"]

    with st.container(border=True):
        image_col, detail_col = st.columns(
            [1, 2.4]
        )

        with image_col:
            image_url = item.get(
                "image_url"
            )

            if image_url:
                try:
                    st.image(
                        image_url,
                        use_container_width=True,
                    )
                except Exception:
                    st.caption(
                        "Preview unavailable"
                    )
            else:
                st.caption(
                    "No image URL"
                )

        with detail_col:
            st.markdown(
                f"**{index}. "
                f"{item.get('name','Unnamed product')}**"
            )
            st.caption(
                f"{row.get('page_label','')} · "
                f"Expected: "
                f"{item.get('expected_category','Unknown')} · "
                f"Detected: "
                f"{item.get('detected_category','Unknown')}"
            )

            reason = item.get(
                "filter_reason",
                "Filtered by category gate.",
            )
            st.warning(
                reason
            )

            evidence = item.get(
                "detected_category_evidence",
                [],
            )

            if evidence:
                st.markdown(
                    "**Evidence:** "
                    + ", ".join(
                        f"`{term}`"
                        for term in evidence
                    )
                )

            if item.get(
                "product_url"
            ):
                st.link_button(
                    "🔗 Open product",
                    item["product_url"],
                    key=(
                        f"filtered_gallery_open_"
                        f"{row.get('source_kind','source')}_"
                        f"{row.get('page_label','page')}_"
                        f"{index}"
                    ),
                )


def build_side_by_side(
    original_bytes: bytes,
    normalized: Image.Image,
    product_name: str,
    issues: list[str] | None = None,
) -> Image.Image:
    """
    Create a portable comparison image similar to the side-by-side preview in
    the app: Current on the left, Perfected on the right.
    """
    original = Image.open(io.BytesIO(original_bytes))
    original = ImageOps.exif_transpose(original).convert("RGB")
    normalized = normalized.convert("RGB")

    panel_w = 820
    panel_h = 700
    gap = 40
    outer = 36
    title_h = 92
    label_h = 48
    footer_h = 58 if issues else 24

    canvas_w = outer * 2 + panel_w * 2 + gap
    canvas_h = outer * 2 + title_h + label_h + panel_h + footer_h
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    title = product_name[:180]
    draw.text((outer, outer + 8), title, fill="black", font=font)

    left_x = outer
    right_x = outer + panel_w + gap
    label_y = outer + title_h

    draw.text((left_x, label_y + 12), "CURRENT", fill="black", font=font)
    draw.text((right_x, label_y + 12), "PERFECTED", fill="black", font=font)

    image_y = label_y + label_h

    def paste_contained(source: Image.Image, x: int):
        fitted = ImageOps.contain(source, (panel_w, panel_h), Image.Resampling.LANCZOS)
        px = x + (panel_w - fitted.width) // 2
        py = image_y + (panel_h - fitted.height) // 2
        canvas.paste(fitted, (px, py))

    paste_contained(original, left_x)
    paste_contained(normalized, right_x)

    # Simple panel boundaries make the exported comparison readable on white pages.
    draw.rectangle(
        (left_x, image_y, left_x + panel_w, image_y + panel_h),
        outline=(190, 190, 190),
        width=2,
    )
    draw.rectangle(
        (right_x, image_y, right_x + panel_w, image_y + panel_h),
        outline=(190, 190, 190),
        width=2,
    )

    if issues:
        issue_text = "Detected: " + " | ".join(issues)
        if len(issue_text) > 220:
            issue_text = issue_text[:217] + "..."
        draw.text(
            (outer, image_y + panel_h + 20),
            issue_text,
            fill=(60, 60, 60),
            font=font,
        )

    return canvas



def build_page_export_zip(
    scan: dict,
    detection_category: str,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    output_format: str,
    flagged_only: bool = False,
    strict_mode: bool = False,
    normalization_mode: str = "standard",
    fine_tune: dict | None = None,
    manual_overrides: dict | None = None,
    progress_callback=None,
) -> tuple[bytes, dict]:
    selected = [
        item
        for item in scan["items"]
        if (
            (not flagged_only)
            or item["status"] != "pass"
            or item.get("operator_decision") in {
                "use_fixed",
                "complete_rework",
            }
        )
    ]

    zip_buffer = io.BytesIO()
    manifest_buffer = io.StringIO()
    profile_overrides_map = build_page_profile_overrides(scan, strict_mode)

    manifest = csv.writer(manifest_buffer)
    manifest.writerow([
        "product_name",
        "export_folder",
        "status_before",
        "operator_decision",
        "consistency_score_before",
        "profile",
        "subtype",
        "body_mode",
        "anchor_mode",
        "accessory_area_ratio",
        "normalized_file",
        "comparison_file",
        "source_image_url",
        "product_url",
        "detected_issues",
        "normalization_qa",
        "normalization_warnings",
        "manual_scale_bias",
        "manual_x_nudge",
        "manual_y_nudge",
        "manual_baseline_shift",
    ])

    used_names = set()
    successes = 0
    failures = []

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        total = max(len(selected), 1)

        for idx, item in enumerate(selected, start=1):
            if progress_callback:
                progress_callback(idx - 1, total, item["name"])

            export_name = unique_export_name(item["name"], used_names)
            manual = manual_override_for(item, manual_overrides)
            body_mode = manual.get("body_mode", "auto")
            adjustments = merged_adjustments(fine_tune, manual)

            try:
                operator_decision = (
                    item.get(
                        "operator_decision"
                    )
                    or ""
                )

                if operator_decision == "complete_rework":
                    original_png = _png_bytes_from_any_image_bytes(
                        item[
                            "image_bytes"
                        ]
                    )
                    prompt_text = _advanced_fix_prompt(
                        item,
                        normalization_mode,
                    )
                    rework_base = (
                        f"complete_rework/{export_name}"
                    )
                    original_rel = (
                        f"{rework_base}/01_ORIGINAL.png"
                    )
                    prompt_rel = (
                        f"{rework_base}/COMPLETE_REWORK_PROMPT.txt"
                    )
                    zf.writestr(
                        original_rel,
                        original_png,
                    )
                    zf.writestr(
                        prompt_rel,
                        prompt_text,
                    )
                    manifest.writerow([
                        item["name"],
                        export_name,
                        item["status"],
                        operator_decision,
                        f"{item['consistency_score']:.2f}",
                        item["profile"],
                        item.get("subtype", ""),
                        body_mode,
                        manual.get("anchor_mode", "auto"),
                        f"{item['metrics'].get('accessory_area_ratio', 0):.6f}",
                        original_rel,
                        "",
                        item["image_url"],
                        item.get("product_url") or "",
                        " | ".join(automatic_issues_for_item(item)),
                        "COMPLETE REWORK",
                        "Do not use original or generated fix; replace source image completely.",
                        manual.get("scale_bias", 0.0),
                        manual.get("x_nudge", 0.0),
                        manual.get("y_nudge", 0.0),
                        manual.get("baseline_shift", 0.0),
                    ])
                    successes += 1
                    continue

                if operator_decision == "keep_original":
                    normalized_image = Image.open(
                        io.BytesIO(
                            item[
                                "image_bytes"
                            ]
                        )
                    ).convert("RGB")
                    normalized_result = {
                        "metrics": {
                            "qa_pass": True,
                            "warnings": [],
                        }
                    }
                else:
                    category_value = normalization_category_for_item(
                        item,
                        scan,
                        detection_category,
                    )

                    normalized_result = process_image_bytes(
                        item["image_bytes"],
                        category=category_value,
                        canvas_size=canvas_size,
                        segmentation_mode=segmentation_mode,
                        min_padding=min_padding,
                        profile_overrides=get_profile_override_for_item(
                            item, profile_overrides_map
                        ),
                        adjustments=adjustments,
                        body_mode=body_mode,
                        normalization_mode=normalization_mode,
                    )

                    normalized_image = normalized_result["image"]

                normalized_bytes, normalized_ext = image_to_bytes(
                    normalized_image, output_format
                )

                normalized_rel = (
                    f"perfected_images/{export_name}/"
                    f"{export_name}{normalized_ext}"
                )

                comparison = build_side_by_side(
                    item["image_bytes"],
                    normalized_image,
                    item["name"],
                    item.get("issues") or [],
                )
                comparison_buf = io.BytesIO()
                comparison.save(
                    comparison_buf,
                    format="JPEG",
                    quality=94,
                    subsampling=0,
                )
                comparison_rel = f"side_by_side_previews/{export_name}.jpg"

                zf.writestr(normalized_rel, normalized_bytes)
                zf.writestr(comparison_rel, comparison_buf.getvalue())

                nm = normalized_result["metrics"]
                manifest.writerow([
                    item["name"],
                    export_name,
                    item["status"],
                    item.get("operator_decision") or "",
                    f"{item['consistency_score']:.2f}",
                    item["profile"],
                    item.get("subtype", ""),
                    body_mode,
                    manual.get("anchor_mode", "auto"),
                    f"{item['metrics'].get('accessory_area_ratio', 0):.6f}",
                    normalized_rel,
                    comparison_rel,
                    item["image_url"],
                    item.get("product_url") or "",
                    " | ".join(item.get("issues") or []),
                    "PASS" if nm["qa_pass"] else "REVIEW",
                    " | ".join(nm["warnings"]),
                    manual.get("scale_bias", 0.0),
                    manual.get("x_nudge", 0.0),
                    manual.get("y_nudge", 0.0),
                    manual.get("baseline_shift", 0.0),
                ])
                successes += 1

            except Exception as exc:
                failures.append((item["name"], str(exc)))
                manifest.writerow([
                    item["name"], export_name, item["status"],
                    item.get("operator_decision") or "",
                    f"{item['consistency_score']:.2f}",
                    item.get("profile", ""),
                    item.get("subtype", ""),
                    body_mode,
                    manual.get("anchor_mode", "auto"),
                    f"{item['metrics'].get('accessory_area_ratio', 0):.6f}",
                    "", "", item["image_url"],
                    item.get("product_url") or "",
                    " | ".join(item.get("issues") or []),
                    "FAILED", str(exc),
                    manual.get("scale_bias", 0.0),
                    manual.get("x_nudge", 0.0),
                    manual.get("y_nudge", 0.0),
                    manual.get("baseline_shift", 0.0),
                ])

        zf.writestr("export_manifest.csv", manifest_buffer.getvalue())

        if successes == 0:
            zf.writestr(
                "perfected_images/README.txt",
                "No images were successfully exported.",
            )
            zf.writestr(
                "side_by_side_previews/README.txt",
                "No comparisons were successfully exported.",
            )

        if failures:
            zf.writestr(
                "export_failures.txt",
                "\n".join(f"{name}: {error}" for name, error in failures),
            )

    if progress_callback:
        progress_callback(len(selected), max(len(selected), 1), "Finished")

    return zip_buffer.getvalue(), {
        "selected": len(selected),
        "successes": successes,
        "failures": failures,
        "flagged_only": flagged_only,
    }



def build_showcase_items(
    scan: dict,
    detection_category: str,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    normalize_scope: str = "flagged",
    strict_mode: bool = False,
    normalization_mode: str = "standard",
    fine_tune: dict | None = None,
    manual_overrides: dict | None = None,
    progress_callback=None,
) -> list[dict]:
    result = []
    total = max(len(scan.get("items", [])), 1)
    profile_overrides_map = build_page_profile_overrides(scan, strict_mode)

    for idx, item in enumerate(scan.get("items", []), start=1):
        operator_decision = (
            item.get(
                "operator_decision"
            )
            or ""
        )

        should_normalize = (
            normalize_scope == "all"
            or item.get("status") != "pass"
            or operator_decision == "use_fixed"
        )

        if operator_decision == "keep_original":
            should_normalize = False

        showcase_item = {
            **item,
            "scan_index": idx - 1,
            "changed": False,
            "showcase_bytes": item["image_bytes"],
            "normalization_metrics": None,
            "normalization_error": None,
        }

        manual = manual_override_for(item, manual_overrides)

        # A manual exception means the user explicitly wants this item rebuilt,
        # even if it originally passed.
        if (
            manual
            and operator_decision
            != "keep_original"
        ):
            should_normalize = True

        if should_normalize:
            try:
                category_value = normalization_category_for_item(
                    item,
                    scan,
                    detection_category,
                )

                normalized = process_image_bytes(
                    item["image_bytes"],
                    category=category_value,
                    canvas_size=canvas_size,
                    segmentation_mode=segmentation_mode,
                    min_padding=min_padding,
                    profile_overrides=get_profile_override_for_item(
                        item, profile_overrides_map
                    ),
                    adjustments=merged_adjustments(fine_tune, manual),
                    body_mode=manual.get("body_mode", "auto"),
                    anchor_mode=manual.get("anchor_mode", "auto"),
                    normalization_mode=normalization_mode,
                )

                normalized_bytes, _ = image_to_bytes(
                    normalized["image"], "PNG"
                )
                showcase_item["showcase_bytes"] = normalized_bytes
                showcase_item["changed"] = True
                showcase_item["normalization_metrics"] = normalized["metrics"]
                showcase_item["normalized_subtype"] = normalized["subtype"]
                showcase_item["body_mode"] = manual.get("body_mode", "auto")
            except Exception as exc:
                showcase_item["normalization_error"] = str(exc)

        applied_record = applied_edit_record_for(
            item
        )
        if (
            applied_record
            and applied_record.get(
                "image_bytes"
            )
            and operator_decision
            not in {
                "keep_original",
                "complete_rework",
            }
        ):
            showcase_item[
                "showcase_bytes"
            ] = applied_record[
                "image_bytes"
            ]
            showcase_item[
                "changed"
            ] = True
            showcase_item[
                "normalization_metrics"
            ] = applied_record.get(
                "metrics"
            )
            showcase_item[
                "normalized_subtype"
            ] = applied_record.get(
                "subtype",
                showcase_item.get(
                    "normalized_subtype"
                ),
            )

        result.append(showcase_item)

        if progress_callback:
            progress_callback(idx, total, item.get("name", "Product"))

    return result


def showcase_cache_key(
    scan: dict,
    detection_category: str,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    normalize_scope: str,
    strict_mode: bool,
    normalization_mode: str,
    fine_tune: dict,
    manual_overrides: dict | None = None,
) -> tuple:
    signature = tuple(
        (
            item.get("name", ""),
            item.get("image_url", ""),
            item.get(
                "_automatic_status",
                item.get("status", ""),
            ),
            item.get("subtype", ""),
            round(float(item.get("consistency_score", 0)), 2),
        )
        for item in scan.get("items", [])
    )

    return (
        scan.get("url", ""),
        signature,
        detection_category,
        segmentation_mode,
        int(canvas_size),
        round(float(min_padding), 4),
        normalize_scope,
        strict_mode,
        normalization_mode,
        round(float(fine_tune.get("scale_bias", 0.0)), 4),
        round(float(fine_tune.get("x_nudge", 0.0)), 4),
        round(float(fine_tune.get("y_nudge", 0.0)), 4),
        round(float(fine_tune.get("baseline_shift", 0.0)), 4),
    )


def build_page_profile_overrides(
    scan: dict | None,
    strict_mode: bool,
) -> dict[str, dict]:
    """
    V6 strict calibration is subgroup-aware:
        profile + structural subtype

    Examples:
      boxy::boxy
      tall::tall_standing
      boxy::accessory_heavy
      wide::multipart

    If a subgroup is too small, no page override is forced; the canonical
    category/geometric defaults remain safer.
    """
    if not strict_mode or not scan or not scan.get("items"):
        return {}

    grouped: dict[str, list[dict]] = {}
    for item in scan.get("items", []):
        key = item.get("group_key") or f"{item.get('profile','')}::{item.get('subtype','boxy')}"
        grouped.setdefault(key, []).append(item)

    overrides: dict[str, dict] = {}

    for group_key, items in grouped.items():
        if len(items) < 2:
            continue

        profile_name = items[0].get("profile", "")
        profile = dict(PROFILES.get(profile_name, {}))
        if not profile:
            continue

        metrics = [x["metrics"] for x in items]

        if "target_height" in profile:
            values = sorted(m["body_height_occupancy"] for m in metrics)
            median = values[len(values) // 2]
            profile["target_height"] = max(
                0.40, min(0.90, round(float(median), 4))
            )

            if profile.get("alignment") == "bottom_center":
                baselines = sorted(
                    m.get("body_baseline_y", m.get("baseline_y", profile.get("baseline", 0.89)))
                    for m in metrics
                )
                base = baselines[len(baselines) // 2]
                profile["baseline"] = max(
                    0.68, min(0.97, round(float(base), 4))
                )
        else:
            values = sorted(m["body_width_occupancy"] for m in metrics)
            median = values[len(values) // 2]
            profile["target_width"] = max(
                0.38, min(0.92, round(float(median), 4))
            )

        overrides[group_key] = profile

    return overrides


def get_profile_override_for_item(
    item: dict,
    profile_overrides_map: dict[str, dict],
) -> dict | None:
    group_key = item.get("group_key") or f"{item.get('profile','')}::{item.get('subtype','boxy')}"
    return profile_overrides_map.get(group_key)



def resolved_audit_main_category(
    typed_category: str,
    selected_detection_category: str,
    scan: dict | None,
) -> str:
    typed = (typed_category or "").strip()
    if typed:
        return typed

    if selected_detection_category != "Auto by page / shape":
        return selected_detection_category

    if scan and scan.get("effective_category"):
        return str(scan["effective_category"])

    return "Uncategorized"


def issue_level(item: dict) -> str:
    issues = item.get("issues") or []
    score = float(item.get("consistency_score", 100))

    if any(
        "segmentation" in x.lower()
        or "foreground detection" in x.lower()
        for x in issues
    ):
        return "Manual review"

    if score < 55 or len(issues) >= 3:
        return "High"

    if score < 78 or len(issues) >= 2:
        return "Medium"

    if issues:
        return "Low"

    return "Pass"




def enrich_audit_rows_with_manual_edits(
    rows: list[dict],
    scan: dict,
    manual_overrides: dict | None,
) -> list[dict]:
    """
    Merge Product Editor overrides into audit rows without re-running the audit.

    The Simple CSV can therefore describe the exact manual tuning that will be
    used by Page Showcase / exports.
    """
    manual_overrides = (
        manual_overrides
        or {}
    )

    by_url = {}
    by_name = {}

    for item in scan.get(
        "items",
        [],
    ):
        product_url = (
            item.get(
                "product_url"
            )
            or ""
        )
        product_name = (
            item.get(
                "name"
            )
            or ""
        )

        if product_url:
            by_url[
                product_url
            ] = item

        if product_name:
            by_name[
                product_name
            ] = item

    for row in rows:
        item = (
            by_url.get(
                row.get(
                    "product_url",
                    "",
                )
            )
            or by_name.get(
                row.get(
                    "product_name",
                    "",
                )
            )
        )

        if not item:
            continue

        manual = manual_override_for(
            item,
            manual_overrides,
        )

        if manual:
            row[
                "manual_edit_applied"
            ] = "Yes"
            row[
                "manual_zoom"
            ] = (
                f"{1.0 + float(manual.get('scale_bias',0.0)):.3f}x"
            )
            row[
                "manual_move_left_right"
            ] = (
                f"{float(manual.get('x_nudge',0.0)):+.4f}"
            )
            row[
                "manual_move_up_down"
            ] = (
                f"{float(manual.get('y_nudge',0.0)):+.4f}"
            )
            row[
                "manual_floor_line"
            ] = (
                f"{float(manual.get('baseline_shift',0.0)):+.4f}"
            )
            row["manual_crop_left"] = f"{float(manual.get('crop_left',0.0)):.4f}"
            row["manual_crop_right"] = f"{float(manual.get('crop_right',0.0)):.4f}"
            row["manual_crop_top"] = f"{float(manual.get('crop_top',0.0)):.4f}"
            row["manual_crop_bottom"] = f"{float(manual.get('crop_bottom',0.0)):.4f}"
            row[
                "manual_body_mode"
            ] = manual.get(
                "body_mode",
                "auto",
            )
            row[
                "manual_center_mode"
            ] = manual.get(
                "anchor_mode",
                "auto",
            )
        else:
            row[
                "manual_edit_applied"
            ] = "No"
            row[
                "manual_zoom"
            ] = ""
            row[
                "manual_move_left_right"
            ] = ""
            row[
                "manual_move_up_down"
            ] = ""
            row[
                "manual_floor_line"
            ] = ""
            row["manual_crop_left"] = ""
            row["manual_crop_right"] = ""
            row["manual_crop_top"] = ""
            row["manual_crop_bottom"] = ""
            row[
                "manual_body_mode"
            ] = ""
            row[
                "manual_center_mode"
            ] = ""

    return rows


def resolve_current_showcase_scan() -> tuple[dict | None, str]:
    """
    Resolve the page currently selected for Page Showcase without relying on
    local variables created by the Showcase tab.

    This is what allows Page Showcase itself to run as an isolated fragment.
    """
    detection_scan = st.session_state.get(
        "detection_result"
    )
    advanced_pages = st.session_state.get(
        "advanced_audit_pages",
        [],
    )
    advanced_context = st.session_state.get(
        "advanced_audit_context",
        {},
    )

    source = st.session_state.get(
        "showcase_source"
    )

    if (
        advanced_pages
        and (
            source
            == "Advanced Audit page"
            or not detection_scan
        )
    ):
        index = int(
            st.session_state.get(
                "showcase_advanced_page_index",
                0,
            )
        )
        index = max(
            0,
            min(
                index,
                len(
                    advanced_pages
                )
                - 1,
            ),
        )

        return (
            advanced_pages[
                index
            ][
                "scan"
            ],
            advanced_context.get(
                "detection_category",
                "Auto by page / shape",
            ),
        )

    if detection_scan:
        return (
            detection_scan,
            st.session_state.get(
                "detection_category"
            )
            or "Auto by page / shape",
        )

    if advanced_pages:
        return (
            advanced_pages[
                0
            ][
                "scan"
            ],
            advanced_context.get(
                "detection_category",
                "Auto by page / shape",
            ),
        )

    return (
        None,
        "Auto by page / shape",
    )



def advanced_page_rows(
    audit_pages: list[dict],
    context: dict,
    include_passed: bool,
    manual_overrides: dict | None = None,
) -> list[dict]:
    rows = []

    for page in audit_pages:
        page_rows = build_audit_rows(
            page["scan"],
            main_category=context.get(
                "main_category",
                "Uncategorized",
            ),
            subcategory=context.get(
                "subcategory",
                "General",
            ),
            page_label=page.get(
                "page_label",
                "Page",
            ),
            include_passed=include_passed,
        )

        enrich_audit_rows_with_manual_edits(
            page_rows,
            page["scan"],
            manual_overrides,
        )

        rows.extend(
            page_rows
        )

    return rows


def _png_bytes_from_any_image_bytes(
    data: bytes,
) -> bytes:
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(
        image
    ).convert("RGB")

    buf = io.BytesIO()
    image.save(
        buf,
        format="PNG",
        optimize=True,
    )
    return buf.getvalue()


def persist_advanced_visual_media(
    audit_pages: list[dict],
    context: dict,
    include_passed: bool,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    strict_mode: bool,
    normalization_mode: str,
    fine_tune: dict,
    manual_overrides: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """
    Generate and persist Current / Normalized / Before-After assets for the
    rows being saved into the master audit.

    The returned CSV rows contain relative image paths.
    """
    all_rows = []
    failures = []

    for page in audit_pages:
        scan = page["scan"]
        page_label = page.get(
            "page_label",
            "Page",
        )

        rows = build_audit_rows(
            scan,
            main_category=context.get(
                "main_category",
                "Uncategorized",
            ),
            subcategory=context.get(
                "subcategory",
                "General",
            ),
            page_label=page_label,
            include_passed=include_passed,
        )

        enrich_audit_rows_with_manual_edits(
            rows,
            scan,
            manual_overrides,
        )

        # Replacing a page should not leave old image files around.
        clear_page_media(
            context.get(
                "main_category",
                "Uncategorized",
            ),
            context.get(
                "subcategory",
                "General",
            ),
            page_label,
        )

        profile_overrides_map = (
            build_page_profile_overrides(
                scan,
                strict_mode,
            )
        )

        row_lookup = {
            (
                row.get("product_name", ""),
                row.get("_source_image_url", ""),
            ): row
            for row in rows
        }

        for item in scan.get("items", []):
            if (
                not include_passed
                and item.get("status") == "pass"
                and not item.get(
                    "operator_decision"
                )
            ):
                continue

            key = (
                item.get("name", ""),
                item.get("image_url", ""),
            )
            row = row_lookup.get(key)

            if row is None:
                continue

            try:
                technical_category = (
                    item.get("item_category")
                    or scan.get("effective_category")
                    or (
                        None
                        if context.get(
                            "detection_category"
                        )
                        == "Auto by page / shape"
                        else context.get(
                            "detection_category"
                        )
                    )
                )

                manual = manual_override_for(
                    item,
                    manual_overrides,
                )

                normalized = process_image_bytes(
                    item["image_bytes"],
                    category=technical_category,
                    canvas_size=canvas_size,
                    segmentation_mode=segmentation_mode,
                    min_padding=min_padding,
                    profile_overrides=get_profile_override_for_item(
                        item,
                        profile_overrides_map,
                    ),
                    adjustments=merged_adjustments(
                        fine_tune,
                        manual,
                    ),
                    body_mode=manual.get(
                        "body_mode",
                        "auto",
                    ),
                    anchor_mode=manual.get(
                        "anchor_mode",
                        "auto",
                    ),
                    normalization_mode=normalization_mode,
                )

                normalized_bytes, _ = image_to_bytes(
                    normalized["image"],
                    "PNG",
                )
                current_bytes = (
                    _png_bytes_from_any_image_bytes(
                        item["image_bytes"]
                    )
                )

                comparison = build_side_by_side(
                    item["image_bytes"],
                    normalized["image"],
                    item["name"],
                    item.get("issues") or [],
                )

                comparison_buf = io.BytesIO()
                comparison.save(
                    comparison_buf,
                    format="JPEG",
                    quality=94,
                    subsampling=0,
                )

                paths = save_audit_media(
                    row=row,
                    current_bytes=current_bytes,
                    normalized_bytes=normalized_bytes,
                    comparison_bytes=comparison_buf.getvalue(),
                )

                row.update(paths)

            except Exception as exc:
                failures.append(
                    (
                        f"{page_label} / "
                        f"{item.get('name','Product')}: "
                        f"{exc}"
                    )
                )

        all_rows.extend(rows)

    return all_rows, failures



def _advanced_issue_text(
    item: dict,
    page_label: str,
    page_url: str,
    context: dict,
    normalization_mode: str,
    normalized_result: dict | None = None,
    normalization_error: str | None = None,
) -> str:
    issues = automatic_issues_for_item(
        item
    )
    decision = (
        item.get(
            "operator_decision"
        )
        or ""
    )

    if decision == "keep_original":
        suggested_fix = (
            "Keep the original website image. "
            "Operator rejected the generated replacement."
        )
    elif decision == "use_fixed":
        suggested_fix = (
            "Use the approved normalized replacement."
        )
    elif decision == "complete_rework":
        suggested_fix = (
            "Do not use the original or generated fix. Replace/rebuild the product image "
            "from a clean source, then audit the new image again."
        )
    else:
        suggested_fix = suggested_fix_from_issues(
            issues
        )

    status = (
        "COMPLETE REWORK REQUIRED"
        if decision == "complete_rework"
        else (
            "NEEDS REVIEW"
            if item.get("status") != "pass"
            else "PASS — NO REVIEW REQUIRED"
        )
    )

    lines = [
        f"PRODUCT: {item.get('name','Unnamed product')}",
        f"PRODUCT URL: {item.get('product_url') or ''}",
        f"PAGE: {page_label}",
        f"PAGE URL: {page_url}",
        (
            "AUDIT LOCATION: "
            f"{context.get('main_category','Uncategorized')} / "
            f"{context.get('subcategory','General')}"
        ),
        f"STATUS: {status}",
        (
            "OPERATOR DECISION: "
            + (
                operator_decision_label(item)
                or "None"
            )
        ),
        f"CONSISTENCY / FRAMING SCORE: {float(item.get('consistency_score',0)):.2f}/100",
        f"PROFILE: {item.get('profile','')}",
        f"SUBTYPE: {item.get('subtype','')}",
        f"DETECTED PRODUCT CATEGORY: {item.get('item_category') or 'Unknown'}",
        f"NORMALIZATION MODE: {normalization_mode}",
        "",
        "DETECTED ISSUE(S):",
    ]

    if issues:
        lines.extend(
            f"- {issue}"
            for issue in issues
        )
    else:
        lines.append(
            "- None. This product passed the audit."
        )

    lines.extend(
        [
            "",
            "SUGGESTED FIX:",
            (
                suggested_fix
                if suggested_fix
                else (
                    "No correction is required. "
                    "Keep the current image."
                )
            ),
            "",
        ]
    )

    if normalized_result is not None:
        metrics = normalized_result.get(
            "metrics",
            {},
        )

        lines.extend(
            [
                "GENERATED FIX / NORMALIZATION DETAILS:",
                (
                    "- Amazon A compliant: "
                    + (
                        "Yes"
                        if metrics.get(
                            "amazon_a_compliant"
                        )
                        else "No / constrained"
                    )
                    if normalization_mode == "amazon_a"
                    else "- Normalized using selected FBF mode."
                ),
                (
                    "- Size lock: "
                    f"{metrics.get('size_lock_actual_px',0)} / "
                    f"{metrics.get('size_lock_target_px',0)} px"
                ),
                (
                    "- Horizontal anchor error: "
                    f"{float(metrics.get('anchor_error_x_px',0)):.2f} px"
                ),
                (
                    "- Vertical anchor error: "
                    f"{float(metrics.get('anchor_error_y_px',0)):.2f} px"
                ),
                (
                    "- Full edge padding: "
                    f"{float(metrics.get('full_edge_padding',0)):.2%}"
                ),
                "",
            ]
        )

    if normalization_error:
        lines.extend(
            [
                "NORMALIZATION ERROR:",
                normalization_error,
                "",
            ]
        )

    if decision == "complete_rework":
        lines.extend(
            [
                "FILES IN THIS PRODUCT FOLDER:",
                "- 01_ORIGINAL.png — unusable source image for reference only",
                "- COMPLETE_REWORK_PROMPT.txt — replacement/rebuild brief",
                "",
                "No fixed image or before/after comparison is exported for a complete-rework item.",
            ]
        )
    else:
        lines.extend(
            [
                "FILES IN THIS PRODUCT FOLDER:",
                "- 01_ORIGINAL.png — image currently used on the website",
                (
                    "- 02_FIXED.png — recommended corrected image"
                    if item.get("status") != "pass"
                    else (
                        "- 02_FIXED.png — same as the original because "
                        "this product passed and does not require correction"
                    )
                ),
                "- 03_BEFORE_AFTER.jpg — visual comparison",
                "- issue_and_fix.txt — this report",
                "- fix_prompt.txt — concise handoff instructions",
                "",
                (
                    "NOTE: File/folder names preserve the website product name as "
                    "closely as Windows/macOS/Linux filenames allow."
                ),
            ]
        )

    return "\n".join(lines)


def _advanced_fix_prompt(
    item: dict,
    normalization_mode: str,
) -> str:
    issues = automatic_issues_for_item(
        item
    )
    decision = (
        item.get(
            "operator_decision"
        )
        or ""
    )

    if decision == "keep_original":
        return (
            f"Product: {item.get('name','Unnamed product')}\n\n"
            "Manual review decision: KEEP ORIGINAL. "
            "Do not replace this image with the generated normalized version."
        )

    if decision == "use_fixed":
        return (
            f"Product: {item.get('name','Unnamed product')}\n\n"
            "Manual review decision: USE FIXED IMAGE. "
            "Use the approved Federal Bureau of Framing normalized replacement."
        )

    if decision == "complete_rework":
        issue_text = (
            "\n".join(
                f"- {issue}"
                for issue in issues
            )
            if issues
            else "- Existing source image is not acceptable for catalogue use."
        )
        return (
            f"Product: {item.get('name','Unnamed product')}\n"
            f"Product URL: {item.get('product_url') or ''}\n\n"
            "STATUS: COMPLETE REWORK REQUIRED\n\n"
            "Do NOT use the current source image and do NOT use the generated FBF fix. "
            "This product needs a completely new/reworked catalogue image from a cleaner source.\n\n"
            "Reference problems:\n"
            f"{issue_text}\n\n"
            "REWORK BRIEF:\n"
            "- Obtain or create a clean, high-quality product image.\n"
            "- Preserve the exact product/model identity and legitimate included components.\n"
            "- Use a clean ecommerce/catalogue background.\n"
            "- Keep the full appliance visible with no destructive crop.\n"
            "- After replacement, run the new image through Federal Bureau of Framing again."
        )

    if not issues:
        return (
            f"Product: {item.get('name','Unnamed product')}\n\n"
            "This product passed the Federal Bureau of Framing audit. "
            "No image correction is required. Keep the original image."
        )

    issue_text = "\n".join(
        f"- {issue}"
        for issue in issues
    )
    suggested_fix = suggested_fix_from_issues(
        issues
    )

    mode_instruction = (
        (
            "Use Amazon A Mode adapted to a square 1:1 catalogue card. "
            "Keep proportional scaling only. Tall products should match the "
            "profile height ruler, wide/flat products should match the width "
            "ruler, and boxy/compact products should keep balanced whitespace."
        )
        if normalization_mode == "amazon_a"
        else (
            "Use the selected Federal Bureau of Framing normalization mode "
            "and keep the complete product visible."
        )
    )

    return (
        f"Product: {item.get('name','Unnamed product')}\n\n"
        "Correct this ecommerce product image.\n\n"
        "Detected problems:\n"
        f"{issue_text}\n\n"
        f"Suggested correction:\n{suggested_fix}\n\n"
        f"Framing rule:\n{mode_instruction}\n\n"
        "Do not stretch or squash the appliance. Do not crop legitimate parts "
        "or included structural accessories. Preserve a clean catalogue background "
        "and align the product consistently with other products of the same profile."
    )


def build_advanced_image_review_package(
    audit_pages: list[dict],
    context: dict,
    segmentation_mode: str,
    canvas_size: int,
    min_padding: float,
    strict_mode: bool,
    normalization_mode: str,
    fine_tune: dict,
    manual_overrides: dict | None = None,
    progress_callback=None,
) -> tuple[bytes, dict]:
    """
    Create the Advanced Audit media handoff ZIP requested for production work.

    Structure:
        01_NEEDS_REVIEW/
        02_PASSED/
        03_ALL_PRODUCTS/

    Every product folder contains original/fixed/comparison images plus TXT
    issue/fix information. Products are intentionally duplicated into
    03_ALL_PRODUCTS so that folder can be handed off as a complete catalogue.
    """
    zip_buffer = io.BytesIO()
    manifest_buffer = io.StringIO()
    manifest = csv.writer(
        manifest_buffer
    )
    manifest.writerow(
        [
            "page",
            "product_name",
            "product_url",
            "status",
            "operator_decision",
            "consistency_score",
            "profile",
            "subtype",
            "detected_category",
            "issues",
            "suggested_fix",
            "normalization_mode",
            "status_folder",
            "product_folder",
        ]
    )

    total_products = sum(
        len(
            page.get(
                "scan",
                {},
            ).get(
                "items",
                [],
            )
        )
        for page in audit_pages
    )
    done = 0
    review_count = 0
    pass_count = 0
    generated_count = 0
    complete_rework_count = 0
    failures = []

    with zipfile.ZipFile(
        zip_buffer,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as zf:
        zf.writestr(
            "README.txt",
            (
                "FEDERAL BUREAU OF FRAMING — ADVANCED AUDIT IMAGE REVIEW PACKAGE\n"
                "================================================================\n\n"
                "This ZIP is designed for image-production handoff when CSV is "
                "not convenient for visual review.\n\n"
                "01_NEEDS_REVIEW\n"
                "  Products that the audit flagged as requiring correction.\n\n"
                "02_PASSED\n"
                "  Products that passed and do not require review.\n\n"
                "03_ALL_PRODUCTS\n"
                "  Complete mixture of both groups for one full catalogue view.\n\n"
                "Each product folder contains:\n"
                "  01_ORIGINAL.png\n"
                "  02_FIXED.png\n"
                "  03_BEFORE_AFTER.jpg\n"
                "  issue_and_fix.txt\n"
                "  fix_prompt.txt\n\n"
                "Products that passed keep their original image as 02_FIXED.png "
                "because no replacement is required.\n\n"
                "COMPLETE REWORK EXCEPTION\n"
                "  If a product is manually marked COMPLETE REWORK, its product folder "
                "contains ONLY 01_ORIGINAL.png and COMPLETE_REWORK_PROMPT.txt. "
                "No generated fix or before/after image is included.\n"
            ),
        )

        for page_index, page in enumerate(
            audit_pages,
            start=1,
        ):
            scan = page.get(
                "scan",
                {},
            )
            page_label = page.get(
                "page_label",
                f"Page {page_index}",
            )
            page_url = page.get(
                "url",
                scan.get(
                    "url",
                    "",
                ),
            )

            safe_page = safe_product_name(
                page_label,
                "Page",
            )
            used_product_names = set()

            profile_overrides_map = (
                build_page_profile_overrides(
                    scan,
                    strict_mode,
                )
            )

            for item in scan.get(
                "items",
                [],
            ):
                done += 1

                if progress_callback:
                    progress_callback(
                        done - 1,
                        max(
                            total_products,
                            1,
                        ),
                        item.get(
                            "name",
                            "Product",
                        ),
                    )

                product_folder_name = (
                    unique_export_name(
                        item.get(
                            "name",
                            "Unnamed product",
                        ),
                        used_product_names,
                    )
                )

                operator_decision = (
                    item.get(
                        "operator_decision"
                    )
                    or ""
                )
                complete_rework = (
                    operator_decision
                    == "complete_rework"
                )
                needs_review = (
                    complete_rework
                    or item.get(
                        "status"
                    )
                    != "pass"
                )

                if complete_rework:
                    complete_rework_count += 1

                if needs_review:
                    status_root = (
                        "01_NEEDS_REVIEW"
                    )
                    review_count += 1
                else:
                    status_root = (
                        "02_PASSED"
                    )
                    pass_count += 1

                normalized_result = None
                normalization_error = None

                current_bytes = (
                    _png_bytes_from_any_image_bytes(
                        item[
                            "image_bytes"
                        ]
                    )
                )

                # Passed images do not require a replacement. Keep the original
                # as the recommended file to make the handoff unambiguous.
                fixed_bytes = current_bytes
                comparison_image = Image.open(
                    io.BytesIO(
                        current_bytes
                    )
                ).convert("RGB")

                manual = manual_override_for(
                    item,
                    manual_overrides,
                )

                should_generate_fix = bool(
                    not complete_rework
                    and (
                        needs_review
                        or operator_decision
                        == "use_fixed"
                        or bool(
                            manual
                        )
                    )
                )

                if should_generate_fix:
                    try:
                        technical_category = (
                            item.get(
                                "item_category"
                            )
                            or scan.get(
                                "effective_category"
                            )
                            or (
                                None
                                if context.get(
                                    "detection_category"
                                )
                                == "Auto by page / shape"
                                else context.get(
                                    "detection_category"
                                )
                            )
                        )

                        normalized_result = (
                            process_image_bytes(
                                item[
                                    "image_bytes"
                                ],
                                category=(
                                    technical_category
                                ),
                                canvas_size=(
                                    canvas_size
                                ),
                                segmentation_mode=(
                                    segmentation_mode
                                ),
                                min_padding=(
                                    min_padding
                                ),
                                profile_overrides=(
                                    get_profile_override_for_item(
                                        item,
                                        profile_overrides_map,
                                    )
                                ),
                                adjustments=(
                                    merged_adjustments(
                                        fine_tune,
                                        manual,
                                    )
                                ),
                                body_mode=manual.get(
                                    "body_mode",
                                    "auto",
                                ),
                                anchor_mode=manual.get(
                                    "anchor_mode",
                                    "auto",
                                ),
                                normalization_mode=(
                                    normalization_mode
                                ),
                            )
                        )

                        fixed_bytes, _ = (
                            image_to_bytes(
                                normalized_result[
                                    "image"
                                ],
                                "PNG",
                            )
                        )

                        comparison_image = (
                            normalized_result[
                                "image"
                            ]
                        )
                        generated_count += 1

                    except Exception as exc:
                        normalization_error = (
                            str(exc)
                        )
                        failures.append(
                            (
                                f"{page_label} / "
                                f"{item.get('name','Product')}: "
                                f"{exc}"
                            )
                        )

                prompt_text = (
                    _advanced_fix_prompt(
                        item,
                        normalization_mode,
                    )
                )

                # Same product package is intentionally copied into:
                #   status-specific folder + complete all-products folder.
                roots = [
                    status_root,
                    "03_ALL_PRODUCTS",
                ]

                if complete_rework:
                    for root in roots:
                        base_path = (
                            f"{root}/"
                            f"{safe_page}/"
                            f"{product_folder_name}"
                        )
                        zf.writestr(
                            f"{base_path}/01_ORIGINAL.png",
                            current_bytes,
                        )
                        zf.writestr(
                            f"{base_path}/COMPLETE_REWORK_PROMPT.txt",
                            prompt_text,
                        )
                else:
                    comparison = build_side_by_side(
                        item[
                            "image_bytes"
                        ],
                        comparison_image,
                        item.get(
                            "name",
                            "Unnamed product",
                        ),
                        item.get(
                            "issues"
                        )
                        or [],
                    )

                    comparison_buf = (
                        io.BytesIO()
                    )
                    comparison.save(
                        comparison_buf,
                        format="JPEG",
                        quality=94,
                        subsampling=0,
                    )
                    comparison_bytes = (
                        comparison_buf.getvalue()
                    )

                    issue_text = (
                        _advanced_issue_text(
                            item=item,
                            page_label=page_label,
                            page_url=page_url,
                            context=context,
                            normalization_mode=(
                                normalization_mode
                            ),
                            normalized_result=(
                                normalized_result
                            ),
                            normalization_error=(
                                normalization_error
                            ),
                        )
                    )

                    for root in roots:
                        base_path = (
                            f"{root}/"
                            f"{safe_page}/"
                            f"{product_folder_name}"
                        )

                        zf.writestr(
                            f"{base_path}/01_ORIGINAL.png",
                            current_bytes,
                        )
                        zf.writestr(
                            f"{base_path}/02_FIXED.png",
                            fixed_bytes,
                        )
                        zf.writestr(
                            f"{base_path}/03_BEFORE_AFTER.jpg",
                            comparison_bytes,
                        )
                        zf.writestr(
                            f"{base_path}/issue_and_fix.txt",
                            issue_text,
                        )
                        zf.writestr(
                            f"{base_path}/fix_prompt.txt",
                            prompt_text,
                        )

                        if normalization_error:
                            zf.writestr(
                                (
                                    f"{base_path}/"
                                    "NORMALIZATION_ERROR.txt"
                                ),
                                normalization_error,
                            )

                issues = (
                    item.get(
                        "issues"
                    )
                    or []
                )

                manifest.writerow(
                    [
                        page_label,
                        item.get(
                            "name",
                            "Unnamed product",
                        ),
                        item.get(
                            "product_url"
                        )
                        or "",
                        (
                            "COMPLETE REWORK"
                            if complete_rework
                            else (
                                "NEEDS REVIEW"
                                if needs_review
                                else "PASS"
                            )
                        ),
                        operator_decision,
                        (
                            f"{float(item.get('consistency_score',0)):.2f}"
                        ),
                        item.get(
                            "profile",
                            "",
                        ),
                        item.get(
                            "subtype",
                            "",
                        ),
                        item.get(
                            "item_category",
                            "",
                        ),
                        " | ".join(
                            issues
                        ),
                        (
                            "Replace/rebuild source image completely; do not use original or generated fix."
                            if complete_rework
                            else suggested_fix_from_issues(
                                issues
                            )
                        ),
                        normalization_mode,
                        status_root,
                        (
                            f"{status_root}/"
                            f"{safe_page}/"
                            f"{product_folder_name}"
                        ),
                    ]
                )

        zf.writestr(
            "PACKAGE_MANIFEST.csv",
            manifest_buffer.getvalue(),
        )

        if failures:
            zf.writestr(
                "PACKAGE_GENERATION_FAILURES.txt",
                "\n".join(
                    failures
                ),
            )

    if progress_callback:
        progress_callback(
            total_products,
            max(
                total_products,
                1,
            ),
            "Finished",
        )

    return (
        zip_buffer.getvalue(),
        {
            "products": total_products,
            "needs_review": review_count,
            "passed": pass_count,
            "generated_fixes": generated_count,
            "complete_rework": complete_rework_count,
            "failures": failures,
        },
    )



def audit_pages_summary(
    audit_pages: list[dict],
) -> dict:
    return {
        "pages": len(audit_pages),
        "scouted": sum(
            p["scan"].get(
                "raw_candidate_count",
                p["scan"].get(
                    "candidate_count",
                    0,
                ),
            )
            for p in audit_pages
        ),
        "matched": sum(
            p["scan"].get(
                "candidate_count",
                0,
            )
            for p in audit_pages
        ),
        "filtered": sum(
            p["scan"].get(
                "filtered_out_count",
                0,
            )
            for p in audit_pages
        ),
        "issues": sum(
            p["scan"].get(
                "flagged_count",
                0,
            )
            for p in audit_pages
        ),
    }


st.set_page_config(
    page_title="Federal Bureau of Framing — V7.3.1",
    page_icon="🧭",
    layout="wide",
)

st.title("🕵️ Federal Bureau of Framing")
st.caption("Product Image Audit & Normalization Tool — V7.3.1")
st.caption(
    "Universal appliance framing: primary-body detection, subgroup normalization, page showcase, fine tuning, and one-click export."
)

with st.sidebar:
    st.header("Image engine")
    canvas_size = st.selectbox("Output canvas size", [800, 1000, 1200, 1600], index=1)
    output_format = st.selectbox("Output format", ["PNG", "WEBP"], index=0)

    st.header("Segmentation")
    segmentation_options = ["Auto / white-background"]
    if rembg_available():
        segmentation_options.append("AI background removal (rembg)")
    segmentation_mode_label = st.selectbox("Foreground detection", segmentation_options)
    segmentation_mode = "rembg" if segmentation_mode_label.startswith("AI") else "auto"

    st.header("Default category")
    categories = ["Auto by page / shape"] + sorted(CATEGORY_PROFILE.keys())
    default_category = st.selectbox("Category", categories, index=0)

    st.header("Normalization QA")
    min_padding = st.slider(
        "Minimum safe edge padding",
        0.02,
        0.12,
        0.04,
        0.01,
        help=(
            "Amazon A Mode uses a tight square-card margin. 4% keeps the full "
            "product safe while allowing wide products to reach their 90% width ruler."
        ),
    )
    show_mask = st.checkbox("Show detected foreground mask", value=False)

    st.header("Precision tuning")
    precision_mode = st.radio(
        "Normalization mode",
        [
            "Amazon A Mode — Square 1:1",
            "Standard",
            "Strict / page-calibrated",
        ],
        index=0,
        help=(
            "Amazon A Mode is the default: Tall locks height, Wide locks width, "
            "and Boxy locks a balanced envelope on the square website card."
        ),
    )
    normalization_mode = selected_normalization_mode(
        precision_mode
    )

    if normalization_mode == "amazon_a":
        st.success(
            "Amazon A Mode active · Square 1:1 · hard profile locking"
        )
        st.caption(
            "Tall → same height · Wide/Flat → same width · "
            "Boxy/Compact → balanced all-side padding."
        )
    scale_bias = st.slider(
        "Global size trim",
        min_value=-0.10,
        max_value=0.10,
        value=0.0,
        step=0.01,
        help="Make all normalized outputs slightly smaller or larger.",
    )
    x_nudge = st.slider(
        "Horizontal nudge",
        min_value=-0.08,
        max_value=0.08,
        value=0.0,
        step=0.005,
        help="Shift normalized outputs left or right globally.",
    )
    y_nudge = st.slider(
        "Vertical nudge",
        min_value=-0.08,
        max_value=0.08,
        value=0.0,
        step=0.005,
        help="Shift normalized outputs up or down globally.",
    )
    baseline_shift = st.slider(
        "Baseline trim",
        min_value=-0.06,
        max_value=0.06,
        value=0.0,
        step=0.005,
        help="Move bottom-aligned products slightly higher or lower.",
    )
    fine_tune = {
        "scale_bias": scale_bias,
        "x_nudge": x_nudge,
        "y_nudge": y_nudge,
        "baseline_shift": baseline_shift,
    }

    st.divider()
    with st.expander("Profile reference"):
        reference_profiles = (
            AMAZON_A_PROFILES
            if normalization_mode == "amazon_a"
            else PROFILES
        )

        if normalization_mode == "amazon_a":
            st.caption(
                "Amazon A targets are calibrated for the site's square 1:1 image well."
            )

        for name, profile in reference_profiles.items():
            st.markdown(f"**{name.title()}**")
            if "target_height" in profile:
                st.write(
                    f"Target height: {profile['target_height']:.0%} · "
                    f"Max width: {profile['max_width']:.0%}"
                )
            else:
                st.write(
                    f"Target width: {profile['target_width']:.0%} · "
                    f"Max height: {profile['max_height']:.0%}"
                )

            if normalization_mode == "amazon_a":
                st.caption(
                    profile.get(
                        "amazon_description",
                        "",
                    )
                )
            st.write(f"Alignment: {profile['alignment']}")

st.session_state.setdefault(
    "manual_overrides",
    {},
)

FBF_WORKSPACES = [
    "🔎 Detection Test",
    "🧪 Advanced Audit",
    "🚫 Filtered Out Images",
    "🖼️ Page Showcase",
    "🎛️ Product Editor",
    "🛠️ Normalize Images",
]

pending_workspace = st.session_state.pop(
    "pending_fbf_workspace",
    None,
)
if pending_workspace in FBF_WORKSPACES:
    # Safe because this runs before the workspace radio is instantiated.
    st.session_state[
        "fbf_workspace"
    ] = pending_workspace

active_workspace = st.radio(
    "Workspace",
    FBF_WORKSPACES,
    horizontal=True,
    key="fbf_workspace",
    label_visibility="collapsed",
)

if active_workspace != "🖼️ Page Showcase":
    st.iframe(
        "<script>window.parent.__fbfActiveShowcaseScrollKey = null;</script>",
        width=1,
        height=1,
        tab_index=-1,
    )

if active_workspace == "🔎 Detection Test":
    st.subheader("Detection Test")

    detection_target = st.radio(
        "What do you want to inspect?",
        [
            "Entire catalog / category page",
            "Single product / appliance",
        ],
        horizontal=True,
        key="detection_target_mode",
    )
    single_product_mode = detection_target.startswith(
        "Single"
    )

    if single_product_mode:
        st.write(
            "Paste one product-detail URL. FBF will identify the appliance, "
            "analyze only its hero image, and judge it against its category/profile "
            "standards plus the real normalizer correction it would receive."
        )
    else:
        st.write(
            "Paste a catalog/category URL. FBF finds product images, compares "
            "compatible products, and flags inconsistent scale or alignment."
        )

    d1, d2 = st.columns([2, 1])
    with d1:
        page_url = st.text_input(
            (
                "Product URL"
                if single_product_mode
                else "Page URL"
            ),
            placeholder=(
                "https://example.com/products/model-name"
                if single_product_mode
                else "https://example.com/coffee-machines"
            ),
            key="detection_url",
        )
    with d2:
        detection_category = st.selectbox(
            (
                "Product category override"
                if single_product_mode
                else "Audit category gate"
            ),
            categories,
            index=categories.index(default_category),
            key="detection_category",
            help=(
                "For a single product, Auto identifies the appliance from its name/URL. "
                "Choose a category only if you want to force the technical profile."
                if single_product_mode
                else
                "This controls cross-category filtering, not the product's own classification. "
                "Use Auto for mixed pages such as Home Appliances or Small Appliances."
            ),
        )

    if single_product_mode:
        c1, c2 = st.columns(2)
        max_scan = 1
        with c1:
            scale_tolerance = st.slider(
                "Single-product scale tolerance",
                0.08,
                0.35,
                0.18,
                0.01,
                help=(
                    "Controls how far the product may deviate from its canonical "
                    "category/profile framing before review."
                ),
            )
        with c2:
            alignment_tolerance = st.slider(
                "Single-product alignment tolerance",
                0.02,
                0.12,
                0.055,
                0.005,
                help="Controls accepted alignment drift for this individual product.",
            )
    else:
        c1, c2, c3 = st.columns(3)
        with c1:
            max_scan = st.slider(
                "Maximum products to scan",
                8,
                60,
                32,
                4,
            )
        with c2:
            scale_tolerance = st.slider(
                "Allowed scale difference",
                0.08,
                0.35,
                0.18,
                0.01,
                help="A product outside this percentage of the family median is flagged.",
            )
        with c3:
            alignment_tolerance = st.slider(
                "Allowed alignment drift",
                0.02,
                0.12,
                0.055,
                0.005,
                help="Relative horizontal/vertical alignment tolerance.",
            )

    scan_clicked = st.button(
        (
            "🔎 Inspect single product"
            if single_product_mode
            else "🔎 Scan page"
        ),
        type="primary",
        disabled=not bool(page_url),
    )

    if scan_clicked:
        category_value = (
            None
            if detection_category == "Auto by page / shape"
            else detection_category
        )
        try:
            spinner_text = (
                "Inspecting the product and analyzing its framing..."
                if single_product_mode
                else "Scouting the page and analyzing product framing..."
            )

            with st.spinner(spinner_text):
                detection_scan_config = {
                    "url": page_url,
                    "category": category_value,
                    "segmentation_mode": segmentation_mode,
                    "max_items": max_scan,
                    "scale_tolerance": scale_tolerance,
                    "alignment_tolerance": alignment_tolerance,
                    "normalization_mode": normalization_mode,
                    "scan_mode": (
                        "single_product"
                        if single_product_mode
                        else "page"
                    ),
                }

                if single_product_mode:
                    result = scan_single_product(
                        page_url,
                        category=category_value,
                        segmentation_mode=segmentation_mode,
                        scale_tolerance=scale_tolerance,
                        alignment_tolerance=alignment_tolerance,
                        normalization_mode=normalization_mode,
                    )
                else:
                    previous_scan = st.session_state.get(
                        "detection_result",
                        {
                            "url": page_url,
                        },
                    )
                    overrides = get_filter_overrides_for_scan(
                        previous_scan
                    )

                    result = scan_page(
                        page_url,
                        category=category_value,
                        segmentation_mode=segmentation_mode,
                        max_items=max_scan,
                        scale_tolerance=scale_tolerance,
                        alignment_tolerance=alignment_tolerance,
                        forced_include_keys=set(
                            overrides["include_keys"]
                        ),
                        item_category_overrides=dict(
                            overrides["category_overrides"]
                        ),
                        normalization_mode=normalization_mode,
                    )

                st.session_state[
                    "detection_result"
                ] = result
                st.session_state[
                    "detection_scan_config"
                ] = detection_scan_config
                st.session_state.pop(
                    "showcase_items",
                    None,
                )
                st.session_state.pop(
                    "showcase_cache_key",
                    None,
                )
                st.session_state.pop(
                    "manual_overrides",
                    None,
                )
        except Exception as exc:
            st.error(
                f"Detection failed: {exc}"
            )
            if st.session_state.get("detection_result"):
                st.info(
                    "The previous successful Detection result was preserved because this retry failed."
                )

    scan = st.session_state.get("detection_result")

    if scan:
        st.divider()

        if scan.get("scan_mode") == "single_product":
            item0 = scan["items"][0] if scan.get("items") else {}
            a, b, c, d = st.columns(4)
            a.metric(
                "Product identified",
                "1" if item0 else "0",
            )
            b.metric(
                "Detected category",
                item0.get(
                    "item_category",
                    "Unknown",
                )
                or "Unknown",
            )
            c.metric(
                "Result",
                (
                    "Needs review"
                    if scan.get(
                        "flagged_count",
                        0,
                    )
                    else "Pass"
                ),
            )
            d.metric(
                "Framing score",
                f"{scan.get('page_score', 0):.0f}/100",
            )

            st.caption(
                "Single-product mode does not use a page category gate or peer median. "
                "It evaluates the product against its category/profile standard and "
                "checks whether the normalizer itself would make a visible correction."
            )
            show_filtered_detection = False
        else:
            a, b, c, d, e = st.columns(5)
            a.metric(
                "Scouted cards",
                scan.get(
                    "raw_candidate_count",
                    scan["candidate_count"],
                ),
            )
            b.metric(
                "Category-matched",
                scan["candidate_count"],
            )
            c.metric(
                "Filtered off-category",
                scan.get(
                    "filtered_out_count",
                    0,
                ),
            )
            d.metric(
                "Needs review",
                scan["flagged_count"],
            )
            page_score = scan.get("page_score")
            e.metric(
                "Consistency pass rate",
                (
                    f"{page_score:.0f}%"
                    if page_score is not None
                    else "N/A"
                ),
            )
            if page_score is None:
                st.warning(
                    "No products were successfully analyzed, so this page cannot receive a pass score."
                )
            elif scan.get("failed_count", 0):
                st.caption(
                    f"Partial analysis: {scan.get('failed_count', 0)} product(s) failed and were excluded from the score."
                )

            effective_category = scan.get(
                "effective_category"
            )
            category_gate_mode_value = scan.get(
                "category_gate_mode",
                "auto_listing_context",
            )

            if effective_category:
                gate_origin = (
                    "selected manually"
                    if category_gate_mode_value == "manual"
                    else "inferred from listing URL/title/H1/breadcrumbs"
                )

                st.caption(
                    f"Audit category gate: **{effective_category}** ({gate_origin}). "
                    "This is an audit filtering rule, not the website's actual collection/category."
                )
            else:
                st.caption(
                    "Audit category gate: **Mixed / disabled**. Products are classified "
                    "individually because this listing does not resolve to one appliance family."
                )

            show_filtered_detection = st.toggle(
                (
                    "🚫 Show filtered-out products "
                    f"({scan.get('filtered_out_count', 0)})"
                ),
                value=False,
                key="detection_show_filtered",
                help=(
                    "Show the products removed by the category gate, "
                    "why they were skipped, and manual include controls."
                ),
            )

            if show_filtered_detection:
                render_filtered_product_inspector(
                    scan,
                    key_prefix="detection",
                    expanded=True,
                    max_items=100,
                    scan_config=st.session_state.get(
                        "detection_scan_config"
                    ),
                    result_target="detection",
                )
            elif scan.get(
                "excluded_items"
            ):
                st.caption(
                    "Filtered products are hidden. Turn on "
                    "**Show filtered-out products** above, or open "
                    "the **🚫 Filtered Out Images** tab."
                )

        if scan.get("items"):
            st.markdown("**Quick export shortcut**")
            quick_export_a, quick_export_b = st.columns([1.25, 1.65])

            with quick_export_a:
                if st.button(
                    "🚀 Build fixed-page ZIP",
                    key="quick_build_page_export_top",
                    type="primary",
                    use_container_width=True,
                ):
                    progress = st.progress(0.0)
                    progress_text = st.empty()

                    def update_export_progress(done, total, product_name):
                        progress.progress(min(done / max(total, 1), 1.0))
                        progress_text.caption(
                            f"Processing {min(done + 1, total)}/{total}: {product_name}"
                            if done < total
                            else f"Finished processing {total} product(s)."
                        )

                    try:
                        export_bytes, export_summary = build_page_export_zip(
                            scan=scan,
                            detection_category=detection_category,
                            segmentation_mode=segmentation_mode,
                            canvas_size=canvas_size,
                            min_padding=min_padding,
                            output_format=output_format,
                            flagged_only=False,
                            strict_mode=precision_mode.startswith("Strict"),
                            normalization_mode=normalization_mode,
                            fine_tune=fine_tune,
                            manual_overrides=st.session_state.get("manual_overrides", {}),
                            progress_callback=update_export_progress,
                        )
                        st.session_state["page_export_zip"] = export_bytes
                        st.session_state["page_export_summary"] = export_summary
                        st.session_state["page_export_scope"] = "All analyzed products"
                        progress.progress(1.0)
                    except Exception as exc:
                        st.session_state.pop("page_export_zip", None)
                        st.session_state.pop("page_export_summary", None)
                        st.error(f"Quick export failed: {exc}")

            with quick_export_b:
                if st.session_state.get("page_export_zip"):
                    st.download_button(
                        "⬇️ Download fixed-page ZIP",
                        data=st.session_state["page_export_zip"],
                        file_name="page_normalization_export.zip",
                        mime="application/zip",
                        key="quick_download_page_export_top",
                        use_container_width=True,
                    )
                else:
                    st.caption(
                        "Build a ZIP here, or use the full **One-click page automation** "
                        "section lower on the page."
                    )

        if precision_mode.startswith("Strict"):
            overrides_map = build_page_profile_overrides(scan, True)
            if overrides_map:
                with st.expander("Strict mode page calibration preview"):
                    for profile_name, override in overrides_map.items():
                        st.write(f"**{profile_name.title()}** → {override}")
        if scan["analyzed_count"] == 0:
            st.error(
                "No usable product images could be analyzed. The page may be rendered entirely "
                "with JavaScript, protected by authentication/anti-bot rules, or use imagery the "
                "current scraper could not identify."
            )
        else:
            st.caption(
                "V6.1 compares the primary appliance body inside compatible structural subgroups, "
                "filters obvious off-category product cards, and pixel-locks corrected body anchors "
                "so source-size rounding does not create 1–2 px alignment drift."
            )

            show_all = st.toggle("Show products that passed too", value=False)
            display_items = [
                x for x in scan["items"] if show_all or x["status"] != "pass"
            ]

            if not display_items:
                st.success("No scale/alignment outliers were detected with the current tolerances. ✅")

            for i, item in enumerate(display_items):
                status_icon = "✅" if item["status"] == "pass" else "⚠️"
                with st.container(border=True):
                    img_col, info_col = st.columns([1, 2.2])
                    with img_col:
                        st.image(item["image_bytes"], use_container_width=True)
                    with info_col:
                        st.markdown(f"### {status_icon} {item['name']}")
                        m = item["metrics"]
                        scale_axis_name = (
                            "Height occupancy"
                            if "target_height" in PROFILES[item["profile"]]
                            else "Width occupancy"
                        )
                        scale_axis_value = (
                            m["height_occupancy"]
                            if "target_height" in PROFILES[item["profile"]]
                            else m["width_occupancy"]
                        )

                        q1, q2, q3, q4, q5 = st.columns(5)
                        q1.metric("Profile", item["profile"].title())
                        q2.metric("Subtype", item.get("subtype", "—").replace("_", " ").title())
                        q3.metric("Body occupancy", f"{scale_axis_value:.1%}")
                        q4.metric(
                            (
                                "Normalizer target"
                                if scan.get("scan_mode") == "single_product"
                                else "Vs. subgroup"
                            ),
                            (
                                f"{item.get('normalizer_recommended_scale',1.0):.2f}×"
                                if scan.get("scan_mode") == "single_product"
                                else f"{item['scale_ratio']:.2f}×"
                            ),
                        )
                        q5.metric(
                            (
                                "Framing score"
                                if scan.get("scan_mode") == "single_product"
                                else "Consistency"
                            ),
                            f"{item['consistency_score']:.0f}/100",
                        )

                        if item["issues"]:
                            for issue in item["issues"]:
                                st.warning(issue)
                        else:
                            st.success(
                                (
                                    "Framing is within the individual product standard."
                                    if scan.get("scan_mode") == "single_product"
                                    else "Framing is consistent with comparable products."
                                )
                            )

                        st.caption(
                            f"Primary-body center offset: {m['center_offset_x']:.1%} · "
                            f"Accessory/secondary foreground: {m.get('accessory_area_ratio', 0):.1%} · "
                            f"Components: {m.get('component_count', 1)} · "
                            f"Full-extent edge padding: {m.get('min_edge_padding', 0):.1%}"
                        )

                        if item.get("product_url"):
                            st.link_button("Open product page", item["product_url"])

                        # Let the user immediately prove the detector-to-fixer loop.
                        with st.expander("Preview how the normalizer would fix this image"):
                            try:
                                preview_category = normalization_category_for_item(
                                    item,
                                    scan,
                                    detection_category,
                                )
                                preview_overrides = build_page_profile_overrides(
                                    scan, precision_mode.startswith("Strict")
                                )
                                manual = manual_override_for(
                                    item,
                                    st.session_state.get("manual_overrides", {}),
                                )
                                preview = process_image_bytes(
                                    item["image_bytes"],
                                    category=preview_category,
                                    canvas_size=canvas_size,
                                    segmentation_mode=segmentation_mode,
                                    min_padding=min_padding,
                                    profile_overrides=get_profile_override_for_item(item, preview_overrides),
                                    adjustments=merged_adjustments(fine_tune, manual),
                                    body_mode=manual.get("body_mode", "auto"),
                                    anchor_mode=manual.get("anchor_mode", "auto"),
                                    normalization_mode=normalization_mode,
                                )
                                before, after = st.columns(2)
                                with before:
                                    st.caption("Current")
                                    st.image(item["image_bytes"], use_container_width=True)
                                with after:
                                    st.caption("Normalized preview")
                                    st.image(preview["image"], use_container_width=True)
                            except Exception as exc:
                                st.error(f"Preview failed: {exc}")


            st.divider()
            st.subheader("⚡ One-click page automation")
            st.write(
                "Turn the scanned page into a ready-to-use export package. "
                "The app will normalize the selected products automatically, create a "
                "product-named subfolder for every perfected image, and generate a flat "
                "folder of side-by-side comparisons."
            )

            export_scope = st.radio(
                "What should be exported?",
                ["All analyzed products", "Only products that need review"],
                horizontal=True,
                index=0,
                key="bulk_export_scope",
            )
            flagged_only = export_scope.startswith("Only")

            estimated_count = sum(
                1 for x in scan["items"]
                if (not flagged_only) or x["status"] != "pass"
            )
            st.caption(
                f"{estimated_count} product(s) will be processed. Product names are preserved "
                "as folder/file names; characters that Windows does not allow are replaced with underscores."
            )

            if st.button(
                "🚀 Normalize & build export package",
                type="primary",
                key="build_page_export",
                disabled=estimated_count == 0,
            ):
                progress = st.progress(0.0)
                progress_text = st.empty()

                def update_export_progress(done, total, product_name):
                    progress.progress(min(done / max(total, 1), 1.0))
                    progress_text.caption(
                        f"Processing {min(done + 1, total)}/{total}: {product_name}"
                        if done < total
                        else f"Finished processing {total} product(s)."
                    )

                try:
                    export_bytes, export_summary = build_page_export_zip(
                        scan=scan,
                        detection_category=detection_category,
                        segmentation_mode=segmentation_mode,
                        canvas_size=canvas_size,
                        min_padding=min_padding,
                        output_format=output_format,
                        flagged_only=flagged_only,
                        strict_mode=precision_mode.startswith("Strict"),
                        normalization_mode=normalization_mode,
                        fine_tune=fine_tune,
                        manual_overrides=st.session_state.get("manual_overrides", {}),
                        progress_callback=update_export_progress,
                    )
                    st.session_state["page_export_zip"] = export_bytes
                    st.session_state["page_export_summary"] = export_summary
                    st.session_state["page_export_scope"] = export_scope
                    progress.progress(1.0)
                except Exception as exc:
                    st.session_state.pop("page_export_zip", None)
                    st.session_state.pop("page_export_summary", None)
                    st.error(f"Bulk export failed: {exc}")

            if st.session_state.get("page_export_zip"):
                summary = st.session_state.get("page_export_summary", {})
                if summary.get("failures"):
                    st.warning(
                        f"Package built with {summary.get('successes', 0)} successful product(s) "
                        f"and {len(summary['failures'])} failure(s). See export_failures.txt inside the ZIP."
                    )
                else:
                    st.success(
                        f"Package ready: {summary.get('successes', 0)} product(s) normalized successfully. ✅"
                    )

                st.code(
                    "page_normalization_export/\n"
                    "├── perfected_images/\n"
                    "│   ├── Product Name A/\n"
                    "│   │   └── Product Name A.png\n"
                    "│   └── Product Name B/\n"
                    "│       └── Product Name B.png\n"
                    "├── side_by_side_previews/\n"
                    "│   ├── Product Name A.jpg\n"
                    "│   └── Product Name B.jpg\n"
                    "└── export_manifest.csv",
                    language="text",
                )

                st.download_button(
                    "⬇️ Download complete page export",
                    data=st.session_state["page_export_zip"],
                    file_name="page_normalization_export.zip",
                    mime="application/zip",
                    type="primary",
                    key="download_page_export",
                )

            # CSV report
            report = io.StringIO()
            writer = csv.writer(report)
            writer.writerow(
                [
                    "name",
                    "image_url",
                    "product_url",
                    "profile",
                    "subtype",
                    "comparison_group",
                    "status",
                    "consistency_score",
                    "scale_ratio_to_family_median",
                    "height_occupancy",
                    "width_occupancy",
                    "horizontal_center_offset",
                    "top_padding",
                    "bottom_padding",
                    "accessory_area_ratio",
                    "component_count",
                    "issues",
                ]
            )
            for item in scan["items"]:
                m = item["metrics"]
                writer.writerow(
                    [
                        item["name"],
                        item["image_url"],
                        item.get("product_url") or "",
                        item["profile"],
                        item.get("subtype", ""),
                        item.get("comparison_group", ""),
                        item["status"],
                        f"{item['consistency_score']:.2f}",
                        f"{item['scale_ratio']:.6f}",
                        f"{m['height_occupancy']:.6f}",
                        f"{m['width_occupancy']:.6f}",
                        f"{m['center_offset_x']:.6f}",
                        f"{m['top_padding']:.6f}",
                        f"{m['bottom_padding']:.6f}",
                        f"{m.get('accessory_area_ratio',0):.6f}",
                        m.get("component_count", 1),
                        " | ".join(item["issues"]),
                    ]
                )

            st.download_button(
                "Download detection report (CSV)",
                data=report.getvalue().encode("utf-8"),
                file_name="page_detection_report.csv",
                mime="text/csv",
            )

            if scan["failures"]:
                with st.expander(f"{len(scan['failures'])} images could not be analyzed"):
                    for failure in scan["failures"][:20]:
                        st.write(f"**{failure['name']}** — {failure['error']}")

    with st.expander("What Detection Test is checking"):
        st.markdown(
            """
- **Scale consistency:** Is the product much smaller/larger than comparable items on the page?
- **Horizontal alignment:** Is the object shifted noticeably left or right?
- **Vertical alignment:** Do similar products share a comparable baseline or center?
- **Edge safety:** Is the product touching or nearly touching the source image boundary?
- **Segmentation confidence check:** Does foreground detection appear suspicious?

The detector uses page-relative statistics rather than assuming a refrigerator and a hob
should occupy the same physical dimensions.
            """
        )
        st.caption(
            "Current MVP limitation: the URL scanner reads server-rendered HTML. Pages whose product "
            "cards only appear after JavaScript execution, or pages behind login/anti-bot protection, "
            "may require a browser-rendering connector in a later version."
        )




if active_workspace == "🧪 Advanced Audit":
    st.subheader(
        "🧪 Advanced Multi-Page / Multi-Category Audit"
    )
    st.write(
        "Give V6.5 one category/subcategory link and it can automatically "
        "discover the server-rendered pagination for that listing, audit every "
        "discovered page, show the problem products page-by-page, and add the "
        "whole crawl to your persistent master CSV."
    )

    st.info(
        "Example: paste **Coffee Machines → Espresso → Page 1**. "
        "If the site's pagination exposes Pages 2, 3 and 4, V6.5.3 can discover "
        "and audit them automatically. **Strict Page Membership** is now always "
        "used: only products verified inside that exact page's catalog listing "
        "are accepted. Structured data, related products, recommendations, "
        "recently viewed items and sliders cannot inject extra products into a page."
    )

    a1, a2 = st.columns(
        [1.55, 1]
    )

    with a1:
        advanced_url = st.text_input(
            "Starting page URL",
            placeholder=(
                "https://example.com/"
                "coffee-machines/espresso"
            ),
            key="advanced_audit_url",
        )

    with a2:
        advanced_detection_category = (
            st.selectbox(
                "Detection category",
                categories,
                index=categories.index(
                    default_category
                ),
                key=(
                    "advanced_detection_category"
                ),
                help=(
                    "Controls category filtering "
                    "and appliance geometry. "
                    "Auto uses the page context."
                ),
            )
        )

    h1, h2 = st.columns(2)

    with h1:
        advanced_main_category = (
            st.text_input(
                "Master CSV category",
                placeholder=(
                    "Coffee Machines / ACs / "
                    "Refrigerators"
                ),
                key="advanced_main_category",
            )
        )

    with h2:
        advanced_subcategory = (
            st.text_input(
                "Subcategory",
                placeholder=(
                    "Espresso / Automatic / "
                    "Office / Home / Capsule"
                ),
                key="advanced_subcategory",
            )
        )

    crawl_col, page_col = st.columns(
        [1, 1]
    )

    with crawl_col:
        crawl_all_pages = st.toggle(
            "Automatically discover all pagination pages",
            value=True,
            key="advanced_crawl_all_pages",
        )

    with page_col:
        if crawl_all_pages:
            max_pagination_pages = (
                st.slider(
                    "Maximum pages to crawl",
                    1,
                    50,
                    15,
                    1,
                    key=(
                        "advanced_max_pages"
                    ),
                )
            )
            advanced_page_label = "Page"
        else:
            max_pagination_pages = 1
            advanced_page_label = (
                st.text_input(
                    "Page label",
                    value="Page 1",
                    key=(
                        "advanced_page_label"
                    ),
                )
            )

    c1, c2, c3 = st.columns(3)

    with c1:
        advanced_max_scan = st.slider(
            "Maximum products per page",
            8,
            100,
            40,
            4,
            key="advanced_max_scan",
        )

    with c2:
        advanced_scale_tolerance = (
            st.slider(
                "Scale tolerance",
                0.06,
                0.35,
                0.16,
                0.01,
                key=(
                    "advanced_scale_tolerance"
                ),
            )
        )

    with c3:
        advanced_alignment_tolerance = (
            st.slider(
                "Alignment tolerance",
                0.015,
                0.12,
                0.045,
                0.005,
                key=(
                    "advanced_alignment_tolerance"
                ),
            )
        )

    advanced_scan_button = st.button(
        (
            "🧪 Discover pages & run full audit"
            if crawl_all_pages
            else "🧪 Run advanced audit"
        ),
        type="primary",
        disabled=not bool(
            advanced_url
        ),
        key="run_advanced_audit",
    )

    if advanced_scan_button:
        scan_category = (
            None
            if advanced_detection_category
            == "Auto by page / shape"
            else advanced_detection_category
        )

        try:
            if crawl_all_pages:
                with st.spinner(
                    "Discovering pagination..."
                ):
                    discovered_pages = (
                        discover_pagination_urls(
                            advanced_url,
                            max_pages=(
                                max_pagination_pages
                            ),
                        )
                    )
            else:
                discovered_pages = [
                    {
                        "url": advanced_url,
                        "page_number": 1,
                        "page_label": (
                            advanced_page_label.strip()
                            or "Page 1"
                        ),
                    }
                ]

            progress = st.progress(
                0.0
            )
            progress_text = st.empty()

            audit_pages = []
            total_pages = max(
                len(discovered_pages),
                1,
            )

            for index, page_info in enumerate(
                discovered_pages,
                start=1,
            ):
                label = (
                    page_info.get(
                        "page_label"
                    )
                    or f"Page {index}"
                )

                progress.progress(
                    (index - 1)
                    / total_pages
                )
                progress_text.caption(
                    f"Auditing {label} "
                    f"({index}/{total_pages})"
                )

                page_scan_config = {
                    "url": page_info["url"],
                    "category": scan_category,
                    "segmentation_mode": segmentation_mode,
                    "max_items": advanced_max_scan,
                    "scale_tolerance": advanced_scale_tolerance,
                    "alignment_tolerance": advanced_alignment_tolerance,
                    "normalization_mode": normalization_mode,
                }

                page_override_probe = {
                    "url": page_info["url"],
                }
                page_overrides = get_filter_overrides_for_scan(
                    page_override_probe
                )

                scan_result = scan_page(
                    page_info["url"],
                    category=scan_category,
                    segmentation_mode=(
                        segmentation_mode
                    ),
                    max_items=(
                        advanced_max_scan
                    ),
                    scale_tolerance=(
                        advanced_scale_tolerance
                    ),
                    alignment_tolerance=(
                        advanced_alignment_tolerance
                    ),
                    forced_include_keys=set(
                        page_overrides[
                            "include_keys"
                        ]
                    ),
                    item_category_overrides=dict(
                        page_overrides[
                            "category_overrides"
                        ]
                    ),
                    normalization_mode=normalization_mode,
                )

                audit_pages.append(
                    {
                        "page_label": label,
                        "page_number": (
                            page_info.get(
                                "page_number"
                            )
                            or index
                        ),
                        "url": (
                            page_info["url"]
                        ),
                        "scan": scan_result,
                        "scan_config": page_scan_config,
                    }
                )

            progress.progress(1.0)
            progress_text.empty()

            first_scan = (
                audit_pages[0]["scan"]
                if audit_pages
                else None
            )

            resolved_main_category = (
                resolved_audit_main_category(
                    advanced_main_category,
                    advanced_detection_category,
                    first_scan,
                )
            )

            st.session_state[
                "advanced_audit_pages"
            ] = audit_pages
            st.session_state[
                "advanced_audit_context"
            ] = {
                "main_category": (
                    resolved_main_category
                ),
                "subcategory": (
                    advanced_subcategory.strip()
                    or "General"
                ),
                "detection_category": (
                    advanced_detection_category
                ),
                "starting_url": (
                    advanced_url
                ),
                "crawl_all_pages": (
                    crawl_all_pages
                ),
            }

            # A new crawl invalidates any previously generated media handoff ZIP.
            st.session_state.pop(
                "advanced_image_review_zip",
                None,
            )
            st.session_state.pop(
                "advanced_image_review_summary",
                None,
            )
            st.session_state.pop(
                "advanced_simple_csv_snapshot",
                None,
            )
            st.session_state.pop(
                "advanced_page_csv_snapshot",
                None,
            )
            st.session_state.pop(
                "advanced_csv_snapshot_summary",
                None,
            )

        except Exception as exc:
            st.error(
                f"Advanced audit failed: {exc}"
            )
            if st.session_state.get("advanced_audit_pages"):
                st.info(
                    "The previous successful Advanced Audit was preserved because this retry failed."
                )

    audit_pages = st.session_state.get(
        "advanced_audit_pages",
        [],
    )
    advanced_context = (
        st.session_state.get(
            "advanced_audit_context",
            {},
        )
    )

    if audit_pages:
        st.divider()

        summary = audit_pages_summary(
            audit_pages
        )

        m1, m2, m3, m4, m5 = (
            st.columns(5)
        )
        m1.metric(
            "Pages audited",
            summary["pages"],
        )
        m2.metric(
            "Scouted cards",
            summary["scouted"],
        )
        m3.metric(
            "Category-matched",
            summary["matched"],
        )
        m4.metric(
            "Filtered off-category",
            summary["filtered"],
        )
        m5.metric(
            "Products with issues",
            summary["issues"],
        )

        hierarchy_text = (
            f"{advanced_context.get('main_category','Uncategorized')} "
            f"→ {advanced_context.get('subcategory','General')} "
            f"→ {len(audit_pages)} page(s)"
        )

        st.success(
            f"Audit location: **{hierarchy_text}**"
        )

        st.caption(
            "Discovered pages: "
            + " · ".join(
                (
                    f"{page['page_label']} "
                    f"({page['url']})"
                )
                for page in audit_pages
            )
        )


        verified_total = sum(
            page["scan"].get(
                "dom_verified_count",
                page["scan"].get("candidate_count", 0),
            )
            for page in audit_pages
        )
        structured_only_total = sum(
            max(
                0,
                page["scan"].get("jsonld_discovered_count", 0)
                - page["scan"].get("dom_verified_count", 0),
            )
            for page in audit_pages
        )

        recovered_total = sum(
            page["scan"].get(
                "recovered_membership_count",
                0,
            )
            for page in audit_pages
        )

        st.caption(
            f"✅ Page membership: {verified_total} verified listing candidate(s), "
            f"including {recovered_total} legitimate product(s) recovered from "
            f"non-standard catalog markup. Structured-data-only, related and "
            f"recommended products still cannot create audit entries."
        )

        display_toggle_col1, display_toggle_col2 = st.columns(
            2
        )

        with display_toggle_col1:
            show_passing_advanced = st.toggle(
                "Show passing products too",
                value=False,
                key=(
                    "advanced_show_passing"
                ),
            )

        filtered_total_advanced = sum(
            len(
                page.get(
                    "scan",
                    {},
                ).get(
                    "excluded_items",
                    [],
                )
                or []
            )
            for page in audit_pages
        )

        with display_toggle_col2:
            show_filtered_advanced = st.toggle(
                (
                    "🚫 Show filtered-out products "
                    f"({filtered_total_advanced})"
                ),
                value=False,
                key="advanced_show_filtered",
                help=(
                    "Show products removed by the category gate "
                    "under each audit page, with manual include/edit controls."
                ),
            )

        if filtered_total_advanced:
            st.caption(
                "You can also review all filtered products in the "
                "**🚫 Filtered Out Images** tab."
            )

        st.subheader(
            "Problem products by page"
        )

        for advanced_page_index, page in enumerate(
            audit_pages
        ):
            scan_result = page["scan"]
            page_items = [
                item
                for item in scan_result.get(
                    "items",
                    [],
                )
                if (
                    show_passing_advanced
                    or item.get(
                        "status"
                    )
                    != "pass"
                    or bool(
                        item.get(
                            "operator_decision"
                        )
                    )
                )
            ]

            with st.expander(
                (
                    f"{page['page_label']} — "
                    f"{len(page_items)} shown / "
                    f"{scan_result.get('candidate_count',0)} matched"
                ),
                expanded=(
                    len(audit_pages) == 1
                ),
            ):
                st.caption(
                    page["url"]
                )

                if not page_items:
                    st.success(
                        "No product-image problems "
                        "detected on this page. ✅"
                    )

                for item in page_items:
                    with st.container(
                        border=True
                    ):
                        image_col, detail_col = (
                            st.columns(
                                [1, 2.3]
                            )
                        )

                        with image_col:
                            st.image(
                                item[
                                    "image_bytes"
                                ],
                                use_container_width=(
                                    True
                                ),
                            )

                        with detail_col:
                            severity = issue_level(
                                item
                            )
                            severity_icon = {
                                "High": "🔴",
                                "Medium": "🟠",
                                "Low": "🟡",
                                "Manual review": (
                                    "🟣"
                                ),
                                "Pass": "🟢",
                            }.get(
                                severity,
                                "⚠️",
                            )

                            st.markdown(
                                f"### {severity_icon} "
                                f"{item['name']}"
                            )

                            q1, q2, q3, q4 = (
                                st.columns(4)
                            )
                            q1.metric(
                                "Severity",
                                severity,
                            )
                            q2.metric(
                                "Consistency",
                                (
                                    f"{item.get('consistency_score',0):.0f}/100"
                                ),
                            )
                            q3.metric(
                                "Profile",
                                item.get(
                                    "profile",
                                    "—",
                                ).title(),
                            )
                            q4.metric(
                                "Subtype",
                                item.get(
                                    "subtype",
                                    "—",
                                ).replace(
                                    "_",
                                    " ",
                                ).title(),
                            )

                            operator_decision = (
                                item.get(
                                    "operator_decision"
                                )
                                or ""
                            )

                            if operator_decision:
                                st.success(
                                    "Manual Page Showcase decision: "
                                    + operator_decision_label(
                                        item
                                    )
                                )

                            issues = (
                                automatic_issues_for_item(
                                    item
                                )
                                if operator_decision
                                else (
                                    item.get(
                                        "issues"
                                    )
                                    or []
                                )
                            )

                            if issues:
                                st.markdown(
                                    "**Detected problems**"
                                )

                                for issue in issues:
                                    st.warning(
                                        issue
                                    )

                                st.markdown(
                                    "**Suggested correction**"
                                )
                                st.write(
                                    suggested_fix_from_issues(
                                        issues
                                    )
                                )
                            else:
                                st.success(
                                    "No image-quality issue detected."
                                )

                            metrics = (
                                item.get(
                                    "metrics",
                                    {},
                                )
                            )

                            st.caption(
                                (
                                    f"Body H "
                                    f"{metrics.get('body_height_occupancy',metrics.get('height_occupancy',0)):.1%} · "
                                    f"Body W "
                                    f"{metrics.get('body_width_occupancy',metrics.get('width_occupancy',0)):.1%} · "
                                    f"Center offset "
                                    f"{metrics.get('center_offset_x',0):.1%} · "
                                    f"Full edge padding "
                                    f"{metrics.get('min_edge_padding',0):.1%} · "
                                    f"Canonical size "
                                    f"{item.get('canonical_scale_ratio',1.0):.0%} · "
                                    f"Normalizer scale "
                                    f"{item.get('normalizer_recommended_scale',1.0):.0%}"
                                )
                            )

                            if item.get(
                                "product_url"
                            ):
                                st.link_button(
                                    "🔗 Open exact product page",
                                    item[
                                        "product_url"
                                    ],
                                )

                            item_key = product_override_key(
                                item
                            )
                            is_complete_rework = (
                                operator_decision
                                == "complete_rework"
                            )

                            if is_complete_rework:
                                st.error(
                                    "🛑 Complete rework required — original and generated fix are not acceptable."
                                )

                            st.button(
                                (
                                    "↺ Undo complete rework"
                                    if is_complete_rework
                                    else "🛑 Mark complete rework"
                                ),
                                key=(
                                    "advanced_complete_rework_"
                                    + str(
                                        abs(
                                            hash(
                                                page.get(
                                                    "page_label",
                                                    "Page",
                                                )
                                                + item_key
                                            )
                                        )
                                    )
                                ),
                                on_click=(
                                    _undo_complete_rework
                                    if is_complete_rework
                                    else _mark_complete_rework
                                ),
                                args=(
                                    item_key,
                                ),
                                help=(
                                    "Marks this source as unusable and requiring a completely new/reworked image."
                                ),
                            )

                if show_filtered_advanced:
                    render_filtered_product_inspector(
                        scan_result,
                        key_prefix=(
                            "advanced_"
                            + re.sub(
                                r"[^A-Za-z0-9_-]+",
                                "_",
                                page.get(
                                    "page_label",
                                    "page",
                                ),
                            )
                        ),
                        expanded=True,
                        max_items=100,
                        scan_config=page.get(
                            "scan_config"
                        ),
                        result_target="advanced",
                        advanced_page_index=advanced_page_index,
                    )

        st.divider()
        st.subheader(
            "🗂️ Save this crawl"
        )

        audit_scope_label = st.radio(
            "Rows / visuals to record",
            [
                "Only products with problems",
                "All products including passes",
            ],
            horizontal=True,
            index=0,
            key="advanced_audit_scope",
        )

        include_passed_audit = (
            audit_scope_label.startswith(
                "All"
            )
        )

        crawl_rows = advanced_page_rows(
            audit_pages,
            advanced_context,
            include_passed=(
                include_passed_audit
            ),
            manual_overrides=(
                st.session_state.get(
                    "manual_overrides",
                    {},
                )
            ),
        )

        save_visual_media = st.toggle(
            "Save current + normalized + side-by-side images with the master audit",
            value=True,
            key="advanced_save_visual_media",
            help=(
                "Recommended. CSV cannot physically embed image binaries. "
                "V6.5 stores the actual Current / Normalized / Before-After files, "
                "and the Boss Visual Excel report embeds those images directly in the workbook."
            ),
        )

        st.markdown("### 📦 Advanced Audit downloads")
        st.caption(
            "Choose the format that matches the job: CSV for spreadsheet review, "
            "or the visual image package for production / re-upload work."
        )

        csv_download_tab, image_download_tab = st.tabs(
            [
                "📄 CSV / spreadsheet data",
                "🖼️ Images + issue data",
            ]
        )

        with csv_download_tab:
            @st.fragment
            def render_current_advanced_csv_downloads():
                st.info(
                    "Before downloading, click **Refresh CSV from current edits**. "
                    "It rebuilds the CSV snapshot from the latest Product Editor "
                    "overrides and final Original/Edited choices."
                )

                refresh_col, status_col = st.columns(
                    [1.15, 1.4]
                )

                with refresh_col:
                    refresh_csv = st.button(
                        "🔄 Refresh CSV from current edits",
                        type="primary",
                        key="refresh_advanced_csv_snapshot",
                        use_container_width=True,
                    )

                if refresh_csv:
                    refreshed_rows = advanced_page_rows(
                        st.session_state.get(
                            "advanced_audit_pages",
                            [],
                        ),
                        st.session_state.get(
                            "advanced_audit_context",
                            {},
                        ),
                        include_passed=(
                            st.session_state.get(
                                "advanced_audit_scope",
                                "Only products with problems",
                            ).startswith(
                                "All"
                            )
                        ),
                        manual_overrides=(
                            st.session_state.get(
                                "manual_overrides",
                                {},
                            )
                        ),
                    )

                    st.session_state[
                        "advanced_simple_csv_snapshot"
                    ] = simple_csv_bytes(
                        refreshed_rows
                    )
                    st.session_state[
                        "advanced_page_csv_snapshot"
                    ] = separate_page_csv_zip(
                        refreshed_rows
                    )
                    st.session_state[
                        "advanced_csv_snapshot_summary"
                    ] = {
                        "rows": len(
                            refreshed_rows
                        ),
                        "manual_edits": sum(
                            1
                            for row in refreshed_rows
                            if row.get(
                                "manual_edit_applied"
                            )
                            == "Yes"
                        ),
                        "manual_decisions": sum(
                            1
                            for row in refreshed_rows
                            if row.get(
                                "operator_decision"
                            )
                        ),
                    }

                snapshot_summary = (
                    st.session_state.get(
                        "advanced_csv_snapshot_summary"
                    )
                )

                with status_col:
                    if snapshot_summary:
                        st.success(
                            (
                                f"CSV refreshed · {snapshot_summary['rows']} row(s) · "
                                f"{snapshot_summary['manual_edits']} manual edit(s) · "
                                f"{snapshot_summary['manual_decisions']} final image decision(s)"
                            )
                        )
                    else:
                        st.warning(
                            "No refreshed CSV snapshot yet."
                        )

                simple_snapshot = (
                    st.session_state.get(
                        "advanced_simple_csv_snapshot"
                    )
                )
                page_snapshot = (
                    st.session_state.get(
                        "advanced_page_csv_snapshot"
                    )
                )

                export_col1, export_col2 = (
                    st.columns(2)
                )

                with export_col1:
                    if simple_snapshot:
                        st.download_button(
                            "⬇️ Download REFRESHED SIMPLE CSV",
                            data=simple_snapshot,
                            file_name=(
                                f"{advanced_context.get('main_category','Category')}_"
                                f"{advanced_context.get('subcategory','Subcategory')}_"
                                "BOSS_SIMPLE_AUDIT_REFRESHED.csv"
                            ),
                            mime="text/csv",
                            key=(
                                "download_advanced_crawl_csv_refreshed"
                            ),
                            use_container_width=True,
                        )
                    else:
                        st.button(
                            "⬇️ SIMPLE CSV — refresh first",
                            disabled=True,
                            key="advanced_csv_disabled_simple",
                            use_container_width=True,
                        )

                with export_col2:
                    if page_snapshot:
                        st.download_button(
                            "📦 Download REFRESHED page CSVs",
                            data=page_snapshot,
                            file_name=(
                                f"{advanced_context.get('main_category','Category')}_"
                                f"{advanced_context.get('subcategory','Subcategory')}_"
                                "page_csvs_REFRESHED.zip"
                            ),
                            mime="application/zip",
                            key=(
                                "download_advanced_crawl_separate_refreshed"
                            ),
                            use_container_width=True,
                        )
                    else:
                        st.button(
                            "📦 Page CSVs — refresh first",
                            disabled=True,
                            key="advanced_csv_disabled_pages",
                            use_container_width=True,
                        )

                st.caption(
                    "The refreshed Simple CSV now includes manual zoom, movement, crop/trim, "
                    "floor-line, body-mode and centering-mode columns."
                )

            render_current_advanced_csv_downloads()

        with image_download_tab:
            st.write(
                "Build a visual handoff ZIP containing the actual website image, "
                "the recommended fixed image, a before/after comparison, and TXT "
                "files explaining the issue and suggested correction."
            )

            st.code(
                """01_NEEDS_REVIEW/
  Page 1/
    Product Name/
      01_ORIGINAL.png
      02_FIXED.png
      03_BEFORE_AFTER.jpg
      issue_and_fix.txt
      fix_prompt.txt

02_PASSED/
03_ALL_PRODUCTS/
PACKAGE_MANIFEST.csv""",
                language="text",
            )

            image_pkg_col1, image_pkg_col2 = st.columns(
                [1.1, 1.35]
            )

            with image_pkg_col1:
                build_image_package = st.button(
                    "🖼️ Build images + issue data ZIP",
                    type="primary",
                    key="build_advanced_image_review_package",
                    disabled=not bool(
                        audit_pages
                    ),
                    use_container_width=True,
                    help=(
                        "Processes every audited product. Review products get a "
                        "new normalized image; passed products keep their original "
                        "as the recommended image."
                    ),
                )

            if build_image_package:
                package_progress = st.progress(
                    0.0
                )
                package_progress_text = (
                    st.empty()
                )

                def update_advanced_package_progress(
                    done,
                    total,
                    product_name,
                ):
                    package_progress.progress(
                        min(
                            done
                            / max(
                                total,
                                1,
                            ),
                            1.0,
                        )
                    )

                    if done < total:
                        package_progress_text.caption(
                            (
                                f"Packaging "
                                f"{min(done + 1,total)}/{total}: "
                                f"{product_name}"
                            )
                        )
                    else:
                        package_progress_text.caption(
                            (
                                f"Finished packaging "
                                f"{total} product(s)."
                            )
                        )

                try:
                    with st.spinner(
                        "Generating original / fixed / before-after images and issue files..."
                    ):
                        (
                            package_bytes,
                            package_summary,
                        ) = (
                            build_advanced_image_review_package(
                                audit_pages=(
                                    audit_pages
                                ),
                                context=(
                                    advanced_context
                                ),
                                segmentation_mode=(
                                    segmentation_mode
                                ),
                                canvas_size=(
                                    canvas_size
                                ),
                                min_padding=(
                                    min_padding
                                ),
                                strict_mode=(
                                    precision_mode.startswith(
                                        "Strict"
                                    )
                                ),
                                normalization_mode=(
                                    normalization_mode
                                ),
                                fine_tune=(
                                    fine_tune
                                ),
                                manual_overrides=(
                                    st.session_state.get(
                                        "manual_overrides",
                                        {},
                                    )
                                ),
                                progress_callback=(
                                    update_advanced_package_progress
                                ),
                            )
                        )

                    st.session_state[
                        "advanced_image_review_zip"
                    ] = package_bytes
                    st.session_state[
                        "advanced_image_review_summary"
                    ] = package_summary

                    package_progress.progress(
                        1.0
                    )

                except Exception as exc:
                    st.session_state.pop(
                        "advanced_image_review_zip",
                        None,
                    )
                    st.session_state.pop(
                        "advanced_image_review_summary",
                        None,
                    )
                    st.error(
                        (
                            "Image review package failed: "
                            f"{exc}"
                        )
                    )

            package_bytes = (
                st.session_state.get(
                    "advanced_image_review_zip"
                )
            )
            package_summary = (
                st.session_state.get(
                    "advanced_image_review_summary",
                    {},
                )
            )

            with image_pkg_col2:
                if package_bytes:
                    safe_main = safe_product_name(
                        advanced_context.get(
                            "main_category",
                            "Category",
                        )
                    )
                    safe_sub = safe_product_name(
                        advanced_context.get(
                            "subcategory",
                            "General",
                        )
                    )

                    st.download_button(
                        "⬇️ Download IMAGES + ISSUE DATA ZIP",
                        data=package_bytes,
                        file_name=(
                            f"{safe_main}_{safe_sub}_"
                            "ADVANCED_IMAGE_REVIEW.zip"
                        ),
                        mime="application/zip",
                        key="download_advanced_image_review_package",
                        type="primary",
                        use_container_width=True,
                    )

                    st.caption(
                        (
                            f"{package_summary.get('products',0)} products · "
                            f"{package_summary.get('needs_review',0)} need review · "
                            f"{package_summary.get('passed',0)} passed · "
                            f"{package_summary.get('complete_rework',0)} complete rework · "
                            f"{package_summary.get('generated_fixes',0)} fixes generated"
                        )
                    )

                    if package_summary.get(
                        "failures"
                    ):
                        st.warning(
                            (
                                f"{len(package_summary['failures'])} product(s) "
                                "could not generate a normalized fix. Their original "
                                "image and error report are still included."
                            )
                        )
                else:
                    st.info(
                        "Click **Build images + issue data ZIP** once after the audit. "
                        "The package is cached until you run a new Advanced Audit."
                    )

        master_update_mode_label = (
            st.selectbox(
                "When saving to the master CSV",
                [
                    "Replace these pages if they already exist",
                    "Append only new products",
                ],
                key=(
                    "master_update_mode"
                ),
            )
        )

        if st.button(
            "➕ Add / update full crawl in master audit",
            type="primary",
            key="save_advanced_to_master",
            disabled=not bool(
                crawl_rows
            ),
        ):
            rows_to_save = crawl_rows
            media_failures = []

            if save_visual_media:
                progress = st.progress(
                    0.0
                )

                with st.spinner(
                    "Generating persistent current / normalized / comparison images..."
                ):
                    (
                        rows_to_save,
                        media_failures,
                    ) = (
                        persist_advanced_visual_media(
                            audit_pages=(
                                audit_pages
                            ),
                            context=(
                                advanced_context
                            ),
                            include_passed=(
                                include_passed_audit
                            ),
                            segmentation_mode=(
                                segmentation_mode
                            ),
                            canvas_size=(
                                canvas_size
                            ),
                            min_padding=(
                                min_padding
                            ),
                            strict_mode=(
                                precision_mode.startswith(
                                    "Strict"
                                )
                            ),
                            normalization_mode=(
                                normalization_mode
                            ),
                            fine_tune=(
                                fine_tune
                            ),
                            manual_overrides=(
                                st.session_state.get(
                                    "manual_overrides",
                                    {},
                                )
                            ),
                        )
                    )

                progress.progress(
                    1.0
                )

            mode = (
                "replace_page"
                if master_update_mode_label.startswith(
                    "Replace"
                )
                else "append"
            )

            update_summary = (
                update_master_rows(
                    rows_to_save,
                    mode=mode,
                )
            )

            message = (
                f"Master audit updated: "
                f"{update_summary['added']} row(s) added, "
                f"{update_summary['removed']} old row(s) replaced. "
                f"Total rows: {update_summary['after']}."
            )

            if media_failures:
                message += (
                    f" {len(media_failures)} visual asset(s) failed; "
                    "their CSV image-path columns were left blank."
                )

            st.session_state[
                "master_audit_save_message"
            ] = message
            st.session_state[
                "advanced_media_failures"
            ] = media_failures
            st.rerun()

    save_message = (
        st.session_state.pop(
            "master_audit_save_message",
            None,
        )
    )

    if save_message:
        st.success(
            save_message
        )

    media_failures = (
        st.session_state.pop(
            "advanced_media_failures",
            [],
        )
    )

    if media_failures:
        with st.expander(
            f"Visual asset failures ({len(media_failures)})"
        ):
            for failure in media_failures:
                st.write(
                    failure
                )

    # ----------------------------------------------------------
    # Persistent master audit manager
    # ----------------------------------------------------------
    st.divider()
    st.subheader(
        "📚 Master website image audit"
    )

    master_rows = load_master_rows()
    tree = hierarchy_summary(
        master_rows
    )

    d1, d2, d3, d4 = (
        st.columns(4)
    )
    d1.metric(
        "Audit rows",
        len(master_rows),
    )
    d2.metric(
        "Main categories",
        len(tree),
    )
    d3.metric(
        "Subcategories",
        sum(
            len(subcats)
            for subcats in tree.values()
        ),
    )
    d4.metric(
        "Pages",
        sum(
            len(pages)
            for subcats in tree.values()
            for pages in subcats.values()
        ),
    )

    if master_rows:
        st.caption(
            (
                f"Persistent master file: "
                f"`{MASTER_AUDIT_PATH.name}`. "
                "Image columns reference files inside audit_data/media."
            )
        )

        with st.expander(
            "Browse audit hierarchy",
            expanded=True,
        ):
            for category in sorted(
                tree
            ):
                st.markdown(
                    f"### 📁 {category}"
                )

                for subcategory in sorted(
                    tree[category]
                ):
                    st.markdown(
                        f"**↳ {subcategory}**"
                    )

                    page_parts = [
                        (
                            f"{page_label} "
                            f"({count} row"
                            f"{'s' if count != 1 else ''})"
                        )
                        for (
                            page_label,
                            count,
                        ) in sorted(
                            tree[
                                category
                            ][
                                subcategory
                            ].items()
                        )
                    ]

                    st.caption(
                        " · ".join(
                            page_parts
                        )
                    )

        category_filter_options = (
            ["All categories"]
            + sorted(
                tree.keys()
            )
        )

        preview_category = st.selectbox(
            "Preview category",
            category_filter_options,
            key=(
                "master_audit_preview_category"
            ),
        )

        preview_rows = (
            master_rows
            if preview_category
            == "All categories"
            else [
                row
                for row in master_rows
                if row.get(
                    "main_category"
                )
                == preview_category
            ]
        )

        st.dataframe(
            [
                {
                    "Main Category": row.get("main_category"),
                    "Subcategory": row.get("subcategory"),
                    "Page": row.get("page_label"),
                    "Product": row.get("product_name"),
                    "Product URL": row.get("product_url"),
                    "Status": row.get("status"),
                    "Problem": row.get("problem_summary"),
                    "Consistency": row.get("consistency_score"),
                    "Suggested Fix": row.get("suggested_fix"),
                    "Profile": row.get("profile"),
                    "Subtype": row.get("subtype"),
                }
                for row in preview_rows
            ],
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("### Report downloads")
        r1, r2, r3, r4 = st.columns(4)

        with r1:
            st.download_button(
                "👔 Boss SIMPLE CSV",
                data=simple_csv_bytes(master_rows),
                file_name="01_Boss_Simple_Audit.csv",
                mime="text/csv",
                type="primary",
                key="download_boss_simple_csv",
                help=(
                    "Clean management report: category, subcategory, page, product, "
                    "status, problem, score, suggested fix, profile and subtype."
                ),
            )

        with r2:
            st.download_button(
                "🛠️ Advanced TECHNICAL CSV",
                data=advanced_csv_bytes(master_rows),
                file_name="02_Advanced_Technical_Audit.csv",
                mime="text/csv",
                key="download_advanced_technical_csv",
            )

        with r3:
            try:
                boss_excel_bytes = build_boss_excel_report(master_rows)
                st.download_button(
                    "🖼️ Boss VISUAL EXCEL",
                    data=boss_excel_bytes,
                    file_name="03_Boss_Visual_Audit.xlsx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    key="download_boss_visual_excel",
                    help=(
                        "Two sheets: Simple Audit + Advanced Audit, with actual "
                        "Current / Normalized / Before-After images embedded."
                    ),
                )
            except Exception as exc:
                st.error(f"Excel report unavailable: {exc}")

        with r4:
            st.download_button(
                "📦 Complete BOSS REPORT PACKAGE",
                data=build_master_visual_package(master_rows),
                file_name="Product_Image_Audit_Boss_Package.zip",
                mime="application/zip",
                key="download_master_visual_audit",
                help=(
                    "Includes Simple CSV, Advanced CSV, Visual Excel, HTML visual report, "
                    "and the actual image files."
                ),
            )

        st.download_button(
            "📂 Optional: separate Simple + Advanced CSVs by category / subcategory / page",
            data=separate_page_csv_zip(master_rows),
            file_name="Product_Image_Audit_Separate_Page_CSVs.zip",
            mime="application/zip",
            key="download_separate_audit_csvs",
        )

        st.info(
            "For your boss, use **Boss SIMPLE CSV** or **Boss VISUAL EXCEL**. "
            "The simple CSV intentionally hides engineering measurements, URLs, "
            "source-image links, effective-detection fields, and audit timestamps. "
            "Because CSV cannot display actual image binaries, the Visual Excel report "
            "embeds the Current / Normalized / Before-After images directly."
        )

        with st.expander(
            "Danger zone — start a new master audit"
        ):
            confirm_clear = (
                st.checkbox(
                    (
                        "I understand this deletes "
                        "the locally saved master "
                        "audit CSV."
                    ),
                    key=(
                        "confirm_clear_master_audit"
                    ),
                )
            )

            if st.button(
                "Delete local master audit",
                disabled=not confirm_clear,
                key="clear_master_audit",
            ):
                clear_master_rows()
                st.success(
                    "Local master audit cleared."
                )
                st.rerun()
    else:
        st.info(
            "The master audit is empty. Run an Advanced Audit crawl and "
            "click **Add / update full crawl in master audit**."
        )



if active_workspace == "🚫 Filtered Out Images":
    st.subheader("🚫 Filtered Out Images")
    st.write(
        "This tab is the dedicated review center for every product the "
        "category gate removed. You can review the image, product name, "
        "detected category, skip reason, download a filtered-products CSV, "
        "and reopen the page-level editor to manually include a product."
    )

    detection_scan_filtered = st.session_state.get(
        "detection_result"
    )
    advanced_pages_filtered = st.session_state.get(
        "advanced_audit_pages",
        [],
    )

    filtered_groups = collect_filtered_rows(
        detection_scan_filtered,
        advanced_pages_filtered,
    )

    detection_filtered_rows = filtered_groups[
        "detection"
    ]
    advanced_filtered_rows = filtered_groups[
        "advanced"
    ]

    total_filtered_rows = (
        len(detection_filtered_rows)
        + len(advanced_filtered_rows)
    )

    if total_filtered_rows == 0:
        st.info(
            "No filtered products are currently available. "
            "Run **Detection Test** or **Advanced Audit** first."
        )
    else:
        summary1, summary2, summary3 = st.columns(
            3
        )
        summary1.metric(
            "Filtered products available",
            total_filtered_rows,
        )
        summary2.metric(
            "Latest Detection Test",
            len(detection_filtered_rows),
        )
        summary3.metric(
            "Advanced Audit",
            len(advanced_filtered_rows),
        )

        source_options = []

        if advanced_pages_filtered:
            source_options.append(
                "Advanced Audit"
            )

        if detection_scan_filtered:
            source_options.append(
                "Latest Detection Test"
            )

        filtered_source = st.radio(
            "Filtered source",
            source_options,
            horizontal=True,
            key="filtered_tab_source",
        )

        if filtered_source == "Advanced Audit":
            source_rows = advanced_filtered_rows

            st.download_button(
                (
                    "⬇️ Download ALL Advanced Audit "
                    f"filtered products ({len(source_rows)})"
                ),
                data=filtered_items_csv_with_page_bytes(
                    source_rows
                ),
                file_name=(
                    "advanced_audit_filtered_products.csv"
                ),
                mime="text/csv",
                key="filtered_tab_advanced_all_csv",
                type="primary",
            )

            page_options = [
                "All pages"
            ]

            for page in advanced_pages_filtered:
                count = len(
                    page.get(
                        "scan",
                        {},
                    ).get(
                        "excluded_items",
                        [],
                    )
                    or []
                )

                if count:
                    page_options.append(
                        (
                            f"{page.get('page_label','Page')} "
                            f"({count})"
                        )
                    )

            selected_filtered_page = st.selectbox(
                "Filtered page",
                page_options,
                key="filtered_tab_advanced_page",
            )

            if selected_filtered_page == "All pages":
                st.caption(
                    "Read-only overview of every filtered product from the "
                    "latest Advanced Audit. Choose an individual page above "
                    "to use the **Include in audit** editor."
                )

                gallery_rows = source_rows

                if not gallery_rows:
                    st.success(
                        "The latest Advanced Audit has no filtered products."
                    )
                else:
                    for gallery_index, row in enumerate(
                        gallery_rows,
                        start=1,
                    ):
                        render_filtered_tab_card(
                            row,
                            gallery_index,
                        )
            else:
                selected_page_label = (
                    selected_filtered_page.rsplit(
                        " (",
                        1,
                    )[0]
                )

                matching_indexes = [
                    idx
                    for idx, page in enumerate(
                        advanced_pages_filtered
                    )
                    if page.get(
                        "page_label",
                        "Page",
                    )
                    == selected_page_label
                ]

                if matching_indexes:
                    selected_index = matching_indexes[
                        0
                    ]
                    selected_page = advanced_pages_filtered[
                        selected_index
                    ]
                    selected_scan = selected_page[
                        "scan"
                    ]

                    st.caption(
                        selected_page.get(
                            "url",
                            selected_scan.get(
                                "url",
                                "",
                            ),
                        )
                    )

                    render_filtered_product_inspector(
                        selected_scan,
                        key_prefix=(
                            "filtered_tab_advanced_"
                            + re.sub(
                                r"[^A-Za-z0-9_-]+",
                                "_",
                                selected_page_label,
                            )
                        ),
                        expanded=True,
                        max_items=200,
                        scan_config=selected_page.get(
                            "scan_config"
                        ),
                        result_target="advanced",
                        advanced_page_index=selected_index,
                    )

        else:
            source_rows = detection_filtered_rows

            st.download_button(
                (
                    "⬇️ Download Detection Test "
                    f"filtered products ({len(source_rows)})"
                ),
                data=filtered_items_csv_with_page_bytes(
                    source_rows
                ),
                file_name=(
                    "detection_test_filtered_products.csv"
                ),
                mime="text/csv",
                key="filtered_tab_detection_csv",
                type="primary",
            )

            if detection_scan_filtered:
                render_filtered_product_inspector(
                    detection_scan_filtered,
                    key_prefix=(
                        "filtered_tab_detection"
                    ),
                    expanded=True,
                    max_items=200,
                    scan_config=st.session_state.get(
                        "detection_scan_config"
                    ),
                    result_target="detection",
                )


if active_workspace == "🖼️ Page Showcase":
    @st.fragment
    def render_page_showcase_fragment():
        st.subheader("🖼️ Reconstructed page showcase")
        st.write(
            "See the scanned product collection together after the fixes are applied. "
            "Cards now use a lightweight native square preview so the full appliance stays "
            "visible. Use the flip button for old/new comparison and open the focused editor "
            "only when a product needs manual tuning."
        )

        detection_scan = st.session_state.get("detection_result")
        advanced_pages = st.session_state.get("advanced_audit_pages", [])
        advanced_context = st.session_state.get("advanced_audit_context", {})

        scan = None
        showcase_detection_category = (
            st.session_state.get("detection_category")
            or "Auto by page / shape"
        )

        available_sources = []
        if detection_scan:
            available_sources.append("Latest Detection Test")
        if advanced_pages:
            available_sources.append("Advanced Audit page")

        if available_sources:
            default_source_index = (
                available_sources.index("Advanced Audit page")
                if "Advanced Audit page" in available_sources
                else 0
            )

            showcase_source = st.radio(
                "Showcase source",
                available_sources,
                horizontal=True,
                index=default_source_index,
                key="showcase_source",
            )

            if showcase_source == "Advanced Audit page":
                page_labels = [
                    (
                        f"{page.get('page_label','Page')} — "
                        f"{page['scan'].get('candidate_count',0)} product(s)"
                    )
                    for page in advanced_pages
                ]
                selected_page_label = st.selectbox(
                    "Advanced Audit page to preview",
                    page_labels,
                    key="showcase_advanced_page",
                )
                selected_page_index = page_labels.index(selected_page_label)
                st.session_state[
                    "showcase_advanced_page_index"
                ] = selected_page_index
                scan = advanced_pages[selected_page_index]["scan"]
                showcase_detection_category = advanced_context.get(
                    "detection_category",
                    "Auto by page / shape",
                )
                st.caption(
                    f"Previewing {advanced_pages[selected_page_index].get('page_label','Page')} · "
                    f"{advanced_pages[selected_page_index].get('url', scan.get('url',''))}"
                )
            else:
                scan = detection_scan
                showcase_detection_category = (
                    st.session_state.get("detection_category")
                    or "Auto by page / shape"
                )

        if not scan:
            st.info(
                "Run **Detection Test** or **Advanced Audit** first. Advanced Audit "
                "pages can now be selected one at a time here instead of rendering "
                "the entire crawl at once."
            )
        elif not scan.get("items"):
            st.warning("The selected scan did not contain any analyzable product images.")
        else:
            render_showcase_scroll_guard(
                (
                    scan.get("url", "")
                    + "::"
                    + str(
                        st.session_state.get(
                            "showcase_advanced_page_index",
                            0,
                        )
                    )
                )
            )

            s1, s2, s3 = st.columns([1.15, 1.15, 1.7])
            with s1:
                showcase_mode = st.radio(
                    "Preview",
                    ["Fixed page", "Current page", "Changed only"],
                    horizontal=False,
                    index=0,
                    key="showcase_mode",
                )
            with s2:
                default_scope_index = (
                    1
                    if normalization_mode == "amazon_a"
                    else 0
                )

                if "showcase_scope" not in st.session_state:
                    st.session_state["showcase_scope"] = (
                        "Normalize every image"
                        if default_scope_index == 1
                        else "Fix flagged images only"
                    )

                normalization_scope_label = st.radio(
                    "Replacement policy",
                    ["Fix flagged images only", "Normalize every image"],
                    horizontal=False,
                    index=default_scope_index,
                    key="showcase_scope",
                    help=(
                        "Amazon A defaults to Normalize every image so the entire page "
                        "uses the same profile rulers."
                    ),
                )
                normalize_scope = (
                    "flagged"
                    if normalization_scope_label.startswith("Fix flagged")
                    else "all"
                )
            with s3:
                columns_per_row = st.slider(
                    "Products per row",
                    min_value=2,
                    max_value=6,
                    value=4,
                    step=1,
                    key="showcase_columns",
                )
                show_details = st.toggle(
                    "Show product diagnostics",
                    value=False,
                    key="showcase_diagnostics",
                )
                st.caption(
                    "This recreates the product-card image grid for visual QA. It does not copy "
                    "the website's exact CSS, fonts, navigation, or other page chrome."
                )


            current_showcase_key = showcase_cache_key(
                scan=scan,
                detection_category=showcase_detection_category,
                segmentation_mode=segmentation_mode,
                canvas_size=canvas_size,
                min_padding=min_padding,
                normalize_scope=normalize_scope,
                strict_mode=precision_mode.startswith("Strict"),
                normalization_mode=normalization_mode,
                fine_tune=fine_tune,
                manual_overrides=st.session_state.get("manual_overrides", {}),
            )

            page_cache = st.session_state.setdefault(
                "showcase_page_cache",
                {},
            )

            if current_showcase_key not in page_cache:
                progress = st.progress(0.0)
                progress_text = st.empty()

                def update_showcase_progress(done, total, name):
                    progress.progress(
                        min(
                            done
                            / max(
                                total,
                                1,
                            ),
                            1.0,
                        )
                    )
                    progress_text.caption(
                        f"Preparing selected page {done}/{total}: {name}"
                    )

                with st.spinner(
                    "Preparing only the selected showcase page..."
                ):
                    showcase_items = build_showcase_items(
                        scan=scan,
                        detection_category=showcase_detection_category,
                        segmentation_mode=segmentation_mode,
                        canvas_size=canvas_size,
                        min_padding=min_padding,
                        normalize_scope=normalize_scope,
                        strict_mode=precision_mode.startswith("Strict"),
                        normalization_mode=normalization_mode,
                        fine_tune=fine_tune,
                        manual_overrides=st.session_state.get(
                            "manual_overrides",
                            {},
                        ),
                        progress_callback=update_showcase_progress,
                    )

                page_cache[
                    current_showcase_key
                ] = showcase_items

                # Keep the session cache bounded on very large catalogues.
                while len(
                    page_cache
                ) > 24:
                    oldest_key = next(
                        iter(
                            page_cache
                        )
                    )
                    page_cache.pop(
                        oldest_key,
                        None,
                    )

                progress.progress(1.0)
                progress_text.empty()
            else:
                showcase_items = page_cache[
                    current_showcase_key
                ]

            st.session_state[
                "showcase_items"
            ] = showcase_items
            st.session_state[
                "showcase_cache_key"
            ] = current_showcase_key

            changed_count = sum(1 for x in showcase_items if x.get("changed"))
            untouched_count = len(showcase_items) - changed_count
            error_count = sum(1 for x in showcase_items if x.get("normalization_error"))

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Products on showcase", len(showcase_items))
            k2.metric("Images replaced", changed_count)
            k3.metric("Images kept unchanged", untouched_count)
            k4.metric("Replacement errors", error_count)

            st.divider()

            display_items = showcase_items
            if showcase_mode == "Changed only":
                display_items = [x for x in showcase_items if x.get("changed")]

            if not display_items:
                st.success("Nothing needs changing with the current replacement policy. ✅")
            else:
                showcase_download_signature = (
                    str(
                        current_showcase_key
                    )
                    + "::"
                    + showcase_mode
                    + "::"
                    + str(
                        len(
                            display_items
                        )
                    )
                )

                if (
                    st.session_state.get(
                        "showcase_download_signature"
                    )
                    != showcase_download_signature
                ):
                    st.session_state.pop(
                        "showcase_download_zip",
                        None,
                    )
                    st.session_state[
                        "showcase_download_signature"
                    ] = showcase_download_signature

                quick_dl_a, quick_dl_b = st.columns(
                    [1.2, 1.8]
                )

                with quick_dl_a:
                    if (
                        "showcase_download_zip"
                        not in st.session_state
                    ):
                        if st.button(
                            "📦 Prepare showcase download",
                            key="showcase_prepare_download",
                            use_container_width=True,
                        ):
                            with st.spinner(
                                "Building showcase ZIP..."
                            ):
                                st.session_state[
                                    "showcase_download_zip"
                                ] = build_showcase_download_zip(
                                    display_items,
                                    showcase_mode=showcase_mode,
                                    changed_only=(
                                        showcase_mode
                                        == "Changed only"
                                    ),
                                )
                            st.rerun(
                                scope="fragment"
                            )
                    else:
                        st.download_button(
                            "⬇️ Download current showcase images",
                            data=st.session_state[
                                "showcase_download_zip"
                            ],
                            file_name=(
                                "showcase_changed_only.zip"
                                if showcase_mode
                                == "Changed only"
                                else (
                                    "showcase_current_page.zip"
                                    if showcase_mode
                                    == "Current page"
                                    else "showcase_fixed_page.zip"
                                )
                            ),
                            mime="application/zip",
                            key="showcase_download_current_grid",
                            type="primary",
                            use_container_width=True,
                        )

                with quick_dl_b:
                    st.caption(
                        "The ZIP is now built only when you ask for it, instead of being "
                        "recompressed every time a card is flipped or the editor changes."
                    )

                st.info(
                    "Flip / Keep original / Undo keep original update only the card you clicked. "
                    "Use **Tune** or **Stencil** to open the separate Product Editor tab."
                )

                for row_start in range(
                    0,
                    len(
                        display_items
                    ),
                    columns_per_row,
                ):
                    row_items = display_items[
                        row_start:
                        row_start
                        + columns_per_row
                    ]
                    cols = st.columns(
                        columns_per_row
                    )

                    for col, item in zip(
                        cols,
                        row_items,
                    ):
                        with col:
                            render_showcase_card_fragment(
                                product_override_key(
                                    item
                                ),
                                showcase_mode,
                                show_details,
                            )

            st.divider()
            show_full_page_comparison = st.toggle(
                "🔁 Render full before-vs-fixed comparison grids",
                value=False,
                key="showcase_render_full_comparison",
                help=(
                    "Off by default for performance. Turn it on only when you want "
                    "the two extra full-page image grids."
                ),
            )

            if show_full_page_comparison:
                st.subheader(
                    "🔁 Before vs fixed page"
                )
                st.write(
                    "Use these two complete grids to judge the overall visual rhythm."
                )

                before_col, after_col = st.columns(
                    2
                )

                def render_mini_grid(
                    container,
                    items,
                    fixed=False,
                ):
                    with container:
                        st.markdown(
                            "**Fixed page**"
                            if fixed
                            else "**Current page**"
                        )
                        for r in range(
                            0,
                            len(
                                items
                            ),
                            2,
                        ):
                            mini_cols = st.columns(
                                2
                            )
                            for mini_col, item in zip(
                                mini_cols,
                                items[
                                    r:r+2
                                ],
                            ):
                                with mini_col:
                                    image_bytes = (
                                        item[
                                            "showcase_bytes"
                                        ]
                                        if (
                                            fixed
                                            and item.get(
                                                "changed"
                                            )
                                        )
                                        else item[
                                            "image_bytes"
                                        ]
                                    )
                                    st.image(
                                        square_card_preview_bytes(
                                            image_bytes
                                        ),
                                        use_container_width=True,
                                    )
                                    name = item[
                                        "name"
                                    ]
                                    if len(
                                        name
                                    ) > 58:
                                        name = (
                                            name[:55]
                                            + "..."
                                        )
                                    if (
                                        fixed
                                        and item.get(
                                            "changed"
                                        )
                                    ):
                                        st.caption(
                                            f"✨ {name}"
                                        )
                                    else:
                                        st.caption(
                                            name
                                        )

                render_mini_grid(
                    before_col,
                    showcase_items,
                    fixed=False,
                )
                render_mini_grid(
                    after_col,
                    showcase_items,
                    fixed=True,
                )



    render_page_showcase_fragment()


if active_workspace == "🎛️ Product Editor":
    st.subheader("🎛️ Product Editor")
    st.write(
        "V7.1.4 keeps the editing canvas and its main controls together. "
        "Dragging and resizing are client-side and stay visually in place instead of snapping back."
    )

    (
        editor_scan,
        editor_category,
    ) = resolve_current_showcase_scan()

    if not editor_scan or not editor_scan.get("items"):
        st.info(
            "Run Detection Test or Advanced Audit first. Then choose a page in "
            "**Page Showcase** and click **Tune** or **Stencil** on a product."
        )
    else:
        @st.fragment
        def render_product_editor_fragment():
            st.session_state.setdefault(
                "manual_overrides",
                {},
            )
            st.session_state.setdefault(
                "editor_draft_overrides",
                {},
            )
            st.session_state.setdefault(
                "editor_reset_revisions",
                {},
            )

            scan_items = list(
                editor_scan["items"]
            )
            product_options = {
                f"{i+1}. {item['name']}": item
                for i, item in enumerate(
                    scan_items
                )
            }
            product_labels = list(
                product_options.keys()
            )

            pending_target_key = st.session_state.pop(
                "showcase_pending_tune_key",
                None,
            )
            pending_stencil_key = st.session_state.pop(
                "showcase_pending_stencil_key",
                None,
            )

            available_product_keys = {
                product_override_key(item)
                for item in scan_items
            }
            previous_target_key = st.session_state.get(
                "showcase_editor_target_key"
            )
            previous_stencil_key = st.session_state.get(
                "showcase_editor_stencil_key"
            )

            if (
                pending_target_key
                in available_product_keys
            ):
                target_key = pending_target_key
            elif (
                previous_target_key
                in available_product_keys
            ):
                target_key = previous_target_key
            elif (
                pending_stencil_key
                in available_product_keys
            ):
                # If the user starts with "Use as Stencil" and the old edit
                # target belongs to another page, use this product as both the
                # initial target and reference rather than silently choosing item 1.
                target_key = pending_stencil_key
            else:
                target_key = product_override_key(
                    scan_items[0]
                )

            if (
                pending_stencil_key
                in available_product_keys
            ):
                stencil_key = pending_stencil_key
            elif (
                previous_stencil_key
                in available_product_keys
            ):
                stencil_key = previous_stencil_key
            else:
                stencil_key = target_key

            st.session_state[
                "showcase_editor_target_key"
            ] = target_key
            st.session_state[
                "showcase_editor_stencil_key"
            ] = stencil_key
            st.session_state[
                "showcase_editor_open"
            ] = True

            def label_for_key(
                wanted_key: str,
            ) -> str:
                for label, item in product_options.items():
                    if (
                        product_override_key(
                            item
                        )
                        == wanted_key
                    ):
                        return label
                return product_labels[0]

            # Pending card selections are consumed before these widgets exist,
            # so Streamlit's widget-state rule is respected.
            if (
                pending_target_key
                or st.session_state.get(
                    "v714_editor_target"
                )
                not in product_labels
            ):
                st.session_state[
                    "v714_editor_target"
                ] = label_for_key(
                    target_key
                )

            if (
                pending_stencil_key
                or st.session_state.get(
                    "v714_editor_stencil"
                )
                not in product_labels
            ):
                st.session_state[
                    "v714_editor_stencil"
                ] = label_for_key(
                    stencil_key
                )

            selector_a, selector_b = st.columns(
                2
            )

            with selector_a:
                selected_label = st.selectbox(
                    "🎯 Product to edit",
                    product_labels,
                    index=product_labels.index(
                        label_for_key(
                            target_key
                        )
                    ),
                    key="v714_editor_target",
                )

            selected_item = product_options[
                selected_label
            ]
            override_key = product_override_key(
                selected_item
            )
            st.session_state[
                "showcase_editor_target_key"
            ] = override_key

            with selector_b:
                stencil_label = st.selectbox(
                    "🧷 Product to match / stencil",
                    product_labels,
                    index=product_labels.index(
                        label_for_key(
                            stencil_key
                        )
                    ),
                    key="v714_editor_stencil",
                )

            stencil_item = product_options[
                stencil_label
            ]
            stencil_key = product_override_key(
                stencil_item
            )
            st.session_state[
                "showcase_editor_stencil_key"
            ] = stencil_key

            token = str(
                abs(
                    hash(
                        override_key
                    )
                )
            )

            saved_tuning = dict(
                st.session_state[
                    "manual_overrides"
                ].get(
                    override_key,
                    {},
                )
            )
            draft_tuning = dict(
                st.session_state[
                    "editor_draft_overrides"
                ].get(
                    override_key,
                    saved_tuning,
                )
            )

            body_modes = {
                "Smart — main product automatically": "auto",
                "Main appliance only": "main",
                "Everything visible together": "full",
                "Multiple-piece product / bundle": "multipart",
            }
            reverse_body = {
                value: label
                for label, value
                in body_modes.items()
            }

            anchor_modes = {
                "Smart center": "auto",
                "Center the main appliance": "primary",
                "Center everything visible": "full_center",
                "Center by visual weight": "visual_center",
            }
            reverse_anchor = {
                value: label
                for label, value
                in anchor_modes.items()
            }

            body_key = (
                f"v714_body_{token}"
            )
            anchor_key = (
                f"v714_anchor_{token}"
            )
            baseline_key = (
                f"v714_baseline_{token}"
            )

            current_body_label = (
                st.session_state.get(
                    body_key
                )
                or reverse_body.get(
                    draft_tuning.get(
                        "body_mode",
                        "auto",
                    ),
                    list(
                        body_modes.keys()
                    )[0],
                )
            )
            current_anchor_label = (
                st.session_state.get(
                    anchor_key
                )
                or reverse_anchor.get(
                    draft_tuning.get(
                        "anchor_mode",
                        "auto",
                    ),
                    list(
                        anchor_modes.keys()
                    )[0],
                )
            )
            current_baseline = float(
                st.session_state.get(
                    baseline_key,
                    draft_tuning.get(
                        "baseline_shift",
                        0.0,
                    ),
                )
            )

            current_tuning = {
                "body_mode": body_modes.get(
                    current_body_label,
                    "auto",
                ),
                "anchor_mode": anchor_modes.get(
                    current_anchor_label,
                    "auto",
                ),
                "scale_bias": float(
                    draft_tuning.get(
                        "scale_bias",
                        0.0,
                    )
                ),
                "x_nudge": float(
                    draft_tuning.get(
                        "x_nudge",
                        0.0,
                    )
                ),
                "y_nudge": float(
                    draft_tuning.get(
                        "y_nudge",
                        0.0,
                    )
                ),
                "baseline_shift": (
                    current_baseline
                ),
                "crop_left": float(
                    draft_tuning.get(
                        "crop_left",
                        0.0,
                    )
                ),
                "crop_right": float(
                    draft_tuning.get(
                        "crop_right",
                        0.0,
                    )
                ),
                "crop_top": float(
                    draft_tuning.get(
                        "crop_top",
                        0.0,
                    )
                ),
                "crop_bottom": float(
                    draft_tuning.get(
                        "crop_bottom",
                        0.0,
                    )
                ),
            }

            record_editor_history(
                override_key,
                current_tuning,
            )

            history_controls = st.columns(
                [0.75, 0.75, 2.0]
            )
            with history_controls[0]:
                if st.button(
                    "↶ Undo",
                    key=(
                        "v720_undo_"
                        + token
                    ),
                    disabled=(
                        not editor_history_can_step(
                            override_key,
                            -1,
                        )
                    ),
                    use_container_width=True,
                ):
                    if restore_editor_history(
                        override_key,
                        -1,
                    ):
                        st.rerun(
                            scope="fragment"
                        )

            with history_controls[1]:
                if st.button(
                    "↷ Redo",
                    key=(
                        "v720_redo_"
                        + token
                    ),
                    disabled=(
                        not editor_history_can_step(
                            override_key,
                            1,
                        )
                    ),
                    use_container_width=True,
                ):
                    if restore_editor_history(
                        override_key,
                        1,
                    ):
                        st.rerun(
                            scope="fragment"
                        )

            with history_controls[2]:
                st.caption(
                    "Undo/Redo covers drag, resize, crop and advanced editor changes."
                )

            tuner_category = (
                normalization_category_for_item(
                    selected_item,
                    editor_scan,
                    editor_category,
                )
            )
            tuner_profile_overrides = (
                build_page_profile_overrides(
                    editor_scan,
                    precision_mode.startswith(
                        "Strict"
                    ),
                )
            )

            tuned = None
            cutout_png = None
            base_drag_box = None
            tuned_for_tuning = canonical_editor_tuning(
                current_tuning
            )

            try:
                tuned = process_image_bytes(
                    selected_item[
                        "image_bytes"
                    ],
                    category=(
                        tuner_category
                    ),
                    canvas_size=(
                        canvas_size
                    ),
                    segmentation_mode=(
                        segmentation_mode
                    ),
                    min_padding=(
                        min_padding
                    ),
                    profile_overrides=(
                        get_profile_override_for_item(
                            selected_item,
                            tuner_profile_overrides,
                        )
                    ),
                    adjustments=(
                        merged_adjustments(
                            fine_tune,
                            current_tuning,
                        )
                    ),
                    body_mode=(
                        current_tuning[
                            "body_mode"
                        ]
                    ),
                    anchor_mode=(
                        current_tuning[
                            "anchor_mode"
                        ]
                    ),
                    normalization_mode=(
                        normalization_mode
                    ),
                )

                (
                    cutout_png,
                    base_drag_box,
                ) = draggable_cutout_and_box(
                    tuned
                )

            except Exception as exc:
                st.error(
                    "Editor preview failed: "
                    + str(
                        exc
                    )
                )

            cached_showcase_items = (
                st.session_state.get(
                    "showcase_items",
                    [],
                )
            )
            cached_showcase_map = {
                product_override_key(
                    item
                ): item
                for item in cached_showcase_items
            }
            stencil_cached = (
                cached_showcase_map.get(
                    stencil_key,
                    stencil_item,
                )
            )
            stencil_bytes = (
                showcase_display_bytes(
                    stencil_cached,
                    showcase_mode="Fixed page",
                )
            )
            stencil_square = (
                square_card_preview_bytes(
                    stencil_bytes
                )
            )

            workspace_left, workspace_right = st.columns(
                [1.65, 0.85],
                gap="large",
            )

            with workspace_left:
                st.markdown(
                    "#### ✋ Visual editor"
                )

                if (
                    cutout_png
                    and base_drag_box
                ):
                    revision = int(
                        st.session_state[
                            "editor_reset_revisions"
                        ].get(
                            override_key,
                            0,
                        )
                    )
                    reset_token = geometry_reset_token(
                        override_key,
                        current_tuning,
                        category=tuner_category,
                        canvas_size=canvas_size,
                        segmentation_mode=segmentation_mode,
                        normalization_mode=normalization_mode,
                        revision=revision,
                    )

                    drag_value = (
                        product_transform_editor(
                            product_png=(
                                cutout_png
                            ),
                            stencil_png=(
                                stencil_square
                            ),
                            initial_box=(
                                base_drag_box
                            ),
                            initial_crop={
                                "left": current_tuning.get("crop_left", 0.0),
                                "right": current_tuning.get("crop_right", 0.0),
                                "top": current_tuning.get("crop_top", 0.0),
                                "bottom": current_tuning.get("crop_bottom", 0.0),
                            },
                            stencil_opacity=0.24,
                            reset_token=(
                                reset_token
                            ),
                            key=(
                                "v714_transform_"
                                + token
                            ),
                        )
                    )

                    if (
                        isinstance(
                            drag_value,
                            dict,
                        )
                        and drag_value.get(
                            "event_id"
                        )
                    ):
                        event_key = (
                            "v714_last_drag_event_"
                            + token
                        )
                        event_id = (
                            drag_value[
                                "event_id"
                            ]
                        )

                        if (
                            st.session_state.get(
                                event_key
                            )
                            != event_id
                        ):
                            crop_aware_tuning = {
                                **current_tuning,
                                "crop_left": float(drag_value.get("crop_left", current_tuning.get("crop_left", 0.0))),
                                "crop_right": float(drag_value.get("crop_right", current_tuning.get("crop_right", 0.0))),
                                "crop_top": float(drag_value.get("crop_top", current_tuning.get("crop_top", 0.0))),
                                "crop_bottom": float(drag_value.get("crop_bottom", current_tuning.get("crop_bottom", 0.0))),
                            }
                            adjusted = (
                                tuning_after_drag_transform(
                                    current_tuning=(
                                        crop_aware_tuning
                                    ),
                                    global_tuning=(
                                        fine_tune
                                    ),
                                    base_box=(
                                        base_drag_box
                                    ),
                                    dragged_box=(
                                        drag_value
                                    ),
                                )
                            )

                            st.session_state[
                                event_key
                            ] = event_id
                            st.session_state[
                                "editor_draft_overrides"
                            ][
                                override_key
                            ] = adjusted
                            record_editor_history(
                                override_key,
                                adjusted,
                            )

                            # Use the new values immediately for Apply buttons
                            # on this same fragment run. No second rerun = no snap.
                            current_tuning = adjusted
                else:
                    st.info(
                        "The foreground cutout could not be created for this product. "
                        "Advanced controls on the right are still available."
                    )

            with workspace_right:
                st.markdown(
                    "#### Edit settings"
                )
                st.caption(
                    "The main move/zoom controls are now directly beside the image. "
                    "These options only change how FBF understands the product."
                )

                with st.expander(
                    "Advanced",
                    expanded=False,
                ):
                    body_label = st.selectbox(
                        "What should move together?",
                        list(
                            body_modes.keys()
                        ),
                        index=list(
                            body_modes.keys()
                        ).index(
                            current_body_label
                        ),
                        key=body_key,
                    )

                    anchor_label = st.selectbox(
                        "What should be centered?",
                        list(
                            anchor_modes.keys()
                        ),
                        index=list(
                            anchor_modes.keys()
                        ).index(
                            current_anchor_label
                        ),
                        key=anchor_key,
                    )

                    manual_baseline = st.slider(
                        "Raise / lower floor line",
                        -0.10,
                        0.10,
                        current_baseline,
                        0.0025,
                        key=baseline_key,
                    )

                # Fold any advanced widget changes into the current draft.
                current_tuning = {
                    **current_tuning,
                    "body_mode": body_modes[
                        body_label
                    ],
                    "anchor_mode": anchor_modes[
                        anchor_label
                    ],
                    "baseline_shift": (
                        manual_baseline
                    ),
                }
                st.session_state[
                    "editor_draft_overrides"
                ][
                    override_key
                ] = dict(
                    current_tuning
                )
                record_editor_history(
                    override_key,
                    current_tuning,
                )

                st.caption(
                    "Draft transform"
                )
                st.code(
                    (
                        f"Zoom: {1.0 + current_tuning['scale_bias']:.3f}×\n"
                        f"Left/right: {current_tuning['x_nudge']:+.3f}\n"
                        f"Up/down: {current_tuning['y_nudge']:+.3f}\n"
                        f"Crop L/R/T/B: "
                        f"{current_tuning.get('crop_left',0.0):.1%} / "
                        f"{current_tuning.get('crop_right',0.0):.1%} / "
                        f"{current_tuning.get('crop_top',0.0):.1%} / "
                        f"{current_tuning.get('crop_bottom',0.0):.1%}"
                    ),
                    language="text",
                )

                apply_a, apply_b = st.columns(
                    2
                )

                with apply_a:
                    if st.button(
                        "✅ Apply",
                        key=(
                            "v714_apply_"
                            + token
                        ),
                        type="primary",
                        use_container_width=True,
                    ):
                        try:
                            apply_editor_tuning_and_sync(
                                selected_item=selected_item,
                                override_key=override_key,
                                current_tuning=current_tuning,
                                tuner_category=tuner_category,
                                tuner_profile_overrides=tuner_profile_overrides,
                                segmentation_mode=segmentation_mode,
                                canvas_size=canvas_size,
                                min_padding=min_padding,
                                fine_tune=fine_tune,
                                normalization_mode=normalization_mode,
                            )
                            st.success(
                                "Edit applied and Page Showcase updated."
                            )
                        except Exception as exc:
                            st.error(
                                "Could not apply this edit: "
                                + str(exc)
                            )

                with apply_b:
                    if st.button(
                        "💾 Apply + remember",
                        key=(
                            "v714_remember_"
                            + token
                        ),
                        use_container_width=True,
                    ):
                        try:
                            apply_editor_tuning_and_sync(
                                selected_item=selected_item,
                                override_key=override_key,
                                current_tuning=current_tuning,
                                tuner_category=tuner_category,
                                tuner_profile_overrides=tuner_profile_overrides,
                                segmentation_mode=segmentation_mode,
                                canvas_size=canvas_size,
                                min_padding=min_padding,
                                fine_tune=fine_tune,
                                normalization_mode=normalization_mode,
                            )
                        except Exception as exc:
                            st.error(
                                "Could not apply this edit; no Apply state was committed: "
                                + str(exc)
                            )
                        else:
                            try:
                                save_exception(
                                    selected_item,
                                    current_tuning,
                                    category=tuner_category,
                                )
                            except ExceptionLibraryCorruptError as exc:
                                st.warning(
                                    "The edit was applied, but remembered tuning was NOT changed because the tuning library needs recovery. "
                                    + str(exc)
                                )
                            except Exception as exc:
                                st.warning(
                                    "The edit was applied, but the Remember step failed: "
                                    + str(exc)
                                )
                            else:
                                st.success(
                                    "Edit applied, Showcase updated, and tuning remembered."
                                )

                if st.button(
                    "↩️ Reset editor",
                    key=(
                        "v714_reset_"
                        + token
                    ),
                    use_container_width=True,
                ):
                    st.session_state[
                        "manual_overrides"
                    ].pop(
                        override_key,
                        None,
                    )
                    st.session_state.setdefault(
                        "applied_edit_registry",
                        {},
                    ).pop(
                        override_key,
                        None,
                    )
                    reset_tuning = canonical_editor_tuning({})
                    st.session_state[
                        "editor_draft_overrides"
                    ][
                        override_key
                    ] = reset_tuning
                    record_editor_history(
                        override_key,
                        reset_tuning,
                    )
                    clear_editor_widget_state_for_product(
                        override_key
                    )
                    st.session_state[
                        "editor_reset_revisions"
                    ][
                        override_key
                    ] = (
                        int(
                            st.session_state[
                                "editor_reset_revisions"
                            ].get(
                                override_key,
                                0,
                            )
                        )
                        + 1
                    )
                    clear_generated_review_caches()
                    st.rerun(
                        scope="fragment"
                    )

                try:
                    remembered_suggestions = find_exception_suggestions(
                        selected_item
                    )
                except ExceptionLibraryCorruptError as exc:
                    remembered_suggestions = []
                    st.warning(
                        "Remembered tuning is temporarily unavailable: "
                        + str(exc)
                    )

                if remembered_suggestions:
                    remembered = (
                        remembered_suggestions[
                            0
                        ]
                    )

                    if st.button(
                        "✨ Load remembered tuning",
                        key=(
                            "v714_load_memory_"
                            + token
                        ),
                        use_container_width=True,
                    ):
                        remembered_override = dict(
                            remembered.get(
                                "override",
                                {},
                            )
                        )
                        st.session_state[
                            "editor_draft_overrides"
                        ][
                            override_key
                        ] = remembered_override
                        record_editor_history(
                            override_key,
                            remembered_override,
                        )
                        clear_editor_widget_state_for_product(
                            override_key
                        )
                        st.session_state[
                            "editor_reset_revisions"
                        ][
                            override_key
                        ] = (
                            int(
                                st.session_state[
                                    "editor_reset_revisions"
                                ].get(
                                    override_key,
                                    0,
                                )
                            )
                            + 1
                        )
                        st.rerun(
                            scope="fragment"
                        )

            # The drag component can change current_tuning after the initial
            # normalization at the top of this fragment. Recompute only when
            # the draft actually changed, so the preview below always shows
            # what the operator is editing instead of the old automatic base.
            live_tuned = tuned
            final_tuning_signature = canonical_editor_tuning(
                current_tuning
            )
            if (
                tuned is not None
                and final_tuning_signature
                != tuned_for_tuning
            ):
                try:
                    live_tuned = render_editor_normalized_result(
                        selected_item=selected_item,
                        current_tuning=current_tuning,
                        tuner_category=tuner_category,
                        tuner_profile_overrides=tuner_profile_overrides,
                        segmentation_mode=segmentation_mode,
                        canvas_size=canvas_size,
                        min_padding=min_padding,
                        fine_tune=fine_tune,
                        normalization_mode=normalization_mode,
                    )
                except Exception as exc:
                    st.warning(
                        "Live draft preview could not refresh: "
                        + str(exc)
                    )

            st.divider()

            preview_a, preview_b, preview_c = st.columns(
                3
            )

            with preview_a:
                st.caption(
                    "Original"
                )
                st.image(
                    square_card_preview_bytes(
                        selected_item[
                            "image_bytes"
                        ]
                    ),
                    use_container_width=True,
                )

            with preview_b:
                applied_record = applied_edit_record_for(
                    override_key
                )
                st.caption(
                    "Applied edited image"
                    if applied_record
                    else "Live draft preview"
                )

                if applied_record and applied_record.get(
                    "image_bytes"
                ):
                    preview_bytes = applied_record[
                        "image_bytes"
                    ]
                elif live_tuned:
                    preview_bytes, _ = image_to_bytes(
                        live_tuned[
                            "image"
                        ],
                        "PNG",
                    )
                else:
                    preview_bytes = selected_item[
                        "image_bytes"
                    ]

                st.image(
                    square_card_preview_bytes(
                        preview_bytes
                    ),
                    use_container_width=True,
                )

            with preview_c:
                st.caption(
                    "Stencil"
                )
                st.image(
                    stencil_square,
                    use_container_width=True,
                )

            existing_decision = (
                selected_item.get(
                    "operator_decision"
                )
                or ""
            )
            final_choice_options = [
                "Automatic",
                "Original image",
                "Edited image",
                "Complete rework",
            ]
            final_choice_index = {
                "": 0,
                "keep_original": 1,
                "use_fixed": 2,
                "complete_rework": 3,
            }.get(
                existing_decision,
                0,
            )

            final_choice = st.radio(
                "Final image for reports / exports",
                final_choice_options,
                index=final_choice_index,
                horizontal=True,
                key=(
                    "v714_final_choice_"
                    + token
                ),
            )

            if st.button(
                "Save final image choice",
                key=(
                    "v714_save_final_choice_"
                    + token
                ),
            ):
                decision_map = {
                    "Automatic": "reset",
                    "Original image": "keep_original",
                    "Edited image": "use_fixed",
                    "Complete rework": "complete_rework",
                }

                apply_operator_decision_everywhere(
                    override_key,
                    decision_map[
                        final_choice
                    ],
                )

                _set_showcase_flip(
                    override_key,
                    (
                        "original"
                        if final_choice
                        in {
                            "Original image",
                            "Complete rework",
                        }
                        else "fixed"
                    ),
                )

                st.success(
                    "Final image choice saved."
                )

        render_product_editor_fragment()



if active_workspace == "🛠️ Normalize Images":
    st.subheader("Normalize uploaded product images")
    uploaded_files = st.file_uploader(
        "Upload product images",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        help="Best results come from images with a plain or transparent background and the full product visible.",
    )

    if not uploaded_files:
        st.info(
            "Upload one or more images here, or use Detection Test first to find problem images on a live page. "
            "Strict mode for uploaded-only images applies the fine-tune sliders but cannot use page calibration "
            "unless a page scan is available in this session."
        )
    else:
        results = []

        for idx, uploaded in enumerate(uploaded_files):
            st.divider()
            st.subheader(f"{idx + 1}. {uploaded.name}")

            c1, c2, c3 = st.columns([1, 1, 1.6])
            with c1:
                category = st.selectbox(
                    "Category / profile",
                    categories,
                    index=categories.index(default_category),
                    key=f"category_{idx}_{uploaded.name}",
                )
            with c2:
                upload_body_mode_label = st.selectbox(
                    "Body interpretation",
                    [
                        "Auto — smart primary body",
                        "Main body only",
                        "Full visible extent",
                        "Multipart",
                    ],
                    key=f"body_mode_{idx}_{uploaded.name}",
                )
                upload_body_mode = {
                    "Auto — smart primary body": "auto",
                    "Main body only": "main",
                    "Full visible extent": "full",
                    "Multipart": "multipart",
                }[upload_body_mode_label]
                upload_anchor_label = st.selectbox(
                    "Alignment reference",
                    [
                        "Auto — category aware",
                        "Primary body",
                        "Full visible appliance",
                    ],
                    key=f"anchor_mode_{idx}_{uploaded.name}",
                )
                upload_anchor_mode = {
                    "Auto — category aware": "auto",
                    "Primary body": "primary",
                    "Full visible appliance": "full_center",
                }[upload_anchor_label]
            with c3:
                st.caption(
                    "V6 scales from the primary appliance body while preserving "
                    "the full visible product/accessory extent. Override the body "
                    "interpretation only for unusual images."
                )

            try:
                raw = uploaded.getvalue()
                result = process_image_bytes(
                    raw,
                    category=None if category == "Auto by page / shape" else category,
                    canvas_size=canvas_size,
                    segmentation_mode=segmentation_mode,
                    min_padding=min_padding,
                    adjustments=fine_tune,
                    body_mode=upload_body_mode,
                    anchor_mode=upload_anchor_mode,
                    normalization_mode=normalization_mode,
                )
            except Exception as exc:
                st.error(f"Could not process this image: {exc}")
                continue

            original = Image.open(io.BytesIO(raw)).convert("RGB")
            normalized = result["image"]

            left, right = st.columns(2)
            with left:
                st.markdown("**Before**")
                st.image(original, use_container_width=True)
            with right:
                st.markdown("**After**")
                st.image(normalized, use_container_width=True)

            if show_mask:
                mask_a, mask_b = st.columns(2)
                with mask_a:
                    st.markdown("**Full foreground mask**")
                    st.image(result["mask"], clamp=True, use_container_width=False)
                with mask_b:
                    st.markdown("**V6 primary-body mask**")
                    st.image(result["body_mask"], clamp=True, use_container_width=False)

            metrics = result["metrics"]
            m1, m2, m3, m4, m5, m6, m7, m8 = st.columns(8)
            m1.metric("Profile", result["profile"].title())
            m2.metric("Subtype", result.get("subtype", "—").replace("_", " ").title())
            m3.metric("Anchor", result.get("anchor_mode", "—").replace("_", " ").title())
            m4.metric("Body height", f"{metrics['body_height_occupancy']:.1%}")
            m5.metric("Body width", f"{metrics['body_width_occupancy']:.1%}")
            m6.metric("Accessory area", f"{metrics.get('accessory_area_ratio',0):.1%}")
            m7.metric(
                "Pixel lock",
                "✅" if metrics.get("pixel_lock_ok", False)
                else f"{metrics.get('anchor_error_y_px', 0):.1f}px",
            )
            m8.metric("QA", "✅ PASS" if metrics["qa_pass"] else "⚠️ REVIEW")

            if metrics["warnings"]:
                st.warning(" · ".join(metrics["warnings"]))
            else:
                st.success("Product is fully visible, centered, and inside the configured safe frame.")

            out_buf = io.BytesIO()
            save_format = "PNG" if output_format == "PNG" else "WEBP"
            if save_format == "WEBP":
                normalized.save(out_buf, format="WEBP", quality=95, method=6)
                suffix = ".webp"
                mime = "image/webp"
            else:
                normalized.save(out_buf, format="PNG", optimize=True)
                suffix = ".png"
                mime = "image/png"

            out_bytes = out_buf.getvalue()
            base = Path(uploaded.name).stem
            filename = f"{base}_normalized{suffix}"

            st.download_button(
                f"Download {filename}",
                data=out_bytes,
                file_name=filename,
                mime=mime,
                key=f"download_{idx}_{uploaded.name}",
            )

            results.append((filename, out_bytes, result))

        if results:
            st.divider()
            st.subheader("📦 Batch export")
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for filename, out_bytes, _ in results:
                    zf.writestr(filename, out_bytes)

                csv_lines = [
                    "filename,profile,height_occupancy,width_occupancy,center_offset,qa_pass,warnings"
                ]
                for filename, _, result in results:
                    m = result["metrics"]
                    warnings = " | ".join(m["warnings"]).replace('"', '""')
                    csv_lines.append(
                        f'"{filename}","{result["profile"]}",'
                        f'{m["height_occupancy"]:.6f},{m["width_occupancy"]:.6f},'
                        f'{m["center_offset"]:.6f},{str(m["qa_pass"]).lower()},"{warnings}"'
                    )
                zf.writestr("qa_report.csv", "\n".join(csv_lines))

            st.download_button(
                "Download all normalized images + QA report",
                data=zip_buffer.getvalue(),
                file_name="normalized_product_images.zip",
                mime="application/zip",
                type="primary",
            )