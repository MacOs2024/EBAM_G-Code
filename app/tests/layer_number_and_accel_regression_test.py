"""Regression: layer number on E4 and acceleration-aware time estimates.

Two mechanisms taken from the real machine configuration (ebam.ini / *.hal):

* E4 is documented by the Bormash pendant as "M68 E4 Qxx - layer number", but
  motion.analog-out-04 is not wired in the supplied HAL, so emitting it must be
  opt-in and must not change a single line of output while it is off.
* The rotary table accelerates at 100 deg/s^2 ([AXIS_C] MAX_ACCELERATION), an
  order below the linear axes. The linear acceleration of a point at radius R is
  a_C * pi * R / 180, so on small rings the commanded speed is never reached and
  the ideal path/speed estimate is short by a factor, not by a percent.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ebam_gcode_studio import core

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:160]})


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_STL = ROOT / "qualification" / "FlangeFamily_v42919_REFERENCE_ID40" / "Flange1_2_.STL"

RING_PARAMS = {
    "profile_type": "cylinder_cup", "height": 20.0, "max_diameter": 60.0,
    "wall_thickness": 4.0, "bottom_solid_mm": 2.0,
    "bottom_diameter": 60.0, "neck_diameter": 60.0,
}


def gcode_of(result) -> str:
    return result.gcode if isinstance(result.gcode, str) else "\n".join(result.gcode)


# ---------------------------------------------------------------- acceleration
# 1. trapezoid: long move reaches the commanded speed, accel costs exactly v/a
t = core.motion_time_with_accel_s(1000.0, 10.0, 100.0)
check("trapezoid_costs_v_over_a", abs(t - (1000.0 / 10.0 + 10.0 / 100.0)) < 1e-9, t)

# 2. triangle: the move is too short to reach the commanded speed
short = core.motion_time_with_accel_s(0.5, 10.0, 100.0)   # v^2/a = 1.0 > 0.5
check("triangle_formula", abs(short - 2.0 * math.sqrt(0.5 / 100.0)) < 1e-9, short)
check("triangle_slower_than_ideal", short > 0.5 / 10.0, short)

# 3. degenerate inputs stay finite and non-negative
check("zero_distance", core.motion_time_with_accel_s(0.0, 10.0, 100.0) == 0.0)
check("zero_velocity", core.motion_time_with_accel_s(10.0, 0.0, 100.0) == 0.0)
check("no_accel_is_ideal", abs(core.motion_time_with_accel_s(100.0, 10.0, 0.0) - 10.0) < 1e-12)

# 4. acceleration never makes a move faster than the ideal estimate
for d in (0.1, 1.0, 10.0, 100.0, 1000.0):
    for v in (1.0, 10.0, 50.0):
        for a in (5.0, 100.0, 1000.0):
            check(f"never_faster_{d}_{v}_{a}", core.motion_time_with_accel_s(d, v, a) >= d / v - 1e-12)

# 5. the rotary table converts angular acceleration through the radius
s_def = core.ProcessSettings()
for r in (5.0, 50.0):
    li = core.LayerInfo(index=1, z=0.0, z_next=1.0, ratio=0.0, current_ma=30.0, feed_mm_min=600.0,
                        travel_speed_mm_s=10.0, wire_mm_s=5.0, energy_j_mm=100.0, energy_actual_j_mm=100.0,
                        current_required_ma=30.0, current_clipped_by_min=False, current_clipped_by_max=False,
                        energy_j_mm3=70.0, layer_pause_s=0.0, path_length_mm=2.0 * math.pi * r,
                        rotary_radius_mm=r)
    expected_a = 100.0 * math.pi * r / 180.0
    check(f"radius_accel_{r}", abs(core._effective_linear_accel_mm_s2(li, s_def) - expected_a) < 1e-9)

# 6. a small ring is limited by table acceleration, a large one is not
small = core.LayerInfo(index=1, z=0.0, z_next=1.0, ratio=0.0, current_ma=30.0, feed_mm_min=1620.0,
                       travel_speed_mm_s=27.0, wire_mm_s=5.0, energy_j_mm=100.0, energy_actual_j_mm=100.0,
                       current_required_ma=30.0, current_clipped_by_min=False, current_clipped_by_max=False,
                       energy_j_mm3=70.0, layer_pause_s=0.0, path_length_mm=2.0 * math.pi * 5.0,
                       rotary_radius_mm=5.0)
big = core.LayerInfo(index=1, z=0.0, z_next=1.0, ratio=0.0, current_ma=30.0, feed_mm_min=1620.0,
                     travel_speed_mm_s=27.0, wire_mm_s=5.0, energy_j_mm=100.0, energy_actual_j_mm=100.0,
                     current_required_ma=30.0, current_clipped_by_min=False, current_clipped_by_max=False,
                     energy_j_mm3=70.0, layer_pause_s=0.0, path_length_mm=2.0 * math.pi * 80.0,
                     rotary_radius_mm=80.0)
small_ratio = core.layer_active_time_s(small, s_def) / (small.path_length_mm / small.travel_speed_mm_s)
big_ratio = core.layer_active_time_s(big, s_def) / (big.path_length_mm / big.travel_speed_mm_s)
check("small_ring_costs_multiples", small_ratio > 2.0, f"x{small_ratio:.2f}")
check("large_ring_costs_percents", 1.0 < big_ratio < 1.10, f"x{big_ratio:.2f}")

# 7. the switch restores the previous ideal estimate exactly
s_off = core.ProcessSettings(estimate_time_with_acceleration=False)
check("switch_restores_ideal",
      abs(core.layer_active_time_s(small, s_off) - small.path_length_mm / small.travel_speed_mm_s) < 1e-12)

# 8. a generated part is never estimated faster with acceleration than without
res_on = core._generate_rotational_shell_rotary_c(RING_PARAMS, core.ProcessSettings())
res_off = core._generate_rotational_shell_rotary_c(RING_PARAMS, s_off)
t_on = float(res_on.stats["estimated_active_time_s"])
t_off = float(res_off.stats["estimated_active_time_s"])
check("generated_time_not_shorter", t_on >= t_off - 1e-9, f"{t_on:.1f} < {t_off:.1f}")

# ---------------------------------------------------------------- E4 layer number
# 10. off by default: output must be byte-identical
off = gcode_of(core._generate_rotational_shell_rotary_c(RING_PARAMS, core.ProcessSettings()))
check("e4_absent_by_default", "M68 E4" not in off and "M67 E4" not in off)

# 11. on request the number appears once per layer in every strategy
for name, gen in (
    ("rotary_c", core._generate_rotational_shell_rotary_c),
    ("ring_spiral", core._generate_rotational_shell_ring_spiral),
):
    txt = gcode_of(gen(RING_PARAMS, core.ProcessSettings(emit_layer_number_e4=True)))
    numbers = [int(float(v)) for v in re.findall(r"M68 E4 Q([0-9.]+)", txt)]
    check(f"e4_present_{name}", len(numbers) > 0, len(numbers))
    check(f"e4_one_per_layer_{name}", numbers == sorted(set(numbers)) and numbers == list(range(1, len(numbers) + 1)),
          numbers[:6])

# 12. continuous C mode emits one number per LAYER, not per ring
s_np = core.ProcessSettings(emit_layer_number_e4=True, rotary_c_radius_variation_tolerance_mm=50.0)
tube = dict(RING_PARAMS, wall_thickness=1.0, bottom_solid_mm=0.0)
np_txt = gcode_of(core._generate_rotational_shell_rotary_c_no_pause(tube, s_np))
e4_count = np_txt.count("M68 E4")
ring_count = np_txt.count("FIXED_Z_NO_PAUSE_RING")
check("e4_not_per_ring", e4_count <= ring_count, f"{e4_count} E4 vs {ring_count} rings")
check("e4_present_no_pause", e4_count > 0, e4_count)

# 13. the layer number always goes out as M68: it is not a process value, and M67
#     would require the HAL kit that has not been passed
m67 = core.ProcessSettings(emit_layer_number_e4=True, analog_output_mode="m67_synchronized",
                           machine_m67_confirmed=True)
m67_txt = gcode_of(core._generate_rotational_shell_rotary_c(RING_PARAMS, m67))
check("e4_always_m68", "M68 E4" in m67_txt and "M67 E4" not in m67_txt)

# 14. E4 must not collide with the channels that drive the process or kinematics
for line in [l for l in m67_txt.split("\n") if " E4 " in l]:
    check("e4_line_is_layer_only", "layer number" in line, line)

# ---------------------------------------------------------------- flange family
if REFERENCE_STL.is_file():
    from flange_family_generator import FlangeFamilySettings, analyze_stl_file, build_plan
    fs = FlangeFamilySettings()
    _data, profile = analyze_stl_file(REFERENCE_STL, fs)
    proc = build_plan(profile, fs).summary["process"]
    planned = float(proc["planned_total_time_h"])
    with_accel = float(proc["estimated_total_time_with_accel_h"])
    check("flange_accel_reported", with_accel >= planned, f"{with_accel:.3f} < {planned:.3f}")
    check("flange_accel_is_a_correction", with_accel < planned * 1.5, f"{with_accel:.3f} vs {planned:.3f}")
    check("flange_plan_unchanged", 4.0 <= planned <= 6.0, planned)

print(json.dumps({"cases_total": len(failures) + 60, "failures": failures}, ensure_ascii=False))
if failures:
    raise SystemExit(1)
