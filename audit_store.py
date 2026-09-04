from __future__ import annotations

import csv
import io
import re
import zipfile
import html
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Dict, Tuple

from safe_io import (
    UnsafePathError,
    canonical_http_url,
    product_storage_component,
    resolve_under_root,
    safe_archive_name,
    sanitize_path_component,
    stable_product_id,
)


APP_DIR = Path(__file__).resolve().parent
AUDIT_DIR = APP_DIR / "audit_data"
MASTER_AUDIT_PATH = AUDIT_DIR / "master_audit.csv"


AUDIT_FIELDS = [
    "main_category",
    "subcategory",
    "page_label",
    "page_url",
    "product_name",
    "product_url",
    "status",
    "operator_decision",
    "manual_edit_applied",
    "manual_zoom",
    "manual_move_left_right",
    "manual_move_up_down",
    "manual_floor_line",
    "manual_crop_left",
    "manual_crop_right",
    "manual_crop_top",
    "manual_crop_bottom",
    "manual_body_mode",
    "manual_center_mode",
    "consistency_score",
    "problem_summary",
    "suggested_fix",
    "profile",
    "subtype",
    "comparison_group",
    "body_height_occupancy",
    "body_width_occupancy",
    "horizontal_center_offset",
    "body_baseline_y",
    "full_edge_padding",
    "accessory_area_ratio",
    "component_count",
    "current_image_file",
    "normalized_image_file",
    "comparison_image_file",
    "visual_report_section",
]

SIMPLE_EXPORT_FIELDS = [
    ("Main Category", "main_category"),
    ("Subcategory", "subcategory"),
    ("Page Label", "page_label"),
    ("Product Name", "product_name"),
    ("Product URL", "product_url"),
    ("Product Status", "status"),
    ("Operator Decision", "operator_decision"),
    ("Manual Edit", "manual_edit_applied"),
    ("Manual Zoom", "manual_zoom"),
    ("Manual Left / Right", "manual_move_left_right"),
    ("Manual Up / Down", "manual_move_up_down"),
    ("Manual Floor Line", "manual_floor_line"),
    ("Crop Left", "manual_crop_left"),
    ("Crop Right", "manual_crop_right"),
    ("Crop Top", "manual_crop_top"),
    ("Crop Bottom", "manual_crop_bottom"),
    ("Manual Body Mode", "manual_body_mode"),
    ("Manual Center Mode", "manual_center_mode"),
    ("Problem Summary", "problem_summary"),
    ("Consistency Score", "consistency_score"),
    ("Suggested Fix", "suggested_fix"),
    ("Profile", "profile"),
    ("Subtype", "subtype"),
]

ADVANCED_EXPORT_FIELDS = [
    ("Main Category", "main_category"),
    ("Subcategory", "subcategory"),
    ("Page Label", "page_label"),
    ("Product Name", "product_name"),
    ("Product URL", "product_url"),
    ("Product Status", "status"),
    ("Operator Decision", "operator_decision"),
    ("Manual Edit", "manual_edit_applied"),
    ("Manual Zoom", "manual_zoom"),
    ("Manual Left / Right", "manual_move_left_right"),
    ("Manual Up / Down", "manual_move_up_down"),
    ("Manual Floor Line", "manual_floor_line"),
    ("Crop Left", "manual_crop_left"),
    ("Crop Right", "manual_crop_right"),
    ("Crop Top", "manual_crop_top"),
    ("Crop Bottom", "manual_crop_bottom"),
    ("Manual Body Mode", "manual_body_mode"),
    ("Manual Center Mode", "manual_center_mode"),
    ("Problem Summary", "problem_summary"),
    ("Consistency Score", "consistency_score"),
    ("Suggested Fix", "suggested_fix"),
    ("Profile", "profile"),
    ("Subtype", "subtype"),
    ("Comparison Group", "comparison_group"),
    ("Body Height Occupancy", "body_height_occupancy"),
    ("Body Width Occupancy", "body_width_occupancy"),
    ("Horizontal Center Offset", "horizontal_center_offset"),
    ("Body Baseline", "body_baseline_y"),
    ("Full Edge Padding", "full_edge_padding"),
    ("Accessory Area Ratio", "accessory_area_ratio"),
    ("Component Count", "component_count"),
    ("Current Image File", "current_image_file"),
    ("Normalized Image File", "normalized_image_file"),
    ("Before / After Image File", "comparison_image_file"),
]



