"""Regression: adaptive inter-layer thermal dwell (v4.2.9.23).
Field basis: R6 had 16 dwells (116-189 s) ONLY on short hub layers; R7 had none."""
from __future__ import annotations
import json, re
from ebam_gcode_studio import core

failures = []
def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:120]})

S = core.ProcessSettings
# 1) helper unit behaviour
check("disabled_zero", core._thermal_dwell_for_layer(S(), 30.0) == 0.0)
en = S(thermal_min_layer_cycle_enabled=True, thermal_min_layer_cycle_min=3.0, thermal_min_dwell_s=120.0)
check("long_layer_zero", core._thermal_dwell_for_layer(en, 1200.0) == 0.0)
check("short_layer_floor", abs(core._thermal_dwell_for_layer(en, 120.0) - 120.0) < 1e-9, core._thermal_dwell_for_layer(en, 120.0))
check("short_layer_gap", abs(core._thermal_dwell_for_layer(en, 30.0) - 150.0) < 1e-9, core._thermal_dwell_for_layer(en, 30.0))
en0 = core.replace(en, thermal_min_dwell_s=0.0)
check("floor_zero_gap_only", abs(core._thermal_dwell_for_layer(en0, 100.0) - 80.0) < 1e-9)

# 2) generation smoke: small fast-layer cup -> dwells appear; disabled -> none
params = dict(profile_type='straight_cup', height_mm=8.0, top_diameter_mm=60.0, bottom_diameter_mm=60.0,
              max_diameter_mm=60.0, wall_thickness_mm=6.0, bottom_solid_mm=0.0, resolution=96)
base = dict(rotational_path_strategy="rotary_c_rings", rotational_radial_step_mm=2.0, hatch_spacing=2.0,
            layer_height=2.0, feed_bottom_mm_min=600.0, feed_top_mm_min=600.0,
            rotary_c_max_deg_min=600.0, rotary_c_min_radius_mm=5.0,
            beam_current_mode="current", beam_current_bottom_ma=30.0, beam_current_top_ma=30.0,
            wire_feed_mode="auto", deposition_efficiency=1.0, wire_diameter_mm=1.2)
res_on = core.generate_rotational_shell(params, S(**base, thermal_min_layer_cycle_enabled=True,
                                                 thermal_min_layer_cycle_min=3.0, thermal_min_dwell_s=120.0))
dw = re.findall(r'^G4 P([0-9.]+) \(THERMAL_DWELL', res_on.gcode, re.M)
check("dwells_emitted", len(dw) >= 2, f"found {len(dw)}")
check("dwell_at_least_floor", all(float(x) >= 120.0 - 1e-6 for x in dw), dw[:3])
check("audit_reports_dwells", "ТЕПЛОВЫЕ ВЫДЕРЖКИ" in res_on.audit_text)
tot = sum(float(x) for x in dw)
m = re.search(r'ТЕПЛОВЫЕ ВЫДЕРЖКИ: (\d+) шт, суммарно ([0-9.]+) мин', res_on.audit_text)
check("audit_numbers_match", m and int(m.group(1)) == len(dw) and abs(float(m.group(2)) - tot/60.0) < 0.06,
      m.group(0) if m else "no line")
res_off = core.generate_rotational_shell(params, S(**base))
check("disabled_no_dwells", "THERMAL_DWELL" not in res_off.gcode)
check("disabled_no_audit_line", "ТЕПЛОВЫЕ ВЫДЕРЖКИ" not in res_off.audit_text)

# 3) v4.2.9.26: XY ring/spiral parametric path must honour dwells too
res_xy = core.generate_rotational_shell(params, S(**{**base, "rotational_path_strategy": "rings"},
                                                  thermal_min_layer_cycle_enabled=True,
                                                  thermal_min_layer_cycle_min=3.0, thermal_min_dwell_s=120.0))
dw_xy = re.findall(r'^G4 P([0-9.]+) \(THERMAL_DWELL', res_xy.gcode, re.M)
check("xy_dwells_emitted", len(dw_xy) >= 2, f"found {len(dw_xy)}")
check("xy_audit_reports", "ТЕПЛОВЫЕ ВЫДЕРЖКИ" in res_xy.audit_text)
res_xy_off = core.generate_rotational_shell(params, S(**{**base, "rotational_path_strategy": "rings"}))
check("xy_disabled_none", "THERMAL_DWELL" not in res_xy_off.gcode)

# 4) no-pause C path: dwell with beam-off cycle and re-arm
res_np = core.generate_rotational_shell(params, S(**{**base, "rotational_path_strategy": "rotary_c_rings",
                                                     "rotary_c_motion_mode": "no_pause_flat_rings",
                                                     "rotary_c_max_deg_min": 600.0},
                                                  thermal_min_layer_cycle_enabled=True,
                                                  thermal_min_layer_cycle_min=3.0, thermal_min_dwell_s=120.0))
dw_np = re.findall(r'THERMAL_DWELL L\d+', res_np.gcode)
check("np_dwells_emitted", len(dw_np) >= 2, f"found {len(dw_np)}")
check("np_beam_off_cycle", "beam off before dwell" in res_np.gcode and "re-arm beam after thermal dwell" in res_np.gcode)
check("np_wire_cycle", "wire off before dwell" in res_np.gcode and "re-arm wire after thermal dwell" in res_np.gcode)

print(json.dumps({"cases_total": 17, "failures": failures,
                  "dwells": len(dw), "dwell_total_min": round(tot/60.0, 2)}, ensure_ascii=False))
if failures:
    raise SystemExit(1)
