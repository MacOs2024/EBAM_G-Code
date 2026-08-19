from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ebam_gcode_studio.core import APP_VERSION, ProcessSettings, generate_rotational_shell


CYLINDER = {
    "profile_type": "straight_cup",
    "height_mm": 2.0,
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
        hatch_spacing=2.25,
        rotational_path_strategy="rotary_c_rings",
        rotational_radial_step_mm=2.25,
        rotary_c_motion_mode="no_pause_flat_rings",
        rotary_c_max_deg_min=10000.0,
        feed_bottom_mm_min=299.433,
        feed_top_mm_min=299.433,
        beam_current_mode="current",
        beam_current_bottom_ma=28.0,
        beam_current_top_ma=26.0,
        wire_feed_mode="manual_bottom_top",
        wire_feed_bottom_mm_s=5.5,
        wire_feed_top_mm_s=7.0,
        rotary_c_disable_layer_pauses=True,
        rotary_c_disable_w_retract=True,
        rotary_c_disable_z_hop=True,
    )
    result = generate_rotational_shell(CYLINDER, settings)
    rows = list(csv.DictReader(io.StringIO(result.layer_csv)))
    must(len(rows) == 4, "unexpected layer count")

    legacy_columns = {"current_ma", "wire_mm_s", "travel_mm_min", "energy_actual_j_mm"}
    must(legacy_columns.issubset(rows[0]), "legacy CSV columns were removed")
    checks = ["legacy_columns_preserved"]

    calculated_e2 = {row["wire_mm_s"] for row in rows}
    actual_e2 = {row["actual_e2_command_mm_s"] for row in rows}
    must(len(calculated_e2) > 1, "test profile must vary calculated E2")
    must(len(actual_e2) == 1, "once-average mode must have one actual E2")
    checks.append("calculated_and_actual_e2_are_distinct")

    actual_e0 = {row["actual_e0_command_ma"] for row in rows}
    must(len(actual_e0) == 1, "once-average mode must have one actual E0")
    checks.append("actual_e0_is_once_average")

    expected_mode = "m68_once_average"
    must({row["analog_command_mode"] for row in rows} == {expected_mode}, "command mode mismatch")
    must([row["analog_command_update"] for row in rows] == ["1", "0", "0", "0"], "command update markers mismatch")
    checks.extend(["command_mode_recorded", "only_first_layer_marks_update"])

    gcode_e2 = re.search(r"M68 E2 Q([0-9.]+) \(wire feed ON once", result.gcode)
    gcode_e0 = re.search(r"M68 E0 Q([0-9.]+) \(beam current ON once", result.gcode)
    must(gcode_e2 is not None and gcode_e0 is not None, "one-time G-code commands missing")
    must(gcode_e2.group(1) == next(iter(actual_e2)), "CSV E2 command differs from G-code")
    must(gcode_e0.group(1) == next(iter(actual_e0)), "CSV E0 command differs from G-code")
    checks.extend(["csv_e2_matches_gcode", "csv_e0_matches_gcode"])

    payload = {
        "version": APP_VERSION,
        "cases_total": len(checks),
        "cases_ok": len(checks),
        "cases_failed": 0,
        "checks": checks,
        "actual_e0_command_ma": next(iter(actual_e0)),
        "actual_e2_command_mm_s": next(iter(actual_e2)),
    }
    Path("command_trace_csv_regression_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