def safe_path_part(value: str, fallback: str = "Uncategorized") -> str:
    return sanitize_path_component(value, fallback, max_length=120)


def _natural_key(value: str):
    parts = re.split(r"(\d+)", (value or "").lower())
    return [int(x) if x.isdigit() else x for x in parts]


def _row_sort_key(row: dict):
    return (
        _natural_key(row.get("main_category", "")),
        _natural_key(row.get("subcategory", "")),
        _natural_key(row.get("page_label", "")),
        _natural_key(row.get("product_name", "")),
    )


def rows_to_csv_bytes(rows: Iterable[dict]) -> bytes:
    """
    Internal persisted master CSV. This uses the advanced field model but
    excludes URLs to products/images and excludes scan timestamps.
    """
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=AUDIT_FIELDS,
        extrasaction="ignore",
    )
    writer.writeheader()

    for row in sorted(list(rows), key=_row_sort_key):
        writer.writerow(
            {
                field: row.get(field, "")
                for field in AUDIT_FIELDS
            }
        )

    return buf.getvalue().encode("utf-8-sig")


def export_csv_bytes(
    rows: Iterable[dict],
    field_map,
) -> bytes:
    """
    Boss/report export with readable column headings.
    """
    rows = sorted(list(rows), key=_row_sort_key)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)

    writer.writerow([label for label, _ in field_map])

    for row in rows:
        writer.writerow(
            [row.get(field, "") for _, field in field_map]
        )

    return buf.getvalue().encode("utf-8-sig")


def simple_csv_bytes(rows: Iterable[dict]) -> bytes:
    return export_csv_bytes(rows, SIMPLE_EXPORT_FIELDS)


def advanced_csv_bytes(rows: Iterable[dict]) -> bytes:
    return export_csv_bytes(rows, ADVANCED_EXPORT_FIELDS)


def load_master_rows() -> List[dict]:
    if not MASTER_AUDIT_PATH.exists():
        return []

    with MASTER_AUDIT_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [
            {field: row.get(field, "") for field in AUDIT_FIELDS}
            for row in reader
        ]


def save_master_rows(rows: Iterable[dict]) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_AUDIT_PATH.write_bytes(rows_to_csv_bytes(rows))


def page_identity(row: dict) -> Tuple[str, str, str, str]:
    return (
        (row.get("main_category") or "").strip().casefold(),
        (row.get("subcategory") or "").strip().casefold(),
        (row.get("page_label") or "").strip().casefold(),
        canonical_http_url(row.get("page_url")) or (row.get("page_url") or "").strip().casefold(),
    )


def audit_record_identity(row: dict) -> Tuple[str, str]:
    """Stable product identity; display names never act as the unique key alone."""
    product_url = canonical_http_url(row.get("product_url"))
    if product_url:
        return ("url", product_url)
    return ("stable", stable_product_id(row, length=32))


def product_identity(row: dict) -> Tuple[str, str, str, str, str, str]:
    kind, key = audit_record_identity(row)
    return (*page_identity(row), kind, key)


def _dedupe_rows(rows: Iterable[dict]) -> list[dict]:
    # Last record in the incoming batch wins deterministically for the same
    # canonical identity while preserving the first-seen order.
    ordered: dict[Tuple[str, ...], dict] = {}
    for row in rows:
        ordered[product_identity(row)] = row
    return list(ordered.values())


def update_master_rows(
    new_rows: List[dict],
    mode: str = "replace_page",
) -> dict:
    """
    mode:
      replace_page -> remove rows for the same hierarchy/page and replace them
      append       -> add new records while avoiding exact product duplicates
    """
    existing = _dedupe_rows(load_master_rows())
    new_rows = _dedupe_rows(new_rows)

    if not new_rows:
        return {
            "before": len(existing),
            "after": len(existing),
            "added": 0,
            "removed": 0,
            "mode": mode,
        }

    before = len(existing)
    removed = 0

    if mode == "replace_page":
        identities = {page_identity(row) for row in new_rows}
        kept = [row for row in existing if page_identity(row) not in identities]
        removed = len(existing) - len(kept)
        combined = kept + new_rows
    else:
        existing_keys = {product_identity(row) for row in existing}
        additions = []
        seen = set(existing_keys)
        for row in new_rows:
            identity = product_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            additions.append(row)
        combined = existing + additions
        new_rows = additions

    save_master_rows(combined)

    return {
        "before": before,
        "after": len(combined),
        "added": len(new_rows),
        "removed": removed,
        "mode": mode,
    }


