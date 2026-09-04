from __future__ import annotations
import os
from pathlib import Path
import pytest

from safe_io import (
    UnsafePathError,
    canonical_http_url,
    product_storage_component,
    resolve_under_root,
    sanitize_path_component,
    stable_product_id,
)

@pytest.mark.parametrize("raw,expected_prefix", [
    ("CON", "_CON"), ("NUL", "_NUL"), ("COM1", "_COM1"), ("LPT9", "_LPT9"),
    ("hello.", "hello"), ("hello ", "hello"), ("a/b:c*", "a_b_c_"),
    ("Café 冰箱", "Café 冰箱"), ("", "Item"),
])
def test_windows_safe_names(raw, expected_prefix):
    assert sanitize_path_component(raw).startswith(expected_prefix)


def test_very_long_name_is_bounded():
    assert len(sanitize_path_component("x" * 500, max_length=80)) <= 80


def test_same_name_different_urls_have_different_ids():
    a = {"product_name": "Same Name", "product_url": "https://example.com/a"}
    b = {"product_name": "Same Name", "product_url": "https://example.com/b"}
    assert stable_product_id(a) != stable_product_id(b)
    assert product_storage_component(a) != product_storage_component(b)


def test_same_url_is_predictable():
    a = {"product_name": "A", "product_url": "https://EXAMPLE.com/p/1/"}
    b = {"product_name": "B", "product_url": "https://example.com/p/1"}
    assert stable_product_id(a) == stable_product_id(b)


def test_empty_names_still_get_stable_storage_component():
    value = product_storage_component({"product_name": "", "product_url": "https://example.com/p"})
    assert value.startswith("Product__")


def test_url_canonicalization():
    assert canonical_http_url("HTTPS://Example.COM:443/a/?b=2&a=1#x") == "https://example.com/a?a=1&b=2"


def test_resolve_normal_nested(tmp_path):
    root = tmp_path / "audit"
    nested = root / "media" / "x.png"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    assert resolve_under_root(root, "media/x.png") == nested.resolve()

@pytest.mark.parametrize("ref", ["../secret.txt", "../../etc/passwd"])
def test_resolve_rejects_traversal(tmp_path, ref):
    root = tmp_path / "audit"
    root.mkdir()
    with pytest.raises(UnsafePathError):
        resolve_under_root(root, ref, must_exist=False)


def test_resolve_rejects_absolute_external(tmp_path):
    root = tmp_path / "audit"; root.mkdir()
    external = tmp_path / "secret.txt"; external.write_text("secret")
    with pytest.raises(UnsafePathError):
        resolve_under_root(root, external)


def test_resolve_rejects_symlink_escape(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    root = tmp_path / "audit"; root.mkdir()
    outside = tmp_path / "outside.txt"; outside.write_text("secret")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(UnsafePathError):
        resolve_under_root(root, "link.txt")


def test_sanitization_collisions_receive_different_hash_suffixes():
    assert sanitize_path_component("a/b") != sanitize_path_component("a:b")
