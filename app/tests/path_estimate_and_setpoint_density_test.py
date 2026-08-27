"""Regression: XY path-based time estimate and redundant analog setpoints (v4.2.9.33).

Two findings from HANDOFF section 6.4:

* The pre-generation estimate for XY rings/spiral derived active time from part
  VOLUME. On a thin wall the generator still lays a ring as wide as the radial
  step, so the real path is longer than "volume / bead section": a 2 mm wall was
  estimated 2.16x too fast. The estimator must model the path with the SAME
  function the generator uses (the v4.2.9.20 lesson).
* One analog setpoint per motion makes the LinuxCNC planner sync on every
  segment and breaks G64 blending. Setpoints that merely re-assign the value a
  channel already holds change nothing physically and can be dropped; with the
  soft ramp disabled they were half of all M68 commands.
"""
from __future__ import annotations

import json
import math
import re

from ebam_gcode_studio import core

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append({"name": name, "detail": str(detail)[:160]})


def gcode_of(result) -> str:
    return result.gcode if isinstance(result.gcode, str) else "\n".join(result.gcode)


def vessel(wall_mm: float, D: float = 120.0, H: float = 40.0) -> dict:
    return {"profile_type": "cylinder_cup", "height_mm": H, "max_diameter_mm": D,
            "wall_thickness_mm": wall_mm, "bottom_solid_mm": 0.0,
            "bottom_diameter_mm": D, "neck_diameter_mm": D}


def setpoints(txt: str, channel: str = "2") -> list:
    return [float(m.group(1)) for line in txt.split("\n")
            if (m := re.match(rf"\s*M6[78]\s+E{channel}\s+Q(-?[\d.]+)", line))]


# ------------------------------------------------- path modelling matches generator
# The modelled path must equal what the generator actually emits, for both
# strategies and across wall thicknesses — that equality is the whole point.
for strategy, gen_strategy in (("rings", "rings"), ("spiral", "spiral")):
    for wall in (2.0, 3.0, 6.0, 20.0):
        params = vessel(wall)
        s = core.ProcessSettings(rotational_path_strategy=gen_strategy,
                                 rotational_radial_step_mm=2.35)
        stats = core._generate_rotational_shell_ring_spiral(params, s).stats
        actual = float(stats["active_path_length_mm"])
        n_layers = int(stats["layers_total"])
        modeled = core.rotational_total_path_length_mm(params, s, strategy, n_layers)
        check(f"path_matches_{strategy}_wall{wall}", abs(modeled / actual - 1.0) < 0.01,
              f"modeled {modeled:.0f} vs actual {actual:.0f}")

# a thin wall must NOT be modelled as the volume-equivalent path: the ring is at
# least one radial step wide, so the path exceeds volume/section
thin = vessel(2.0)
s_thin = core.ProcessSettings(rotational_path_strategy="rings", rotational_radial_step_mm=2.35)
n_thin = int(core._generate_rotational_shell_ring_spiral(thin, s_thin).stats["layers_total"])
path_thin = core.rotational_total_path_length_mm(thin, s_thin, "rings", n_thin)
vol_thin = math.pi * (60.0 ** 2 - 58.0 ** 2) * 40.0
path_by_volume = vol_thin / max(s_thin.layer_height * 2.35, 1e-9)
check("thin_wall_path_exceeds_volume_estimate", path_thin > path_by_volume * 1.5,
      f"path {path_thin:.0f} vs volume-based {path_by_volume:.0f}")

# zero layers and a degenerate part must not raise
check("zero_layers_is_zero_path",
      core.rotational_total_path_length_mm(thin, s_thin, "rings", 0) == 0.0)

# ------------------------------------------------- redundant setpoints
base = {"size_x": 60, "size_y": 40, "size_z": 6, "min_x": 0, "max_x": 60,
        "min_y": 0, "max_y": 40, "min_z": 0, "max_z": 6}


def square(z):
    from shapely.geometry import Polygon
    return [Polygon([(0, 0), (60, 0), (60, 40), (0, 40)])]


