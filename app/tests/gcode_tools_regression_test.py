"""Regression: v4.2.9.27 backend tools (post-check, wire-freeze, diff)."""
from __future__ import annotations
import json
from ebam_gcode_studio import gcode_tools as gt

failures = []
def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:120]})

# synthetic minimal G-code with known properties
GOOD = """G21
M68 E0 Q30.000
M68 E2 Q25.000
G1 C360.000 F1.665278 (RING 1/2 R=40.000 CSPD=599.5 E0=30.000 E2=25.000 QV=58.00)
M68 E2 Q0.000 (wire off before dwell)
M68 E0 Q0.000 (beam off before dwell)
G94
G1 W-0.800 F720.0 (wire retract)
G93
G4 P120.0 (THERMAL_DWELL L1: adaptive)
G1 W0.800 F720.0
M68 E0 Q30.000
G1 C360.000 F1.665278 (RING 2/2 R=42.000 CSPD=599.5 E0=30.000 E2=25.000 QV=58.00)
"""
BAD = """G21
M68 E0 Q39.000
G1 C360.000 F1.5 (RING 1/1 R=80.000 CSPD=450.0 E0=39.000 E2=48.000 QV=42.00)
G4 P150.0 (layer thermal stabilization)
G0 X10.0 Y5.0
"""

# post_generation_check
r = gt.post_generation_check(GOOD, e0_cap_ma=35.0, qv_floor_j_mm3=55.0)
check("good_overall_ok", r["status"] == "ok", r["status"])
check("good_rings", r["rings"] == 2, r["rings"])
check("good_dwell_counted", r["dwell_count"] == 1, r["dwell_count"])

rb = gt.post_generation_check(BAD, e0_cap_ma=35.0, e2_cap_mm_s=50.0, qv_floor_j_mm3=55.0)
check("bad_overall_bad", rb["status"] == "bad", rb["status"])
_e0 = next(i for i in rb["items"] if i["name"].startswith("Ток"))
check("bad_e0_flagged", _e0["status"] == "bad", _e0)
_e2 = next(i for i in rb["items"] if i["name"].startswith("Подача"))
check("bad_e2_hot_warn", _e2["status"] in ("warn", "bad"), _e2)
_qv = next(i for i in rb["items"] if i["name"].startswith("Плотность"))
check("bad_qv_bad", _qv["status"] == "bad", _qv)
_g0 = next(i for i in rb["items"] if "G0" in i["name"])
check("bad_g0_hot", _g0["status"] == "bad", _g0)

# wire_freeze_check
w_good = gt.wire_freeze_check(GOOD, dwell_threshold_s=60.0)
check("good_no_freeze", w_good["status"] == "ok", w_good)
w_bad = gt.wire_freeze_check(BAD, dwell_threshold_s=60.0)
check("bad_freeze_flagged", w_bad["status"] == "bad" and len(w_bad["flagged"]) == 1, w_bad)

# compare_gcode
d = gt.compare_gcode(GOOD, GOOD, "x", "x")
check("diff_identical", d["identical"] and all(r["delta"] == "—" for r in d["rows"]))
d2 = gt.compare_gcode(GOOD, BAD, "g", "b")
check("diff_detects_change", not d2["identical"])
_qvrow = next(r for r in d2["rows"] if r["field"].startswith("QV"))
check("diff_qv_delta", _qvrow["delta"] != "—", _qvrow)

# --- v4.2.9.28 data features ---
def _check2(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:120]})

_cal = gt.calibrate_from_bead(4.0, 1.7, "tom", 0.9)
_check2("cal_hatch_tom", abs(_cal["hatch_spacing_mm"] - 2.952) < 0.01, _cal["hatch_spacing_mm"])
_check2("cal_layer", abs(_cal["layer_height_mm"] - 1.53) < 0.01, _cal["layer_height_mm"])
_check2("cal_overlap", abs(_cal["overlap_percent"] - 26.2) < 0.1, _cal["overlap_percent"])
_cal_fom = gt.calibrate_from_bead(4.0, 1.7, "fom", 0.9)
_check2("cal_fom_differs", _cal_fom["hatch_spacing_mm"] < _cal["hatch_spacing_mm"], _cal_fom["hatch_spacing_mm"])
_cal_bad = gt.calibrate_from_bead(0.0, 0.0, "tom")
_check2("cal_bad_status", _cal_bad["status"] == "bad", _cal_bad["status"])

_pj = gt.make_profile("P1", '{"layer_height": 1.53}', 4.0, 1.7, "n")
_pr = gt.read_profile(_pj)
_check2("profile_roundtrip", _pr["ok"] and _pr["name"] == "P1" and not _pr["bare"], _pr)
_pr_bare = gt.read_profile('{"layer_height": 2.0}')
_check2("profile_bare_tolerated", _pr_bare["ok"] and _pr_bare["bare"], _pr_bare)
_pr_err = gt.read_profile("not json{")
_check2("profile_bad_error", not _pr_err["ok"], _pr_err)

_e = gt.make_journal_entry("R7", "sha123", {"OD": 165.0}, {"OD": 160.8}, "ok", 3)
_check2("journal_delta", abs(_e["deltas"]["OD"] - (-4.2)) < 0.01, _e["deltas"])
_js = gt.journal_to_json([_e])
_back = gt.journal_from_json(_js)
_check2("journal_roundtrip", len(_back) == 1 and _back[0]["part"] == "R7", _back)

# --- v4.2.9.29 simulator data extractor ---
_SIMG = """(LAYER 1/2 Z=0.000..1.530 ZONE=FLANGE DIR=x TRACKS=2 PITCH=2.5 ADEP=3.9 ACTIVE=5min DWELL=0s)
G1 C360.000 F1.0 (RING 1/2 R=20.000 CSPD=599.5 E0=25.000 E2=13.000 QV=100.00)
G1 C360.000 F1.0 (RING 2/2 R=40.000 CSPD=500.0 E0=35.000 E2=45.000 QV=46.00)
(LAYER 2/2 Z=1.530..3.060 ZONE=HUB DIR=x TRACKS=1 PITCH=2.5 ADEP=3.9 ACTIVE=2min DWELL=120s)
G1 C360.000 F1.0 (RING 1/1 R=22.000 CSPD=599.5 E0=30.000 E2=22.000 QV=70.00)
"""
_sp = gt.layer_ring_profile(_SIMG)
_check2("sim_has_data", _sp["has_data"], _sp["has_data"])
_check2("sim_two_layers", len(_sp["layers"]) == 2, len(_sp["layers"]))
_check2("sim_layer1_rings", len(_sp["layers"][0]["rings"]) == 2, _sp["layers"][0])
_check2("sim_qv_range", abs(_sp["qv_min"] - 46.0) < 0.1 and abs(_sp["qv_max"] - 100.0) < 0.1, (_sp["qv_min"], _sp["qv_max"]))
_check2("sim_zone", _sp["layers"][1]["zone"] == "HUB", _sp["layers"][1]["zone"])
# XY fallback (no LAYER markers): synthesize single bucket
_XYG = "G1 C360.000 F1.0 (RING 1/1 R=10.000 CSPD=500.0 E0=30.000 E2=20.000 QV=60.00)\n"
_xp = gt.layer_ring_profile(_XYG)
_check2("sim_xy_fallback", _xp["has_data"] and len(_xp["layers"]) == 1, _xp)

print(json.dumps({"cases_total_pkg3": 31, "failures": failures}, ensure_ascii=False))
if failures:
    raise SystemExit(1)
