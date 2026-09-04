from __future__ import annotations
import json
from pathlib import Path
import pytest
import profile_config as pc


def test_profile_config_validates():
    pc.validate_profile_config()


def test_profiles_json_is_generated_from_python_source_of_truth():
    path = Path(__file__).resolve().parents[1] / "profiles.json"
    assert json.loads(path.read_text(encoding="utf-8")) == pc.generated_profiles_document("7.3.1")


def test_all_category_profiles_exist():
    assert set(pc.CATEGORY_PROFILE.values()) <= set(pc.PROFILES)


def test_all_anchor_categories_are_known():
    assert set(pc.CATEGORY_ANCHOR_POLICY) <= set(pc.CATEGORY_PROFILE)


def test_crop_range_single_source_is_45_percent():
    assert pc.CROP_MIN == 0.0 and pc.CROP_MAX == 0.45


def test_invalid_alignment_is_detected(monkeypatch):
    broken = dict(pc.PROFILES)
    broken["tall"] = {**broken["tall"], "alignment":"sideways"}
    monkeypatch.setattr(pc, "PROFILES", broken)
    with pytest.raises(ValueError): pc.validate_profile_config()
