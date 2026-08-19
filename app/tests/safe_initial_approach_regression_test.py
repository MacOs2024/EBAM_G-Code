from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ebam_gcode_studio.core import APP_VERSION, ProcessSettings, generate_rotational_shell


CYLINDER = {
    "profile_type": "straight_cup",
    "height_mm": 1.5,
    "bottom_diameter_mm": 77.0,
    "top_diameter_mm": 77.0,
    "max_diameter_mm": 77.0,
    "wall_thickness_mm": 3.0,
    "bottom_solid_mm": 0.0,
    "resolution": 96,
}

BASE = ProcessSettings(
    layer_height=0.5,
    hatch_spacing=2.25,
    rotational_path_strategy="rotary_c_rings",
    rotational_radial_step_mm=2.25,
    rotary_c_motion_mode="no_pause_flat_rings",
    rotary_c_max_deg_min=10000.0,
    rotary_c_disable_z_hop=True,
    rotary_c_disable_w_retract=True,
    rotary_c_disable_layer_pauses=True,
)


def must(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def line_index(lines: list[str], prefix: str) -> int:
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    raise AssertionError(f"line not found: {prefix}")


def main() -> None:
    checks: list[str] = []
    legacy = generate_rotational_shell(CYLINDER, BASE)
    must("SAFE_INITIAL_APPROACH:" not in legacy.gcode, "default must keep legacy approach")
    must("G0 Z0.000" in legacy.gcode, "legacy no-pause Z0 header changed")
    must("ПОДВОД НА МАЛОЙ ВЫСОТЕ" in legacy.audit_text, "legacy low-Z warning missing")
    checks.extend(["default_off_is_compatible", "legacy_low_z_warning_preserved"])

    safe = generate_rotational_shell(
        CYLINDER,
        replace(BASE, safe_initial_approach_enabled=True, safe_initial_approach_z_mm=7.0),
    )
    lines = safe.gcode.splitlines()
    i_off_e0 = line_index(lines, "M68 E0 Q0.000")
    i_off_e2 = line_index(lines, "M68 E2 Q0.000")
    i_safe_z = line_index(lines, "G0 Z7.000")
    i_b = line_index(lines, "G0 B")
    i_c = line_index(lines, "G0 C")
    i_xy = line_index(lines, "G0 X")
    i_work_z = line_index(lines, "G1 Z0.000")
    must(max(i_off_e0, i_off_e2) < i_safe_z < i_b < i_c < i_xy < i_work_z, "safe approach order mismatch")
    checks.append("safe_motion_order")

    must(safe.stats["initial_positioning_z_mm"] == 7.0, "initial positioning Z stat mismatch")
    must(safe.stats["safe_initial_approach_enabled"] is True, "safe approach stat missing")
    checks.append("safe_approach_stats")

    must("ПОДВОД НА МАЛОЙ ВЫСОТЕ" not in safe.audit_text, "low-Z false positive remains")
    must("БЕЗОПАСНЫЙ НАЧАЛЬНЫЙ ПОДВОД ВКЛЮЧЁН" in safe.audit_text, "machine dry-run note missing")
    checks.append("audit_requires_machine_dry_run")

    try:
        generate_rotational_shell(
            CYLINDER,
            replace(BASE, safe_initial_approach_enabled=True, safe_initial_approach_z_mm=-1.0),
        )
    except ValueError as exc:
        must("safe_initial_approach_z_mm" in str(exc), "wrong validation error")
    else:
        raise AssertionError("negative safe initial Z was accepted")
    checks.append("negative_safe_z_rejected")

    payload = {
        "version": APP_VERSION,
        "cases_total": len(checks),
        "cases_ok": len(checks),
        "cases_failed": 0,
        "checks": checks,
        "default_enabled": ProcessSettings().safe_initial_approach_enabled,
    }
    Path("safe_initial_approach_regression_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
