"""Regression: time-target estimator fix + selectable levers (v4.2.9.20).

Guards against the bug where the tuner reported '+0.0%' but real G-code was ~24x
the target, because the no-pause estimator modelled one ring per layer.
"""
from __future__ import annotations
import ast, math, json
from pathlib import Path
from dataclasses import replace
from ebam_gcode_studio import core

# Load tuner + estimator from app.py without launching Streamlit.
_src = Path("app.py").read_text()
_mod = ast.parse(_src)
_g = {"math": math, "ProcessSettings": core.ProcessSettings, "replace": replace}
for _name in ["_rotary_c_like_strategy", "_wire_for_feed", "_current_limited_feed_cap",
              "_estimate_time_before_generation", "_fit_settings_to_target_time"]:
    _fn = [n for n in _mod.body if isinstance(n, ast.FunctionDef) and n.name == _name]
    if _fn:
        exec(compile(ast.Module([_fn[0]], []), "<x>", "exec"), _g)
_est = _g["_estimate_time_before_generation"]
_fit = _g["_fit_settings_to_target_time"]

SUMMARY = {"size_x": 164.0, "size_y": 164.0, "size_z": 47.5, "volume_mm3": 493811.0}
BASE = core.ProcessSettings(
    rotational_path_strategy="stl_rotary_c_rings", rotary_c_motion_mode="no_pause_flat_rings",
    rotational_radial_step_mm=2.0, hatch_spacing=2.0, layer_height=0.4,
    feed_bottom_mm_min=188.5, feed_top_mm_min=188.5, rotary_c_max_deg_min=450.0,
    rotary_c_min_radius_mm=18.0, rotary_c_auto_limit_feed=True, rotary_c_transition_angle_deg=17.0,
    beam_current_mode="energy", target_energy_bottom_j_per_mm=166.0, target_energy_top_j_per_mm=145.0,
    current_max_ma=35.0, wire_feed_mode="auto", deposition_efficiency=1.0, wire_diameter_mm=1.2,
    rotary_c_disable_layer_pauses=True)

def L(**kw):
    return {k: {"enabled": v[0], "max": v[1]} for k, v in kw.items()}

failures = []
def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": detail})

# 1. Estimator must NOT wildly under-count no-pause C time (the core bug).
eta = _est(SUMMARY, BASE)
hours = eta["total_s"] / 3600.0
check("estimator_not_absurdly_low", hours > 40.0,
      f"no-pause flange estimate {hours:.1f}h should be tens of hours, not minutes")

# 2. Monotonic: thicker layer -> less time.
def t_with(**over):
    return _est(SUMMARY, replace(BASE, **over))["total_s"]
check("layer_monotonic", t_with(layer_height=0.4) > t_with(layer_height=1.2))
check("step_monotonic", t_with(rotational_radial_step_mm=1.0, hatch_spacing=1.0) >
                        t_with(rotational_radial_step_mm=4.0, hatch_spacing=4.0))
check("feed_monotonic", t_with(feed_bottom_mm_min=100, feed_top_mm_min=100) >
                        t_with(feed_bottom_mm_min=600, feed_top_mm_min=600))

# 3. Levers: all free can approach an aggressive target; result reported honestly.
out, plan = _fit(SUMMARY, BASE, 5 * 3600, "full_process",
                 L(layer_height=(True, 2.5), radial_step=(True, 5.0), c_speed=(True, 600), current=(True, 50)))
check("all_free_reasonable", plan["adjusted_total_s"] / 3600.0 < 12.0,
      f"all-levers-free should get within ~2x of 5h, got {plan['adjusted_total_s']/3600:.1f}h")
check("all_free_has_error_pct", "error_pct" in plan)

# 4. Locking a lever must PIN it at base (honest constraint).
out_lock, plan_lock = _fit(SUMMARY, BASE, 5 * 3600, "full_process",
                           L(layer_height=(False, None), radial_step=(True, 5.0), c_speed=(True, 600), current=(True, 50)))
check("locked_layer_pinned", abs(out_lock.layer_height - BASE.layer_height) < 1e-6,
      f"locked layer height moved to {out_lock.layer_height}")

# 5. Impossible target with everything locked -> possible=False + honest verdict + min time.
out_imp, plan_imp = _fit(SUMMARY, BASE, 5 * 3600, "full_process",
                         L(layer_height=(False, None), radial_step=(False, None), c_speed=(False, 450), current=(False, 35)))
check("impossible_flagged", plan_imp["possible"] is False)
check("impossible_reports_min", plan_imp.get("min_achievable_s", 0) > 5 * 3600,
      "must report a real achievable minimum above the target")
check("impossible_has_message", any("уложиться" in m or "минимум" in m for m in plan_imp["messages"]))

# 6. Hard caps respected: current cap and C cap actually clamp the output settings.
out_cap, _ = _fit(SUMMARY, BASE, 5 * 3600, "full_process",
                  L(layer_height=(True, 2.5), radial_step=(True, 5.0), c_speed=(True, 500), current=(True, 25)))
check("current_cap_respected", out_cap.current_max_ma <= 25.0 + 1e-6, f"Imax={out_cap.current_max_ma}")
check("cspeed_cap_respected", out_cap.rotary_c_max_deg_min <= 500.0 + 1e-6, f"C={out_cap.rotary_c_max_deg_min}")
check("layer_cap_respected", out_cap.layer_height <= 2.5 + 1e-6, f"LH={out_cap.layer_height}")
check("step_cap_respected", out_cap.hatch_spacing <= 5.0 + 1e-6, f"step={out_cap.hatch_spacing}")

print(json.dumps({"cases_total": 16, "failures": failures}, ensure_ascii=False, indent=2))
if failures:
    raise SystemExit(1)
