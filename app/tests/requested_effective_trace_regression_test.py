from __future__ import annotations

import json
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


def must(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    settings = ProcessSettings(
        layer_height=0.5,
        hatch_spacing=1.8,
        rotational_path_strategy="rotary_c_rings",
        rotational_radial_step_mm=2.25,
        rotary_c_motion_mode="no_pause_flat_rings",
        rotary_c_max_deg_min=10000.0,
        z_hop_mm=7.0,
        use_w_retract=True,
        w_retract_mm=0.8,
        beam_preheat_s=0.03,
        wire_settle_s=0.02,
        beam_off_pause_s=0.03,
        layer_pause_bottom_s=0.1,
        layer_pause_top_s=0.45,
        rotary_c_disable_z_hop=True,
        rotary_c_disable_w_retract=True,
        rotary_c_disable_layer_pauses=True,
    )
    result = generate_rotational_shell(CYLINDER, settings)
    changes = result.stats.get("requested_effective_changes", {})

    must(changes["z_hop_mm"] == {"requested": 7.0, "effective": 0.0, "reason": "rotary_c_disable_z_hop"}, "Z-hop trace mismatch")
    must(changes["use_w_retract"]["requested"] is True and changes["use_w_retract"]["effective"] is False, "W trace mismatch")
    must(changes["hatch_spacing"]["effective"] == 2.25, "effective radial step not traced")
    must(changes["layer_pause_top_s"]["effective"] == 0.0, "pause trace mismatch")
    must(result.stats["requested_effective_change_count"] == len(changes) >= 8, "trace count mismatch")
    must("Requested -> effective settings trace:" in result.audit_text, "audit trace section missing")
    must("z_hop_mm: requested=7.0 -> effective=0.0" in result.audit_text, "audit Z-hop detail missing")
    must("REQUESTED_EFFECTIVE_TRACE: changed_fields=" in result.gcode, "G-code trace marker missing")

    checks = [
        "z_hop_requested_effective",
        "w_retract_requested_effective",
        "radial_step_requested_effective",
        "pause_requested_effective",
        "trace_count_consistent",
        "audit_trace_section",
        "audit_trace_detail",
        "gcode_trace_marker",
    ]
    payload = {
        "version": APP_VERSION,
        "cases_total": len(checks),
        "cases_ok": len(checks),
        "cases_failed": 0,
        "checks": checks,
        "changed_fields": sorted(changes),
    }
    Path("requested_effective_trace_regression_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
