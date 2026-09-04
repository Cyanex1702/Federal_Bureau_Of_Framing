from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

import streamlit.components.v1 as components


_COMPONENT_DIR = (
    Path(__file__).resolve().parent
    / "components"
    / "product_transform"
)

_product_transform_component = (
    components.declare_component(
        "fbf_product_transform",
        path=str(_COMPONENT_DIR),
    )
)


def _b64(data: bytes | None) -> str:
    if not data:
        return ""
    return base64.b64encode(
        data
    ).decode("ascii")


def product_transform_editor(
    *,
    product_png: bytes,
    stencil_png: Optional[bytes],
    initial_box: dict,
    initial_crop: dict | None = None,
    key: str,
    stencil_opacity: float = 0.24,
    reset_token: str = "",
):
    """
    Interactive transform component.

    Returns a dict after the user finishes a drag/resize gesture:
      {
        "x": 0..1,
        "y": 0..1,
        "w": 0..1,
        "h": 0..1,
        "crop_left": 0..0.45,
        "crop_right": 0..0.45,
        "crop_top": 0..0.45,
        "crop_bottom": 0..0.45,
        "event_id": "...",
      }

    x/y/w/h are normalized to the square editing canvas.
    """
    return _product_transform_component(
        product_image=_b64(
            product_png
        ),
        stencil_image=_b64(
            stencil_png
        ),
        initial_box=initial_box,
        initial_crop=(
            initial_crop
            or {}
        ),
        stencil_opacity=float(
            stencil_opacity
        ),
        reset_token=str(
            reset_token
        ),
        default=None,
        key=key,
    )