def clear_master_rows() -> None:
    if MASTER_AUDIT_PATH.exists():
        MASTER_AUDIT_PATH.unlink()


def suggested_fix_from_issues(issues: List[str]) -> str:
    if not issues:
        return "No correction required."

    fixes = []

    for issue in issues:
        lower = issue.lower()

        if "zoomed out" in lower:
            fixes.append("Increase normalized product scale.")
        elif "zoomed in" in lower or "too large" in lower:
            fixes.append("Reduce normalized product scale.")
        elif "shifted too far right" in lower:
            fixes.append("Move the product left to the canonical center.")
        elif "shifted too far left" in lower:
            fixes.append("Move the product right to the canonical center.")
        elif "baseline" in lower:
            fixes.append("Align the primary-body baseline with its compatible subgroup.")
        elif "vertical centering" in lower:
            fixes.append("Recenter the selected appliance envelope vertically.")
        elif "edge" in lower or "padding" in lower:
            fixes.append("Increase safe padding or reduce scale so the full visible extent fits.")
        elif "foreground detection" in lower or "segmentation" in lower:
            fixes.append("Review foreground/body detection; consider AI segmentation or a manual body override.")
        else:
            fixes.append("Review normalization settings for this product.")

    # Preserve order but remove duplicates.
    deduped = []
    seen = set()
    for fix in fixes:
        if fix not in seen:
            seen.add(fix)
            deduped.append(fix)

    return " ".join(deduped)


