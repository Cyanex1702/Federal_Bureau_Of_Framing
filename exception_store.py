from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from profile_config import CROP_MAX, CROP_MIN

STORE_PATH = Path(__file__).resolve().parent / "audit_data" / "product_exception_library.json"
SCHEMA_VERSION = 1


class ExceptionLibraryError(RuntimeError):
    pass


class ExceptionLibraryCorruptError(ExceptionLibraryError):
    pass


def _canonical_url(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        path = re.sub(r"/+$", "", parts.path or "/")
        return urlunsplit((scheme, netloc, path, "", ""))
    except Exception:
        return value.split("#")[0].split("?")[0].rstrip("/").casefold()


def _normalized_name(value: str | None) -> str:
    text = (value or "").casefold().replace("®", " ").replace("™", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _identity_seed(item: dict) -> str:
    product_url = _canonical_url(item.get("product_url"))
    if product_url:
        return f"url::{product_url}"
    name = _normalized_name(item.get("name"))
    category = (item.get("item_category") or "").casefold()
    archetype = (item.get("item_archetype") or "").casefold()
    return f"name::{name}::category::{category}::archetype::{archetype}"


def exception_identity(item: dict) -> str:
    return hashlib.sha256(_identity_seed(item).encode("utf-8")).hexdigest()[:24]


def _empty_store() -> dict:
    return {"schema_version": SCHEMA_VERSION, "entries": {}}


def _validate_override(override: object) -> dict:
    if not isinstance(override, dict):
        raise ExceptionLibraryCorruptError("Remembered tuning data has an invalid override record.")
    body = override.get("body_mode", "auto")
    anchor = override.get("anchor_mode", "auto")
    if body not in {"auto", "main", "full", "multipart"}:
        raise ExceptionLibraryCorruptError("Remembered tuning data has an invalid body mode.")
    if anchor not in {"auto", "primary", "full_center", "visual_center"}:
        raise ExceptionLibraryCorruptError("Remembered tuning data has an invalid anchor mode.")
    result = {
        "body_mode": body,
        "anchor_mode": anchor,
        "scale_bias": float(override.get("scale_bias", 0.0)),
        "x_nudge": float(override.get("x_nudge", 0.0)),
        "y_nudge": float(override.get("y_nudge", 0.0)),
        "baseline_shift": float(override.get("baseline_shift", 0.0)),
    }
    if not (-0.45 <= result["scale_bias"] <= 0.60):
        raise ExceptionLibraryCorruptError("Remembered tuning scale is outside the supported range.")
    if not (-0.30 <= result["x_nudge"] <= 0.30 and -0.30 <= result["y_nudge"] <= 0.30):
        raise ExceptionLibraryCorruptError("Remembered tuning position is outside the supported range.")
    if not (-0.10 <= result["baseline_shift"] <= 0.10):
        raise ExceptionLibraryCorruptError("Remembered tuning baseline is outside the supported range.")
    for key in ("crop_left", "crop_right", "crop_top", "crop_bottom"):
        value = float(override.get(key, 0.0))
        # Historical V7.2 builds officially supported up to 45%; preserve that range.
        if not (CROP_MIN <= value <= CROP_MAX):
            raise ExceptionLibraryCorruptError("Remembered tuning crop is outside the supported range.")
        result[key] = value
    return result


def _validate_store(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ExceptionLibraryCorruptError("Remembered tuning library has an invalid root object.")
    version = payload.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version < 1:
        raise ExceptionLibraryCorruptError("Remembered tuning library has an unsupported schema version.")
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise ExceptionLibraryCorruptError("Remembered tuning library has an invalid entries section.")
    validated = {"schema_version": version, "entries": {}}
    for key, entry in entries.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ExceptionLibraryCorruptError("Remembered tuning library contains an invalid entry.")
        clean = dict(entry)
        clean["override"] = _validate_override(clean.get("override", {}))
        validated["entries"][key] = clean
    return validated


def _corrupt_backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(path.name + f".corrupt.{stamp}.bak")


def _backup_corrupt_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = _corrupt_backup_path(path)
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError:
        return None


def load_exception_library() -> dict:
    if not STORE_PATH.exists():
        return _empty_store()
    try:
        raw = STORE_PATH.read_text(encoding="utf-8")
        if not raw.strip():
            raise ExceptionLibraryCorruptError("Remembered tuning library is empty/corrupted.")
        payload = json.loads(raw)
        return _validate_store(payload)
    except ExceptionLibraryCorruptError:
        _backup_corrupt_file(STORE_PATH)
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError, TypeError) as exc:
        _backup_corrupt_file(STORE_PATH)
        raise ExceptionLibraryCorruptError(
            "Remembered tuning library could not be loaded safely. The original file was preserved."
        ) from exc


def _replace_file(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _write_store(payload: dict) -> None:
    validated = _validate_store(payload)
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=STORE_PATH.name + ".",
        suffix=".tmp",
        dir=str(STORE_PATH.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(validated, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Verify the exact bytes we are about to promote.
        _validate_store(json.loads(temp_path.read_text(encoding="utf-8")))
        if STORE_PATH.exists():
            # Only back up an already-valid database. Corrupted originals are never overwritten
            # because load_exception_library() raises before _write_store is reached.
            _validate_store(json.loads(STORE_PATH.read_text(encoding="utf-8")))
            shutil.copy2(STORE_PATH, STORE_PATH.with_suffix(STORE_PATH.suffix + ".bak"))
        _replace_file(temp_path, STORE_PATH)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def save_exception(
    item: dict,
    override: dict,
    category: Optional[str] = None,
    note: str = "",
) -> dict:
    # Critical safety behavior: a corrupt store raises here, so we never convert it
    # into an empty database and overwrite recoverable history.
    payload = load_exception_library()
    identity = exception_identity(item)
    now = datetime.now(timezone.utc).isoformat()
    previous = payload["entries"].get(identity, {})
    entry = {
        "identity": identity,
        "product_url": _canonical_url(item.get("product_url")),
        "name": item.get("name", "Unnamed product"),
        "normalized_name": _normalized_name(item.get("name")),
        "category": category or item.get("item_category") or "",
        "archetype": item.get("item_archetype", ""),
        "image_url": item.get("image_url", ""),
        "override": _validate_override(override),
        "note": note.strip(),
        "created_at": previous.get("created_at") or previous.get("saved_at") or now,
        "updated_at": now,
        "saved_at": now,
    }
    payload["entries"][identity] = entry
    _write_store(payload)
    return entry


def delete_exception(identity: str) -> bool:
    payload = load_exception_library()
    if identity not in payload["entries"]:
        return False
    payload["entries"].pop(identity, None)
    _write_store(payload)
    return True


def _timestamp_rank(entry: dict) -> float:
    for field in ("updated_at", "saved_at", "created_at"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError, OverflowError):
            continue
    return float("-inf")


def find_exception_suggestions(item: dict, limit: int = 4) -> list[dict]:
    """Exact URL outranks fallback name; newest valid record wins within a match level."""
    payload = load_exception_library()
    target_url = _canonical_url(item.get("product_url"))
    target_name = _normalized_name(item.get("name"))
    target_category = (item.get("item_category") or "").casefold()
    target_archetype = (item.get("item_archetype") or "").casefold()
    ranked = []
    for entry in payload["entries"].values():
        score = 0
        match_type = None
        if target_url and entry.get("product_url") == target_url:
            score = 100
            match_type = "Exact product URL"
        elif target_name and entry.get("normalized_name") == target_name:
            score = 75
            match_type = "Exact product name"
            if target_category and str(entry.get("category", "")).casefold() == target_category:
                score += 5
            if target_archetype and str(entry.get("archetype", "")).casefold() == target_archetype:
                score += 5
        else:
            continue
        ranked.append({**entry, "match_score": score, "match_type": match_type})
    ranked.sort(key=lambda x: (-int(x.get("match_score", 0)), -_timestamp_rank(x), str(x.get("identity", ""))))
    return ranked[: max(0, int(limit))]
