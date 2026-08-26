"""Regression: stainless wire diameters on the rollers the Bormash actually has.

The machine carries six feed rollers (ebam.ini [ROLLER_SCALE]): 0.8 / 1.2 / 1.6 /
2.0 / 2.4 / 3.2 mm. A 2.5 mm profile shipped up to v4.2.9.31 by mistake — there is
no such roller — and is now migrated to 2.4 mm.

QV (J/mm^3) is a volumetric quantity and does not depend on wire diameter, so the
field-proven 72/63 is reused as a starting point. Everything that DOES depend on
diameter must rescale automatically: wire cross-section (grows as d^2), the wire
feed ceiling (falls as 1/d^2, because the BEAM is the bottleneck, not the feeder),
and the sane layer-height band.
"""
from __future__ import annotations
import json, math
from ebam_gcode_studio import core

failures = []
def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:140]})

WIRES = {"stainless_steel_08_wire": 0.8, "stainless_steel_12_wire": 1.2,
         "stainless_steel_16_wire": 1.6, "stainless_steel_20_wire": 2.0,
         "stainless_steel_24_wire": 2.4, "stainless_steel_32_wire": 3.2}

# 1. every roller has a profile with QV and the correct diameter
for key, d in WIRES.items():
    check(f"exists_{key}", key in core.MATERIAL_LIBRARY, key)
    m = core.MATERIAL_LIBRARY[key]
    check(f"diam_{key}", abs(float(m["wire_diameter_mm"]) - d) < 1e-9, m.get("wire_diameter_mm"))
    check(f"qv_{key}", float(m.get("qv_bottom_j_mm3", 0)) > 0, m.get("qv_bottom_j_mm3"))
# only 1.2 is field calibrated
check("only_12_calibrated",
      core.MATERIAL_LIBRARY["stainless_steel_12_wire"].get("field_calibrated") is True
      and all(core.MATERIAL_LIBRARY[k].get("field_calibrated") is False for k in WIRES if k != "stainless_steel_12_wire"))

# 2. area grows as d^2
for d in WIRES.values():
    check(f"area_{d}", abs(core.wire_area_from_diameter(d) - math.pi * d * d / 4) < 1e-12)
check("area_ratio_24_vs_12",
      abs(core.wire_area_from_diameter(2.4) / core.wire_area_from_diameter(1.2) - 4.0) < 1e-9)

# 3. beam-limited feed falls as 1/area, and matches the empirical 40 mm/s for 1.2 mm
f12 = core.max_wire_feed_for_beam(1.2, 40.0)
check("feed_12_matches_empirical", 36.0 <= f12 <= 44.0, f12)
prev = None
for d in (0.8, 1.2, 1.6, 2.0, 2.4, 3.2):
    f = core.max_wire_feed_for_beam(d, 40.0)
    if prev is not None:
        check(f"feed_falls_{d}", f < prev, f"{f} !< {prev}")
    prev = f
check("feed_scales_inverse_area",
      abs(core.max_wire_feed_for_beam(2.4, 40.0) * core.wire_area_from_diameter(2.4)
          - core.max_wire_feed_for_beam(1.2, 40.0) * core.wire_area_from_diameter(1.2)) < 1e-6)

# 4. layer bounds scale with diameter
for d in WIRES.values():
    lo, hi = core.layer_height_bounds_for_wire(d)
    check(f"layer_bounds_{d}", abs(lo - 0.30 * d) < 1e-9 and abs(hi - 1.40 * d) < 1e-9, (lo, hi))

# 5. recommender: QV in band, power above floor, feed under the beam ceiling — every wire/mode
for key, d in WIRES.items():
    for mode in ("quality", "balanced", "speed"):
        s = core.recommend_settings_from_summary({"size_x": 30., "size_y": 70., "size_z": 30.}, mode, key)
        check(f"diam_applied_{key}_{mode}", abs(s.wire_diameter_mm - d) < 1e-9, s.wire_diameter_mm)
        sec = s.layer_height * s.hatch_spacing
        qv = s.target_energy_bottom_j_per_mm / sec
        cur = s.target_energy_bottom_j_per_mm * s.feed_bottom_mm_min / (60.0 * s.voltage_kv)
        power = s.voltage_kv * cur
        area = core.wire_area_from_diameter(d)
        e2_req = sec * (s.feed_bottom_mm_min / 60.0) / (area * s.deposition_efficiency)
        check(f"qv_band_{key}_{mode}", core.QV_MIN_J_MM3 - 1 <= qv <= core.QV_MAX_J_MM3 + 1, f"QV={qv:.1f}")
        check(f"power_floor_{key}_{mode}", power >= s.min_beam_power_w, f"P={power:.0f}")
        check(f"feed_under_ceiling_{key}_{mode}", e2_req <= s.wire_max_mm_s + 1e-6, f"{e2_req:.2f} > {s.wire_max_mm_s}")
        check(f"layer_in_bounds_{key}_{mode}",
              core.layer_height_bounds_for_wire(d)[0] - 1e-6 <= s.layer_height <= core.layer_height_bounds_for_wire(d)[1] + 1e-6,
              s.layer_height)
        core.validate_process_settings(s, height=30.0)  # must not raise

# 6. wire_max stays an operator-set WARNING threshold (project convention), so an
#    over-optimistic value must NOT block generation - but the physics helper must
#    still report the true beam-limited ceiling for that diameter.
core.validate_process_settings(core.ProcessSettings(
    wire_diameter_mm=2.4, wire_max_mm_s=40.0, current_max_ma=40.0), height=30)
check("wire_max_is_not_a_hard_block", True)
check("ceiling_much_lower_than_naive_40", core.max_wire_feed_for_beam(2.4, 40.0) < 12.0,
      core.max_wire_feed_for_beam(2.4, 40.0))

# 7. the profile set matches the real roller list, and the retired 2.5 mm key is
#    migrated explicitly instead of silently falling back to 1.2 mm.
profile_diams = sorted({round(float(core.MATERIAL_LIBRARY[k]["wire_diameter_mm"]), 3) for k in WIRES})
check("profiles_match_machine_rollers", tuple(profile_diams) == core.BORMASH_WIRE_ROLLERS_MM, profile_diams)
check("no_25_profile_left", "stainless_steel_25_wire" not in core.MATERIAL_LIBRARY)
_key, _note = core.resolve_material_key("stainless_steel_25_wire")
check("legacy_25_migrates_to_24", _key == "stainless_steel_24_wire", _key)
check("legacy_25_explains_itself", bool(_note) and "2.4" in str(_note), _note)
_k2, _n2 = core.resolve_material_key("stainless_steel_12_wire")
check("known_key_unchanged_and_silent", _k2 == "stainless_steel_12_wire" and _n2 is None, (_k2, _n2))
_k3, _n3 = core.resolve_material_key("nonsense_profile")
check("unknown_key_falls_back_loudly", _k3 == "stainless_steel_12_wire" and bool(_n3), (_k3, _n3))

print(json.dumps({"cases_total": len(failures) + 90, "failures": failures}, ensure_ascii=False))
if failures:
    raise SystemExit(1)
