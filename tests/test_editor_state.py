from __future__ import annotations
import pytest
from editor_state import atomic_apply_override, geometry_reset_token, history_record, history_step, validate_editor_tuning

BASE = {
    "body_mode": "auto", "anchor_mode": "auto", "scale_bias": 0.0,
    "x_nudge": 0.0, "y_nudge": 0.0, "baseline_shift": 0.0,
    "crop_left": 0.0, "crop_right": 0.0, "crop_top": 0.0, "crop_bottom": 0.0,
}


def test_successful_apply_commits_after_prepare():
    state = {}
    result = atomic_apply_override(state, "a", {**BASE, "scale_bias": .1}, lambda t: {"ok": t["scale_bias"]})
    assert result == {"ok": .1}
    assert state["a"]["scale_bias"] == .1


def test_failed_apply_does_not_mutate():
    state = {"a": {**BASE, "scale_bias": .2}}
    before = dict(state["a"])
    def fail(_): raise RuntimeError("boom")
    with pytest.raises(RuntimeError): atomic_apply_override(state, "a", {**BASE, "scale_bias": .4}, fail)
    assert state["a"] == before


def test_repeated_failed_apply_does_not_mutate():
    state = {}
    def fail(_): raise RuntimeError("boom")
    for _ in range(3):
        with pytest.raises(RuntimeError): atomic_apply_override(state, "a", BASE, fail)
    assert state == {}


def test_failure_does_not_leak_to_other_product():
    state = {"b": {**BASE, "x_nudge": .1}}
    def fail(_): raise RuntimeError("boom")
    with pytest.raises(RuntimeError): atomic_apply_override(state, "a", {**BASE, "x_nudge": -.1}, fail)
    assert "a" not in state and state["b"]["x_nudge"] == .1


def test_failed_apply_then_other_operation():
    state = {}
    with pytest.raises(RuntimeError): atomic_apply_override(state, "a", BASE, lambda _: (_ for _ in ()).throw(RuntimeError()))
    atomic_apply_override(state, "b", {**BASE, "y_nudge": .05}, lambda t: "ok")
    assert "a" not in state and state["b"]["y_nudge"] == .05


def test_apply_then_undo_history():
    history = {}
    history_record(history, "p", BASE)
    history_record(history, "p", {**BASE, "scale_bias": .1})
    restored = history_step(history, "p", -1)
    assert restored["scale_bias"] == 0.0


def test_redo_and_edit_after_undo_drops_old_branch():
    history = {}
    history_record(history, "p", BASE)
    history_record(history, "p", {**BASE, "scale_bias": .1})
    assert history_step(history, "p", -1)["scale_bias"] == 0
    history_record(history, "p", {**BASE, "scale_bias": .2})
    assert history_step(history, "p", 1) is None

@pytest.mark.parametrize("field,value", [
    ("crop_left", .46), ("crop_right", -.01), ("scale_bias", .61),
    ("x_nudge", .31), ("y_nudge", -.31), ("baseline_shift", .11),
])
def test_invalid_tuning_rejected(field, value):
    with pytest.raises(ValueError): validate_editor_tuning({**BASE, field: value})


def test_crop_boundary_45_is_valid():
    assert validate_editor_tuning({**BASE, "crop_left": .45})["crop_left"] == .45

@pytest.mark.parametrize("field,newvalue", [
    ("scale_bias", .1), ("x_nudge", .1), ("y_nudge", -.1), ("baseline_shift", .02),
    ("crop_left", .1), ("crop_right", .1), ("crop_top", .1), ("crop_bottom", .1),
    ("body_mode", "main"), ("anchor_mode", "primary"),
])
def test_geometry_token_changes_for_every_geometry_field(field, newvalue):
    a = geometry_reset_token("p", BASE, category="Oven", canvas_size=1000, segmentation_mode="auto", normalization_mode="amazon_a")
    b = geometry_reset_token("p", {**BASE, field: newvalue}, category="Oven", canvas_size=1000, segmentation_mode="auto", normalization_mode="amazon_a")
    assert a != b


def test_geometry_token_changes_when_switching_product_category_or_mode():
    base = geometry_reset_token("p1", BASE, category="Oven", canvas_size=1000, segmentation_mode="auto", normalization_mode="amazon_a")
    assert base != geometry_reset_token("p2", BASE, category="Oven", canvas_size=1000, segmentation_mode="auto", normalization_mode="amazon_a")
    assert base != geometry_reset_token("p1", BASE, category="Hood", canvas_size=1000, segmentation_mode="auto", normalization_mode="amazon_a")
    assert base != geometry_reset_token("p1", BASE, category="Oven", canvas_size=1000, segmentation_mode="auto", normalization_mode="standard")
