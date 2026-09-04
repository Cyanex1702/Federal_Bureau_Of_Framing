from __future__ import annotations

from copy import deepcopy

CROP_MIN = 0.0
CROP_MAX = 0.45
VALID_ALIGNMENTS = {"center", "bottom_center"}
VALID_ANCHOR_MODES = {"primary", "full_center", "visual_center"}

PROFILES = {
    "tall": {"target_height": 0.80, "max_width": 0.66, "alignment": "bottom_center", "baseline": 0.91},
    "boxy": {"target_height": 0.72, "max_width": 0.74, "alignment": "bottom_center", "baseline": 0.89},
    "wide": {"target_width": 0.82, "max_height": 0.62, "alignment": "center"},
    "flat": {"target_width": 0.84, "max_height": 0.44, "alignment": "center"},
    "compact": {"target_height": 0.70, "max_width": 0.68, "alignment": "bottom_center", "baseline": 0.89},
}

AMAZON_A_PROFILES = {
    "tall": {"target_height": 0.88, "max_width": 0.82, "alignment": "bottom_center", "baseline": 0.94, "amazon_axis": "height", "amazon_description": "Lock height; natural left/right padding."},
    "boxy": {"target_height": 0.84, "max_width": 0.84, "alignment": "center", "amazon_axis": "envelope", "amazon_description": "Balanced padding on all four sides."},
    "wide": {"target_width": 0.90, "max_height": 0.68, "alignment": "center", "amazon_axis": "width", "amazon_description": "Lock width; natural top/bottom padding."},
    "flat": {"target_width": 0.90, "max_height": 0.50, "alignment": "center", "amazon_axis": "width", "amazon_description": "Lock width; generous top/bottom air."},
    "compact": {"target_height": 0.80, "max_width": 0.80, "alignment": "center", "amazon_axis": "envelope", "amazon_description": "Balanced compact envelope."},
}

CATEGORY_PROFILE = {
    "Air conditioner": "wide", "Blender": "compact", "Cart / trolley": "wide",
    "Coffee machine": "boxy", "Citrus press": "boxy", "Cooktop": "flat",
    "Dishwasher": "tall", "Freezer": "tall", "Hob": "flat", "Hood": "wide",
    "Ice cream maker": "wide", "Ice machine": "boxy", "Juicer": "boxy",
    "Kettle": "compact", "Laundry equipment": "boxy", "Microwave": "boxy",
    "Oven": "boxy", "Refrigerator": "tall", "Vacuum cleaner": "boxy",
    "Waffle maker": "wide", "Water dispenser": "tall", "Water heater": "tall",
}

CATEGORY_STANDARDS = {
    "Air conditioner": {"target_width": 0.82, "max_height": 0.58, "alignment": "center"},
    "Coffee machine": {"target_height": 0.72, "max_width": 0.74, "alignment": "bottom_center", "baseline": 0.89},
    "Citrus press": {"target_height": 0.78, "max_width": 0.76, "alignment": "center"},
    "Cooktop": {"target_width": 0.84, "max_height": 0.46, "alignment": "center"},
    "Dishwasher": {"target_height": 0.79, "max_width": 0.70, "alignment": "bottom_center", "baseline": 0.91},
    "Freezer": {"target_height": 0.81, "max_width": 0.68, "alignment": "bottom_center", "baseline": 0.92},
    "Hob": {"target_width": 0.84, "max_height": 0.46, "alignment": "center"},
    "Hood": {"target_width": 0.82, "max_height": 0.68, "alignment": "center"},
    "Ice cream maker": {"target_width": 0.82, "max_height": 0.72, "alignment": "center"},
    "Juicer": {"target_height": 0.80, "max_width": 0.80, "alignment": "center"},
    "Microwave": {"target_height": 0.67, "max_width": 0.78, "alignment": "center"},
    "Oven": {"target_height": 0.72, "max_width": 0.76, "alignment": "center"},
    "Refrigerator": {"target_height": 0.82, "max_width": 0.66, "alignment": "bottom_center", "baseline": 0.92},
    "Water heater": {"target_height": 0.78, "max_width": 0.68, "alignment": "center"},
    "Cart / trolley": {"target_width": 0.78, "max_height": 0.70, "alignment": "center"},
    "Ice machine": {"target_height": 0.72, "max_width": 0.76, "alignment": "bottom_center", "baseline": 0.90},
    "Laundry equipment": {"target_height": 0.78, "max_width": 0.74, "alignment": "bottom_center", "baseline": 0.91},
    "Vacuum cleaner": {"target_height": 0.78, "max_width": 0.80, "alignment": "center"},
    "Waffle maker": {"target_width": 0.82, "max_height": 0.72, "alignment": "center"},
    "Water dispenser": {"target_height": 0.80, "max_width": 0.66, "alignment": "bottom_center", "baseline": 0.91},
}