def build_audit_rows(
    scan: dict,
    main_category: str,
    subcategory: str,
    page_label: str,
    include_passed: bool = False,
) -> List[dict]:
    rows = []

    for item in scan.get("items", []):
        operator_decision = (
            item.get("operator_decision")
            or ""
        )

        # Manual decisions must remain visible in the audit export even after
        # they resolve the product to a PASS state.
        if (
            not include_passed
            and item.get("status") == "pass"
            and not operator_decision
        ):
            continue

        metrics = item.get("metrics", {})
        issues = item.get("issues") or []
        automatic_issues = (
            item.get("_automatic_issues")
            or issues
        )

        if operator_decision == "keep_original":
            export_status = "APPROVED ORIGINAL"
            decision_label = "Keep original"
            automatic_summary = (
                " | ".join(automatic_issues)
                if automatic_issues
                else "No automatic issue text available"
            )
            problem_summary = (
                "MANUAL REVIEW — original image approved. "
                "Automatic detector originally reported: "
                + automatic_summary
            )
            suggested_fix = (
                "Keep the original website image. "
                "Operator rejected the generated replacement."
            )

        elif operator_decision == "use_fixed":
            export_status = "APPROVED FIX"
            decision_label = "Use fixed image"
            automatic_summary = (
                " | ".join(automatic_issues)
                if automatic_issues
                else "No automatic issue text available"
            )
            problem_summary = (
                "MANUAL REVIEW — normalized replacement approved. "
                "Original automatic findings: "
                + automatic_summary
            )
            suggested_fix = (
                "Use the approved normalized replacement image."
            )

        elif operator_decision == "complete_rework":
            export_status = "COMPLETE REWORK"
            decision_label = "Complete rework required"
            automatic_summary = (
                " | ".join(automatic_issues)
                if automatic_issues
                else "No automatic issue text available"
            )
            problem_summary = (
                "MANUAL REVIEW — source image is not usable as-is and the "
                "generated fix is also not acceptable. A new/reworked source "
                "image is required. Automatic findings: "
                + automatic_summary
            )
            suggested_fix = (
                "Do not use the original or generated fix. Replace/rebuild the "
                "product image from a clean source, then run it through FBF again."
            )

        else:
            export_status = item.get("status", "")
            decision_label = ""
            problem_summary = (
                " | ".join(issues)
                if issues
                else "PASS — no issue detected"
            )
            suggested_fix = suggested_fix_from_issues(
                issues
            )

        rows.append(
            {
                "main_category": (
                    main_category
                    or scan.get("effective_category")
                    or "Uncategorized"
                ),
                "subcategory": subcategory or "General",
                "page_label": page_label or "Page",
                "page_url": scan.get("url", ""),
                "product_name": item.get(
                    "name",
                    "Unnamed product",
                ),
                "product_url": item.get("product_url") or "",
                "status": export_status,
                "operator_decision": decision_label,
                "manual_edit_applied": "",
                "manual_zoom": "",
                "manual_move_left_right": "",
                "manual_move_up_down": "",
                "manual_floor_line": "",
                "manual_crop_left": "",
                "manual_crop_right": "",
                "manual_crop_top": "",
                "manual_crop_bottom": "",
                "manual_body_mode": "",
                "manual_center_mode": "",
                "consistency_score": (
                    f"{float(item.get('consistency_score', 0)):.2f}"
                ),
                "problem_summary": problem_summary,
                "suggested_fix": suggested_fix,
                "profile": item.get("profile", ""),
                "subtype": item.get("subtype", ""),
                "comparison_group": item.get(
                    "comparison_group",
                    "",
                ),
                "body_height_occupancy": (
                    f"{float(metrics.get('body_height_occupancy', metrics.get('height_occupancy', 0))):.6f}"
                ),
                "body_width_occupancy": (
                    f"{float(metrics.get('body_width_occupancy', metrics.get('width_occupancy', 0))):.6f}"
                ),
                "horizontal_center_offset": (
                    f"{float(metrics.get('center_offset_x', 0)):.6f}"
                ),
                "body_baseline_y": (
                    f"{float(metrics.get('body_baseline_y', metrics.get('baseline_y', 0))):.6f}"
                ),
                "full_edge_padding": (
                    f"{float(metrics.get('min_edge_padding', metrics.get('full_edge_padding', 0))):.6f}"
                ),
                "accessory_area_ratio": (
                    f"{float(metrics.get('accessory_area_ratio', 0)):.6f}"
                ),
                "component_count": metrics.get(
                    "component_count",
                    1,
                ),
                "current_image_file": "",
                "normalized_image_file": "",
                "comparison_image_file": "",
                "visual_report_section": (
                    f"{main_category or scan.get('effective_category') or 'Uncategorized'} / "
                    f"{subcategory or 'General'} / "
                    f"{page_label or 'Page'}"
                ),

                # Private in-memory matching field. It is intentionally NOT in
                # AUDIT_FIELDS, SIMPLE_EXPORT_FIELDS or ADVANCED_EXPORT_FIELDS,
                # so it never appears in the saved/exported audit.
                "_source_image_url": item.get("image_url") or "",
            }
        )

    return rows


def media_directory_for_row(row: dict) -> Path:
    return (
        AUDIT_DIR
        / "media"
        / safe_path_part(
            row.get("main_category"),
            "Uncategorized",
        )
        / safe_path_part(
            row.get("subcategory"),
            "General",
        )
        / safe_path_part(
            row.get("page_label"),
            "Page",
        )
        / product_storage_component(
            row,
            max_length=120,
        )
    )


def clear_page_media(
    main_category: str,
    subcategory: str,
    page_label: str,
) -> None:
    page_dir = (
        AUDIT_DIR
        / "media"
        / safe_path_part(main_category, "Uncategorized")
        / safe_path_part(subcategory, "General")
        / safe_path_part(page_label, "Page")
    )

    if page_dir.exists():
        shutil.rmtree(page_dir)


def save_audit_media(
    row: dict,
    current_bytes: bytes,
    normalized_bytes: bytes,
    comparison_bytes: bytes,
) -> dict:
    """
    Save portable visual assets beside the persistent master audit.

    Paths returned here are relative to audit_data/ so the CSV can point to
    actual files without storing binary image data inside CSV cells.
    """
    product_dir = media_directory_for_row(row)
    product_dir.mkdir(parents=True, exist_ok=True)

    current_path = product_dir / "current.png"
    normalized_path = product_dir / "normalized.png"
    comparison_path = product_dir / "before_after.jpg"

    current_path.write_bytes(current_bytes)
    normalized_path.write_bytes(normalized_bytes)
    comparison_path.write_bytes(comparison_bytes)

    return {
        "current_image_file": current_path.relative_to(
            AUDIT_DIR
        ).as_posix(),
        "normalized_image_file": normalized_path.relative_to(
            AUDIT_DIR
        ).as_posix(),
        "comparison_image_file": comparison_path.relative_to(
            AUDIT_DIR
        ).as_posix(),
    }


