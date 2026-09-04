from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone
import pytest
import exception_store as es

BASE_OVERRIDE = {
    "body_mode": "auto", "anchor_mode": "auto", "scale_bias": 0.0,
    "x_nudge": 0.0, "y_nudge": 0.0, "baseline_shift": 0.0,
    "crop_left": 0.0, "crop_right": 0.0, "crop_top": 0.0, "crop_bottom": 0.0,
}

@pytest.fixture
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "product_exception_library.json"
    monkeypatch.setattr(es, "STORE_PATH", path)
    return path


def entry(identity, *, url="https://example.com/p", name="Thing", when="2026-01-01T00:00:00+00:00", scale=0.0):
    return {
        "identity": identity,
        "product_url": url,
        "name": name,
        "normalized_name": name.casefold(),
        "category": "Oven",
        "archetype": "",
        "image_url": "",
        "override": {**BASE_OVERRIDE, "scale_bias": scale},
        "saved_at": when,
        "updated_at": when,
    }


def test_valid_json_loads(store_path):
    store_path.write_text(json.dumps({"schema_version": 1, "entries": {}}))
    assert es.load_exception_library()["entries"] == {}

@pytest.mark.parametrize("content", ["", "{", '{"schema_version":1,"entries":'])
def test_corrupt_json_preserved_and_backed_up(store_path, content):
    store_path.write_text(content)
    with pytest.raises(es.ExceptionLibraryCorruptError):
        es.load_exception_library()
    assert store_path.read_text() == content
    assert list(store_path.parent.glob(store_path.name + ".corrupt.*.bak"))


def test_unexpected_schema_is_not_silently_replaced(store_path):
    original = '{"schema_version":1,"entries":[]}'
    store_path.write_text(original)
    with pytest.raises(es.ExceptionLibraryCorruptError):
        es.save_exception({"name":"X"}, BASE_OVERRIDE)
    assert store_path.read_text() == original


def test_save_is_atomic_and_creates_backup(store_path):
    store_path.write_text(json.dumps({"schema_version": 1, "entries": {}}))
    es.save_exception({"name":"X", "product_url":"https://example.com/x"}, BASE_OVERRIDE)
    payload = json.loads(store_path.read_text())
    assert payload["entries"]
    assert store_path.with_suffix(store_path.suffix + ".bak").exists()


def test_interrupted_replace_preserves_old_store(store_path, monkeypatch):
    original = {"schema_version": 1, "entries": {}}
    store_path.write_text(json.dumps(original))
    def explode(src, dst):
        raise OSError("simulated crash")
    monkeypatch.setattr(es, "_replace_file", explode)
    with pytest.raises(OSError):
        es.save_exception({"name":"X", "product_url":"https://example.com/x"}, BASE_OVERRIDE)
    assert json.loads(store_path.read_text()) == original


def test_newest_matching_tuning_wins(store_path):
    payload = {"schema_version": 1, "entries": {
        "old": entry("old", when="2025-01-01T00:00:00+00:00", scale=.1),
        "new": entry("new", when="2026-01-01T00:00:00+00:00", scale=.2),
    }}
    store_path.write_text(json.dumps(payload))
    result = es.find_exception_suggestions({"name":"Thing", "product_url":"https://example.com/p", "item_category":"Oven"})
    assert result[0]["identity"] == "new"
    assert result[0]["override"]["scale_bias"] == .2


def test_malformed_timestamp_does_not_crash(store_path):
    payload = {"schema_version": 1, "entries": {
        "bad": entry("bad", when="not-a-date", scale=.1),
        "good": entry("good", when="2026-01-01T00:00:00+00:00", scale=.2),
    }}
    store_path.write_text(json.dumps(payload))
    result = es.find_exception_suggestions({"name":"Thing", "product_url":"https://example.com/p"})
    assert result[0]["identity"] == "good"


def test_exact_url_outranks_newer_name_fallback(store_path):
    old_exact = entry("exact", url="https://example.com/p", when="2025-01-01T00:00:00+00:00", scale=.1)
    newer_name = entry("name", url="https://example.com/other", when="2026-01-01T00:00:00+00:00", scale=.3)
    payload = {"schema_version":1, "entries":{"exact":old_exact, "name":newer_name}}
    store_path.write_text(json.dumps(payload))
    result = es.find_exception_suggestions({"name":"Thing", "product_url":"https://example.com/p", "item_category":"Oven"})
    assert result[0]["identity"] == "exact"


def test_crop_45_persists(store_path):
    store_path.write_text(json.dumps({"schema_version":1,"entries":{}}))
    saved = es.save_exception({"name":"X", "product_url":"https://example.com/x"}, {**BASE_OVERRIDE, "crop_left":.45})
    assert saved["override"]["crop_left"] == .45


def test_crop_above_45_rejected(store_path):
    store_path.write_text(json.dumps({"schema_version":1,"entries":{}}))
    with pytest.raises(es.ExceptionLibraryCorruptError):
        es.save_exception({"name":"X"}, {**BASE_OVERRIDE, "crop_left":.46})
