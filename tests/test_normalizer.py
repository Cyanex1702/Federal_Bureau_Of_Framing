from __future__ import annotations
import io
import pytest
from PIL import Image, ImageDraw

from normalizer import process_image_bytes
from profile_config import CATEGORY_PROFILE, CROP_MAX


def image_bytes(size=(260,260), *, mode="RGB", transparent=False, aspect_box=(60,40,200,220)):
    if transparent or mode == "RGBA":
        img = Image.new("RGBA", size, (255,255,255,0) if transparent else (255,255,255,255))
        fill = (30,30,30,255)
    else:
        img = Image.new("RGB", size, (255,255,255))
        fill = (30,30,30)
    draw = ImageDraw.Draw(img)
    draw.rectangle(aspect_box, fill=fill)
    out = io.BytesIO(); img.save(out, format="PNG")
    return out.getvalue()

@pytest.mark.parametrize("category", sorted(CATEGORY_PROFILE))
def test_all_categories_normalize(category):
    result = process_image_bytes(image_bytes(), category=category, canvas_size=256, normalization_mode="amazon_a")
    assert result["image"].size == (256,256)
    assert result["metrics"]["qa_pass"] in {True, False}
    assert result["metrics"]["cropped"] is False

@pytest.mark.parametrize("body_mode", ["auto","main","full","multipart"])
def test_body_modes(body_mode):
    result = process_image_bytes(image_bytes(), category="Coffee machine", canvas_size=256, body_mode=body_mode)
    assert result["image"].size == (256,256)

@pytest.mark.parametrize("anchor_mode", ["auto","primary","full_center","visual_center"])
def test_anchor_modes(anchor_mode):
    result = process_image_bytes(image_bytes(), category="Hood", canvas_size=256, anchor_mode=anchor_mode)
    assert result["image"].size == (256,256)

@pytest.mark.parametrize("size,box", [
    ((80,500),(10,70,70,440)),
    ((500,80),(70,10,440,70)),
    ((32,32),(5,5,27,27)),
    ((1200,800),(250,120,950,700)),
])
def test_unusual_tiny_large_aspect_ratios(size, box):
    result = process_image_bytes(image_bytes(size, aspect_box=box), category="Oven", canvas_size=256)
    assert result["image"].size == (256,256)

@pytest.mark.parametrize("mode,transparent", [("RGB",False),("RGBA",False),("RGBA",True)])
def test_rgb_rgba_transparency(mode, transparent):
    result = process_image_bytes(image_bytes(mode=mode, transparent=transparent), category="Oven", canvas_size=256)
    assert result["image"].size == (256,256)

@pytest.mark.parametrize("field,value", [
    ("crop_left",0.0),("crop_left",CROP_MAX),("crop_right",CROP_MAX),
    ("crop_top",CROP_MAX),("crop_bottom",CROP_MAX),
    ("scale_bias",-0.45),("scale_bias",0.60),
    ("x_nudge",-0.30),("x_nudge",0.30),("y_nudge",-0.30),("y_nudge",0.30),
    ("baseline_shift",-0.10),("baseline_shift",0.10),
])
def test_adjustment_boundaries(field, value):
    result = process_image_bytes(image_bytes(), category="Oven", canvas_size=256, adjustments={field:value})
    assert result["image"].size == (256,256)

@pytest.mark.parametrize("field,value", [
    ("crop_left",0.451),("crop_right",-0.001),("scale_bias",0.601),("scale_bias",-0.451),
    ("x_nudge",.301),("y_nudge",-.301),("baseline_shift",.101),
])
def test_invalid_adjustments_rejected(field, value):
    with pytest.raises(ValueError):
        process_image_bytes(image_bytes(), category="Oven", canvas_size=256, adjustments={field:value})

@pytest.mark.parametrize("body_mode", ["wat"])
def test_invalid_body_mode(body_mode):
    with pytest.raises(ValueError): process_image_bytes(image_bytes(), body_mode=body_mode, canvas_size=256)

@pytest.mark.parametrize("anchor_mode", ["wat"])
def test_invalid_anchor_mode(anchor_mode):
    with pytest.raises(ValueError): process_image_bytes(image_bytes(), anchor_mode=anchor_mode, canvas_size=256)


def test_corrupt_input_rejected():
    with pytest.raises(Exception): process_image_bytes(b"not an image")


def test_deterministic_geometry_same_input():
    data = image_bytes()
    a = process_image_bytes(data, category="Coffee machine", canvas_size=256, normalization_mode="amazon_a")
    b = process_image_bytes(data, category="Coffee machine", canvas_size=256, normalization_mode="amazon_a")
    assert a["image"].tobytes() == b["image"].tobytes()
    assert a["metrics"]["size_lock_actual_px"] == b["metrics"]["size_lock_actual_px"]
    assert a["metrics"]["anchor_error_x_px"] == b["metrics"]["anchor_error_x_px"]


def test_crop_is_reported_and_stays_in_canvas():
    result = process_image_bytes(image_bytes(), category="Oven", canvas_size=256, adjustments={"crop_left":.2})
    assert result["metrics"]["manual_crop_applied"] is True
    assert result["metrics"]["crop_left"] == pytest.approx(.2)
    assert result["image"].size == (256,256)
