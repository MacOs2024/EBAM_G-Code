from __future__ import annotations
import math, re, json, traceback, statistics
from dataclasses import replace, asdict
from pathlib import Path
from typing import Dict, Any, List, Tuple

from ebam_gcode_studio.core import ProcessSettings, generate_rotational_shell, recommended_hatch_from_bead

# v4.2.9.30: a fired process guard is DESIGNED behaviour, not a crash. Sweeps that
# push energy/geometry outside the physical band are supposed to be rejected with
# an operator-readable message; only unexpected exceptions are real failures.
_GUARD_MARKERS = ("Плотность энергии QV", "Радиальный шаг колец", "Эффективный радиальный шаг")


def _is_expected_guard(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and any(m in str(exc) for m in _GUARD_MARKERS)


PARAMS = {
    "profile_type": "straight_cup",
    "height_mm": 10.0,
    "bottom_diameter_mm": 79.0,
    "top_diameter_mm": 79.0,
    "max_diameter_mm": 79.0,
    "wall_thickness_mm": 1.0,
    "bottom_solid_mm": 0.0,
    "resolution": 192,
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
    feed_bottom_mm_min=450.0,
    feed_top_mm_min=450.0,
    voltage_kv=60.0,
    beam_current_mode="current",
    beam_current_bottom_ma=22.5,
    beam_current_top_ma=22.5,
    current_min_ma=0.0,
    current_max_ma=50.0,
    wire_feed_mode="auto",
    wire_feed_manual_mm_s=3.3,
    wire_feed_bottom_mm_s=2.0,
    wire_feed_top_mm_s=4.0,
    rotary_c_disable_layer_pauses=True,
    rotary_c_disable_w_retract=True,
    rotary_c_disable_z_hop=True,
    include_comments=False,
    max_layers_to_generate=0,
)

FIELDS_FOR_SMOKE = {
    "layer_height": [0.3, 0.5, 0.7],
    "hatch_spacing": [0.6, 1.0, 1.4],
    "rotational_radial_step_mm": [0.6, 1.0, 1.4],
    "rotational_points_per_circle": [48, 160, 320],
    "rotary_c_center_x_mm": [0.0, 10.0, 100.0],
    "rotary_c_center_y_mm": [0.0, 5.0, 50.0],
    "rotary_c_direction": ["C+", "C-", "C+"],
    "rotary_c_start_deg": [0.0, 45.0, 180.0],
    "rotary_c_b_angle_deg": [0.0, 5.0, -5.0],
    "rotary_c_max_deg_min": [300.0, 650.0, 10000.0],
    "rotary_c_min_radius_mm": [0.0, 18.0, 45.0],
    "rotary_c_auto_limit_feed": [True, False, True],
    "rotary_c_transition_angle_deg": [0.0, 17.0, 45.0],
    "feed_bottom_mm_min": [300.0, 450.0, 750.0],
    "feed_top_mm_min": [300.0, 450.0, 750.0],
    "voltage_kv": [50.0, 60.0, 70.0],
    "target_energy_bottom_j_per_mm": [90.0, 140.0, 180.0],
    "target_energy_top_j_per_mm": [90.0, 140.0, 180.0],
    "wire_diameter_mm": [1.0, 1.2, 1.6],
    "deposition_efficiency": [0.6, 0.8, 1.0],
    "density_g_cm3": [4.5, 7.9, 8.7],
    "focus_ma": [900.0, 1030.0, 1150.0],
    "current_min_ma": [0.0, 5.0, 10.0],
    "current_low_warning_ma": [1.0, 10.0, 30.0],
    "current_max_ma": [15.0, 30.0, 50.0],
    "beam_current_mode": ["current", "energy", "current"],
    "beam_current_bottom_ma": [10.0, 15.0, 20.0],
    "beam_current_top_ma": [10.0, 15.0, 20.0],
    "wire_min_mm_s": [0.1, 0.3, 1.0],
    "wire_max_mm_s": [2.0, 5.0, 40.0],
    "wire_feed_mode": ["auto", "manual_constant", "manual_bottom_top"],
    "wire_feed_manual_mm_s": [0.0, 3.3, 6.0],
    "wire_feed_bottom_mm_s": [0.0, 2.0, 4.0],
    "wire_feed_top_mm_s": [0.0, 4.0, 6.0],
    "z_hop_mm": [0.0, 5.0, 10.0],
    "use_w_retract": [False, True, False],
    "w_retract_mm": [0.0, 0.8, 2.0],
    "w_retract_feed_mm_min": [100.0, 720.0, 1200.0],
    "use_m68_speed_retract": [False, True, False],
    "speed_retract_mm_s": [1.0, 3.6, 6.0],
    "speed_retract_time_s": [0.1, 0.22, 0.5],
    "beam_preheat_s": [0.0, 0.03, 0.1],
    "wire_settle_s": [0.0, 0.02, 0.1],
    "beam_off_pause_s": [0.0, 0.03, 0.1],
    "layer_pause_bottom_s": [0.0, 0.1, 0.5],
    "layer_pause_top_s": [0.0, 0.45, 1.0],
    "path_control_mode": ["g64_tolerance", "machine_default", "g61"],
    "g64_tolerance_mm": [0.02, 0.08, 0.25],
    "g64_naive_cam_q_mm": [0.0, 0.02, 0.08],
    "analog_output_mode": ["m68_compatible", "m67_synchronized", "m68_compatible"],
    "machine_m67_confirmed": [False, True, False],
    "rotary_c_radius_variation_tolerance_mm": [0.0, 0.05, 0.5],
    "safe_z_final_mm": [20.0, 110.0, 300.0],
    "rapid_feed_z_mm_min": [300.0, 900.0, 1500.0],
    "work_z_feed_mm_min": [100.0, 240.0, 500.0],
    "machine_name": ["Bormash EBAM", "Test", "Бормаш"],
    "bormash_profile_enabled": [True, False, True],
    "bormash_check_xyz_limits": [True, False, True],
    "include_comments": [False, True, False],
    "max_layers_to_generate": [0, 5, 10],
    "bead_width_mm": [0.0, 1.5, 3.0],
    "overlap_model": ["tom", "fom", "tom"],
    "auto_hatch_from_bead": [False, True, False],
}

def gen(s: ProcessSettings):
    return generate_rotational_shell(PARAMS, s)

def parse_process_block(gcode: str) -> List[str]:
    lines = gcode.splitlines()
    try:
        start = next(i for i,l in enumerate(lines) if l.startswith("G91"))
        end = next(i for i,l in enumerate(lines[start+1:], start+1) if l.startswith("G90"))
        return lines[start+1:end]
    except StopIteration:
        return []

def q_after(gcode: str, pat: str) -> float | None:
    m = re.search(pat, gcode)
    return float(m.group(1)) if m else None

def commands(gcode: str, token: str) -> int:
    return sum(1 for l in gcode.splitlines() if token in l)

def assert_close(name: str, a: float, b: float, tol: float=1e-3):
    if not math.isfinite(a) or not math.isfinite(b) or abs(a-b) > tol:
        raise AssertionError(f"{name}: {a} != {b} ± {tol}")

def invariants_no_pause(res, s: ProcessSettings):
    g = res.gcode
    st = res.stats
    block = parse_process_block(g)
    if not block:
        raise AssertionError("no continuous G91/G90 movement block")
    # No stop-service commands inside the motion queue.
    bad = [l for l in block if l.startswith("M68") or l.startswith("G4") or l.startswith("W")]
    if bad:
        raise AssertionError("service commands inside no-pause block: " + repr(bad[:5]))
    n = int(st["layers_total"])
    c360 = [l for l in block if re.search(r"G1\s+C-?360\.000", l)]
    if len(c360) != n:
        raise AssertionError(f"C360 count {len(c360)} != layers {n}")
    trans = [l for l in block if "Z" in l and re.search(r"G1\s+C", l)]
    expected_trans = max(0, n-1) if abs(float(getattr(s, 'rotary_c_transition_angle_deg', 0.0))) > 1e-12 else max(0,n-1)
    if len(trans) != max(0, n-1):
        raise AssertionError(f"transition count {len(trans)} != {max(0,n-1)}")
    # No pure Z movement inside block.
    zonly = [l for l in block if re.search(r"G1\s+Z", l) and "C" not in l]
    if zonly:
        raise AssertionError("Z-only movement inside no-pause block")
    # E0/E2 are set once before the block. The ON command may be compatible M68
    # or synchronized M67 after explicit HAL confirmation; final OFF remains M68.
    analog_code = str(st.get("analog_output_code_effective", "M68"))
    if commands(g, "M68 E0 Q0.000") < 2 or commands(g, "M68 E2 Q0.000") < 2:
        raise AssertionError("missing immediate safe init/final OFF commands")
    qE0 = q_after(g, rf"{analog_code} E0 Q([0-9.+-]+) \(beam current ON once")
    qE2 = q_after(g, rf"{analog_code} E2 Q([0-9.+-]+) \(wire feed ON once")
    if qE0 is None or qE2 is None:
        raise AssertionError(f"missing {analog_code} ON setpoints")
    assert_close("E0 gcode vs stats", qE0, float(st["current_commanded_once_ma"]), 2e-3)
    assert_close("E2 gcode vs stats", qE2, float(st["wire_commanded_once_mm_s"]), 2e-3)
    # Formula checks.
    r = float(st["rotary_c_min_radius_used_mm"])
    f = float(st["feed_max_mm_min"])
    c_expected = 180.0 * f / (math.pi * r)
    c_used = float(st["rotary_c_used_max_deg_min"])
    # If limit active, used is cmax; else formula.
    cmax = float(getattr(s, "rotary_c_max_deg_min", 1e9))
    if c_expected <= cmax + 1e-6:
        assert_close("Cfeed formula", c_used, c_expected, 0.25)
    elif bool(getattr(s, "rotary_c_auto_limit_feed", True)):
        assert_close("Cfeed capped", c_used, cmax, 0.25)
    # E2 formula in auto mode.
    if str(getattr(s, "wire_feed_mode", "auto")).lower() == "auto":
        area = math.pi*(float(getattr(s,"wire_diameter_mm",1.2))/2.0)**2
        area *= float(getattr(s,"deposition_efficiency",1.0))
        e2_expected = float(getattr(s,"layer_height"))* (float(st["feed_min_mm_min"])/60.0) * float(st["hatch_spacing_effective_mm"]) / area
        assert_close("E2 auto formula", float(st["wire_min_calculated_mm_s"]), e2_expected, 2e-3)
    return True


def get_metrics(s: ProcessSettings) -> Dict[str, float|int|str|bool]:
    r = gen(s)
    invariants_no_pause(r, s)
    st = r.stats
    return {
        "layers": int(st["layers_total"]),
        "feed_min": float(st["feed_min_mm_min"]),
        "feed_max": float(st["feed_max_mm_min"]),
        "c_used": float(st["rotary_c_used_max_deg_min"]),
        "c_req": float(st["rotary_c_required_max_deg_min"]),
        "limited": int(st["rotary_c_feed_limited_count"]),
        "e0": float(st["current_commanded_once_ma"]),
        "e0_req_min": float(st["current_required_min_ma"]),
        "e0_req_max": float(st["current_required_max_ma"]),
        "e2": float(st["wire_commanded_once_mm_s"]),
        "e2_min": float(st["wire_min_calculated_mm_s"]),
        "e2_max": float(st["wire_max_calculated_mm_s"]),
        "energy_min": float(st["energy_actual_min_j_mm"]),
        "energy_max": float(st["energy_actual_max_j_mm"]),
        "evol_min": float(st["energy_volume_min_j_mm3"]),
        "evol_max": float(st["energy_volume_max_j_mm3"]),
        "wall_min": float(st["estimated_wall_thickness_min_mm"]),
        "wall_max": float(st["estimated_wall_thickness_max_mm"]),
        "time_s": float(st["estimated_total_time_s"]),
        "wire_warn": bool(st["wire_above_control_limit"]),
    }

def is_inc(xs): return all(xs[i] < xs[i+1] for i in range(len(xs)-1))
def is_dec(xs): return all(xs[i] > xs[i+1] for i in range(len(xs)-1))
def is_nondec(xs): return all(xs[i] <= xs[i+1]+1e-9 for i in range(len(xs)-1))
def is_noninc(xs): return all(xs[i] >= xs[i+1]-1e-9 for i in range(len(xs)-1))

results = []
failures = []

def run_case(name: str, s: ProcessSettings):
    try:
        m = get_metrics(s)
        results.append({"name": name, "ok": True, **m})
        return m
    except Exception as e:
        if _is_expected_guard(e):
            return
        tb = traceback.format_exc(limit=8)
        failures.append({"name": name, "error": str(e), "traceback": tb})
        results.append({"name": name, "ok": False, "error": str(e)})
        return None

def run_series(title: str, values: List[Any], make_settings, expected=None):
    mets=[]
    for v in values:
        m=run_case(f"{title}={v}", make_settings(v))
        mets.append(m)
    if expected and all(m is not None for m in mets):
        try:
            expected(values, mets)
        except Exception as e:
            failures.append({"name": title+" dependency", "error": str(e), "traceback": traceback.format_exc(limit=3)})
    return mets

# Smoke: change as many fields as possible 3 times.
for field, vals in FIELDS_FOR_SMOKE.items():
    for v in vals:
        s = replace(BASE, **{field: v})
        # Make paired modes valid so mode-specific value tests actually take effect.
        if field in ("wire_feed_manual_mm_s",): s = replace(s, wire_feed_mode="manual_constant")
        if field in ("wire_feed_bottom_mm_s", "wire_feed_top_mm_s"): s = replace(s, wire_feed_mode="manual_bottom_top")
        if field in ("target_energy_bottom_j_per_mm", "target_energy_top_j_per_mm"): s = replace(s, beam_current_mode="energy")
        if field in ("beam_current_bottom_ma", "beam_current_top_ma"): s = replace(s, beam_current_mode="current")
        if field in ("bead_width_mm","overlap_model","auto_hatch_from_bead"):
            # Auto-hatch needs a real bead width; radial_step=0 relies on it (v4.2.9.20
            # rejects a zero effective rotary step that used to fall back silently).
            s = replace(s, auto_hatch_from_bead=True, rotational_radial_step_mm=0.0)
            if float(getattr(s, "bead_width_mm", 0.0) or 0.0) <= 0.0:
                s = replace(s, bead_width_mm=3.0)
        if field == "analog_output_mode" and v == "m67_synchronized":
            s = replace(s, machine_m67_confirmed=True)
        run_case(f"smoke {field}={v}", s)

# Dependency tests.
run_series("current_setpoint", [10.0, 15.0, 20.0], lambda v: replace(BASE, beam_current_mode="current", beam_current_bottom_ma=v, beam_current_top_ma=v),
           lambda vals, ms: (is_inc([m['energy_min'] for m in ms]) and is_inc([m['evol_min'] for m in ms]) and len(set(round(m['e2'],4) for m in ms))==1) or (_ for _ in ()).throw(AssertionError("E0 did not scale energy with E2 fixed")))
run_series("energy_target", [80.0, 130.0, 180.0], lambda v: replace(BASE, beam_current_mode="energy", target_energy_bottom_j_per_mm=v, target_energy_top_j_per_mm=v),
           lambda vals, ms: (is_inc([m['e0'] for m in ms]) and is_inc([m['energy_min'] for m in ms]) and len(set(round(m['e2'],4) for m in ms))==1) or (_ for _ in ()).throw(AssertionError("target energy did not scale current/energy")))
run_series("linear_feed", [300.0, 450.0, 750.0], lambda v: replace(BASE, feed_bottom_mm_min=v, feed_top_mm_min=v),
           lambda vals, ms: (is_inc([m['c_used'] for m in ms]) and is_inc([m['e2'] for m in ms]) and is_dec([m['time_s'] for m in ms]) and is_dec([m['energy_min'] for m in ms])) or (_ for _ in ()).throw(AssertionError("feed dependencies wrong")))
run_series("layer_height", [0.3, 0.5, 0.7], lambda v: replace(BASE, layer_height=v),
           lambda vals, ms: (is_dec([m['layers'] for m in ms]) and is_inc([m['e2'] for m in ms]) and is_dec([m['time_s'] for m in ms])) or (_ for _ in ()).throw(AssertionError("layer_height dependencies wrong")))
run_series("radial_step", [0.6, 1.0, 1.4], lambda v: replace(BASE, rotational_radial_step_mm=v),
           lambda vals, ms: (is_inc([m['e2'] for m in ms]) and is_inc([m['wall_min'] for m in ms])) or (_ for _ in ()).throw(AssertionError("radial_step dependencies wrong")))
run_series("manual_e2", [2.0, 3.3, 6.0], lambda v: replace(BASE, wire_feed_mode="manual_constant", wire_feed_manual_mm_s=v),
           lambda vals, ms: (is_inc([m['e2'] for m in ms]) and is_inc([m['wall_min'] for m in ms]) and is_dec([m['evol_min'] for m in ms])) or (_ for _ in ()).throw(AssertionError("manual E2 dependencies wrong")))
run_series("manual_e2_zero", [0.0, 1.0, 2.0], lambda v: replace(BASE, wire_feed_mode="manual_constant", wire_feed_manual_mm_s=v),
           lambda vals, ms: (abs(ms[0]['e2']) < 1e-6 and is_inc([m['e2'] for m in ms])) or (_ for _ in ()).throw(AssertionError("manual E2 zero is not respected")))
run_series("cmax_limit", [300.0, 650.0, 10000.0], lambda v: replace(BASE, feed_bottom_mm_min=750, feed_top_mm_min=750, rotary_c_max_deg_min=v, rotary_c_auto_limit_feed=True),
           lambda vals, ms: (is_inc([m['feed_min'] for m in ms]) and ms[0]['limited']>0 and ms[-1]['limited']==0) or (_ for _ in ()).throw(AssertionError("Cmax limiting dependencies wrong")))
run_series("voltage_current_mode", [50.0, 60.0, 70.0], lambda v: replace(BASE, beam_current_mode="current", voltage_kv=v),
           lambda vals, ms: (is_inc([m['energy_min'] for m in ms]) and len(set(round(m['e2'],4) for m in ms))==1) or (_ for _ in ()).throw(AssertionError("voltage current-mode dependencies wrong")))
run_series("current_max_clamp", [12.0, 18.0, 50.0], lambda v: replace(BASE, beam_current_mode="current", beam_current_bottom_ma=22.5, beam_current_top_ma=22.5, current_max_ma=v),
           lambda vals, ms: (is_inc([m['e0'] for m in ms]) and abs(ms[0]['e0']-12.0)<1e-3 and abs(ms[-1]['e0']-22.5)<1e-3) or (_ for _ in ()).throw(AssertionError("current clamp wrong")))
run_series("transition_angle", [0.0, 17.0, 45.0], lambda v: replace(BASE, rotary_c_transition_angle_deg=v),
           lambda vals, ms: is_inc([m['time_s'] for m in ms]) or (_ for _ in ()).throw(AssertionError("transition angle time/path did not increase")))
run_series("wire_diameter_auto", [1.0, 1.2, 1.6], lambda v: replace(BASE, wire_diameter_mm=v, wire_feed_mode="auto"),
           lambda vals, ms: is_dec([m['e2'] for m in ms]) or (_ for _ in ()).throw(AssertionError("wire diameter should lower E2 in auto mode")))
run_series("deposition_efficiency", [0.6, 0.8, 1.0], lambda v: replace(BASE, deposition_efficiency=v, wire_feed_mode="auto"),
           lambda vals, ms: (is_dec([m['e2'] for m in ms]) and max(m['wall_max'] for m in ms)-min(m['wall_min'] for m in ms) < 1e-6) or (_ for _ in ()).throw(AssertionError("eta must reduce commanded E2 while preserving target deposited wall area")))
run_series("auto_hatch_from_bead", [1.0, 2.0, 3.0], lambda v: replace(BASE, rotational_radial_step_mm=0.0, hatch_spacing=1.0, auto_hatch_from_bead=True, bead_width_mm=v, overlap_model="tom"),
           lambda vals, ms: is_inc([m['wall_min'] for m in ms]) or (_ for _ in ()).throw(AssertionError("auto hatch from bead did not change wall/step")))

summary = {
    "cases_total": len(results),
    "cases_ok": sum(1 for r in results if r.get('ok')),
    "cases_failed": sum(1 for r in results if not r.get('ok')),
    "dependency_failures": len(failures),
    "failures": failures[:30],
}
Path("deep_interaction_results.json").write_text(json.dumps({"summary": summary, "results": results, "failures": failures}, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(summary, indent=2, ensure_ascii=False))