def _copy_referenced_media_to_zip(
    zf: zipfile.ZipFile,
    rows: Iterable[dict],
) -> list[str]:
    written = set()
    skipped: list[str] = []

    for row in rows:
        for field in (
            "current_image_file",
            "normalized_image_file",
            "comparison_image_file",
        ):
            relative = (row.get(field) or "").strip()
            if not relative:
                continue
            try:
                source = resolve_under_root(
                    AUDIT_DIR,
                    relative,
                    must_exist=True,
                )
                archive_name = safe_archive_name(
                    AUDIT_DIR,
                    source,
                )
            except UnsafePathError:
                skipped.append(
                    f"Skipped unsafe or unavailable {field} for {row.get('product_name') or 'Unnamed product'}"
                )
                continue

            if archive_name in written:
                continue
            written.add(archive_name)
            zf.write(source, archive_name)

    return skipped


def _safe_report_media_href(reference: object) -> str:
    raw = str(reference or "").strip()
    if not raw:
        return ""
    try:
        path = resolve_under_root(AUDIT_DIR, raw, must_exist=True)
        return safe_archive_name(AUDIT_DIR, path)
    except UnsafePathError:
        return ""


def visual_report_html(rows: Iterable[dict]) -> str:
    rows = sorted(list(rows), key=_row_sort_key)

    chunks = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Website Product Image Visual Audit</title>",
        "<style>",
        """
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 28px;
            background: #f4f5f7;
            color: #18181b;
        }
        h1 { margin-top: 0; }
        h2 {
            margin-top: 42px;
            border-bottom: 2px solid #d4d4d8;
            padding-bottom: 8px;
        }
        h3 { margin-top: 28px; }
        h4 {
            margin: 24px 0 10px;
            background: #e4e4e7;
            padding: 9px 12px;
            border-radius: 8px;
        }
        .card {
            background: white;
            border: 1px solid #d4d4d8;
            border-radius: 12px;
            padding: 16px;
            margin: 14px 0;
        }
        .meta {
            color: #52525b;
            font-size: 13px;
            margin-bottom: 12px;
        }
        .problem {
            background: #fff7ed;
            border-left: 4px solid #ea580c;
            padding: 10px 12px;
            margin: 10px 0;
        }
        .fix {
            background: #f0fdf4;
            border-left: 4px solid #16a34a;
            padding: 10px 12px;
            margin: 10px 0 14px;
        }
        .images {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }
        .image-panel {
            min-width: 0;
        }
        .image-panel strong {
            display: block;
            margin-bottom: 7px;
        }
        img {
            display: block;
            max-width: 100%;
            width: 100%;
            height: auto;
            object-fit: contain;
            background: white;
            border: 1px solid #e4e4e7;
            border-radius: 8px;
        }
        .comparison-link {
            display: inline-block;
            margin-top: 10px;
        }
        @media (max-width: 720px) {
            .images { grid-template-columns: 1fr; }
            body { padding: 14px; }
        }
        """,
        "</style>",
        "</head>",
        "<body>",
        "<h1>Website Product Image Visual Audit</h1>",
        (
            "<p>This report is organized as "
            "<strong>Category → Subcategory → Page</strong>. "
            "Each product shows the current image and normalized image "
            "side by side.</p>"
        ),
    ]

    current_category = None
    current_subcategory = None
    current_page = None

    for row in rows:
        category = row.get(
            "main_category"
        ) or "Uncategorized"
        subcategory = row.get(
            "subcategory"
        ) or "General"
        page_label = row.get(
            "page_label"
        ) or "Page"

        if category != current_category:
            chunks.append(
                f"<h2>{html.escape(category)}</h2>"
            )
            current_category = category
            current_subcategory = None
            current_page = None

        if subcategory != current_subcategory:
            chunks.append(
                f"<h3>{html.escape(subcategory)}</h3>"
            )
            current_subcategory = subcategory
            current_page = None

        if page_label != current_page:
            chunks.append(
                f"<h4>{html.escape(page_label)}</h4>"
            )
            current_page = page_label

        product = html.escape(
            row.get("product_name") or "Unnamed product"
        )
        problem = html.escape(
            row.get("problem_summary") or ""
        )
        suggested = html.escape(
            row.get("suggested_fix") or ""
        )
        score = html.escape(
            str(row.get("consistency_score") or "")
        )
        status = html.escape(
            row.get("status") or ""
        )

        current_image = html.escape(
            _safe_report_media_href(row.get("current_image_file"))
        )
        normalized_image = html.escape(
            _safe_report_media_href(row.get("normalized_image_file"))
        )
        comparison_image = html.escape(
            _safe_report_media_href(row.get("comparison_image_file"))
        )

        chunks.extend(
            [
                '<section class="card">',
                f"<h3>{product}</h3>",
                (
                    '<div class="meta">'
                    f"Status: {status} · Score: {score} · "
                    f"Profile: {html.escape(row.get('profile') or '')} · "
                    f"Subtype: {html.escape(row.get('subtype') or '')}"
                    "</div>"
                ),
                (
                    '<div class="meta"><strong>Product page:</strong> '
                    f'<a href="{html.escape(row.get("product_url") or "")}">'
                    f'{html.escape(row.get("product_url") or "Unavailable")}</a>'
                    "</div>"
                    if row.get("product_url")
                    else '<div class="meta"><strong>Product page:</strong> Unavailable</div>'
                ),
                (
                    f'<div class="problem"><strong>Problem:</strong> '
                    f"{problem}</div>"
                ),
                (
                    f'<div class="fix"><strong>Suggested fix:</strong> '
                    f"{suggested}</div>"
                ),
                '<div class="images">',
                '<div class="image-panel"><strong>Current</strong>',
                (
                    f'<img src="{current_image}" alt="Current {product}">'
                    if current_image
                    else "<em>No saved current image.</em>"
                ),
                "</div>",
                '<div class="image-panel"><strong>Normalized</strong>',
                (
                    f'<img src="{normalized_image}" alt="Normalized {product}">'
                    if normalized_image
                    else "<em>No saved normalized image.</em>"
                ),
                "</div>",
                "</div>",
            ]
        )

        if comparison_image:
            chunks.append(
                (
                    f'<a class="comparison-link" '
                    f'href="{comparison_image}">'
                    "Open exported side-by-side comparison</a>"
                )
            )

        chunks.append("</section>")

    chunks.extend(
        [
            "</body>",
            "</html>",
        ]
    )

    return "\n".join(chunks)


