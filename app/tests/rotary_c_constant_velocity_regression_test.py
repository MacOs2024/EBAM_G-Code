"""Regression: constant-velocity radial feed compensation (v4.2.9.21).

Field problem Flange1_R6: with constant C, outer rings ran 3.75x faster linearly
and wire feed E2 ran away 13->49.5 mm/s while beam power stayed capped, so the
wire climbed out of the pool. Constant-velocity mode must hold E2 in a band.
"""
from __future__ import annotations
import json, math
from ebam_gcode_studio import core

A = math.pi * 0.6 ** 2
failures = []
def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": detail})

s = core.ProcessSettings(
    rotary_c_max_deg_min=600.0, layer_height=1.5333, rotational_radial_step_mm=2.5833,
    rotary_c_constant_velocity=True, rotary_c_wire_comfort_mm_s=29.0,
    rotary_c_shrink_pitch_at_floor=True, rotary_c_min_pitch_factor=0.5,
    deposition_efficiency=1.0, wire_diameter_mm=1.2,
    feed_bottom_mm_min=500.0, feed_top_mm_min=500.0)

radii = [21.3, 26.5, 31.6, 36.8, 42.0, 47.1, 52.3, 57.5, 62.6, 67.8, 73.0, 78.1, 80.7]
e2_vals, c_vals, v_vals = [], [], []
for R in radii:
    c, f, pf = core._rotary_c_ring_kinematics(s, R, 500.0)
    v = f / 60.0
    e2 = s.layer_height * s.hatch_spacing * v / A * pf
    e2_vals.append(e2); c_vals.append(c); v_vals.append(v)

# 1. E2 must stay bounded (no runaway). R6 hit 49.5; we must stay well under.
check("e2_bounded", max(e2_vals) <= 30.0, f"max E2={max(e2_vals):.1f} should stay <=30 (R6 was 49.5)")

# 2. E2 spread across the part must be far smaller than the R6 3.75x runaway.
spread = max(e2_vals) / max(min(e2_vals), 1e-9)
check("e2_spread_small", spread <= 2.6, f"E2 spread {spread:.2f}x (R6 was 3.75x)")

# 3. C must decrease from inner to outer (the 2-3%/ring compensation you observed).
inner_c = c_vals[0]; outer_c = c_vals[-1]
check("c_decreases_outward", outer_c < inner_c, f"C inner={inner_c:.0f} outer={outer_c:.0f}")

# 4. C stays within [450, 600].
check("c_in_limits", all(449.9 <= c <= 600.1 for c in c_vals), f"C range {min(c_vals):.0f}..{max(c_vals):.0f}")

# 5. Pitch shrink only kicks in at the C floor (outer rings), factor in [0.5,1.0].
c_floor, f_floor, pf_floor = core._rotary_c_ring_kinematics(s, 80.7, 500.0)
check("pitch_shrinks_at_floor", 0.5 <= pf_floor < 1.0, f"outer pitch_factor={pf_floor:.3f}")
c_in, f_in, pf_in = core._rotary_c_ring_kinematics(s, 21.3, 500.0)
check("no_shrink_inner", abs(pf_in - 1.0) < 1e-6, f"inner pitch_factor={pf_in:.3f}")

# 6. Legacy mode (constant_velocity OFF) is unchanged: C caps, v rises with R.
s_legacy = core.replace(s, rotary_c_constant_velocity=False)
c1, f1, _ = core._rotary_c_ring_kinematics(s_legacy, 21.3, 500.0)
c2, f2, _ = core._rotary_c_ring_kinematics(s_legacy, 80.7, 500.0)
check("legacy_velocity_rises", (f2 / 60.0) > (f1 / 60.0), "legacy: outer linear speed must exceed inner")

# 7. target linear velocity is derived from the comfort E2 via volume balance.
v_target = core._rotary_c_target_linear_mm_s(s)
v_expected = 29.0 * A / (s.rotational_radial_step_mm * s.layer_height)
check("v_target_from_comfort", abs(v_target - v_expected) < 0.05, f"v_target={v_target:.2f} exp={v_expected:.2f}")

print(json.dumps({"cases_total": 8, "failures": failures,
                  "e2_min": round(min(e2_vals),2), "e2_max": round(max(e2_vals),2),
                  "c_inner": round(inner_c,0), "c_outer": round(outer_c,0)},
                 ensure_ascii=False, indent=2))
if failures:
    raise SystemExit(1)
