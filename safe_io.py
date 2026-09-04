from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


class UnsafePathError(ValueError):
    pass


def sanitize_path_component(
    value: object,
    fallback: str = "Item",
    *,
    max_length: int = 96,
) -> str:
    """Return one Windows-safe, collision-resistant path component."""
    raw = unicodedata.normalize("NFC", str(value or ""))
    source = raw.strip()
    used_fallback = not bool(source)
    if used_fallback:
        raw = unicodedata.normalize("NFC", str(fallback or "Item"))
        source = raw.strip() or "Item"

    text = _INVALID_WINDOWS_CHARS.sub("_", source)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "Item"

    transformed = text != source or raw != source
    stem = text.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        text = "_" + text
        transformed = True

    max_length = max(12, int(max_length))
    if len(text) > max_length:
        transformed = True

    if transformed and not used_fallback:
        token = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]
        suffix = "__" + token
        text = text[: max(1, max_length - len(suffix))].rstrip(" .") + suffix
    else:
        text = text[:max_length].rstrip(" .")

    return text or "Item"


def canonical_http_url(value: object) -> str:
    """Normalize an http(s) URL for stable identities; non-http values return ''."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        p = urlsplit(raw)
    except Exception:
        return ""
    scheme = p.scheme.lower()
    if scheme not in {"http", "https"} or not p.hostname:
        return ""
    host = p.hostname.lower().rstrip(".")
    port = p.port
    default_port = 80 if scheme == "http" else 443
    netloc = host if not port or port == default_port else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/":
        path = path.rstrip("/")
    # Sort the query so common tracking/order variations do not create duplicates.
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def stable_product_id(record: dict, length: int = 16) -> str:
    """Collision-resistant deterministic identifier for audit/media storage."""
    for field in ("product_id", "audit_id", "record_id", "id"):
        value = str(record.get(field) or "").strip()
        if value:
            seed = f"{field}:{value}"
            break
    else:
        product_url = canonical_http_url(record.get("product_url"))
        image_url = canonical_http_url(
            record.get("image_url") or record.get("_source_image_url")
        )
        page_url = canonical_http_url(record.get("page_url"))
        name = unicodedata.normalize(
            "NFKC", str(record.get("product_name") or record.get("name") or "")
        ).casefold().strip()
        if product_url:
            seed = f"product_url:{product_url}"
        elif image_url:
            seed = f"image_url:{image_url}"
        else:
            seed = f"page:{page_url}|name:{name}"
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:length]


def product_storage_component(record: dict, max_length: int = 120) -> str:
    name = sanitize_path_component(
        record.get("product_name") or record.get("name"),
        "Product",
        max_length=max(20, max_length - 19),
    )
    token = stable_product_id(record)
    return sanitize_path_component(f"{name}__{token}", "Product", max_length=max_length)


def resolve_under_root(root: Path, reference: object, *, must_exist: bool = True) -> Path:
    """Resolve a stored path and prove it stays inside *root*, including symlinks."""
    root_resolved = Path(root).resolve()
    raw = str(reference or "").strip()
    if not raw:
        raise UnsafePathError("Empty file reference")
    ref = Path(raw)
    candidate = ref if ref.is_absolute() else root_resolved / ref
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError("Invalid file reference") from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError("File reference escapes the allowed directory") from exc
    if must_exist and (not resolved.exists() or not resolved.is_file()):
        raise UnsafePathError("Referenced file is unavailable")
    return resolved


def safe_archive_name(root: Path, path: Path) -> str:
    """Return a traversal-free POSIX archive name for a validated path."""
    root_resolved = Path(root).resolve()
    path_resolved = Path(path).resolve()
    try:
        relative = path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError("Archive source escapes the allowed directory") from exc
    parts = [sanitize_path_component(p, "Item", max_length=120) for p in relative.parts]
    if not parts or any(part in {".", ".."} for part in parts):
        raise UnsafePathError("Unsafe archive path")
    return "/".join(parts)