def _safe_excel_sheet_name(name: str) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "_", name or "Sheet")
    return name[:31] or "Sheet"


def build_boss_excel_report(rows: Iterable[dict]) -> bytes:
    """
    Actual images cannot live inside a CSV. This workbook solves that problem:
    it contains a Simple Audit sheet and an Advanced Audit sheet with embedded
    Current / Normalized / Before-After thumbnails.
    """
    rows = sorted(list(rows), key=_row_sort_key)

    try:
        import xlsxwriter
    except Exception as exc:
        raise RuntimeError(
            "Excel visual export requires XlsxWriter. "
            "Run the updated requirements installation."
        ) from exc

    out = io.BytesIO()

    workbook = xlsxwriter.Workbook(
        out,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )

    title_format = workbook.add_format(
        {
            "bold": True,
            "font_size": 16,
            "font_color": "#FFFFFF",
            "bg_color": "#1F4E78",
            "align": "left",
            "valign": "vcenter",
        }
    )

    header_format = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#4472C4",
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
        }
    )

    text_format = workbook.add_format(
        {
            "border": 1,
            "valign": "top",
            "text_wrap": True,
        }
    )

    center_format = workbook.add_format(
        {
            "border": 1,
            "valign": "vcenter",
            "align": "center",
            "text_wrap": True,
        }
    )

    simple_sheet = workbook.add_worksheet(
        _safe_excel_sheet_name("Simple Audit")
    )
    advanced_sheet = workbook.add_worksheet(
        _safe_excel_sheet_name("Advanced Audit")
    )

    def write_xlsx_safe_cell(sheet, row_index, col, value, cell_format):
        """Write untrusted strings explicitly as text, never as formulas."""
        if value is None:
            sheet.write_blank(row_index, col, None, cell_format)
        elif isinstance(value, bool):
            sheet.write_boolean(row_index, col, value, cell_format)
        elif isinstance(value, (int, float)):
            sheet.write_number(row_index, col, value, cell_format)
        else:
            sheet.write_string(row_index, col, str(value), cell_format)

    def write_sheet(
        sheet,
        field_map,
        include_images=True,
    ):
        image_headers = (
            [
                "Current Image",
                "Normalized Image",
                "Before / After",
            ]
            if include_images
            else []
        )

        total_cols = len(field_map) + len(image_headers)

        sheet.merge_range(
            0,
            0,
            0,
            max(total_cols - 1, 0),
            (
                "Product Image Audit — "
                + (
                    "Boss-Friendly Summary"
                    if sheet.name == "Simple Audit"
                    else "Advanced Technical Detail"
                )
            ),
            title_format,
        )
        sheet.set_row(0, 26)

        for col, (label, _) in enumerate(field_map):
            sheet.write(
                1,
                col,
                label,
                header_format,
            )

        for offset, label in enumerate(image_headers):
            sheet.write(
                1,
                len(field_map) + offset,
                label,
                header_format,
            )

        sheet.freeze_panes(2, 0)
        sheet.autofilter(
            1,
            0,
            max(1, len(rows) + 1),
            max(total_cols - 1, 0),
        )

        # Sensible boss-friendly widths.
        width_by_label = {
            "Main Category": 18,
            "Subcategory": 18,
            "Page URL": 32,
            "Page Label": 12,
            "Product Name": 34,
            "Product URL": 44,
            "Product Status": 14,
            "Problem Summary": 42,
            "Consistency Score": 16,
            "Suggested Fix": 42,
            "Profile": 14,
            "Subtype": 18,
        }

        for col, (label, _) in enumerate(field_map):
            sheet.set_column(
                col,
                col,
                width_by_label.get(
                    label,
                    20,
                ),
            )

        if include_images:
            start = len(field_map)
            for c in range(start, start + 3):
                sheet.set_column(c, c, 23)

        for row_index, row in enumerate(rows, start=2):
            for col, (_, field) in enumerate(field_map):
                value = row.get(field, "")
                cell_format = (
                    center_format
                    if field
                    in {
                        "status",
                        "consistency_score",
                        "profile",
                        "subtype",
                        "component_count",
                    }
                    else text_format
                )

                if field == "product_url" and value:
                    safe_url = canonical_http_url(value)
                    if safe_url:
                        try:
                            sheet.write_url(
                                row_index,
                                col,
                                safe_url,
                                text_format,
                                string=str(value),
                            )
                            continue
                        except Exception:
                            pass
                write_xlsx_safe_cell(
                    sheet,
                    row_index,
                    col,
                    value,
                    cell_format,
                )


            if include_images:
                sheet.set_row(row_index, 112)

                for image_offset, field in enumerate(
                    (
                        "current_image_file",
                        "normalized_image_file",
                        "comparison_image_file",
                    )
                ):
                    rel = (row.get(field) or "").strip()
                    col = len(field_map) + image_offset

                    if not rel:
                        sheet.write(
                            row_index,
                            col,
                            "No saved image",
                            center_format,
                        )
                        continue

                    try:
                        image_path = resolve_under_root(
                            AUDIT_DIR,
                            rel,
                            must_exist=True,
                        )
                    except UnsafePathError:
                        write_xlsx_safe_cell(
                            sheet,
                            row_index,
                            col,
                            "Unsafe or missing image reference",
                            center_format,
                        )
                        continue

                    try:
                        sheet.insert_image(
                            row_index,
                            col,
                            str(image_path),
                            {
                                "x_scale": 0.20,
                                "y_scale": 0.20,
                                "x_offset": 4,
                                "y_offset": 4,
                                "object_position": 1,
                            },
                        )
                    except Exception:
                        write_xlsx_safe_cell(
                            sheet,
                            row_index,
                            col,
                            rel,
                            text_format,
                        )

    write_sheet(
        simple_sheet,
        SIMPLE_EXPORT_FIELDS,
        include_images=True,
    )
    write_sheet(
        advanced_sheet,
        ADVANCED_EXPORT_FIELDS,
        include_images=True,
    )

    workbook.close()
    return out.getvalue()