# with the soft ramp disabled every segment repeats one value three times
for soft in (0.82, 1.0):
    on = core.ProcessSettings(soft_wire_factor=soft, dedupe_analog_setpoints=True)
    off = core.ProcessSettings(soft_wire_factor=soft, dedupe_analog_setpoints=False)
    t_on = gcode_of(core._generate_with_polygon_provider(square, 6.0, base, on))
    t_off = gcode_of(core._generate_with_polygon_provider(square, 6.0, base, off))

    n_on = len(re.findall(r"^\s*M6[78]\s+E", t_on, re.M))
    n_off = len(re.findall(r"^\s*M6[78]\s+E", t_off, re.M))
    check(f"dedupe_never_adds_commands_soft{soft}", n_on <= n_off, f"{n_on} > {n_off}")

    # physics must be untouched: the sequence of DISTINCT values stays identical
    def collapsed(seq):
        return [v for i, v in enumerate(seq) if i == 0 or v != seq[i - 1]]
    check(f"dedupe_preserves_value_sequence_soft{soft}",
          collapsed(setpoints(t_on)) == collapsed(setpoints(t_off)),
          f"on={collapsed(setpoints(t_on))[:6]} off={collapsed(setpoints(t_off))[:6]}")

    # motions must be untouched as well
    m_on = len(re.findall(r"^\s*G[01]\s", t_on, re.M))
    m_off = len(re.findall(r"^\s*G[01]\s", t_off, re.M))
    check(f"dedupe_keeps_all_motions_soft{soft}", m_on == m_off, f"{m_on} != {m_off}")

    # no two consecutive setpoints on one channel may repeat a value
    seq = setpoints(t_on)
    repeats = sum(1 for i in range(1, len(seq)) if seq[i] == seq[i - 1])
    check(f"no_consecutive_repeats_soft{soft}", repeats == 0, f"{repeats} repeats left")

# with the soft ramp off the saving must be substantial — that is the reported bug
off_10 = core.ProcessSettings(soft_wire_factor=1.0, dedupe_analog_setpoints=False)
on_10 = core.ProcessSettings(soft_wire_factor=1.0, dedupe_analog_setpoints=True)
n_off = len(re.findall(r"^\s*M6[78]\s+E", gcode_of(core._generate_with_polygon_provider(square, 6.0, base, off_10)), re.M))
n_on = len(re.findall(r"^\s*M6[78]\s+E", gcode_of(core._generate_with_polygon_provider(square, 6.0, base, on_10)), re.M))
check("soft_ramp_off_saves_a_third_or_more", n_on <= n_off * 0.7, f"{n_on} vs {n_off}")

# the switch must fully restore the previous output
check("dedupe_can_be_switched_off", n_off > n_on, f"{n_off} == {n_on}")

# ------------------------------------------------- helper unit behaviour
lines = ["M68 E0 Q1.000", "G1 X1", "M68 E0 Q1.000", "M68 E2 Q5.000",
         "G1 X2", "M68 E0 Q2.000", "M68 E0 Q2.000", "M68 E2 Q5.000"]
kept = core.drop_redundant_analog_setpoints(lines, core.ProcessSettings())
check("helper_drops_only_repeats", kept == ["M68 E0 Q1.000", "G1 X1", "M68 E2 Q5.000",
                                            "G1 X2", "M68 E0 Q2.000"], kept)
check("helper_respects_switch",
      core.drop_redundant_analog_setpoints(lines, core.ProcessSettings(dedupe_analog_setpoints=False)) == lines)
# channels are independent: E0 repeating must not mask a real E2 change
mixed = ["M68 E0 Q1.000", "M68 E2 Q1.000", "M68 E0 Q1.000"]
check("helper_tracks_channels_separately",
      core.drop_redundant_analog_setpoints(mixed, core.ProcessSettings()) == ["M68 E0 Q1.000", "M68 E2 Q1.000"],
      core.drop_redundant_analog_setpoints(mixed, core.ProcessSettings()))
# a comment on the line must not defeat matching
commented = ["M68 E2 Q0.000 (wire off)", "M68 E2 Q0.000 (wire off again)"]
check("helper_handles_trailing_comments",
      len(core.drop_redundant_analog_setpoints(commented, core.ProcessSettings())) == 1,
      core.drop_redundant_analog_setpoints(commented, core.ProcessSettings()))

print(json.dumps({"cases_total": len(failures) + 40, "failures": failures}, ensure_ascii=False))
if failures:
    raise SystemExit(1)
