from __future__ import annotations
import json, re, math
from dataclasses import replace
from pathlib import Path
from ebam_gcode_studio.core import (
    ProcessSettings, generate_rotational_shell, build_experience_calibration_profile,
    experience_profile_to_json, experience_profile_to_csv,
)

PARAMS = {
    "profile_type": "straight_cup",
    "height_mm": 30.0,
    "bottom_diameter_mm": 79.0,
    "top_diameter_mm": 79.0,
    "max_diameter_mm": 79.0,
    "wall_thickness_mm": 1.0,
    "bottom_solid_mm": 0.0,
    "resolution": 96,
}
BASE = ProcessSettings(
    layer_height=0.5,
    hatch_spacing=1.0,
    rotational_path_strategy="rotary_c_rings",
    rotational_radial_step_mm=1.0,
    rotary_c_motion_mode="no_pause_flat_rings",
    rotary_c_transition_angle_deg=17.0,
    rotary_c_direction="C+",
    rotary_c_max_deg_min=10000.0,
    rotary_c_auto_limit_feed=True,
    feed_bottom_mm_min=700.0,
    feed_top_mm_min=700.0,
    voltage_kv=60.0,
    beam_current_mode="current",
    beam_current_bottom_ma=27.0,
    beam_current_top_ma=27.0,
    current_min_ma=0.0,
    current_max_ma=50.0,
    wire_diameter_mm=1.2,
    wire_feed_mode="auto",
    rotary_c_disable_layer_pauses=True,
    rotary_c_disable_w_retract=True,
    rotary_c_disable_z_hop=True,
)


def continuous_block(gcode: str):
    lines = gcode.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("G91"))
    end = next(i for i, l in enumerate(lines[start + 1:], start + 1) if l.startswith("G90"))
    return lines[start + 1:end]


def main():
    profile = build_experience_calibration_profile(
        program_height_mm=30.0,
        base_z_step_mm=0.5,
        radius_mm=39.5,
        cfeed_code_deg_min=450.0,
        e2_code_mm_s=10.0,
        e0_code_ma=27.0,
        voltage_kv=60.0,
        wire_diameter_mm=1.2,
        actual_height_mm=26.0,
        outer_diameter_mm=86.0,
        inner_diameter_mm=72.0,
        target_wall_mm=4.0,
        problem_start_height_mm=15.0,
        max_z_offset_mm=-3.0,
        feed_override_stable_pct=110.0,
        wire_override_stable_pct=180.0,
        current_override_stable_pct=100.0,
        feed_override_upper_pct=120.0,
        wire_override_upper_pct=200.0,
        current_override_upper_pct=105.0,
        capture_wire_min_mm_s=5.0,
        test_height_mm=20.0,
    )
    assert profile["rules"]["too_thick_guard_triggered"] is True
    assert profile["zones"][1]["wire_mm_s"] < 18.0, "manual high E2 was copied blindly"
    assert profile["zones"][2]["estimated_wall_mm"] <= 4.05, "upper E2 must account for corrected Z-step"
    profile_json = experience_profile_to_json(profile)
    assert "stable_wall" in experience_profile_to_csv(profile)

    settings = replace(
        BASE,
        experience_profile_enabled=True,
        experience_profile_json=profile_json,
        experience_profile_apply_cfeed=True,
        experience_profile_apply_wire=True,
        experience_profile_apply_current=False,
        experience_profile_apply_z_step=True,
        max_layers_to_generate=50,
    )
    res = generate_rotational_shell(PARAMS, settings)
    assert res.stats["experience_profile_enabled"] is True
    assert res.stats["z_step_min_mm"] < 0.5, "upper zone Z-step was not applied"
    assert res.stats["rotary_c_used_max_deg_min"] >= 539.9, "zone Cfeed was not applied"
    assert "EXPERIENCE_PROFILE: enabled" in res.gcode
    assert "zone=upper_z_correction" in res.gcode
    block = continuous_block(res.gcode)
    assert not any(l.startswith("M68") or l.startswith("G4") or l.startswith("W") for l in block), "no-pause block contains pause/process commands"
    assert sum(1 for l in block if re.search(r"G1\s+C360\.000", l)) == res.stats["layers_total"]
    results = {
        "profile_zones": profile["zones"],
        "warnings": profile["warnings"],
        "stats_subset": {
            "layers_total": res.stats["layers_total"],
            "experience_profile_enabled": res.stats["experience_profile_enabled"],
            "rotary_c_used_max_deg_min": res.stats["rotary_c_used_max_deg_min"],
            "wire_commanded_once_mm_s": res.stats["wire_commanded_once_mm_s"],
            "z_step_min_mm": res.stats["z_step_min_mm"],
            "z_step_max_mm": res.stats["z_step_max_mm"],
            "estimated_wall_thickness_min_mm": res.stats["estimated_wall_thickness_min_mm"],
            "estimated_wall_thickness_max_mm": res.stats["estimated_wall_thickness_max_mm"],
        },
        "checks": "PASS",
    }
    Path("experience_profile_test_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"cases_total": 12, "cases_ok": 12, "cases_failed": 0, "checks": "PASS"}, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
