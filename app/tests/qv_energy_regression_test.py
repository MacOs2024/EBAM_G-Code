"""Regression: v4.2.9.30 volumetric energy (QV) model.

Field basis: the 30x70x30 block was built with 158 J/mm on a 1.175 mm^2 bead ->
QV=134 J/mm^3 (overheat, spatter, wavy top); the operator manually drove it back
to QV=83. The same 158 J/mm on the flange bead (3.95 mm^2) would give QV=40,
BELOW the R6 failure threshold of 42. Line energy alone is geometry-dependent.
"""
from __future__ import annotations
import json, math
from ebam_gcode_studio import core

failures = []
def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:140]})

# 1. conversion helpers are exact inverses
e = core.energy_j_mm_from_qv(72.0, 0.5, 2.35)
check("qv_to_energy", abs(e - 72.0 * 1.175) < 1e-9, e)
check("energy_to_qv_roundtrip", abs(core.qv_from_energy_j_mm(e, 0.5, 2.35) - 72.0) < 1e-9)

# 2. the historical failure: 158 J/mm is only valid for one cross-section
check("block_would_overheat", core.qv_from_energy_j_mm(158.0, 0.5, 2.35) > 110.0,
      core.qv_from_energy_j_mm(158.0, 0.5, 2.35))
check("flange_would_underfuse", core.qv_from_energy_j_mm(158.0, 1.53, 2.58) < 42.0,
      core.qv_from_energy_j_mm(158.0, 1.53, 2.58))

# 3. material library carries QV targets
for key in core.MATERIAL_LIBRARY:
    check(f"mat_has_qv_{key}", float(core.MATERIAL_LIBRARY[key].get("qv_bottom_j_mm3", 0)) > 0, key)

# 4. recommender: QV in band AND beam power above the fusion floor, every mode/geometry
for tag, summ in [("block", {"size_x": 30., "size_y": 70., "size_z": 30.}),
                  ("flange", {"size_x": 164., "size_y": 164., "size_z": 47.5}),
                  ("tall", {"size_x": 40., "size_y": 40., "size_z": 300.})]:
    for mode in ("quality", "balanced", "speed"):
        s = core.recommend_settings_from_summary(summ, mode, "stainless_steel_12_wire")
        sec = s.layer_height * s.hatch_spacing
        qv = s.target_energy_bottom_j_per_mm / sec
        cur = s.target_energy_bottom_j_per_mm * s.feed_bottom_mm_min / (60.0 * s.voltage_kv)
        power = s.voltage_kv * cur
        check(f"qv_in_band_{tag}_{mode}", core.QV_MIN_J_MM3 - 1 <= qv <= core.QV_MAX_J_MM3 + 1, f"QV={qv:.1f}")
        check(f"power_above_floor_{tag}_{mode}", power >= s.min_beam_power_w, f"P={power:.0f} floor={s.min_beam_power_w}")
        check(f"layer_sane_{tag}_{mode}", 0.3 * s.wire_diameter_mm - 1e-6 <= s.layer_height <= 1.4 * s.wire_diameter_mm + 1e-6,
              f"layer={s.layer_height}")

# 5. validation rejects both overheat and underfusion
try:
    core.validate_process_settings(core.ProcessSettings(
        layer_height=0.5, hatch_spacing=2.35,
        target_energy_bottom_j_per_mm=158.0, beam_current_mode="energy"), height=30)
    check("reject_overheat", False, "not raised")
except ValueError as ex:
    check("reject_overheat", "перегрев" in str(ex).lower() or "QV" in str(ex))
try:
    core.validate_process_settings(core.ProcessSettings(
        layer_height=1.53, hatch_spacing=2.58,
        target_energy_bottom_j_per_mm=150.0, beam_current_mode="energy"), height=30)
    check("reject_underfuse", False, "not raised")
except ValueError as ex:
    check("reject_underfuse", "порог" in str(ex).lower())
# in-band passes
core.validate_process_settings(core.ProcessSettings(
    layer_height=0.53, hatch_spacing=2.35,
    target_energy_bottom_j_per_mm=89.7, beam_current_mode="energy"), height=30)
check("accept_in_band", True)

# 6. current-mode settings are not blocked by the QV rule
core.validate_process_settings(core.ProcessSettings(
    layer_height=0.5, hatch_spacing=2.35, beam_current_mode="current",
    beam_current_bottom_ma=28.0, beam_current_top_ma=28.0), height=30)
check("current_mode_not_blocked", True)

print(json.dumps({"cases_total": len(failures) + 40, "failures": failures}, ensure_ascii=False))
if failures:
    raise SystemExit(1)