CATEGORY_ANCHOR_POLICY = {
    "Air conditioner": "full_center", "Cart / trolley": "full_center", "Coffee machine": "primary",
    "Citrus press": "full_center", "Cooktop": "full_center", "Dishwasher": "primary",
    "Freezer": "primary", "Hob": "full_center", "Hood": "visual_center",
    "Ice cream maker": "full_center", "Ice machine": "primary", "Juicer": "full_center",
    "Laundry equipment": "primary", "Microwave": "full_center", "Oven": "full_center",
    "Refrigerator": "primary", "Vacuum cleaner": "full_center", "Waffle maker": "full_center",
    "Water dispenser": "primary", "Water heater": "full_center",
}


def _validate_profile_table(table: dict, name: str) -> None:
    required_names = {"tall", "boxy", "wide", "flat", "compact"}
    if set(table) != required_names:
        raise ValueError(f"{name} must define exactly {sorted(required_names)}")
    for profile_name, profile in table.items():
        if not isinstance(profile, dict):
            raise ValueError(f"{name}.{profile_name} must be an object")
        alignment = profile.get("alignment")
        if alignment not in VALID_ALIGNMENTS:
            raise ValueError(f"Invalid alignment for {name}.{profile_name}: {alignment}")
        has_height = "target_height" in profile
        has_width = "target_width" in profile
        if has_height == has_width:
            raise ValueError(f"{name}.{profile_name} must have exactly one target axis")
        for key in ("target_height", "target_width", "max_width", "max_height", "baseline"):
            if key in profile and not (0.0 < float(profile[key]) <= 1.0):
                raise ValueError(f"{name}.{profile_name}.{key} must be in (0,1]")


def validate_profile_config() -> None:
    _validate_profile_table(PROFILES, "profiles")
    _validate_profile_table(AMAZON_A_PROFILES, "amazon_a_profiles")
    known_profiles = set(PROFILES)
    for category, profile in CATEGORY_PROFILE.items():
        if profile not in known_profiles:
            raise ValueError(f"Unknown profile {profile!r} for category {category!r}")
    if len(CATEGORY_PROFILE) != len(set(CATEGORY_PROFILE)):
        raise ValueError("Duplicate category definitions are not allowed")
    for category, standard in CATEGORY_STANDARDS.items():
        if category not in CATEGORY_PROFILE:
            raise ValueError(f"Category standard without category mapping: {category}")
        if standard.get("alignment") not in VALID_ALIGNMENTS:
            raise ValueError(f"Invalid category alignment: {category}")
        for key, value in standard.items():
            if key != "alignment" and not (0.0 < float(value) <= 1.0):
                raise ValueError(f"Invalid category percentage: {category}.{key}")
    for category, anchor in CATEGORY_ANCHOR_POLICY.items():
        if category not in CATEGORY_PROFILE:
            raise ValueError(f"Anchor policy without category mapping: {category}")
        if anchor not in VALID_ANCHOR_MODES:
            raise ValueError(f"Invalid anchor policy: {category}")
    if not (0 <= CROP_MIN < CROP_MAX < 0.5):
        raise ValueError("Crop range must stay below 50% per edge")


def generated_profiles_document(version: str = "7.3.1") -> dict:
    validate_profile_config()
    return {
        "version": version,
        "source_of_truth": "profile_config.py",
        "generated_file": True,
        "crop_range": {"min": CROP_MIN, "max": CROP_MAX},
        "profiles": deepcopy(PROFILES),
        "amazon_a_profiles": deepcopy(AMAZON_A_PROFILES),
        "category_map": deepcopy(CATEGORY_PROFILE),
        "category_standards": deepcopy(CATEGORY_STANDARDS),
        "category_anchor_policy": deepcopy(CATEGORY_ANCHOR_POLICY),
    }


validate_profile_config()