def build_master_visual_package(
    rows: Iterable[dict],
) -> bytes:
    rows = list(rows)
    out = io.BytesIO()

    with zipfile.ZipFile(
        out,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as zf:
        # Two clearly separated CSV audiences.
        zf.writestr(
            "01_Boss_Simple_Audit.csv",
            simple_csv_bytes(rows),
        )
        zf.writestr(
            "02_Advanced_Technical_Audit.csv",
            advanced_csv_bytes(rows),
        )

        # Human visual reports with actual images.
        try:
            zf.writestr(
                "03_Boss_Visual_Audit.xlsx",
                build_boss_excel_report(rows),
            )
        except Exception as exc:
            zf.writestr(
                "03_Boss_Visual_Audit_EXCEL_ERROR.txt",
                str(exc),
            )

        zf.writestr(
            "04_Visual_Report.html",
            visual_report_html(rows),
        )

        skipped_media = _copy_referenced_media_to_zip(
            zf,
            rows,
        )
        if skipped_media:
            zf.writestr(
                "UNSAFE_OR_MISSING_MEDIA_SKIPPED.txt",
                "\n".join(skipped_media),
            )

        zf.writestr(
            "README.txt",
            (
                "START HERE:\n"
                "1. 01_Boss_Simple_Audit.csv is the clean report for management.\n"
                "2. 02_Advanced_Technical_Audit.csv contains engineering metrics.\n"
                "3. 03_Boss_Visual_Audit.xlsx has embedded Current / Normalized / Before-After images.\n"
                "4. 04_Visual_Report.html is a browser-friendly visual report.\n\n"
                "The CSV reports intentionally do NOT contain product URLs, source image URLs, "
                "effective-detection fields, or audit timestamps.\n"
            ),
        )

    return out.getvalue()



def separate_page_csv_zip(rows: Iterable[dict]) -> bytes:
    """
    Export separate Simple + Advanced CSV files for every:
        Main Category / Subcategory / Page

    Example:
        Coffee Machines/Espresso/Page 1 - Simple.csv
        Coffee Machines/Espresso/Page 1 - Advanced.csv
    """
    grouped: Dict[Tuple[str, str, str], List[dict]] = {}

    for row in rows:
        key = (
            row.get("main_category") or "Uncategorized",
            row.get("subcategory") or "General",
            row.get("page_label") or "Page",
        )
        grouped.setdefault(key, []).append(row)

    out = io.BytesIO()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        if not grouped:
            zf.writestr(
                "README.txt",
                "The master audit is currently empty.",
            )
        else:
            for (
                category,
                subcategory,
                page_label,
            ), page_rows in sorted(
                grouped.items(),
                key=lambda kv: (
                    _natural_key(kv[0][0]),
                    _natural_key(kv[0][1]),
                    _natural_key(kv[0][2]),
                ),
            ):
                base_path = (
                    f"{safe_path_part(category)}/"
                    f"{safe_path_part(subcategory)}/"
                    f"{safe_path_part(page_label)}"
                )

                zf.writestr(
                    f"{base_path} - Simple.csv",
                    simple_csv_bytes(page_rows),
                )
                zf.writestr(
                    f"{base_path} - Advanced.csv",
                    advanced_csv_bytes(page_rows),
                )

    return out.getvalue()


def hierarchy_summary(rows: Iterable[dict]) -> dict:
    """
    Return a compact hierarchy tree used by the Advanced Audit UI:

        {
            "Coffee Machines": {
                "Espresso": {
                    "Page 1": 12,
                    "Page 2": 8,
                }
            }
        }

    The integer value is the number of audit rows saved for that page.
    """
    tree: Dict[str, Dict[str, Dict[str, int]]] = {}

    for row in rows:
        category = row.get("main_category") or "Uncategorized"
        subcategory = row.get("subcategory") or "General"
        page_label = row.get("page_label") or "Page"

        tree.setdefault(category, {})
        tree[category].setdefault(subcategory, {})
        tree[category][subcategory][page_label] = (
            tree[category][subcategory].get(page_label, 0) + 1
        )

    return tree
