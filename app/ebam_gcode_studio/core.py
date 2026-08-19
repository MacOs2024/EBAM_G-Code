"""EBAM G-code Studio core v4.2.9.31.

STL/DXF/CSV -> layer slicing -> EBAM-oriented G-code.
Designed for Bormash/FABMETALL-style electron-beam wire deposition.

This is an engineering generator, not certified process qualification.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from pathlib import Path
from typing import Callable, Iterable, List, Tuple, Dict, Any, Optional
import csv
import io
import json
import math
import re

import numpy as np
import trimesh

try:
    from shapely.geometry import LineString, MultiLineString, GeometryCollection, Polygon, MultiPolygon, Point as ShapelyPoint
    from shapely import unary_union
    from shapely.ops import polygonize
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Shapely is required. Install dependencies from requirements.txt") from exc

Point = Tuple[float, float]
Segment = Tuple[float, float, float, float]

APP_VERSION = "v4.2.9.31"
EXPERIENCE_PROFILE_VERSION = "v4.2.9.31-experience-profile"


BORMASH_LIMITS = {
    "x_min": 0.0, "x_max": 3670.0,
    "y_min": 0.0, "y_max": 1510.0,
    "z_min": 0.0, "z_max": 1443.0,
}


def is_bormash_profile(settings: Any) -> bool:
    name = str(getattr(settings, "machine_name", "")).lower()
    return bool(getattr(settings, "bormash_profile_enabled", False)) or "bormash" in name or "бормаш" in name


def bormash_limits_report(stats: Dict[str, Any], settings: Any) -> Tuple[bool, List[str]]:
    if not is_bormash_profile(settings) or not bool(getattr(settings, "bormash_check_xyz_limits", True)):
        return True, []
    msgs: List[str] = []
    ok = True
    checks = [
        ("X", stats.get("min_x"), stats.get("max_x"), float(getattr(settings, "bormash_x_min_mm", BORMASH_LIMITS["x_min"])), float(getattr(settings, "bormash_x_max_mm", BORMASH_LIMITS["x_max"]))),
        ("Y", stats.get("min_y"), stats.get("max_y"), float(getattr(settings, "bormash_y_min_mm", BORMASH_LIMITS["y_min"])), float(getattr(settings, "bormash_y_max_mm", BORMASH_LIMITS["y_max"]))),
        ("Z", stats.get("min_z"), stats.get("max_z"), float(getattr(settings, "bormash_z_min_mm", BORMASH_LIMITS["z_min"])), float(getattr(settings, "bormash_z_max_mm", BORMASH_LIMITS["z_max"]))),
    ]
    tol = 1e-6
    for axis, vmin, vmax, lim_min, lim_max in checks:
        if vmin is None or vmax is None:
            continue
        try:
            vmin_f = float(vmin); vmax_f = float(vmax)
        except Exception:
            continue
        if vmin_f < lim_min - tol or vmax_f > lim_max + tol:
            ok = False
            msgs.append(f"DANGER: {axis} range {vmin_f:.3f}..{vmax_f:.3f} mm is outside Bormash limit {lim_min:.3f}..{lim_max:.3f} mm")
        else:
            msgs.append(f"OK: {axis} range {vmin_f:.3f}..{vmax_f:.3f} mm fits Bormash limit {lim_min:.3f}..{lim_max:.3f} mm")
    return ok, msgs


# v4.2.9.31: field-validated volumetric energy density band (J/mm^3).
# Anchors: R6 flange failed at QV=42 (wire climbed out of the pool);
# R7/R8 ran clean at 55..83; the 20x70x30 block overheated at 134 (spatter).
QV_FAIL_LOW_J_MM3 = 42.0
QV_MIN_J_MM3 = 55.0
QV_MAX_J_MM3 = 90.0
QV_FAIL_HIGH_J_MM3 = 110.0


def wire_area_from_diameter(wire_diameter_mm: float) -> float:
    """Cross-section of the filler wire, mm^2. Grows with the SQUARE of diameter:
    1.2 -> 1.131, 1.6 -> 2.011, 2.0 -> 3.142, 2.5 -> 4.909 mm^2."""
    d = max(float(wire_diameter_mm), 1e-9)
    return math.pi * d * d / 4.0


def max_wire_feed_for_beam(wire_diameter_mm: float, current_max_ma: float,
                           voltage_kv: float = 60.0, qv_min_j_mm3: float = QV_MIN_J_MM3,
                           efficiency: float = 0.97) -> float:
    """Highest wire feed the BEAM can still melt, mm/s.

    v4.2.9.31: the limit on thick wire is the beam, not the feeder. The beam can melt
    at most  Q = U*I/QV  mm^3/s; a thicker wire delivers that volume at a proportionally
    LOWER linear feed, so the feed ceiling scales as 1/area:
        1.2 mm -> ~40 mm/s, 1.6 -> ~22, 2.0 -> ~14, 2.5 -> ~9  (at 40 mA, QV 55).
    """
    melt_rate = (float(voltage_kv) * float(current_max_ma)) / max(float(qv_min_j_mm3), 1e-9)
    area = wire_area_from_diameter(wire_diameter_mm)
    return melt_rate / max(area * float(efficiency), 1e-9)


def layer_height_bounds_for_wire(wire_diameter_mm: float) -> tuple:
    """(min, max) sane layer height for a given wire, mm.

    Field data exists only for 1.2 mm wire (proven 0.42..1.53 mm, i.e. 0.35..1.27*d).
    The same relative band is applied to thicker wires because bead height scales with
    the deposited volume per unit length, but callers should warn that anything above
    the proven absolute value is uncalibrated territory.
    """
    d = max(float(wire_diameter_mm), 1e-9)
    return (0.30 * d, 1.40 * d)


FIELD_PROVEN_MAX_LAYER_MM = 1.53  # tallest layer actually run on the machine (flange R7/R8)


def energy_j_mm_from_qv(qv_j_mm3: float, layer_height_mm: float, hatch_spacing_mm: float) -> float:
    """Convert volumetric energy density to line energy for a given bead cross-section.

    J/mm = QV * (layer_height * hatch_spacing). This is the conversion that makes a
    material profile portable across geometries.
    """
    section = max(float(layer_height_mm), 1e-9) * max(float(hatch_spacing_mm), 1e-9)
    return float(qv_j_mm3) * section


def qv_from_energy_j_mm(energy_j_mm: float, layer_height_mm: float, hatch_spacing_mm: float) -> float:
    """Inverse of energy_j_mm_from_qv: volumetric energy density of a planned bead."""
    section = max(float(layer_height_mm), 1e-9) * max(float(hatch_spacing_mm), 1e-9)
    return float(energy_j_mm) / section


MATERIAL_LIBRARY: Dict[str, Dict[str, float | str]] = {
    "stainless_steel_12_wire": {
        "name_ru": "Нержавеющая сталь, проволока 1.2 мм",
        "density_g_cm3": 7.9,
        "wire_diameter_mm": 1.2,
        "energy_bottom_j_mm": 158.0,
        "energy_top_j_mm": 138.0,
        # v4.2.9.31: primary target is now VOLUMETRIC energy density (J/mm^3).
        # J/mm alone is geometry-dependent: the same 158 J/mm gives QV=134 on a
        # 1.175 mm^2 bead (overheat, spatter) and QV=40 on a 3.95 mm^2 bead
        # (below the 42 J/mm^3 field failure threshold). Field-validated band
        # from R6/R7/R8 flange builds: 55..90 J/mm^3, comfortable ~72.
        "qv_bottom_j_mm3": 72.0,
        "qv_top_j_mm3": 63.0,
        "field_calibrated": True,
        "note_ru": "Базовый профиль по вашим опытам V16-V21; QV подтверждена наплавками R7/R8."
    },
    # v4.2.9.31: thicker stainless wires. QV is a volumetric quantity and does not
    # depend on wire diameter directly, so the field-proven 72/63 J/mm^3 is used as a
    # STARTING POINT. Everything that DOES depend on diameter (wire cross-section,
    # feed limits, layer bounds) is derived automatically. These profiles are NOT yet
    # field-validated: heat capacity of the incoming metal, pool contact area and
    # melting inertia all grow with diameter, so the real QV may need to be higher.
    "stainless_steel_16_wire": {
        "name_ru": "Нержавеющая сталь, проволока 1.6 мм",
        "density_g_cm3": 7.9,
        "wire_diameter_mm": 1.6,
        "energy_bottom_j_mm": 158.0,
        "energy_top_j_mm": 138.0,
        "qv_bottom_j_mm3": 72.0,
        "qv_top_j_mm3": 63.0,
        "field_calibrated": False,
        "note_ru": "Сечение проволоки в 1.78 раза больше, чем у 1.2 мм: подача и пределы пересчитаны автоматически. QV взята от 1.2 мм — ТРЕБУЕТСЯ калибровочный валик."
    },
    "stainless_steel_20_wire": {
        "name_ru": "Нержавеющая сталь, проволока 2.0 мм",
        "density_g_cm3": 7.9,
        "wire_diameter_mm": 2.0,
        "energy_bottom_j_mm": 158.0,
        "energy_top_j_mm": 138.0,
        "qv_bottom_j_mm3": 72.0,
        "qv_top_j_mm3": 63.0,
        "field_calibrated": False,
        "note_ru": "Сечение в 2.78 раза больше, чем у 1.2 мм. Тот же расход металла достигается втрое меньшей подачей. QV не подтверждена — ТРЕБУЕТСЯ калибровочный валик."
    },
    "stainless_steel_25_wire": {
        "name_ru": "Нержавеющая сталь, проволока 2.5 мм",
        "density_g_cm3": 7.9,
        "wire_diameter_mm": 2.5,
        "energy_bottom_j_mm": 158.0,
        "energy_top_j_mm": 138.0,
        "qv_bottom_j_mm3": 72.0,
        "qv_top_j_mm3": 63.0,
        "field_calibrated": False,
        "note_ru": "Сечение в 4.34 раза больше, чем у 1.2 мм. Требует заметно большей мощности луча на тот же метраж проволоки. QV не подтверждена — ТРЕБУЕТСЯ калибровочный валик."
    },
    "steel_generic": {
        "name_ru": "Сталь, общий стартовый профиль",
        "density_g_cm3": 7.85,
        "wire_diameter_mm": 1.2,
        "energy_bottom_j_mm": 160.0,
        "energy_top_j_mm": 140.0,
        "qv_bottom_j_mm3": 73.0,
        "qv_top_j_mm3": 64.0,
        "note_ru": "Консервативный старт для стали."
    },
    "titanium_generic": {
        "name_ru": "Титан/сплав титана, осторожный профиль",
        "density_g_cm3": 4.5,
        "wire_diameter_mm": 1.2,
        "energy_bottom_j_mm": 145.0,
        "energy_top_j_mm": 125.0,
        "qv_bottom_j_mm3": 66.0,
        "qv_top_j_mm3": 57.0,
        "note_ru": "Требует отдельной квалификации по материалу и вакууму."
    },
    "bronze_experimental": {
        "name_ru": "Бронза, экспериментальный профиль",
        "density_g_cm3": 8.7,
        "wire_diameter_mm": 1.2,
        "energy_bottom_j_mm": 125.0,
        "energy_top_j_mm": 110.0,
        "qv_bottom_j_mm3": 57.0,
        "qv_top_j_mm3": 50.0,
        "note_ru": "Высокий риск испарения/брызг; только после короткого теста."
    },
}


@dataclass
class ProcessSettings:
    # Geometry/slicing
    layer_height: float = 0.5      # v4.2.9.31: 0.3 mm at the old default energy gave QV=263 (evaporation regime)0              # mm
    hatch_spacing: float = 2.35    # v4.2.9.31: matches the field-proven block/flange bead              # mm
    edge_offset: float = 0.80               # mm inward from polygon boundary
    min_segment_length: float = 4.0         # mm
    center_xy: bool = False                 # if False -> shift min X/Y to 0
    z_to_zero: bool = True
    # Geometry orientation / placement. Applied before XY/Z normalization.
    # axis_order means which original axes become output X/Y/Z, e.g. "XZY" makes output Y from original Z.
    axis_order: str = "XYZ"
    rotate_x_deg: float = 0.0
    rotate_y_deg: float = 0.0
    rotate_z_deg: float = 0.0
    mirror_x: bool = False
    mirror_y: bool = False
    mirror_z: bool = False
    output_offset_x_mm: float = 0.0
    output_offset_y_mm: float = 0.0
    output_offset_z_mm: float = 0.0
    direction: str = "Y-"                   # Y-, Y+, X-, X+
    alternate_hatch_shift: bool = True
    shift_fraction_a: float = 0.00
    shift_fraction_b: float = 0.33
    shift_fraction_c: float = -0.33
    thermal_ordering: str = "skip_neighbours"  # natural or skip_neighbours
    # --- EBAM-compliant deposition strategy (literature-based, v4.2.9.6) ---
    # "continuous": zigzag/serpentine raster, beam stays ON across the layer,
    #   adjacent lines joined by short beam-on link moves (WAAM/EBAM standard,
    #   reduces beam restrikes, height error and Z-hop time).
    # "segmented": legacy v4.2.7.x behaviour, beam off + Z-hop between each line.
    deposition_strategy: str = "continuous"
    link_feed_factor: float = 1.30          # feed for beam-on link moves between lines, relative to hatch feed
    # Bead-overlap hatch model. Center distance d between neighbouring beads:
    #   TOM (Ding 2015) critical d* = 0.738 * bead_width  (stable single material)
    #   FOM (Suryakumar 2011) flat-top d = 0.667 * bead_width
    # bead_width_mm should come from a single-bead TEST on the real machine.
    bead_width_mm: float = 0.0              # 0 = do not auto-derive hatch from bead width
    overlap_model: str = "tom"              # "tom" (0.738), "fom" (0.667), "manual"
    auto_hatch_from_bead: bool = False      # if True and bead_width_mm>0, hatch_spacing = factor*bead_width
    # Rotational vessel special strategies. Used only by generate_rotational_shell().
    # hatch = old fill of annular section; rings = concentric circular passes;
    # spiral = Archimedean spiral within the annular/solid section of each layer.
    rotational_path_strategy: str = "hatch"  # hatch / rings / spiral / rotary_c_rings
    rotational_radial_step_mm: float = 0.0    # 0 = use hatch_spacing as radial step/pitch
    rotational_points_per_circle: int = 160   # circular segment resolution for rings/spiral preview
    # Rotary C-table strategy for vessels/balloons. First implementation is B=0, C-axis rings.
    rotary_c_center_x_mm: float = 0.0         # X coordinate of C rotation center; working X = center + radius
    rotary_c_center_y_mm: float = 0.0         # stored for documentation/future checks; first mode keeps Y at this value
    rotary_c_direction: str = "C+"            # C+ or C-
    rotary_c_start_deg: float = 0.0           # start angle before relative C360 turns
    rotary_c_seam_scatter_deg: float = 0.0    # per-ring start-angle advance (beam OFF reposition) to scatter the vertical seam; 0=off; e.g. 137.5 golden angle. Relative-turns mode only.
    rotary_c_b_angle_deg: float = 0.0         # first safe mode keeps B=0; expose for testing but warn if != 0
    rotary_c_max_deg_min: float = 2100.0      # from Bormash AXIS_C MAX_VELOCITY 35 deg/s = 2100 deg/min
    rotary_c_min_radius_mm: float = 18.0      # warning threshold only; C feed limiting uses real ring radius when known
    rotary_c_auto_limit_feed: bool = True     # if C speed too high, reduce linear feed and recalc E0/E2
    rotary_c_relative_turns: bool = True      # use G91 C360 then G90; safest for repeated full turns
    # v4.2.9.7: experimental C-table continuous controls for cylindrical tests.
    # separate_rings = legacy per-ring start/stop;
    # no_pause_flat_rings = C360 at fixed Z, then C+transition_deg with Z+layer_height while E0/E2 stay ON.
    rotary_c_motion_mode: str = "separate_rings"
    rotary_c_transition_angle_deg: float = 17.0
    # v4.2.9.31: radial feed-rate compensation for C-table rings. Field problem
    # (Flange1_R6): with constant C, the outer rings run much faster linearly
    # (v = C*pi*R/180 grows with R), so the volume balance drives wire feed E2 up
    # 3-4x while beam power stays capped -> energy density collapses and the wire
    # climbs out of the pool. This mode holds the LINEAR deposition speed roughly
    # constant so E2 stays in a safe band; where the C floor is reached it shrinks
    # the effective radial pitch instead of letting E2 run away.
    rotary_c_constant_velocity: bool = False
    rotary_c_target_linear_mm_s: float = 0.0   # 0 = auto from wire comfort band
    rotary_c_wire_comfort_mm_s: float = 29.0   # E2 the operator considers stable
    rotary_c_shrink_pitch_at_floor: bool = True
    rotary_c_min_pitch_factor: float = 0.5     # do not shrink pitch below this fraction
    # v4.2.9.31: adaptive inter-layer thermal dwell ("min layer cycle").
    # Field basis: R6 flange-family dwells 116-189 s appeared ONLY on short hub
    # layers; long disc layers self-cool. If a layer finishes faster than the
    # minimum cycle, a G4 dwell is appended (beam/wire already OFF between rings).
    thermal_min_layer_cycle_enabled: bool = False
    thermal_min_layer_cycle_min: float = 3.0
    thermal_min_dwell_s: float = 120.0         # floor when a dwell triggers at all
    rotary_c_continuous_keep_beam_wire_on: bool = False
    rotary_c_disable_layer_pauses: bool = False
    rotary_c_disable_w_retract: bool = False
    rotary_c_disable_z_hop: bool = False

    alternate_layer_rotation: bool = False  # rotate raster ~90 deg each layer (alternate orthogonal strategy)
    contour_passes: int = 0                 # number of perimeter passes per layer
    contour_offset_step: float = 0.70       # mm between contour passes inward
    contour_wire_factor: float = 0.72       # contour wire relative to hatch
    contour_feed_factor: float = 0.88       # contour feed relative to hatch
    contour_every_n_layers: int = 1         # 1 = every layer, 2 = each second layer
    contour_first: bool = False             # False: hatch then contour; True: contour then hatch

    # Thin-wall / complex STL recovery
    adaptive_thin_wall: bool = True          # retry thinner settings when a layer has no hatch
    force_contour_on_empty_layers: bool = True # add contour-only path if hatch disappears
    adaptive_section_probe: bool = True       # retry nearby Z sections when STL slice is numerically empty
    section_probe_fraction: float = 0.45      # max +/- layer_height fraction for nearby STL slicing
    thin_wall_hatch_spacing_factor: float = 0.55
    thin_wall_edge_offset_factor: float = 0.35
    thin_wall_min_segment_length: float = 1.00
    thin_wall_wire_factor: float = 0.65       # wire reduction for contour-only thin-wall fallback
    adaptive_wire_correction: bool = True     # reduce wire feed when adaptive hatch spacing is smaller
    manual_section_fallback: bool = True      # offline-safe triangle/plane section fallback, independent of rtree
    projection_fallback_if_empty: bool = False # last-resort 2.5D projection fallback; disabled by default for real geometry safety
    minimum_generated_layer_fraction: float = 0.90
    max_layers_to_generate: int = 0             # 0 = full part; >0 = first N layers only for test files

    # UI/performance controls
    progress_update_every_layers: int = 1       # progress callback granularity for Streamlit UI

    # Target-time calculation. These are advisory/metadata fields used by UI and audit.
    # The generator still follows the actual feed/layer/hatch/current settings below.
    target_total_time_s: float = 0.0            # 0 = no target time
    target_time_mode: str = "off"              # off / feed_only / feed_layer_hatch

    # EBAM process calculation
    voltage_kv: float = 60.0
    target_energy_bottom_j_per_mm: float = 84.6   # v4.2.9.31: QV=72 J/mm^3 on the default 0.5x2.35 bead
    # v4.2.9.31: volumetric energy density actually targeted by the recommender
    # (informational for the UI/audit; J/mm above stays the value used in G-code).
    target_qv_bottom_j_mm3: float = 0.0
    target_qv_top_j_mm3: float = 0.0
    target_energy_top_j_per_mm: float = 74.0      # v4.2.9.31: QV=63 J/mm^3 on the default bead
    feed_bottom_mm_min: float = 650.0
    feed_top_mm_min: float = 710.0
    wire_diameter_mm: float = 1.2
    deposition_efficiency: float = 1.00      # 1.00 means all wire volume becomes bead
    density_g_cm3: float = 7.9
    focus_ma: float = 1030.0
    current_min_ma: float = 0.0          # hard lower clamp for E0 current; default 0 = do not force extra heat
    current_low_warning_ma: float = 1.0  # advisory warning only; does not change G-code
    current_max_ma: float = 50.0
    # --- Fusion power floor (advisory), ported from parallel v4.2.9.6 branch after
    # the real single-bead experiment: 160 J/mm at slow C-speed gave ~10.5 mA =
    # ~630 W and lack-of-fusion balling; CURR override x2 (~21 mA = ~1.26 kW) fused.
    # Linear energy J/mm alone does NOT guarantee substrate fusion: at low travel
    # speed a "correct" J/mm can still mean too little absolute beam power P=U*I.
    # ADVISORY only: never changes G-code; value MUST be calibrated per machine,
    # wire and material from a single-bead TEST. Set 0 to disable.
    min_beam_power_w: float = 900.0
    power_floor_warning_enabled: bool = True
    beam_current_mode: str = "energy"    # energy = calculate E0 from J/mm; current = use user E0 setpoint and calculate actual J/mm
    beam_current_bottom_ma: float = 28.0  # used only when beam_current_mode == "current"
    beam_current_top_ma: float = 25.0     # used only when beam_current_mode == "current"
    wire_min_mm_s: float = 0.3
    wire_max_mm_s: float = 40.0          # soft warning threshold, not a hard clamp
    # v4.2.9.7: wire-feed control. Auto uses layer_height*F*hatch/area.
    # manual_constant writes one operator-set E2 value and recalculates metal/energy metrics around it.
    # manual_bottom_top interpolates E2 from bottom to top.
    wire_feed_mode: str = "auto"  # auto / manual_constant / manual_bottom_top
    wire_feed_manual_mm_s: float = 0.0
    wire_feed_bottom_mm_s: float = 0.0
    wire_feed_top_mm_s: float = 0.0

    # v4.2.9.9: calibration by actual experience.
    # The JSON profile contains height zones with measured/derived Cfeed/E2/E0/Z-step.
    # It is applied primarily to the no-pause rotary C cylinder mode.
    experience_profile_enabled: bool = False
    experience_profile_json: str = ""
    experience_profile_apply_cfeed: bool = True
    experience_profile_apply_wire: bool = True
    experience_profile_apply_current: bool = False
    experience_profile_apply_z_step: bool = True
    experience_profile_update_m68_at_zone_boundaries: bool = False


    # Start/end shaping
    z_hop_mm: float = 7.0
    # Independent startup positioning height. Disabled by default for exact
    # compatibility because the safe absolute Z depends on machine work offsets.
    safe_initial_approach_enabled: bool = False
    safe_initial_approach_z_mm: float = 7.0
    lead_in_beam_mm: float = 0.6
    # v4.2.9.31: M68 is NOT synchronized with motion in LinuxCNC - every setpoint
    # breaks G64 blending, so the machine decelerates to a stop at each one. The
    # 20x70x30 block had 4065 M68 for 4061 moves (one per move): the whole layer
    # was executed as a chain of accel/decel steps. This option keeps one setpoint
    # per deposition line (no soft start/finish ramp) and roughly halves the count.
    simplify_wire_ramps: bool = False
    soft_start_mm: float = 2.0
    soft_finish_mm: float = 1.8
    tail_beam_mm: float = 0.8
    soft_wire_factor: float = 0.82
    edge_wire_factor_bottom: float = 0.94
    edge_wire_factor_top: float = 0.87
    near_edge_wire_factor_bottom: float = 0.97
    near_edge_wire_factor_top: float = 0.93
    use_w_retract: bool = True
    w_retract_mm: float = 0.80
    w_retract_feed_mm_min: float = 720.0
    use_m68_speed_retract: bool = False
    speed_retract_mm_s: float = 3.6
    speed_retract_time_s: float = 0.22

    # Pauses
    beam_preheat_s: float = 0.030
    wire_settle_s: float = 0.020
    beam_off_pause_s: float = 0.030
    layer_pause_bottom_s: float = 0.10
    layer_pause_top_s: float = 0.45

    # Output/safety and controller semantics
    # path_control_mode: g64_tolerance / machine_default / g61 / g61_1.
    # Q=0 explicitly disables LinuxCNC naive-CAM segment collapsing.
    path_control_mode: str = "g64_tolerance"
    g64_tolerance_mm: float = 0.08
    g64_naive_cam_q_mm: float = 0.0
    # Analog output mode is machine-specific. The Bormash-compatible default remains M68.
    # M67 is enabled only after the operator confirms HAL support.
    analog_output_mode: str = "m68_compatible"  # m68_compatible / m67_synchronized
    machine_m67_confirmed: bool = False
    # No-pause fixed-X modes are only geometrically valid for near-constant radius walls.
    rotary_c_radius_variation_tolerance_mm: float = 0.05
    safe_z_final_mm: float = 110.0
    rapid_feed_z_mm_min: float = 900.0
    work_z_feed_mm_min: float = 240.0
    units: str = "mm"
    machine_name: str = "Bormash EBAM"
    bormash_profile_enabled: bool = True
    bormash_check_xyz_limits: bool = True
    bormash_x_min_mm: float = 0.0
    bormash_x_max_mm: float = 3670.0
    bormash_y_min_mm: float = 0.0
    bormash_y_max_mm: float = 1510.0
    bormash_z_min_mm: float = 0.0
    bormash_z_max_mm: float = 1443.0
    include_comments: bool = True
    max_gcode_lines_warning: int = 500000
    max_gcode_size_mb_warning: float = 20.0

    def wire_area_mm2(self) -> float:
        return math.pi * self.wire_diameter_mm ** 2 / 4.0


@dataclass
class LayerInfo:
    index: int
    z: float
    z_next: float
    ratio: float
    current_ma: float
    feed_mm_min: float
    travel_speed_mm_s: float
    wire_mm_s: float
    energy_j_mm: float                 # target linear energy, kept for backward compatibility
    energy_actual_j_mm: float          # actual linear energy after current min/max clipping
    current_required_ma: float         # current required before clipping
    current_clipped_by_min: bool
    current_clipped_by_max: bool
    energy_j_mm3: float
    layer_pause_s: float
    segments_count: int = 0
    contour_segments_count: int = 0
    path_length_mm: float = 0.0
    contour_length_mm: float = 0.0
    commanded_e0_ma: Optional[float] = None
    commanded_e2_mm_s: Optional[float] = None
    analog_command_mode: str = "not_recorded"
    analog_command_update: bool = False


@dataclass
class AuditResult:
    ok: bool
    messages: List[str]
    stats: Dict[str, Any]


@dataclass
class GenerationResult:
    gcode: str
    layer_csv: str
    audit_text: str
    stats: Dict[str, Any]


def load_mesh_any(stl_path: str | Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(str(stl_path), force="mesh")
    if isinstance(mesh, trimesh.Scene):
        geoms = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError("STL/scene contains no mesh geometry")
        mesh = trimesh.util.concatenate(geoms)
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("File is not a valid triangular mesh")
    if mesh.faces.size == 0 or mesh.vertices.size == 0:
        raise ValueError("Mesh is empty")
    return mesh.copy()


def _normalise_axis_order(axis_order: str) -> str:
    order = (axis_order or "XYZ").upper().replace(" ", "")
    if len(order) != 3 or sorted(order) != ["X", "Y", "Z"]:
        return "XYZ"
    return order


def _apply_mesh_orientation(mesh: trimesh.Trimesh, settings: ProcessSettings) -> trimesh.Trimesh:
    """Apply user-selected STL orientation before normal XY/Z placement.

    This solves the common CAD/STL problem where a model is exported on the wrong
    side or with the build height along X/Y instead of Z.
    """
    m = mesh.copy()
    order = _normalise_axis_order(getattr(settings, "axis_order", "XYZ"))
    axis_index = {"X": 0, "Y": 1, "Z": 2}
    idx = [axis_index[a] for a in order]
    if idx != [0, 1, 2]:
        verts = np.asarray(m.vertices, dtype=float).copy()
        m.vertices = verts[:, idx]
        # An odd axis permutation (XZY, YXZ, ZYX) is a reflection: it flips mesh
        # handedness and inverts face normals, which can corrupt inside/outside
        # (hole) classification during slicing. Restore consistent winding, the
        # same way the mirror branch below does.
        def _perm_parity(p):
            seen = [False] * len(p)
            transpositions = 0
            for i in range(len(p)):
                if seen[i]:
                    continue
                j = i
                cycle = 0
                while not seen[j]:
                    seen[j] = True
                    j = p[j]
                    cycle += 1
                transpositions += cycle - 1
            return transpositions % 2  # 0 = even, 1 = odd
        if _perm_parity(idx) == 1:
            try:
                m.fix_normals()
            except Exception:
                pass

    mirror = np.array([
        -1.0 if getattr(settings, "mirror_x", False) else 1.0,
        -1.0 if getattr(settings, "mirror_y", False) else 1.0,
        -1.0 if getattr(settings, "mirror_z", False) else 1.0,
    ], dtype=float)
    if not np.allclose(mirror, 1.0):
        verts = np.asarray(m.vertices, dtype=float).copy()
        m.vertices = verts * mirror
        # Mirroring changes handedness; normals are not trusted afterwards.
        try:
            m.fix_normals()
        except Exception:
            pass

    def rot(angle_deg: float, axis: Tuple[float, float, float]):
        try:
            a = math.radians(float(angle_deg))
        except Exception:
            a = 0.0
        if abs(a) > 1e-12:
            m.apply_transform(trimesh.transformations.rotation_matrix(a, axis, point=[0.0, 0.0, 0.0]))

    rot(getattr(settings, "rotate_x_deg", 0.0), (1.0, 0.0, 0.0))
    rot(getattr(settings, "rotate_y_deg", 0.0), (0.0, 1.0, 0.0))
    rot(getattr(settings, "rotate_z_deg", 0.0), (0.0, 0.0, 1.0))
    return m


def normalize_mesh(mesh: trimesh.Trimesh, settings: ProcessSettings) -> trimesh.Trimesh:
    m = _apply_mesh_orientation(mesh, settings)
    bounds = m.bounds.copy()
    shift = np.zeros(3)
    if settings.center_xy:
        center_xy = (bounds[0, :2] + bounds[1, :2]) / 2.0
        shift[0] = -center_xy[0]
        shift[1] = -center_xy[1]
    else:
        shift[0] = -bounds[0, 0]
        shift[1] = -bounds[0, 1]
    if settings.z_to_zero:
        shift[2] = -bounds[0, 2]
    m.apply_translation(shift)
    m.apply_translation([
        float(getattr(settings, "output_offset_x_mm", 0.0)),
        float(getattr(settings, "output_offset_y_mm", 0.0)),
        float(getattr(settings, "output_offset_z_mm", 0.0)),
    ])
    return m

def mesh_summary(mesh: trimesh.Trimesh) -> Dict[str, float]:
    b = mesh.bounds
    ext = b[1] - b[0]
    return {
        "min_x": float(b[0, 0]), "max_x": float(b[1, 0]),
        "min_y": float(b[0, 1]), "max_y": float(b[1, 1]),
        "min_z": float(b[0, 2]), "max_z": float(b[1, 2]),
        "size_x": float(ext[0]), "size_y": float(ext[1]), "size_z": float(ext[2]),
        "volume_mm3": float(abs(mesh.volume)) if mesh.is_volume else float("nan"),
        "area_mm2": float(mesh.area),
        "is_watertight": bool(mesh.is_watertight),
        "source_type": "STL",
    }


def polygon_summary(polys: List[Polygon], height: float) -> Dict[str, float]:
    if not polys:
        raise ValueError(
            "Нет валидных 2D-полигонов для расчёта. "
            "Проверьте, что DXF/CSV содержит замкнутый контур X,Y, а не layers.csv/audit/report."
        )
    u = unary_union(polys) if len(polys) > 1 else polys[0]
    if u is None or getattr(u, "is_empty", False):
        raise ValueError("2D-геометрия пустая после очистки/объединения контуров.")
    minx, miny, maxx, maxy = u.bounds
    area = float(u.area)
    return {
        "min_x": float(minx), "max_x": float(maxx),
        "min_y": float(miny), "max_y": float(maxy),
        "min_z": 0.0, "max_z": float(height),
        "size_x": float(maxx - minx), "size_y": float(maxy - miny), "size_z": float(height),
        "volume_mm3": float(area * height),
        "area_mm2": float(area),
        "is_watertight": True,
        "source_type": "2D contour",
    }




def _regular_polygon_points(cx: float, cy: float, radius: float, n: int, rotation_deg: float = 0.0) -> List[Point]:
    n = max(3, int(n))
    rot = math.radians(rotation_deg)
    return [
        (cx + radius * math.cos(rot + 2.0 * math.pi * i / n),
         cy + radius * math.sin(rot + 2.0 * math.pi * i / n))
        for i in range(n)
    ]


def create_standard_shape_polygons(shape_type: str, params: Dict[str, Any]) -> List[Polygon]:
    """Create simple 2D standard shapes as Shapely polygons.

    The coordinate convention is EBAM-friendly: if center_xy is false later,
    shapes will be shifted so min X/Y = 0 before G-code generation.
    """
    stype = (shape_type or "rectangle").strip().lower()
    resolution = int(params.get("resolution", 96))
    resolution = max(16, min(resolution, 512))

    def v(name: str, default: float) -> float:
        try:
            return float(params.get(name, default))
        except Exception:
            return float(default)

    def clean(poly) -> List[Polygon]:
        return _clean_polygons([poly])

    if stype in ["rectangle", "rect", "прямоугольник"]:
        sx = max(v("width_x", 20.0), 0.01)
        sy = max(v("length_y", 100.0), 0.01)
        return clean(Polygon([(0, 0), (sx, 0), (sx, sy), (0, sy)]))

    if stype in ["square", "квадрат"]:
        a = max(v("side", 50.0), 0.01)
        return clean(Polygon([(0, 0), (a, 0), (a, a), (0, a)]))

    if stype in ["circle", "круг", "cylinder"]:
        r = max(v("radius", 25.0), 0.01)
        return clean(ShapelyPoint(0, 0).buffer(r, resolution=resolution))

    if stype in ["ring", "annulus", "кольцо"]:
        ro = max(v("outer_radius", 30.0), 0.01)
        ri = max(v("inner_radius", 20.0), 0.0)
        if ri >= ro:
            ri = ro * 0.65
        outer = ShapelyPoint(0, 0).buffer(ro, resolution=resolution)
        inner = ShapelyPoint(0, 0).buffer(ri, resolution=resolution)
        return clean(outer.difference(inner))

    if stype in ["ellipse", "эллипс"]:
        rx = max(v("radius_x", 40.0), 0.01)
        ry = max(v("radius_y", 20.0), 0.01)
        from shapely.affinity import scale
        return clean(scale(ShapelyPoint(0, 0).buffer(1.0, resolution=resolution), xfact=rx, yfact=ry, origin=(0, 0)))

    if stype in ["triangle", "треугольник"]:
        base = max(v("base", 60.0), 0.01)
        height = max(v("triangle_height", 50.0), 0.01)
        # Isosceles triangle, base along X, tip toward +Y.
        return clean(Polygon([(0.0, 0.0), (base, 0.0), (base / 2.0, height)]))

    if stype in ["regular_polygon", "polygon", "многоугольник"]:
        n = max(3, int(params.get("sides", 6)))
        r = max(v("radius", 30.0), 0.01)
        rot = v("rotation_deg", 0.0)
        return clean(Polygon(_regular_polygon_points(0.0, 0.0, r, n, rot)))

    if stype in ["star", "звезда"]:
        points = max(3, int(params.get("points", 5)))
        ro = max(v("outer_radius", 35.0), 0.01)
        ri = max(v("inner_radius", 17.0), 0.01)
        rot = math.radians(v("rotation_deg", -90.0))
        coords: List[Point] = []
        for i in range(points * 2):
            r = ro if i % 2 == 0 else min(ri, ro * 0.95)
            a = rot + math.pi * i / points
            coords.append((r * math.cos(a), r * math.sin(a)))
        return clean(Polygon(coords))

    if stype in ["capsule", "rounded_slot", "овал-капсула", "капсула"]:
        length = max(v("length_y", 100.0), 0.01)
        width = max(v("width_x", 20.0), 0.01)
        r = width / 2.0
        if length <= width:
            return clean(ShapelyPoint(0, 0).buffer(r, resolution=resolution))
        from shapely.geometry import LineString
        return clean(LineString([(0.0, r), (0.0, length - r)]).buffer(r, resolution=resolution))

    raise ValueError(f"Unsupported standard shape: {shape_type}")



def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, float(x)))
    return x * x * (3.0 - 2.0 * x)


def rotational_shell_outer_radius(z: float, params: Dict[str, Any]) -> float:
    """Outer radius profile for simple EBAM vessels / balloon shells."""
    h = max(float(params.get("height_mm", params.get("height", 80.0))), 1e-6)
    t = max(0.0, min(1.0, float(z) / h))
    profile = str(params.get("profile_type", "bowl")).lower()
    bottom_r = max(float(params.get("bottom_diameter_mm", 60.0)) * 0.5, 0.01)
    top_r = max(float(params.get("top_diameter_mm", 100.0)) * 0.5, 0.01)
    max_r = max(float(params.get("max_diameter_mm", max(bottom_r, top_r) * 2.0)) * 0.5, bottom_r, top_r, 0.01)
    bulge = max(float(params.get("bulge", 1.0)), 0.15)
    peak_z = max(0.15, min(0.85, float(params.get("peak_z_fraction", 0.55))))

    if profile in ["straight_cup", "cylinder", "цилиндр", "стакан"]:
        return top_r
    if profile in ["cone", "конус"]:
        return bottom_r + (top_r - bottom_r) * t
    if profile in ["bowl", "cup", "чаша", "горшок"]:
        # Pot/bowl: from bottom to top with smooth curvature.
        return bottom_r + (top_r - bottom_r) * (t ** bulge)
    if profile in ["balloon", "sphere_balloon", "шар", "шар-баллон", "баллон"]:
        if t <= peak_z:
            return bottom_r + (max_r - bottom_r) * _smoothstep(t / peak_z)
        return max_r + (top_r - max_r) * _smoothstep((t - peak_z) / max(1e-9, 1.0 - peak_z))
    # Fallback: smooth bowl.
    return bottom_r + (top_r - bottom_r) * (t ** bulge)


def rotational_shell_polygons_at_z(z: float, params: Dict[str, Any]) -> List[Polygon]:
    """Return XY annulus/solid polygon for a rotational EBAM vessel layer."""
    res = int(params.get("resolution", 192))
    res = max(32, min(res, 768))
    h = max(float(params.get("height_mm", params.get("height", 80.0))), 1e-6)
    wall = max(float(params.get("wall_thickness_mm", 4.0)), 0.1)
    bottom_solid = max(float(params.get("bottom_solid_mm", max(1.0, wall))), 0.0)
    # Keep the model in positive Bormash table coordinates by default.
    max_d = max(float(params.get("max_diameter_mm", 0.0)), float(params.get("top_diameter_mm", 0.0)), float(params.get("bottom_diameter_mm", 0.0)), 1.0)
    cx = cy = max_d * 0.5 + 2.0
    zc = max(0.0, min(float(z), h))
    ro = max(rotational_shell_outer_radius(zc, params), 0.05)
    # Bottom closed region: solid disk for initial layers.
    if zc <= bottom_solid:
        ri = 0.0
    else:
        ri = max(0.0, ro - wall)
    outer = ShapelyPoint(cx, cy).buffer(ro, resolution=res)
    if ri <= 0.05:
        return _clean_polygons([outer])
    inner = ShapelyPoint(cx, cy).buffer(ri, resolution=res)
    return _clean_polygons([outer.difference(inner)])


def rotational_shell_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    h = max(float(params.get("height_mm", params.get("height", 80.0))), 0.1)
    samples = [rotational_shell_outer_radius(h * i / 200.0, params) for i in range(201)]
    max_r = max(samples) if samples else 1.0
    min_r = min(samples) if samples else 1.0
    diameter = 2.0 * max_r
    wall = max(float(params.get("wall_thickness_mm", 4.0)), 0.1)
    return {
        "source_type": "rotational_vessel",
        "profile_type": str(params.get("profile_type", "bowl")),
        "height_mm": h,
        "wall_thickness_mm": wall,
        "bottom_solid_mm": float(params.get("bottom_solid_mm", max(1.0, wall))),
        "size_x": diameter + 4.0,
        "size_y": diameter + 4.0,
        "size_z": h,
        "min_x": 0.0,
        "min_y": 0.0,
        "min_z": 0.0,
        "max_x": diameter + 4.0,
        "max_y": diameter + 4.0,
        "max_z": h,
        "max_outer_diameter_mm": diameter,
        "min_outer_diameter_mm": 2.0 * min_r,
        "recommended_process": "ring/spiral preferred; this version approximates by per-layer hatch/continuous path through annular STL-like sections",
        "is_watertight": True,
    }


def _rotational_shell_center(params: Dict[str, Any]) -> Tuple[float, float]:
    max_d = max(float(params.get("max_diameter_mm", 0.0)), float(params.get("top_diameter_mm", 0.0)), float(params.get("bottom_diameter_mm", 0.0)), 1.0)
    c = max_d * 0.5 + 2.0
    return c, c


def rotational_shell_inner_radius(z: float, params: Dict[str, Any]) -> float:
    """Inner radius of vessel at Z. Zero means solid/closed region."""
    h = max(float(params.get("height_mm", params.get("height", 80.0))), 1e-6)
    wall = max(float(params.get("wall_thickness_mm", 4.0)), 0.1)
    bottom_solid = max(float(params.get("bottom_solid_mm", max(1.0, wall))), 0.0)
    zc = max(0.0, min(float(z), h))
    ro = max(rotational_shell_outer_radius(zc, params), 0.05)
    if zc <= bottom_solid:
        return 0.0
    return max(0.0, ro - wall)


def _circle_segments(cx: float, cy: float, r: float, n: int, clockwise: bool = False, start_angle: float = 0.0) -> List[Segment]:
    if r <= 1e-6:
        return []
    n = max(24, int(n))
    angles = [start_angle + (2.0 * math.pi * i / n) for i in range(n + 1)]
    if clockwise:
        angles = list(reversed(angles))
    pts = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]
    return [(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) for i in range(len(pts)-1)]


def rotational_ring_segments_at_z(z: float, params: Dict[str, Any], settings: ProcessSettings, layer_index: int) -> List[Segment]:
    """Concentric circular passes inside a rotational vessel layer."""
    cx, cy = _rotational_shell_center(params)
    npts = max(24, min(int(getattr(settings, "rotational_points_per_circle", 160)), 2048))
    radii = rotational_layer_radii_at_z(z, params, settings)
    # Alternate circle direction each layer/ring to spread starts and reduce seam build-up.
    segs: List[Segment] = []
    for i, r in enumerate(radii):
        start = (layer_index % 12) * (math.pi / 6.0) + i * (math.pi / 9.0)
        segs.extend(_circle_segments(cx, cy, r, npts, clockwise=((layer_index + i) % 2 == 0), start_angle=start))
    return segs


def _effective_rotational_radial_step(settings: ProcessSettings) -> float:
    """Return the radial step actually used by rotational ring/spiral/C strategies.

    Priority:
    1) explicit rotational_radial_step_mm if > 0;
    2) auto hatch from measured bead width if enabled;
    3) plain hatch_spacing.

    v4.2.9.9 fixes v4.2.9.7 where auto_hatch_from_bead changed UI text
    but rotary C / vessel ring modes still used the old hatch_spacing.
    """
    explicit = float(getattr(settings, "rotational_radial_step_mm", 0.0) or 0.0)
    if explicit > 0.0:
        return max(explicit, 0.1)
    if bool(getattr(settings, "auto_hatch_from_bead", False)) and float(getattr(settings, "bead_width_mm", 0.0) or 0.0) > 0.0:
        model = str(getattr(settings, "overlap_model", "tom") or "tom").strip().lower()
        factor = 0.667 if model in ("fom", "flat", "flat_top", "0667", "0.667") else 0.738
        return max(float(getattr(settings, "bead_width_mm", 0.0)) * factor, 0.1)
    return max(float(getattr(settings, "hatch_spacing", 0.0) or 0.0), 0.1)


def rotational_spiral_segments_at_z(z: float, params: Dict[str, Any], settings: ProcessSettings, layer_index: int) -> List[Segment]:
    """Archimedean spiral inside a rotational vessel layer."""
    cx, cy = _rotational_shell_center(params)
    ro = max(rotational_shell_outer_radius(z, params), 0.05)
    ri = max(rotational_shell_inner_radius(z, params), 0.0)
    pitch = _effective_rotational_radial_step(settings)
    pitch = max(pitch, 0.1)
    npts_circle = max(48, min(int(getattr(settings, "rotational_points_per_circle", 160)), 2048))
    r_start = max(ri + pitch * 0.35, 0.35 if ri <= 0.05 else ri + 0.05)
    r_end = max(ro - pitch * 0.35, r_start + 0.1)
    radial_span = max(r_end - r_start, 0.1)
    turns = max(1.0, radial_span / pitch)
    samples = max(80, int(npts_circle * turns))
    theta0 = (layer_index % 16) * (math.pi / 8.0)
    clockwise = (layer_index % 2 == 0)
    pts: List[Tuple[float, float]] = []
    for i in range(samples + 1):
        u = i / max(samples, 1)
        r = r_start + radial_span * u
        a = theta0 + ( -1.0 if clockwise else 1.0) * (2.0 * math.pi * turns * u)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) for i in range(len(pts)-1)]


def rotational_layer_radii_at_z(z: float, params: Dict[str, Any], settings: ProcessSettings) -> List[float]:
    """Return physical radii to deposit for one rotational vessel layer.

    Used by XY ring preview/generation and by the rotary C-table strategy.
    v4.2.9.6: rings are intentionally ordered from the inner radius to the outer radius.
    For a solid bottom this means centre/near-centre first, then outward.
    For a shell wall this means inner wall first, then outer wall.
    """
    ro = max(rotational_shell_outer_radius(z, params), 0.05)
    ri = max(rotational_shell_inner_radius(z, params), 0.0)
    step = _effective_rotational_radial_step(settings)
    step = max(step, 0.1)
    radii: List[float] = []
    if ri <= 0.05:
        # Solid layer/bottom: start near the centre and grow outward.
        r = max(step * 0.5, min(ro, step * 0.5))
        while r < ro - step * 0.25:
            radii.append(float(r))
            r += step
        if not radii or radii[-1] < ro - step * 0.35:
            radii.append(float(ro))
    else:
        # Shell/annulus: first pass near the inner wall, next passes outward.
        r = ri + step * 0.5
        while r < ro - step * 0.25:
            radii.append(float(max(r, ri + 0.05)))
            r += step
        if not radii:
            radii.append(float((ro + ri) * 0.5))
        elif radii[-1] < ro - step * 0.35:
            radii.append(float(max(ro - step * 0.5, ri + 0.05)))
    # Defensive monotonicity cleanup: remove duplicates and keep inner -> outer order.
    cleaned: List[float] = []
    for r in sorted(float(x) for x in radii if float(x) > 1e-6):
        if not cleaned or abs(r - cleaned[-1]) > 1e-6:
            cleaned.append(r)
    return cleaned


def _section_radii_from_polygons_for_rotary_c(polys: List[Polygon], center_xy: Tuple[float, float], settings: ProcessSettings) -> Tuple[List[float], Dict[str, float]]:
    """Approximate an arbitrary STL horizontal section by C-table circular radii.

    For true bodies of revolution this matches the real geometry well. For non-round
    STL it is intentionally conservative and returns a warning metric: the operator
    should use it only when the STL is close to an axisymmetric cup/balloon around C.
    """
    step = _effective_rotational_radial_step(settings)
    step = max(step, 0.1)
    cx, cy = float(center_xy[0]), float(center_xy[1])
    outer_radii: List[float] = []
    inner_candidates: List[float] = []
    areas: List[float] = []
    for poly in polys or []:
        if poly is None or poly.is_empty:
            continue
        geoms = [poly] if isinstance(poly, Polygon) else list(getattr(poly, "geoms", []))
        for p in geoms:
            if p is None or p.is_empty or not isinstance(p, Polygon) or p.area <= 1e-6:
                continue
            try:
                ext = list(p.exterior.coords)
                dists = [math.hypot(float(x) - cx, float(y) - cy) for x, y in ext]
                if dists:
                    outer_radii.append(max(dists))
                    areas.append(float(p.area))
                for ring in p.interiors:
                    pts = list(ring.coords)
                    hd = [math.hypot(float(x) - cx, float(y) - cy) for x, y in pts]
                    if hd:
                        # hole boundary: use average, not min/max, to avoid one bad vertex defining the wall
                        inner_candidates.append(sum(hd) / len(hd))
            except Exception:
                continue
    if not outer_radii:
        return [], {"outer_radius_mm": 0.0, "inner_radius_mm": 0.0, "roundness_error_pct": 0.0, "area_mm2": 0.0}
    ro = max(outer_radii)
    # If there is an actual hole, use it as inner radius. If not, treat as solid disk.
    ri = max(0.0, min(inner_candidates) if inner_candidates else 0.0)
    all_dist = outer_radii[:] + inner_candidates[:]
    # roundness error estimated from outer radius spread across polygons/loops
    roundness = 0.0
    if len(outer_radii) >= 2 and ro > 1e-9:
        roundness = (max(outer_radii) - min(outer_radii)) / ro * 100.0
    radii: List[float] = []
    if ri <= 0.05:
        # Solid STL/section: start near the centre and grow outward.
        r = max(step * 0.5, min(ro, step * 0.5))
        while r < ro - step * 0.25:
            radii.append(float(r))
            r += step
        if not radii or radii[-1] < ro - step * 0.35:
            radii.append(float(ro))
    else:
        # Annular STL/section: start from inner diameter and move outward.
        r = ri + step * 0.5
        while r < ro - step * 0.25:
            radii.append(float(max(r, ri + 0.05)))
            r += step
        if not radii:
            radii.append(float((ro + ri) * 0.5))
        elif radii[-1] < ro - step * 0.35:
            radii.append(float(max(ro - step * 0.5, ri + 0.05)))
    radii = sorted(set(round(float(r), 9) for r in radii if float(r) > 1e-6))
    return radii, {
        "outer_radius_mm": float(ro),
        "inner_radius_mm": float(ri),
        "roundness_error_pct": float(roundness),
        "area_mm2": float(sum(areas)),
    }


def _generate_mesh_rotary_c(mesh_n: trimesh.Trimesh, stats: Dict[str, Any], settings: ProcessSettings, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    """Generate experimental C-table rings from an STL by slicing and estimating circular radii.

    This mode makes the recommendation actionable for round/axisymmetric STL models.
    It does not magically make arbitrary STL suitable for C-table: if the section is not
    close to a body of revolution, audit/warnings tell the operator to use normal STL paths
    or the parametric vessel mode.
    """
    validate_process_settings(settings, height=float(stats.get("size_z", 0.0)))
    height = float(stats["size_z"])
    radial_step = _effective_rotational_radial_step(settings)
    radial_step = max(radial_step, 0.1)
    effective_settings = replace(settings, hatch_spacing=radial_step, deposition_strategy="continuous")
    experience_profile = _load_experience_profile(effective_settings)
    z_values_full = _experience_z_values(effective_settings, height, experience_profile)
    n_layers_full = len(z_values_full)
    if effective_settings.max_layers_to_generate and effective_settings.max_layers_to_generate > 0:
        n_layers = max(1, min(int(effective_settings.max_layers_to_generate), n_layers_full))
    else:
        n_layers = n_layers_full
    z_values = z_values_full[:n_layers]
    z_next_values = [z_values_full[i + 1] if i + 1 < len(z_values_full) else height for i in range(n_layers)]
    z_sections = [min(z + max(0.05, (zn - z)) * 0.5, height - 1e-4) for z, zn in zip(z_values, z_next_values)]
    probe = effective_settings.layer_height * effective_settings.section_probe_fraction if effective_settings.adaptive_section_probe else 0.0
    geo_cx = (float(stats.get("min_x", 0.0)) + float(stats.get("max_x", 0.0))) * 0.5
    geo_cy = (float(stats.get("min_y", 0.0)) + float(stats.get("max_y", 0.0))) * 0.5
    lines = _gcode_header(effective_settings, stats)
    lines.append(f"(STL_SPECIAL_PATH: stl_rotary_c_rings; radial_step={_fmt(radial_step,3)} mm; B={_fmt(effective_settings.rotary_c_b_angle_deg,3)} deg; C-axis table rings from STL sections)")
    lines.append("(RING_ORDER: inner_to_outer; first pass uses the smallest available radius, then rings grow outward)")
    lines.append("(STL_C_TABLE_NOTE: STL sections are approximated as circular radii around the model XY center; use only for near-axisymmetric STL)")
    lines.append(f"(STL_GEOMETRY_CENTER_FOR_RADIUS: X={_fmt(geo_cx,3)} Y={_fmt(geo_cy,3)})")
    lines.append(f"G0 B{_fmt(effective_settings.rotary_c_b_angle_deg,3)}")
    lines.append(f"G0 C{_fmt(effective_settings.rotary_c_start_deg,3)}")
    if effective_settings.use_w_retract and effective_settings.w_retract_mm > 0:
        lines.append("(Initial W retract calibration)")
        lines.append("G10 L20 P0 W0")
        lines.extend(_relative_w_move(-effective_settings.w_retract_mm, effective_settings.w_retract_feed_mm_min))
    warnings: List[str] = [
        "STL rotary C-table strategy: STL layer sections are approximated by circular radii around the STL XY centre.",
        "Use this only for bodies close to revolution. If section arrows/preview do not match the real part, use normal STL path or parametric vessel mode.",
    ]
    if abs(float(effective_settings.rotary_c_b_angle_deg)) > 1e-6:
        warnings.append("WARNING: STL rotary C mode is intended first with B=0. Non-zero B requires TCP/centre calibration and collision checks.")
    layer_infos: List[LayerInfo] = []
    max_c_required = 0.0
    max_c_used = 0.0
    min_radius_used = float("inf")
    max_radius_used = 0.0
    feed_limited_count = 0
    thermal_dwell_count = 0
    thermal_dwell_total_s = 0.0
    small_radius_count = 0
    total_active_len = 0.0
    total_rings = 0
    max_rings_layer = 0
    max_roundness_pct = 0.0
    skipped_layers = 0
    for idx, (z, zsec, z_next) in enumerate(zip(z_values, z_sections, z_next_values), start=1):
        if progress_callback and ((idx == 1) or (idx == n_layers) or (idx % max(1, int(effective_settings.progress_update_every_layers)) == 0)):
            try:
                progress_callback(idx, n_layers, "stl_rotary_c")
            except Exception:
                pass
        polys = _section_polygons_at_z(mesh_n, zsec, probe_radius=probe)
        radii, rstats = _section_radii_from_polygons_for_rotary_c(polys, (geo_cx, geo_cy), effective_settings)
        max_roundness_pct = max(max_roundness_pct, float(rstats.get("roundness_error_pct", 0.0)))
        if not radii:
            skipped_layers += 1
            warnings.append(f"Layer {idx}: no STL C-radii at Z={zsec:.3f}; skipped")
            continue
        max_rings_layer = max(max_rings_layer, len(radii))
        lines.append(f"(--- LAYER {idx}/{n_layers} Z={_fmt(z,3)} STL_ROTARY_C rings={len(radii)} outerR={_fmt(rstats.get('outer_radius_mm',0.0),3)} innerR={_fmt(rstats.get('inner_radius_mm',0.0),3)} ---)")
        for j, radius in enumerate(radii, start=1):
            base = layer_parameters(z, height, effective_settings)
            required_c = rotary_c_speed_deg_min(base.feed_mm_min, radius)
            max_c_required = max(max_c_required, required_c)
            # v4.2.9.31: radial feed-rate compensation (constant-velocity mode).
            c_feed, actual_feed, pitch_factor = _rotary_c_ring_kinematics(
                effective_settings, radius, base.feed_mm_min)
            if radius < float(effective_settings.rotary_c_min_radius_mm):
                small_radius_count += 1
            if (not bool(getattr(effective_settings, "rotary_c_constant_velocity", False))
                    and required_c > float(effective_settings.rotary_c_max_deg_min) + 1e-9):
                if bool(getattr(effective_settings, "rotary_c_auto_limit_feed", True)):
                    feed_limited_count += 1
                else:
                    warnings.append(f"WARNING: layer {idx} radius {radius:.3f} requires C feed {required_c:.1f} deg/min above limit {effective_settings.rotary_c_max_deg_min:.1f}")
            layer = _layer_parameters_with_feed(z, height, effective_settings, actual_feed)
            if pitch_factor < 0.999:
                layer.wire_mm_s = float(layer.wire_mm_s) * pitch_factor
                _area_pf = effective_settings.wire_area_mm2()
                _qm = _area_pf * layer.wire_mm_s * float(effective_settings.deposition_efficiency)
                layer.energy_j_mm3 = float(effective_settings.voltage_kv * layer.current_ma / _qm) if _qm > 0 else float("inf")
            layer.index = idx
            layer.segments_count = 1
            layer.contour_segments_count = 0
            layer.path_length_mm = 2.0 * math.pi * float(radius)
            layer.contour_length_mm = 0.0
            lines.extend(_rotary_c_pass_gcode(radius, layer, effective_settings, j, len(radii), c_feed))
            layer_infos.append(layer)
            total_active_len += layer.path_length_mm
            total_rings += 1
            min_radius_used = min(min_radius_used, float(radius))
            max_radius_used = max(max_radius_used, float(radius))
            max_c_used = max(max_c_used, c_feed)
        if layer_infos and layer_infos[-1].layer_pause_s > 0:
            lines.append(f"G4 P{_fmt(layer_infos[-1].layer_pause_s,3)} (layer thermal stabilization)")
        _tdw = _thermal_dwell_for_layer(effective_settings, sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos[-len(radii):]))
        if _tdw > 0:
            lines.append(f"G4 P{_fmt(_tdw,1)} (THERMAL_DWELL L{idx}: adaptive - layer cycle below minimum)")
            thermal_dwell_count += 1
            thermal_dwell_total_s += _tdw
    if not layer_infos:
        raise RuntimeError("No STL rotary C layers were generated. Check STL orientation, sectioning, radial step and height.")
    if thermal_dwell_count:
        warnings.append(f"ТЕПЛОВЫЕ ВЫДЕРЖКИ: {thermal_dwell_count} шт, суммарно {thermal_dwell_total_s/60.0:.1f} мин "
                        f"(мин. цикл слоя {float(getattr(effective_settings, 'thermal_min_layer_cycle_min', 0.0)):.1f} мин, пол {float(getattr(effective_settings, 'thermal_min_dwell_s', 0.0)):.0f} с); время включено в G-code (G4).")
    if feed_limited_count:
        warnings.append(f"WARNING: {feed_limited_count} STL rotary C passes were feed-limited by C max speed; E0/E2 were recalculated for the reduced linear speed.")
    if small_radius_count:
        warnings.append(f"WARNING: {small_radius_count} STL rotary C passes use radius below configured warning radius {effective_settings.rotary_c_min_radius_mm:.3f} mm.")
    if max_roundness_pct > 8.0:
        warnings.append(f"WARNING: STL sections are not very round (estimated spread up to {max_roundness_pct:.1f}%). C-table circular approximation may not match the STL shape.")
    footer_settings = replace(effective_settings, safe_z_final_mm=max(effective_settings.safe_z_final_mm, height + effective_settings.z_hop_mm + 5.0))
    lines.extend(_gcode_footer(footer_settings))
    gcode = "\n".join(lines) + "\n"
    audit = audit_gcode(gcode, effective_settings)
    out_stats = dict(stats)
    out_stats.update({
        "app_version": APP_VERSION,
        "source_type": "STL rotary C-table",
        "rotational_path_strategy": "stl_rotary_c_rings",
        "rotary_c_mode": "B_fixed_C_axis_rings_from_STL",
        "stl_rotary_c_geo_center_x_mm": float(geo_cx),
        "stl_rotary_c_geo_center_y_mm": float(geo_cy),
        "stl_rotary_c_roundness_error_max_pct": float(max_roundness_pct),
        "stl_rotary_c_skipped_layers": int(skipped_layers),
        "rotary_c_center_x_mm": float(effective_settings.rotary_c_center_x_mm),
        "rotary_c_center_y_mm": float(effective_settings.rotary_c_center_y_mm),
        "rotary_c_direction": str(effective_settings.rotary_c_direction),
        "rotary_c_start_deg": float(effective_settings.rotary_c_start_deg),
        "rotary_c_b_angle_deg": float(effective_settings.rotary_c_b_angle_deg),
        "rotary_c_max_deg_min": float(effective_settings.rotary_c_max_deg_min),
        "rotary_c_min_radius_mm": float(effective_settings.rotary_c_min_radius_mm),
        "rotary_c_feed_limited_count": int(feed_limited_count),
        "rotary_c_small_radius_count": int(small_radius_count),
        "rotary_c_required_max_deg_min": float(max_c_required),
        "rotary_c_used_max_deg_min": float(max_c_used),
        "rotary_c_min_radius_used_mm": 0.0 if min_radius_used == float("inf") else float(min_radius_used),
        "rotary_c_max_radius_used_mm": float(max_radius_used),
        "rotary_c_warning_radius_below_mm": float(effective_settings.rotary_c_min_radius_mm),
        "rotary_c_real_radius_limit_note": "C feed limiting uses each real ring radius; warning radius is only a warning threshold",
        "min_x": float(effective_settings.rotary_c_center_x_mm) + (0.0 if min_radius_used == float("inf") else float(min_radius_used)),
        "max_x": float(effective_settings.rotary_c_center_x_mm) + float(max_radius_used),
        "min_y": float(effective_settings.rotary_c_center_y_mm),
        "max_y": float(effective_settings.rotary_c_center_y_mm),
        "rotational_radial_step_mm": radial_step,
        "layers_total": len(set(li.index for li in layer_infos)),
        "passes_total": len(layer_infos),
        "rings_total": int(total_rings),
        "layers_requested": n_layers,
        "layers_full_model": n_layers_full,
        "is_test_truncated": bool(n_layers < n_layers_full),
        "segments_total": int(total_rings),
        "contour_segments_total": 0,
        "contour_path_length_mm": 0.0,
        "max_segments_per_layer": max_rings_layer,
        "active_path_length_mm": total_active_len,
        "active_path_length_m": total_active_len / 1000.0,
        "estimated_active_time_s": sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos),
        "estimated_wire_length_mm": sum((li.path_length_mm / max(li.travel_speed_mm_s, 1e-9)) * li.wire_mm_s for li in layer_infos),
        "wire_min_calculated_mm_s": min((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_max_calculated_mm_s": max((li.wire_mm_s for li in layer_infos), default=0.0),
        "feed_min_mm_min": min((li.feed_mm_min for li in layer_infos), default=0.0),
        "feed_max_mm_min": max((li.feed_mm_min for li in layer_infos), default=0.0),
        "process_wire_warning_limit_mm_s": effective_settings.wire_max_mm_s,
        "wire_above_control_limit": any(li.wire_mm_s > effective_settings.wire_max_mm_s for li in layer_infos),
        "current_min_ma": effective_settings.current_min_ma,
        "current_low_warning_ma": effective_settings.current_low_warning_ma,
        "current_limit_ma": effective_settings.current_max_ma,
        "beam_current_mode": str(getattr(effective_settings, "beam_current_mode", "energy")),
        "beam_current_bottom_ma": float(getattr(effective_settings, "beam_current_bottom_ma", 0.0)),
        "beam_current_top_ma": float(getattr(effective_settings, "beam_current_top_ma", 0.0)),
        "current_required_min_ma": min((li.current_required_ma for li in layer_infos), default=0.0),
        "current_required_max_ma": max((li.current_required_ma for li in layer_infos), default=0.0),
        "current_clipped_by_min": any(li.current_clipped_by_min for li in layer_infos),
        "current_clipped_by_max": any(li.current_clipped_by_max for li in layer_infos),
        "current_clipped_by_limit": any(li.current_clipped_by_max for li in layer_infos),
        "energy_target_min_j_mm": min((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_target_max_j_mm": max((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_actual_min_j_mm": min((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_actual_max_j_mm": max((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_volume_min_j_mm3": min((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "energy_volume_max_j_mm3": max((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "bormash_profile_enabled": bool(is_bormash_profile(effective_settings)),
        "gcode_lines": len(lines),
        "gcode_size_mb": len(gcode.encode("utf-8")) / (1024.0 * 1024.0),
        "deposition_strategy": "stl_rotary_c_table_rings",
        "hatch_spacing_effective_mm": radial_step,
    })
    out_stats["estimated_active_time_h"] = out_stats["estimated_active_time_s"] / 3600.0
    passes = len(layer_infos)
    layer_pause_time_s = sum(li.layer_pause_s for li in layer_infos)
    per_pass_delay_s = effective_settings.beam_preheat_s + effective_settings.wire_settle_s + effective_settings.beam_off_pause_s
    if effective_settings.use_w_retract and effective_settings.w_retract_mm > 0 and effective_settings.w_retract_feed_mm_min > 0:
        per_pass_delay_s += 2.0 * effective_settings.w_retract_mm / (effective_settings.w_retract_feed_mm_min / 60.0)
    service_time_s = passes * per_pass_delay_s + layer_pause_time_s
    out_stats["estimated_service_time_s"] = service_time_s
    out_stats["estimated_total_time_s"] = out_stats["estimated_active_time_s"] + service_time_s
    out_stats["estimated_total_time_h"] = out_stats["estimated_total_time_s"] / 3600.0
    report = audit_report(effective_settings, out_stats, audit, warnings, layer_infos)
    return GenerationResult(gcode=gcode, layer_csv=layer_table_csv(layer_infos), audit_text=report, stats=out_stats)


def _beam_current_and_energy_for_layer(z: float, zmax: float, settings: ProcessSettings, feed_mm_min: float) -> Tuple[float, float, float, bool, bool]:
    """Return (target_energy_j_mm, current_required_ma, current_gcode_ma, clipped_min, clipped_max).

    In normal mode desired linear energy J/mm is primary and E0 current is calculated.
    In current-setpoint mode the operator chooses E0 bottom/top; actual J/mm then
    follows from the selected current, voltage and travel speed.
    """
    ratio = 0.0 if zmax <= 1e-9 else min(max(float(z) / float(zmax), 0.0), 1.0)
    f = max(0.1, float(feed_mm_min))
    travel = f / 60.0
    mode = str(getattr(settings, "beam_current_mode", "energy") or "energy").strip().lower()
    if mode in ("current", "manual_current", "e0", "fixed_current"):
        c0 = float(getattr(settings, "beam_current_bottom_ma", 28.0))
        c1 = float(getattr(settings, "beam_current_top_ma", 25.0))
        current_required = c0 + (c1 - c0) * ratio
        current = max(float(settings.current_min_ma), min(float(settings.current_max_ma), current_required))
        e_target = float(settings.voltage_kv) * current_required / max(travel, 1e-9)
    else:
        e_target = float(settings.target_energy_bottom_j_per_mm) + (float(settings.target_energy_top_j_per_mm) - float(settings.target_energy_bottom_j_per_mm)) * ratio
        current_required = e_target * f / max(60.0 * float(settings.voltage_kv), 1e-9)
        current = max(float(settings.current_min_ma), min(float(settings.current_max_ma), current_required))
    clipped_by_min = current_required < float(settings.current_min_ma) - 1e-9
    clipped_by_max = current_required > float(settings.current_max_ma) + 1e-9
    return float(e_target), float(current_required), float(current), bool(clipped_by_min), bool(clipped_by_max)



def _auto_wire_for_feed(settings: ProcessSettings, feed_mm_min: float) -> float:
    """Calculate E2 wire feed from geometry and actual linear feed."""
    travel = max(float(feed_mm_min), 0.0) / 60.0
    area = max(settings.wire_area_mm2() * float(settings.deposition_efficiency), 1e-9)
    return max(0.0, float(settings.layer_height) * travel * float(settings.hatch_spacing) / area)


def _wire_for_layer_ratio(settings: ProcessSettings, feed_mm_min: float, ratio: float) -> float:
    """Return E2 for a layer.

    v4.2.9.9 rule:
    - auto: E2 follows actual F, layer height and hatch/radial step;
    - manual_constant: operator value is used directly, including 0.000 mm/s for dry/no-wire tests;
    - manual_bottom_top: E2 is interpolated by build height, including explicit zeros.

    Earlier v4.2.9.7 treated manual 0 as "not set" and silently fell back to auto.
    That was unsafe for diagnostics because the UI said "manual", but the G-code used auto E2.
    """
    mode = str(getattr(settings, "wire_feed_mode", "auto") or "auto").strip().lower()
    if mode == "manual_constant":
        q = float(getattr(settings, "wire_feed_manual_mm_s", 0.0) or 0.0)
        return max(0.0, q)
    if mode == "manual_bottom_top":
        qb = max(0.0, float(getattr(settings, "wire_feed_bottom_mm_s", 0.0) or 0.0))
        qt = max(0.0, float(getattr(settings, "wire_feed_top_mm_s", 0.0) or 0.0))
        r = min(max(float(ratio), 0.0), 1.0)
        return max(0.0, qb + (qt - qb) * r)
    return _auto_wire_for_feed(settings, feed_mm_min)




# ------------------------- v4.2.9.9 experience calibration -------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(str(value).replace(',', '.'))
    except Exception:
        return float(default)


def _experience_wall_from_od_id(outer_diameter_mm: float, inner_diameter_mm: float, measured_wall_mm: float = 0.0) -> float:
    od = _safe_float(outer_diameter_mm, 0.0)
    inner = _safe_float(inner_diameter_mm, 0.0)
    measured = _safe_float(measured_wall_mm, 0.0)
    if measured > 0:
        return measured
    if od > 0 and inner >= 0 and od >= inner:
        return (od - inner) * 0.5
    return 0.0


def _linear_feed_from_cfeed(cfeed_deg_min: float, radius_mm: float) -> float:
    return max(0.0, _safe_float(cfeed_deg_min, 0.0) * math.pi * max(_safe_float(radius_mm, 0.0), 1e-9) / 180.0)


def _wire_for_target_wall(target_wall_mm: float, z_step_mm: float, linear_feed_mm_min: float, wire_diameter_mm: float, deposition_efficiency: float = 1.0) -> float:
    """Approximate E2 for a one-bead wall from desired wall thickness and Z pitch.

    Model: A_path = wall * Zstep; E2 = A_path * (F/60) / Awire.
    This is intentionally simple and transparent; real EBAM still needs TEST calibration.
    """
    wall = max(_safe_float(target_wall_mm, 0.0), 0.0)
    zstep = max(_safe_float(z_step_mm, 0.0), 1e-9)
    f = max(_safe_float(linear_feed_mm_min, 0.0), 0.0) / 60.0
    d = max(_safe_float(wire_diameter_mm, 1.2), 1e-9)
    area = math.pi * d * d / 4.0
    eta = max(_safe_float(deposition_efficiency, 1.0), 1e-9)
    return max(0.0, wall * zstep * f / max(area * eta, 1e-9))


def build_experience_calibration_profile(
    *,
    program_height_mm: float,
    base_z_step_mm: float,
    radius_mm: float,
    cfeed_code_deg_min: float,
    e2_code_mm_s: float,
    e0_code_ma: float,
    voltage_kv: float,
    wire_diameter_mm: float,
    deposition_efficiency: float = 1.0,
    actual_height_mm: float = 0.0,
    outer_diameter_mm: float = 0.0,
    inner_diameter_mm: float = 0.0,
    measured_wall_mm: float = 0.0,
    target_wall_mm: float = 4.0,
    problem_start_height_mm: float = 0.0,
    max_z_offset_mm: float = 0.0,
    feed_override_stable_pct: float = 100.0,
    wire_override_stable_pct: float = 100.0,
    current_override_stable_pct: float = 100.0,
    feed_override_upper_pct: float = 100.0,
    wire_override_upper_pct: float = 100.0,
    current_override_upper_pct: float = 100.0,
    capture_wire_min_mm_s: float = 0.0,
    test_height_mm: float = 20.0,
) -> Dict[str, Any]:
    """Build a transparent zone profile from measured EBAM cylinder experience.

    The profile deliberately does not copy high manual wire override blindly when the measured
    wall is much thicker than target. It keeps the useful operator experience (C/feed/E0 and
    capture minimum) but calculates E2 against the target wall where needed.
    """
    h = max(_safe_float(program_height_mm, 100.0), 1e-6)
    z0 = max(_safe_float(base_z_step_mm, 0.5), 0.05)
    r = max(_safe_float(radius_mm, 39.5), 1e-6)
    c_code = max(_safe_float(cfeed_code_deg_min, 450.0), 0.0)
    e2_code = max(_safe_float(e2_code_mm_s, 0.0), 0.0)
    e0_code = max(_safe_float(e0_code_ma, 0.0), 0.0)
    u = max(_safe_float(voltage_kv, 60.0), 1e-9)
    d = max(_safe_float(wire_diameter_mm, 1.2), 1e-9)
    eta = min(max(_safe_float(deposition_efficiency, 1.0), 1e-6), 1.0)
    target_wall = max(_safe_float(target_wall_mm, 4.0), 0.1)
    actual_wall = _experience_wall_from_od_id(outer_diameter_mm, inner_diameter_mm, measured_wall_mm)
    problem_h = _safe_float(problem_start_height_mm, 0.0)
    if problem_h <= 0 or problem_h >= h:
        problem_h = h * 0.55
    z_offset_abs = abs(_safe_float(max_z_offset_mm, 0.0))
    warnings: List[str] = []
    c_stable = c_code * max(_safe_float(feed_override_stable_pct, 100.0), 0.0) / 100.0
    c_upper = c_code * max(_safe_float(feed_override_upper_pct, 100.0), 0.0) / 100.0
    f_stable = _linear_feed_from_cfeed(c_stable, r)
    f_upper = _linear_feed_from_cfeed(c_upper, r)
    e2_manual_stable = e2_code * max(_safe_float(wire_override_stable_pct, 100.0), 0.0) / 100.0
    e2_manual_upper = e2_code * max(_safe_float(wire_override_upper_pct, 100.0), 0.0) / 100.0
    e0_stable = e0_code * max(_safe_float(current_override_stable_pct, 100.0), 0.0) / 100.0
    e0_upper = e0_code * max(_safe_float(current_override_upper_pct, 100.0), 0.0) / 100.0
    zone_layers = max(1.0, (h - problem_h) / max(z0, 1e-9))
    z_upper = z0
    if z_offset_abs > 0.0 and problem_h < h:
        z_upper = max(0.10, z0 - z_offset_abs / zone_layers)
        if z_upper < z0 * 0.75:
            warnings.append(f"Z offset correction is large: Z-step after {problem_h:.1f} mm becomes {z_upper:.3f} mm. Check with TEST first.")
    e2_target_stable = _wire_for_target_wall(target_wall, z0, f_stable, d, eta)
    # Important: calculate upper-zone E2 with the corrected upper Z-step, otherwise the wall becomes too thick when Z-step is reduced.
    e2_target_upper = _wire_for_target_wall(target_wall, z_upper, f_upper, d, eta)
    capture_min = max(_safe_float(capture_wire_min_mm_s, 0.0), 0.0)
    too_thick = actual_wall > 0 and actual_wall > target_wall * 1.30
    if too_thick:
        warnings.append(
            f"Measured wall {actual_wall:.2f} mm is >130% of target {target_wall:.2f} mm: manual high E2 is not copied blindly."
        )
        e2_stable = max(e2_target_stable, capture_min)
        e2_upper = max(e2_target_upper, capture_min)
        # Start zone may keep a limited capture boost, but not the full high override if it would overfill badly.
        e2_start = min(max(e2_stable * 1.20, capture_min), max(e2_manual_stable, e2_stable))
    else:
        e2_stable = max(e2_manual_stable, capture_min, e2_target_stable * 0.80)
        e2_upper = max(e2_manual_upper, capture_min, e2_target_upper * 0.80)
        e2_start = max(e2_stable, capture_min)
    start_end = min(max(5.0, h * 0.20), problem_h)
    if start_end >= problem_h:
        start_end = max(0.0, problem_h * 0.5)
    zones = [
        {
            "name": "start_capture",
            "z_from_mm": 0.0,
            "z_to_mm": round(start_end, 3),
            "cfeed_deg_min": round(c_stable, 3),
            "linear_feed_mm_min": round(f_stable, 3),
            "wire_mm_s": round(e2_start, 3),
            "current_ma": round(e0_stable, 3),
            "z_step_mm": round(z0, 3),
            "note": "Short capture zone: helps wire enter the pool, but is limited if wall was too thick.",
        },
        {
            "name": "stable_wall",
            "z_from_mm": round(start_end, 3),
            "z_to_mm": round(problem_h, 3),
            "cfeed_deg_min": round(c_stable, 3),
            "linear_feed_mm_min": round(f_stable, 3),
            "wire_mm_s": round(e2_stable, 3),
            "current_ma": round(e0_stable, 3),
            "z_step_mm": round(z0, 3),
            "note": "Main wall zone calculated against target wall thickness.",
        },
        {
            "name": "upper_z_correction",
            "z_from_mm": round(problem_h, 3),
            "z_to_mm": round(h, 3),
            "cfeed_deg_min": round(c_upper, 3),
            "linear_feed_mm_min": round(f_upper, 3),
            "wire_mm_s": round(e2_upper, 3),
            "current_ma": round(e0_upper, 3),
            "z_step_mm": round(z_upper, 3),
            "note": "Upper zone compensates measured Z-offset and later wire-out-of-pool problems.",
        },
    ]
    # Drop empty zones but keep order.
    zones = [z for z in zones if float(z["z_to_mm"]) > float(z["z_from_mm"]) + 1e-9]
    for z in zones:
        qmetal = (math.pi * d * d / 4.0) * max(float(z["wire_mm_s"]), 0.0) * eta
        f = max(float(z["linear_feed_mm_min"]) / 60.0, 1e-9)
        apath = qmetal / f
        wall_est = apath / max(float(z["z_step_mm"]), 1e-9)
        pwr = u * max(float(z["current_ma"]), 0.0)
        z["metal_cross_section_mm2"] = round(apath, 3)
        z["estimated_wall_mm"] = round(wall_est, 3)
        z["energy_j_mm"] = round(pwr / f, 3)
        z["energy_j_mm3"] = round(pwr / max(qmetal, 1e-9), 3) if qmetal > 0 else None
    profile = {
        "profile_version": EXPERIENCE_PROFILE_VERSION,
        "source": {
            "program_height_mm": round(h, 3),
            "base_z_step_mm": round(z0, 3),
            "radius_mm": round(r, 3),
            "cfeed_code_deg_min": round(c_code, 3),
            "e2_code_mm_s": round(e2_code, 3),
            "e0_code_ma": round(e0_code, 3),
            "voltage_kv": round(u, 3),
            "wire_diameter_mm": round(d, 3),
            "deposition_efficiency": round(eta, 6),
        },
        "measurements": {
            "actual_height_mm": round(_safe_float(actual_height_mm, 0.0), 3),
            "outer_diameter_mm": round(_safe_float(outer_diameter_mm, 0.0), 3),
            "inner_diameter_mm": round(_safe_float(inner_diameter_mm, 0.0), 3),
            "measured_wall_mm": round(_safe_float(measured_wall_mm, 0.0), 3),
            "actual_wall_mm": round(actual_wall, 3),
            "target_wall_mm": round(target_wall, 3),
            "problem_start_height_mm": round(problem_h, 3),
            "max_z_offset_mm": round(_safe_float(max_z_offset_mm, 0.0), 3),
        },
        "manual_overrides": {
            "feed_override_stable_pct": round(_safe_float(feed_override_stable_pct, 100.0), 3),
            "wire_override_stable_pct": round(_safe_float(wire_override_stable_pct, 100.0), 3),
            "current_override_stable_pct": round(_safe_float(current_override_stable_pct, 100.0), 3),
            "feed_override_upper_pct": round(_safe_float(feed_override_upper_pct, 100.0), 3),
            "wire_override_upper_pct": round(_safe_float(wire_override_upper_pct, 100.0), 3),
            "current_override_upper_pct": round(_safe_float(current_override_upper_pct, 100.0), 3),
            "capture_wire_min_mm_s": round(capture_min, 3),
        },
        "rules": {
            "do_not_copy_wire_blindly_if_wall_above_target_ratio": 1.30,
            "too_thick_guard_triggered": bool(too_thick),
            "test_height_mm": round(max(1.0, _safe_float(test_height_mm, 20.0)), 3),
            "recommended_first_run": "Generate TEST 20-30 mm and dry-run first. For calibrated comparison, keep external WIRE override at 100%; the program does not disable operator overrides.",
        },
        "zones": zones,
        "warnings": warnings,
    }
    return profile


def experience_profile_to_json(profile: Dict[str, Any]) -> str:
    return json.dumps(profile, indent=2, ensure_ascii=False)


def experience_profile_to_csv(profile: Dict[str, Any]) -> str:
    out = io.StringIO()
    fieldnames = ["name", "z_from_mm", "z_to_mm", "cfeed_deg_min", "linear_feed_mm_min", "wire_mm_s", "current_ma", "z_step_mm", "estimated_wall_mm", "energy_j_mm", "energy_j_mm3", "note"]
    w = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    for zone in profile.get("zones", []) or []:
        w.writerow(zone)
    return out.getvalue()


def _load_experience_profile(settings: ProcessSettings) -> Optional[Dict[str, Any]]:
    if not bool(getattr(settings, "experience_profile_enabled", False)):
        return None
    raw = str(getattr(settings, "experience_profile_json", "") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("zones"), list):
        return None
    return data


def _experience_zone_for_z(profile: Optional[Dict[str, Any]], z_mm: float, height_mm: float) -> Optional[Dict[str, Any]]:
    if not profile:
        return None
    z = float(z_mm)
    zones = profile.get("zones", []) or []
    for zone in zones:
        z0 = _safe_float(zone.get("z_from_mm", 0.0), 0.0)
        z1 = _safe_float(zone.get("z_to_mm", height_mm), height_mm)
        if z >= z0 - 1e-9 and z < z1 - 1e-9:
            return zone
    if zones and z >= _safe_float(zones[-1].get("z_to_mm", height_mm), height_mm) - 1e-9:
        return zones[-1]
    return None


def _experience_z_values(settings: ProcessSettings, height_mm: float, profile: Optional[Dict[str, Any]]) -> List[float]:
    if not profile or not bool(getattr(settings, "experience_profile_apply_z_step", True)):
        n_layers_full = int(math.ceil(float(height_mm) / float(settings.layer_height)))
        return [min(i * float(settings.layer_height), float(height_mm)) for i in range(n_layers_full)]
    z_values: List[float] = []
    z = 0.0
    guard = 0
    while z < float(height_mm) - 1e-9 and guard < 20000:
        z_values.append(round(z, 6))
        zone = _experience_zone_for_z(profile, z, height_mm)
        z_step = _safe_float((zone or {}).get("z_step_mm", settings.layer_height), settings.layer_height)
        z += max(z_step, 0.05)
        guard += 1
    return z_values or [0.0]


def _recompute_layer_after_experience(layer: LayerInfo, settings: ProcessSettings, *, wire_mm_s: Optional[float] = None, current_ma: Optional[float] = None, z_next: Optional[float] = None) -> LayerInfo:
    wire = max(0.0, _safe_float(wire_mm_s, layer.wire_mm_s)) if wire_mm_s is not None else layer.wire_mm_s
    current = max(0.0, _safe_float(current_ma, layer.current_ma)) if current_ma is not None else layer.current_ma
    travel = max(float(layer.feed_mm_min) / 60.0, 1e-9)
    qmetal = settings.wire_area_mm2() * max(wire, 0.0) * float(settings.deposition_efficiency)
    pwr = float(settings.voltage_kv) * current
    e_actual = pwr / travel
    e_vol = pwr / qmetal if qmetal > 0 else float("inf")
    return replace(
        layer,
        wire_mm_s=float(wire),
        current_ma=float(current),
        energy_actual_j_mm=float(e_actual),
        energy_j_mm3=float(e_vol),
        z_next=float(z_next) if z_next is not None else layer.z_next,
    )

def _layer_parameters_with_feed(z: float, zmax: float, settings: ProcessSettings, feed_mm_min: float) -> LayerInfo:
    """Like layer_parameters(), but with a ring-specific linear feed.

    Rotary C mode may have to reduce the linear speed at small radii to stay below
    the configured C-axis angular speed limit. E0 and E2 must then be recalculated
    from the actual linear speed, not from the nominal feed.
    """
    ratio = 0.0 if zmax <= 1e-9 else min(max(float(z) / float(zmax), 0.0), 1.0)
    f = max(0.1, float(feed_mm_min))
    e_target, current_required, current, clipped_by_min, clipped_by_max = _beam_current_and_energy_for_layer(z, zmax, settings, f)
    travel = f / 60.0
    area = settings.wire_area_mm2()
    wire = _wire_for_layer_ratio(settings, f, ratio)
    qmetal = area * wire * float(settings.deposition_efficiency)
    p = settings.voltage_kv * current
    e_actual = p / max(travel, 1e-9)
    e_vol = p / qmetal if qmetal > 0 else float("inf")
    pause = settings.layer_pause_bottom_s + (settings.layer_pause_top_s - settings.layer_pause_bottom_s) * ratio
    return LayerInfo(index=0, z=float(z), z_next=float(z + settings.layer_height), ratio=float(ratio),
                     current_ma=float(current), feed_mm_min=float(f), travel_speed_mm_s=float(travel),
                     wire_mm_s=float(wire), energy_j_mm=float(e_target), energy_actual_j_mm=float(e_actual),
                     current_required_ma=float(current_required), current_clipped_by_min=bool(clipped_by_min),
                     current_clipped_by_max=bool(clipped_by_max), energy_j_mm3=float(e_vol),
                     layer_pause_s=float(pause))


def rotary_c_speed_deg_min(linear_feed_mm_min: float, radius_mm: float) -> float:
    """Convert desired linear deposition speed to rotary C feed in deg/min."""
    r = max(float(radius_mm), 1e-9)
    return 180.0 * float(linear_feed_mm_min) / (math.pi * r)


def _thermal_dwell_for_layer(settings: ProcessSettings, layer_active_s: float) -> float:
    """Adaptive inter-layer thermal dwell: max(min_cycle - t_layer, floor); 0 if
    disabled or the layer already ran longer than the minimum cycle."""
    if not bool(getattr(settings, "thermal_min_layer_cycle_enabled", False)):
        return 0.0
    cyc = max(0.0, float(getattr(settings, "thermal_min_layer_cycle_min", 0.0) or 0.0)) * 60.0
    if cyc <= 0.0 or float(layer_active_s) >= cyc:
        return 0.0
    return max(cyc - float(layer_active_s), max(0.0, float(getattr(settings, "thermal_min_dwell_s", 0.0) or 0.0)))


def _rotary_c_target_linear_mm_s(settings: ProcessSettings) -> float:
    """Target linear deposition speed for constant-velocity radial compensation.

    If the operator set an explicit target, use it. Otherwise derive it from the
    comfortable wire feed E2 and the fill geometry, using the volume balance
    E2 = pitch*z*v/A_wire  ->  v = E2_comfort * A_wire / (pitch*z). This is the
    speed at which the deposited volume matches a stable, in-pool wire feed.
    """
    explicit = float(getattr(settings, "rotary_c_target_linear_mm_s", 0.0) or 0.0)
    if explicit > 1e-6:
        return explicit
    e2_comfort = max(float(getattr(settings, "rotary_c_wire_comfort_mm_s", 29.0) or 29.0), 1e-6)
    area = settings.wire_area_mm2()
    z = max(float(settings.layer_height), 1e-6)
    pitch = max(_effective_rotational_radial_step(settings), 1e-6)
    eff = max(float(settings.deposition_efficiency), 1e-6)
    v = e2_comfort * area * eff / (pitch * z)
    # Clamp to what the C axis can actually deliver across the radii range.
    return max(0.5, min(v, 200.0))


def _rotary_c_ring_kinematics(settings: ProcessSettings, radius_mm: float,
                              nominal_feed_mm_min: float) -> tuple:
    """Return (c_feed_deg_min, actual_linear_feed_mm_min, pitch_factor) for one ring.

    Constant-velocity mode: hold linear speed at the target by lowering C as R
    grows. Where C would exceed its max (small R), clamp to max C (speed rises,
    but energy density is high there so the pool copes). Where C would fall below
    its min (large R), clamp to min C; the linear speed then rises again, so we
    signal a pitch shrink (<1.0) to keep E2 bounded. Legacy mode returns the
    classic behaviour (nominal feed, C capped at max).
    """
    r = max(float(radius_mm), 1e-9)
    c_max = float(settings.rotary_c_max_deg_min)
    c_min_floor = 450.0 if c_max >= 450.0 else max(1.0, 0.25 * c_max)
    if not bool(getattr(settings, "rotary_c_constant_velocity", False)):
        required_c = rotary_c_speed_deg_min(nominal_feed_mm_min, r)
        if required_c > c_max + 1e-9 and bool(getattr(settings, "rotary_c_auto_limit_feed", True)):
            return c_max, c_max * math.pi * r / 180.0, 1.0
        return required_c, nominal_feed_mm_min, 1.0
    # constant-velocity target
    v_target = _rotary_c_target_linear_mm_s(settings)          # mm/s
    f_target = v_target * 60.0                                  # mm/min
    c_needed = rotary_c_speed_deg_min(f_target, r)             # deg/min to hold v_target
    pitch_factor = 1.0
    if c_needed > c_max:
        c_feed = c_max                                         # small R: run at max C
        actual_f = c_feed * math.pi * r / 180.0
    elif c_needed < c_min_floor:
        c_feed = c_min_floor                                   # large R: C floor
        actual_f = c_feed * math.pi * r / 180.0
        if bool(getattr(settings, "rotary_c_shrink_pitch_at_floor", True)):
            v_actual = actual_f / 60.0
            pitch_factor = max(float(getattr(settings, "rotary_c_min_pitch_factor", 0.5) or 0.5),
                               min(1.0, v_target / max(v_actual, 1e-9)))
    else:
        c_feed = c_needed
        actual_f = f_target
    return c_feed, actual_f, pitch_factor


def _rotary_c_pass_gcode(radius_mm: float, layer: LayerInfo, settings: ProcessSettings, ring_no: int, rings_total: int, c_deg_min: float) -> List[str]:
    """Emit one C-table ring: X/Z position, beam/wire ON, relative C360, beam/wire OFF."""
    sign = -1.0 if str(settings.rotary_c_direction).strip().upper().endswith('-') else 1.0
    turn = 360.0 * sign
    x_pos = float(settings.rotary_c_center_x_mm) + float(radius_mm)
    y_pos = float(settings.rotary_c_center_y_mm)
    z_safe = layer.z + settings.z_hop_mm
    wire_soft = layer.wire_mm_s * settings.soft_wire_factor
    lines: List[str] = []
    lines.append(f"(ROTARY_C_RING L{layer.index} RING={ring_no}/{rings_total} R={_fmt(radius_mm,3)} X={_fmt(x_pos,3)} Z={_fmt(layer.z,3)} Cfeed={_fmt(c_deg_min,1)}deg/min linearF={_fmt(layer.feed_mm_min,1)}mm/min)")
    lines.append(f"G0 Z{_fmt(z_safe,3)}")
    lines.append(f"G0 X{_fmt(x_pos,3)} Y{_fmt(y_pos,3)}")
    lines.append(f"G1 Z{_fmt(layer.z,3)} F{_fmt(settings.work_z_feed_mm_min,1)}")
    seam = float(getattr(settings, "rotary_c_seam_scatter_deg", 0.0) or 0.0)
    if seam != 0.0 and bool(getattr(settings, "rotary_c_relative_turns", True)):
        # Advance the ring start angle while beam and wire are OFF so the
        # start/stop seam does not stack vertically on one generatrix
        # (handoff PDF defect: "сильный вертикальный шов"). Relative
        # reposition keeps the cumulative C counter consistent.
        lines.append("G91 (relative seam scatter)")
        lines.append(f"G0 C{_fmt(sign*seam,3)} (seam scatter: start-angle advance, beam OFF)")
        lines.append("G90 (absolute XYZBC)")
    lines.append(f"M68 E0 Q{_fmt(layer.current_ma,3)}")
    if settings.beam_preheat_s > 0:
        lines.append(f"G4 P{_fmt(settings.beam_preheat_s,3)}")
    if settings.use_w_retract:
        lines.extend(_relative_w_move(settings.w_retract_mm, settings.w_retract_feed_mm_min))
    lines.append(f"M68 E2 Q{_fmt(wire_soft,3)}")
    if settings.wire_settle_s > 0:
        lines.append(f"G4 P{_fmt(settings.wire_settle_s,3)}")
    lines.append(f"M68 E2 Q{_fmt(layer.wire_mm_s,3)}")
    if bool(getattr(settings, 'rotary_c_relative_turns', True)):
        lines.append("G91 (relative C turn)")
        lines.append(f"G1 C{_fmt(turn,3)} F{_fmt(c_deg_min,1)}")
        lines.append("G90 (absolute XYZBC)")
    else:
        target_c = float(settings.rotary_c_start_deg) + turn
        lines.append(f"G1 C{_fmt(target_c,3)} F{_fmt(c_deg_min,1)}")
    lines.append(f"M68 E2 Q{_fmt(wire_soft,3)}")
    lines.append("M68 E2 Q0.000")
    if settings.beam_off_pause_s > 0:
        lines.append(f"G4 P{_fmt(settings.beam_off_pause_s,3)}")
    lines.append("M68 E0 Q0.000")
    if settings.use_w_retract:
        lines.extend(_relative_w_move(-settings.w_retract_mm, settings.w_retract_feed_mm_min))
    elif settings.use_m68_speed_retract:
        lines.append(f"M68 E2 Q-{_fmt(settings.speed_retract_mm_s,3)}")
        lines.append(f"G4 P{_fmt(settings.speed_retract_time_s,3)}")
        lines.append("M68 E2 Q0.000")
    lines.append(f"G0 Z{_fmt(z_safe,3)}")
    return lines



def _generate_rotational_shell_rotary_c_no_pause(params: Dict[str, Any], settings: ProcessSettings, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    """Generate one-ring fixed-Z C passes with no beam/wire stop between layers.

    v4.2.9.7 experimental mode for Bormash cylinder tests:
    - each layer is a fixed-Z C360 ring;
    - between layers, C continues by a small transition angle while Z rises by layer_height;
    - E0/E2 are enabled once at start and disabled once at the end;
    - W retract, Z-hop and thermal pauses can be disabled by the UI.
    """
    validate_process_settings(settings, height=float(params.get("height_mm", params.get("height", 80.0))))
    stats = rotational_shell_summary(params)
    height = float(stats["size_z"])
    radial_step = _effective_rotational_radial_step(settings)
    radial_step = max(radial_step, 0.1)
    effective_settings = replace(settings, hatch_spacing=radial_step, deposition_strategy="continuous")
    if bool(getattr(effective_settings, "rotary_c_disable_z_hop", False)):
        effective_settings = replace(effective_settings, z_hop_mm=0.0)
    if bool(getattr(effective_settings, "rotary_c_disable_w_retract", False)):
        effective_settings = replace(effective_settings, use_w_retract=False, w_retract_mm=0.0, use_m68_speed_retract=False)
    if bool(getattr(effective_settings, "rotary_c_disable_layer_pauses", False)):
        effective_settings = replace(effective_settings, layer_pause_bottom_s=0.0, layer_pause_top_s=0.0, beam_preheat_s=0.0, wire_settle_s=0.0, beam_off_pause_s=0.0)
    trace_reasons = {
        "hatch_spacing": "rotational_radial_step",
        "deposition_strategy": "no_pause_requires_continuous",
        "z_hop_mm": "rotary_c_disable_z_hop",
        "use_w_retract": "rotary_c_disable_w_retract",
        "w_retract_mm": "rotary_c_disable_w_retract",
        "use_m68_speed_retract": "rotary_c_disable_w_retract",
        "layer_pause_bottom_s": "rotary_c_disable_layer_pauses",
        "layer_pause_top_s": "rotary_c_disable_layer_pauses",
        "beam_preheat_s": "rotary_c_disable_layer_pauses",
        "wire_settle_s": "rotary_c_disable_layer_pauses",
        "beam_off_pause_s": "rotary_c_disable_layer_pauses",
    }
    requested_effective_changes: Dict[str, Dict[str, Any]] = {}
    for field_name, reason in trace_reasons.items():
        requested_value = getattr(settings, field_name)
        effective_value = getattr(effective_settings, field_name)
        if requested_value != effective_value:
            requested_effective_changes[field_name] = {
                "requested": requested_value,
                "effective": effective_value,
                "reason": reason,
            }
    stats["requested_effective_trace_mode"] = "rotary_c_no_pause"
    stats["requested_effective_change_count"] = len(requested_effective_changes)
    stats["requested_effective_changes"] = requested_effective_changes
    experience_profile = _load_experience_profile(effective_settings)
    z_values_full = _experience_z_values(effective_settings, height, experience_profile)
    n_layers_full = len(z_values_full)
    if effective_settings.max_layers_to_generate and effective_settings.max_layers_to_generate > 0:
        n_layers = max(1, min(int(effective_settings.max_layers_to_generate), n_layers_full))
    else:
        n_layers = n_layers_full
    z_values = z_values_full[:n_layers]
    z_next_values = [z_values_full[i + 1] if i + 1 < len(z_values_full) else height for i in range(n_layers)]
    z_sections = [min(z + max(0.05, (zn - z)) * 0.5, height - 1e-4) for z, zn in zip(z_values, z_next_values)]
    sign = -1.0 if str(effective_settings.rotary_c_direction).strip().upper().endswith('-') else 1.0
    turn = 360.0 * sign
    transition_deg = max(0.0, float(getattr(effective_settings, "rotary_c_transition_angle_deg", 17.0) or 0.0)) * sign
    warnings: List[str] = [
        "Experimental fixed-Z no-pause rotary C mode: E0/E2 remain ON, C does not stop between rings, Z rises during a short C+Z transition sector.",
        "This mode must be dry-run tested on the real Bormash controller because blended C+Z feed interpretation can be controller-specific.",
        "External feed/current/wire overrides are not changed by the generated program; operator adjustment remains available.",
    ]
    path_mode = str(getattr(effective_settings, "path_control_mode", "g64_tolerance")).strip().lower()
    if path_mode in ("g61", "g61_1", "g61.1"):
        warnings.append("WARNING: G61/G61.1 can slow or stop at segment boundaries and conflicts with the no-pause objective.")
    if float(getattr(effective_settings, "g64_naive_cam_q_mm", 0.0)) > 0.0:
        warnings.append("WARNING: G64 Q>0 enables naive-CAM collapsing; verify that short C+Z transitions are not simplified by the controller.")
    if float(getattr(effective_settings, "deposition_efficiency", 1.0)) >= 0.999999:
        warnings.append("CALIBRATION NOTE: deposition efficiency eta=1.0 is an uncalibrated upper bound; measure bead/wall geometry before relying on absolute E2 predictions.")
    if transition_deg == 0.0 and n_layers > 1:
        warnings.append("WARNING: transition angle is 0; Z transition may force C stop. Use 10...30 deg for no-pause C motion.")
    if experience_profile:
        warnings.append("Experience profile is enabled: Cfeed/E2/E0/Z-step may be zone-corrected from measured results.")
        for msg in experience_profile.get("warnings", []) or []:
            warnings.append("EXPERIENCE WARNING: " + str(msg))
        if not bool(getattr(effective_settings, "experience_profile_update_m68_at_zone_boundaries", False)) and (bool(getattr(effective_settings, "experience_profile_apply_wire", True)) or bool(getattr(effective_settings, "experience_profile_apply_current", False))):
            warnings.append("EXPERIENCE NOTE: zone E0/E2 values are averaged unless boundary updates are enabled; synchronized M67 is available only after HAL confirmation.")
    lines = _gcode_header(effective_settings, stats)
    lines.append(f"(REQUESTED_EFFECTIVE_TRACE: changed_fields={len(requested_effective_changes)}; see audit/stats)")
    lines.append(f"(ROTATIONAL_SPECIAL_PATH: rotary_c_fixed_z_no_pause; radial_step={_fmt(radial_step,3)} mm; one C ring per layer; continuous E0/E2)")
    z_mode_note = "zone Z-step from experience profile" if experience_profile and bool(getattr(effective_settings, "experience_profile_apply_z_step", True)) else f"Z+{_fmt(effective_settings.layer_height,3)}"
    lines.append(f"(NO_PAUSE_MODE: C360 at fixed Z, then C{_fmt(transition_deg,3)} with {z_mode_note}; no E0/E2 off inside process)")
    if experience_profile:
        lines.append(f"(EXPERIENCE_PROFILE: enabled; zones={len(experience_profile.get('zones', []) or [])}; apply_C={bool(getattr(effective_settings, 'experience_profile_apply_cfeed', True))}; apply_E2={bool(getattr(effective_settings, 'experience_profile_apply_wire', True))}; apply_E0={bool(getattr(effective_settings, 'experience_profile_apply_current', False))}; apply_Z={bool(getattr(effective_settings, 'experience_profile_apply_z_step', True))})")
    lines.append(f"(ROTARY_C_LIMITS: max_C_feed={_fmt(effective_settings.rotary_c_max_deg_min,1)} deg/min; min_radius_warning={_fmt(effective_settings.rotary_c_min_radius_mm,3)} mm)")
    lines.append(f"(WIRE_FEED_MODE: {getattr(effective_settings, 'wire_feed_mode', 'auto')}; manual={_fmt(getattr(effective_settings, 'wire_feed_manual_mm_s', 0.0),3)} bottom/top={_fmt(getattr(effective_settings, 'wire_feed_bottom_mm_s', 0.0),3)}/{_fmt(getattr(effective_settings, 'wire_feed_top_mm_s', 0.0),3)} mm/s)")
    lines.append(f"G0 B{_fmt(effective_settings.rotary_c_b_angle_deg,3)}")
    lines.append(f"G0 C{_fmt(effective_settings.rotary_c_start_deg,3)}")
    layer_infos: List[LayerInfo] = []
    radii_used: List[float] = []
    cfeeds_used: List[float] = []
    feed_limited_count = 0
    thermal_dwell_count = 0
    thermal_dwell_total_s = 0.0
    max_c_required = 0.0
    max_c_used = 0.0
    total_active_len = 0.0
    for idx, (z, zsec, z_next) in enumerate(zip(z_values, z_sections, z_next_values), start=1):
        if progress_callback and ((idx == 1) or (idx == n_layers) or (idx % max(1, int(effective_settings.progress_update_every_layers)) == 0)):
            try:
                progress_callback(idx, n_layers, "rotary_c_no_pause")
            except Exception:
                pass
        radii = rotational_layer_radii_at_z(zsec, params, effective_settings)
        if not radii:
            warnings.append(f"Layer {idx}: no C-radius at Z={zsec:.3f}; skipped")
            continue
        # No-pause mode is one wall line. If the section produces several rings, choose the center radius
        # of the generated ring band and warn. For one-ring thin-wall/cylinder settings this is exactly that ring.
        if len(radii) > 1:
            radius = (min(radii) + max(radii)) * 0.5
            if idx == 1:
                warnings.append(f"WARNING: no-pause mode uses one centerline ring, but section has {len(radii)} radii; increase radial step or reduce wall thickness for a true one-ring path.")
        else:
            radius = float(radii[0])
        zone = _experience_zone_for_z(experience_profile, z, height)
        zone_name = str((zone or {}).get("name", ""))
        base = layer_parameters(z, height, effective_settings)
        if zone and bool(getattr(effective_settings, "experience_profile_apply_cfeed", True)) and _safe_float(zone.get("cfeed_deg_min", 0.0), 0.0) > 0:
            c_feed = _safe_float(zone.get("cfeed_deg_min", 0.0), 0.0)
            actual_feed = _linear_feed_from_cfeed(c_feed, radius)
            required_c = c_feed
            pitch_factor = 1.0
        else:
            required_c = rotary_c_speed_deg_min(base.feed_mm_min, radius)
            # v4.2.9.31: radial feed-rate compensation (constant-velocity mode).
            c_feed, actual_feed, pitch_factor = _rotary_c_ring_kinematics(
                effective_settings, radius, base.feed_mm_min)
        max_c_required = max(max_c_required, required_c)
        if (not bool(getattr(effective_settings, "rotary_c_constant_velocity", False))
                and required_c > float(effective_settings.rotary_c_max_deg_min) + 1e-9):
            if bool(getattr(effective_settings, "rotary_c_auto_limit_feed", True)):
                # kinematics already capped c_feed/actual_feed to the C max; just count it.
                feed_limited_count += 1
            else:
                warnings.append(f"WARNING: layer {idx} radius {radius:.3f} requires C feed {required_c:.1f} deg/min above limit {effective_settings.rotary_c_max_deg_min:.1f}")
        layer = _layer_parameters_with_feed(z, height, effective_settings, actual_feed)
        if pitch_factor < 0.999:
            layer.wire_mm_s = float(layer.wire_mm_s) * pitch_factor
        wire_override = None
        current_override = None
        if zone and bool(getattr(effective_settings, "experience_profile_apply_wire", True)) and _safe_float(zone.get("wire_mm_s", -1.0), -1.0) >= 0:
            wire_override = _safe_float(zone.get("wire_mm_s", layer.wire_mm_s), layer.wire_mm_s)
        if zone and bool(getattr(effective_settings, "experience_profile_apply_current", False)) and _safe_float(zone.get("current_ma", -1.0), -1.0) >= 0:
            current_override = _safe_float(zone.get("current_ma", layer.current_ma), layer.current_ma)
        layer = _recompute_layer_after_experience(layer, effective_settings, wire_mm_s=wire_override, current_ma=current_override, z_next=z_next)
        layer.index = idx
        layer.segments_count = 1
        # Include transition sector in the layer length except after the last generated ring.
        ring_len = 2.0 * math.pi * float(radius)
        trans_len = (abs(transition_deg) / 360.0) * ring_len if idx < n_layers else 0.0
        layer.path_length_mm = ring_len + trans_len
        layer.contour_length_mm = 0.0
        layer_infos.append(layer)
        radii_used.append(float(radius))
        cfeeds_used.append(float(c_feed))
        max_c_used = max(max_c_used, float(c_feed))
        total_active_len += layer.path_length_mm
    if not layer_infos:
        raise RuntimeError("No no-pause rotary C layers were generated. Check vessel geometry and height.")

    # Industrial safety guard: fixed-X no-pause rings cannot reproduce a bowl/balloon
    # whose radius changes with Z. Earlier versions only wrote a warning and then kept
    # X fixed, which silently produced the wrong geometry.
    radius_span = max(radii_used) - min(radii_used)
    radius_tol = max(0.0, float(getattr(effective_settings, "rotary_c_radius_variation_tolerance_mm", 0.05)))
    if radius_span > radius_tol + 1e-9:
        raise RuntimeError(
            f"No-pause fixed-X C mode is valid only for a near-constant-radius wall: "
            f"radius span is {radius_span:.3f} mm, tolerance is {radius_tol:.3f} mm. "
            "Use separate C rings with X repositioning, or generate a true synchronized X+C+Z path after machine validation."
        )

    current_q = sum(li.current_ma for li in layer_infos) / max(len(layer_infos), 1)
    wire_q = sum(li.wire_mm_s for li in layer_infos) / max(len(layer_infos), 1)
    zone_updates = bool(
        experience_profile
        and getattr(effective_settings, "experience_profile_update_m68_at_zone_boundaries", False)
        and (getattr(effective_settings, "experience_profile_apply_wire", True) or getattr(effective_settings, "experience_profile_apply_current", False))
    )
    analog_code = _deposition_analog_code(effective_settings)
    if str(getattr(effective_settings, "analog_output_mode", "m68_compatible")).lower() == "m67_synchronized" and analog_code != "M67":
        warnings.append("M67 was requested but machine/HAL confirmation is off; generator safely fell back to M68.")
    if zone_updates and analog_code == "M68":
        warnings.append("Zone E0/E2 updates use M68: LinuxCNC documents that M68 is immediate and breaks blending. Dry-run the zone boundaries.")
    if zone_updates and analog_code == "M67":
        warnings.append("Zone E0/E2 updates use synchronized M67 and are applied at the start of the next ring motion.")

    first_r = radii_used[0]
    x_pos = float(effective_settings.rotary_c_center_x_mm) + float(first_r)
    y_pos = float(effective_settings.rotary_c_center_y_mm)
    lines.append(f"G0 X{_fmt(x_pos,3)} Y{_fmt(y_pos,3)}")
    lines.append(f"G1 Z{_fmt(layer_infos[0].z,3)} F{_fmt(effective_settings.work_z_feed_mm_min,1)}")
    lines.append(f"(ANALOG_OUTPUT_MODE: requested={getattr(effective_settings, 'analog_output_mode', 'm68_compatible')}; effective={analog_code}; HAL_M67_confirmed={bool(getattr(effective_settings, 'machine_m67_confirmed', False))})")

    last_output_pair = None
    if not zone_updates:
        # Keep the one-time start commands outside the continuous G91 motion block.
        # With M67 they remain queued until the first following G1 motion.
        lines.append(_analog_setpoint_line(effective_settings, 0, current_q, "beam current ON once; representative profile value"))
        lines.append(_analog_setpoint_line(effective_settings, 2, wire_q, "wire feed ON once; no off between rings"))
        last_output_pair = (round(current_q, 9), round(wire_q, 9))

    lines.append("G91 (continuous relative C/Z block sequence)")
    for i, layer in enumerate(layer_infos):
        radius = radii_used[i]
        c_feed = cfeeds_used[i]
        x_here = float(effective_settings.rotary_c_center_x_mm) + float(radius)
        if i == 0:
            lines.append(f"(START_RADIUS R={_fmt(radius,3)} X={_fmt(x_here,3)} Z={_fmt(layer.z,3)})")
        zone = _experience_zone_for_z(experience_profile, layer.z, height)
        zone_txt = f" zone={zone.get('name')}" if zone else ""

        commanded_current = layer.current_ma if zone_updates and bool(getattr(effective_settings, "experience_profile_apply_current", False)) else current_q
        commanded_wire = layer.wire_mm_s if zone_updates and bool(getattr(effective_settings, "experience_profile_apply_wire", True)) else wire_q
        output_pair = (round(commanded_current, 9), round(commanded_wire, 9))
        command_changed = (i == 0 and not zone_updates) or (zone_updates and output_pair != last_output_pair)
        if zone_updates and output_pair != last_output_pair:
            lines.append(_analog_setpoint_line(effective_settings, 0, commanded_current, f"zone setpoint before ring {layer.index}"))
            lines.append(_analog_setpoint_line(effective_settings, 2, commanded_wire, f"zone setpoint before ring {layer.index}"))
            last_output_pair = output_pair
        layer.commanded_e0_ma = float(commanded_current)
        layer.commanded_e2_mm_s = float(commanded_wire)
        layer.analog_command_mode = f"{analog_code.lower()}_{'zone' if zone_updates else 'once_average'}"
        layer.analog_command_update = bool(command_changed)

        lines.append(f"(FIXED_Z_NO_PAUSE_RING L{layer.index}/{len(layer_infos)} Z={_fmt(layer.z,3)} R={_fmt(radius,3)} linearF={_fmt(layer.feed_mm_min,1)}mm/min Cfeed={_fmt(c_feed,1)}deg/min E0cmd={_fmt(commanded_current,3)}mA E2cmd={_fmt(commanded_wire,3)}mm/s{zone_txt})")
        lines.append(f"G1 C{_fmt(turn,3)} F{_fmt(c_feed,1)}")
        if i < len(layer_infos) - 1:
            dz = max(0.0, float(layer_infos[i+1].z) - float(layer.z))
            _np_tdw = _thermal_dwell_for_layer(effective_settings, abs(float(turn)) / max(float(c_feed), 1e-9) * 60.0)
            if _np_tdw > 0:
                lines.append(_analog_setpoint_line(effective_settings, 2, 0.0, "THERMAL_DWELL: wire off before dwell"))
                lines.append(_analog_setpoint_line(effective_settings, 0, 0.0, "THERMAL_DWELL: beam off before dwell"))
                lines.append(f"G4 P{_fmt(_np_tdw,1)} (THERMAL_DWELL L{layer.index}: adaptive - one restrike per dwell)")
                lines.append(_analog_setpoint_line(effective_settings, 0, float(commanded_current), "re-arm beam after thermal dwell"))
                lines.append(_analog_setpoint_line(effective_settings, 2, float(commanded_wire), "re-arm wire after thermal dwell"))
                thermal_dwell_count += 1
                thermal_dwell_total_s += _np_tdw
            lines.append(f"G1 C{_fmt(transition_deg,3)} Z{_fmt(dz,3)} F{_fmt(c_feed,1)} (no-pause C/Z transition to next layer)")
    lines.append("G90 (absolute XYZBC)")
    # Immediate final OFF is intentional even when M67 is used for deposition: M67
    # would not take effect without a following motion command.
    lines.append("M68 E2 Q0.000 (wire feed OFF at final end only)")
    lines.append("M68 E0 Q0.000 (beam current OFF at final end only)")
    footer_settings = replace(effective_settings, safe_z_final_mm=max(effective_settings.safe_z_final_mm, height + 5.0))
    lines.extend(_gcode_footer(footer_settings))
    gcode = "\n".join(lines) + "\n"
    audit = audit_gcode(gcode, effective_settings)
    if thermal_dwell_count:
        warnings.append(f"ТЕПЛОВЫЕ ВЫДЕРЖКИ: {thermal_dwell_count} шт, суммарно {thermal_dwell_total_s/60.0:.1f} мин "
                        f"(мин. цикл слоя {float(getattr(effective_settings, 'thermal_min_layer_cycle_min', 0.0)):.1f} мин, пол {float(getattr(effective_settings, 'thermal_min_dwell_s', 0.0)):.0f} с); время включено в G-code (G4).")
    if feed_limited_count:
        warnings.append(f"WARNING: {feed_limited_count} no-pause C rings were feed-limited by C max; E2 was recalculated from actual F unless manual wire mode is enabled.")
    qmetal_values = [effective_settings.wire_area_mm2() * li.wire_mm_s * float(effective_settings.deposition_efficiency) for li in layer_infos]
    bead_cross_sections = [q / max(li.travel_speed_mm_s, 1e-9) for q, li in zip(qmetal_values, layer_infos)]
    wall_est = [cs / max(float(li.z_next - li.z), 1e-9) for cs, li in zip(bead_cross_sections, layer_infos)]
    stats.update({
        "app_version": APP_VERSION,
        "rotational_path_strategy": "rotary_c_rings",
        "rotary_c_mode": "fixed_z_no_pause_cz_transition",
        "rotary_c_motion_mode": "no_pause_flat_rings",
        "rotary_c_transition_angle_deg": abs(float(transition_deg)),
        "rotary_c_center_x_mm": float(effective_settings.rotary_c_center_x_mm),
        "rotary_c_center_y_mm": float(effective_settings.rotary_c_center_y_mm),
        "rotary_c_direction": str(effective_settings.rotary_c_direction),
        "rotary_c_max_deg_min": float(effective_settings.rotary_c_max_deg_min),
        "rotary_c_feed_limited_count": int(feed_limited_count),
        "rotary_c_required_max_deg_min": float(max_c_required),
        "rotary_c_used_max_deg_min": float(max_c_used),
        "rotary_c_min_radius_used_mm": min(radii_used),
        "rotary_c_max_radius_used_mm": max(radii_used),
        "layers_total": len(layer_infos),
        "passes_total": len(layer_infos),
        "rings_total": len(layer_infos),
        "layers_requested": n_layers,
        "layers_full_model": n_layers_full,
        "is_test_truncated": bool(n_layers < n_layers_full),
        "segments_total": len(layer_infos),
        "contour_segments_total": 0,
        "active_path_length_mm": total_active_len,
        "active_path_length_m": total_active_len / 1000.0,
        "estimated_active_time_s": sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos),
        "estimated_wire_length_mm": sum((li.path_length_mm / max(li.travel_speed_mm_s, 1e-9)) * li.wire_mm_s for li in layer_infos),
        "wire_feed_mode": str(getattr(effective_settings, "wire_feed_mode", "auto")),
        "wire_min_calculated_mm_s": min((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_max_calculated_mm_s": max((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_commanded_once_mm_s": float(wire_q),
        "zone_output_updates_enabled": bool(zone_updates),
        "analog_output_mode_requested": str(getattr(effective_settings, "analog_output_mode", "m68_compatible")),
        "analog_output_code_effective": str(analog_code),
        "machine_m67_confirmed": bool(getattr(effective_settings, "machine_m67_confirmed", False)),
        "path_control_mode": str(getattr(effective_settings, "path_control_mode", "g64_tolerance")),
        "g64_tolerance_mm": float(getattr(effective_settings, "g64_tolerance_mm", 0.08)),
        "g64_naive_cam_q_mm": float(getattr(effective_settings, "g64_naive_cam_q_mm", 0.0)),
        "deposition_efficiency": float(getattr(effective_settings, "deposition_efficiency", 1.0)),
        "radius_span_mm": float(radius_span),
        "radius_variation_tolerance_mm": float(radius_tol),
        "feed_min_mm_min": min((li.feed_mm_min for li in layer_infos), default=0.0),
        "feed_max_mm_min": max((li.feed_mm_min for li in layer_infos), default=0.0),
        "current_commanded_once_ma": float(current_q),
        "current_required_min_ma": min((li.current_required_ma for li in layer_infos), default=0.0),
        "current_required_max_ma": max((li.current_required_ma for li in layer_infos), default=0.0),
        "current_limit_ma": effective_settings.current_max_ma,
        "beam_current_mode": str(getattr(effective_settings, "beam_current_mode", "energy")),
        "energy_actual_min_j_mm": min((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_actual_max_j_mm": max((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_volume_min_j_mm3": min((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "energy_volume_max_j_mm3": max((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "bead_cross_section_min_mm2": min(bead_cross_sections, default=0.0),
        "bead_cross_section_max_mm2": max(bead_cross_sections, default=0.0),
        "estimated_wall_thickness_min_mm": min(wall_est, default=0.0),
        "estimated_wall_thickness_max_mm": max(wall_est, default=0.0),
        "experience_profile_enabled": bool(experience_profile is not None),
        "experience_profile_zones": [str(z.get("name", "zone")) for z in (experience_profile or {}).get("zones", [])] if experience_profile else [],
        "experience_profile_apply_cfeed": bool(getattr(effective_settings, "experience_profile_apply_cfeed", True)),
        "experience_profile_apply_wire": bool(getattr(effective_settings, "experience_profile_apply_wire", True)),
        "experience_profile_apply_current": bool(getattr(effective_settings, "experience_profile_apply_current", False)),
        "experience_profile_apply_z_step": bool(getattr(effective_settings, "experience_profile_apply_z_step", True)),
        "safe_initial_approach_enabled": bool(getattr(effective_settings, "safe_initial_approach_enabled", False)),
        "safe_initial_approach_z_mm": float(getattr(effective_settings, "safe_initial_approach_z_mm", 7.0)),
        "initial_positioning_z_mm": float(
            getattr(effective_settings, "safe_initial_approach_z_mm", 7.0)
            if bool(getattr(effective_settings, "safe_initial_approach_enabled", False))
            else effective_settings.z_hop_mm
        ),
        "z_step_min_mm": min((li.z_next - li.z for li in layer_infos), default=float(effective_settings.layer_height)),
        "z_step_max_mm": max((li.z_next - li.z for li in layer_infos), default=float(effective_settings.layer_height)),
        "process_wire_warning_limit_mm_s": effective_settings.wire_max_mm_s,
        "wire_above_control_limit": any(li.wire_mm_s > effective_settings.wire_max_mm_s for li in layer_infos),
        "gcode_lines": len(lines),
        "gcode_size_mb": len(gcode.encode("utf-8")) / (1024.0 * 1024.0),
        "deposition_strategy": "rotary_c_fixed_z_no_pause",
        "hatch_spacing_effective_mm": radial_step,
    })
    stats["estimated_active_time_h"] = stats["estimated_active_time_s"] / 3600.0
    stats["estimated_pause_time_s"] = 0.0
    stats["estimated_aux_time_s"] = 0.0
    stats["estimated_service_time_s"] = 0.0
    stats["estimated_total_time_s"] = stats["estimated_active_time_s"] * 1.03
    stats["estimated_total_time_h"] = stats["estimated_total_time_s"] / 3600.0
    report = audit_report(effective_settings, stats, audit, warnings, layer_infos)
    return GenerationResult(gcode=gcode, layer_csv=layer_table_csv(layer_infos), audit_text=report, stats=stats)


def _generate_rotational_shell_rotary_c(params: Dict[str, Any], settings: ProcessSettings, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    """Generate rotary-table C-axis rings for rotational vessels/balloons.

    First safe implementation: M429/identity, B fixed (default B=0), X positions the
    beam at radius, Z steps layers, and G91 C360 performs each circular pass.
    """
    if str(getattr(settings, "rotary_c_motion_mode", "separate_rings")).strip().lower() == "no_pause_flat_rings":
        return _generate_rotational_shell_rotary_c_no_pause(params, settings, progress_callback=progress_callback)
    validate_process_settings(settings, height=float(params.get("height_mm", params.get("height", 80.0))))
    stats = rotational_shell_summary(params)
    height = float(stats["size_z"])
    radial_step = _effective_rotational_radial_step(settings)
    radial_step = max(radial_step, 0.1)
    effective_settings = replace(settings, hatch_spacing=radial_step, deposition_strategy="continuous")
    n_layers_full = int(math.ceil(height / float(effective_settings.layer_height)))
    if effective_settings.max_layers_to_generate and effective_settings.max_layers_to_generate > 0:
        n_layers = max(1, min(int(effective_settings.max_layers_to_generate), n_layers_full))
    else:
        n_layers = n_layers_full
    z_values = [min(i * effective_settings.layer_height, height) for i in range(n_layers)]
    z_sections = [min(z + effective_settings.layer_height * 0.5, height - 1e-4) for z in z_values]
    lines = _gcode_header(effective_settings, stats)
    lines.append(f"(ROTATIONAL_SPECIAL_PATH: rotary_c_rings; radial_step={_fmt(radial_step,3)} mm; B={_fmt(effective_settings.rotary_c_b_angle_deg,3)} deg; C-axis table rings)")
    lines.append("(RING_ORDER: inner_to_outer; first pass uses inner/small radius and subsequent passes grow outward)")
    lines.append("(REAL_C_RADIUS_LIMIT_MODE: M429 identity; X = C_center_X + radius; Z = layer height; C uses relative 360 degree turns)")
    # Pre-calculate radii for the information comment. Per-ring safety limiting below remains authoritative.
    try:
        _all_radii_for_comment = [float(r) for _zsec in z_sections for r in rotational_layer_radii_at_z(_zsec, params, effective_settings)]
        _real_rmin_comment = min(_all_radii_for_comment) if _all_radii_for_comment else 0.0
    except Exception:
        _real_rmin_comment = 0.0
    lines.append(f"(ROTARY_C_LIMITS: max_C_feed={_fmt(effective_settings.rotary_c_max_deg_min,1)} deg/min; min_radius_warning={_fmt(effective_settings.rotary_c_min_radius_mm,3)} mm)")
    if _real_rmin_comment > 0:
        lines.append(f"(ROTARY_C_REAL_RADIUS_LIMIT: real_Rmin={_fmt(_real_rmin_comment,3)} warning_below={_fmt(effective_settings.rotary_c_min_radius_mm,3)} source=real_toolpath_radius; warning radius does not clamp F)")
    lines.append(f"G0 B{_fmt(effective_settings.rotary_c_b_angle_deg,3)}")
    lines.append(f"G0 C{_fmt(effective_settings.rotary_c_start_deg,3)}")
    if effective_settings.use_w_retract and effective_settings.w_retract_mm > 0:
        lines.append("(Initial W retract calibration)")
        lines.append("G10 L20 P0 W0")
        lines.extend(_relative_w_move(-effective_settings.w_retract_mm, effective_settings.w_retract_feed_mm_min))
    layer_infos: List[LayerInfo] = []
    warnings: List[str] = [
        "Rotary C-table strategy: B fixed, C performs the circular deposition, X sets radius, Z sets layer.",
        "This is experimental for the real Bormash setup: verify C sign, G91 C360 behaviour, C center and speed limits by dry run first.",
    ]
    if abs(float(effective_settings.rotary_c_b_angle_deg)) > 1e-6:
        warnings.append("WARNING: rotary C mode is designed for first tests with B=0. Non-zero B requires TCP/center calibration and collision checks.")
    max_c_required = 0.0
    max_c_used = 0.0
    min_radius_used = float("inf")
    max_radius_used = 0.0
    feed_limited_count = 0
    thermal_dwell_count = 0
    thermal_dwell_total_s = 0.0
    small_radius_count = 0
    total_active_len = 0.0
    total_rings = 0
    max_rings_layer = 0
    for idx, (z, zsec) in enumerate(zip(z_values, z_sections), start=1):
        if progress_callback and ((idx == 1) or (idx == n_layers) or (idx % max(1, int(effective_settings.progress_update_every_layers)) == 0)):
            try:
                progress_callback(idx, n_layers, "rotary_c")
            except Exception:
                pass
        radii = rotational_layer_radii_at_z(zsec, params, effective_settings)
        if not radii:
            warnings.append(f"Layer {idx}: no C-radii at Z={zsec:.3f}; skipped")
            continue
        max_rings_layer = max(max_rings_layer, len(radii))
        lines.append(f"(--- LAYER {idx}/{n_layers} Z={_fmt(z,3)} ROTARY_C rings={len(radii)} ---)")
        for j, radius in enumerate(radii, start=1):
            base = layer_parameters(z, height, effective_settings)
            required_c = rotary_c_speed_deg_min(base.feed_mm_min, radius)
            max_c_required = max(max_c_required, required_c)
            # v4.2.9.31: radial feed-rate compensation (constant-velocity mode).
            c_feed, actual_feed, pitch_factor = _rotary_c_ring_kinematics(
                effective_settings, radius, base.feed_mm_min)
            if radius < float(effective_settings.rotary_c_min_radius_mm):
                small_radius_count += 1
            if (not bool(getattr(effective_settings, "rotary_c_constant_velocity", False))
                    and required_c > float(effective_settings.rotary_c_max_deg_min) + 1e-9):
                if bool(getattr(effective_settings, "rotary_c_auto_limit_feed", True)):
                    feed_limited_count += 1
                else:
                    warnings.append(f"WARNING: layer {idx} radius {radius:.3f} requires C feed {required_c:.1f} deg/min above limit {effective_settings.rotary_c_max_deg_min:.1f}")
            layer = _layer_parameters_with_feed(z, height, effective_settings, actual_feed)
            if pitch_factor < 0.999:
                layer.wire_mm_s = float(layer.wire_mm_s) * pitch_factor
                _area_pf = effective_settings.wire_area_mm2()
                _qm = _area_pf * layer.wire_mm_s * float(effective_settings.deposition_efficiency)
                layer.energy_j_mm3 = float(effective_settings.voltage_kv * layer.current_ma / _qm) if _qm > 0 else float("inf")
            layer.index = idx
            layer.segments_count = 1
            layer.contour_segments_count = 0
            layer.path_length_mm = 2.0 * math.pi * float(radius)
            layer.contour_length_mm = 0.0
            lines.extend(_rotary_c_pass_gcode(radius, layer, effective_settings, j, len(radii), c_feed))
            layer_infos.append(layer)
            total_active_len += layer.path_length_mm
            total_rings += 1
            min_radius_used = min(min_radius_used, float(radius))
            max_radius_used = max(max_radius_used, float(radius))
            max_c_used = max(max_c_used, c_feed)
        if layer_infos and layer_infos[-1].layer_pause_s > 0:
            lines.append(f"G4 P{_fmt(layer_infos[-1].layer_pause_s,3)} (layer thermal stabilization)")
        _tdw = _thermal_dwell_for_layer(effective_settings, sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos[-len(radii):]))
        if _tdw > 0:
            lines.append(f"G4 P{_fmt(_tdw,1)} (THERMAL_DWELL L{idx}: adaptive - layer cycle below minimum)")
            thermal_dwell_count += 1
            thermal_dwell_total_s += _tdw
    if not layer_infos:
        raise RuntimeError("No rotary C layers were generated. Check vessel geometry, radial step and height.")
    if thermal_dwell_count:
        warnings.append(f"ТЕПЛОВЫЕ ВЫДЕРЖКИ: {thermal_dwell_count} шт, суммарно {thermal_dwell_total_s/60.0:.1f} мин "
                        f"(мин. цикл слоя {float(getattr(effective_settings, 'thermal_min_layer_cycle_min', 0.0)):.1f} мин, пол {float(getattr(effective_settings, 'thermal_min_dwell_s', 0.0)):.0f} с); время включено в G-code (G4).")
    if feed_limited_count:
        warnings.append(f"WARNING: {feed_limited_count} rotary C passes were feed-limited by C max speed; E0/E2 were recalculated for the reduced linear speed.")
    if small_radius_count:
        warnings.append(f"WARNING: {small_radius_count} rotary C passes use radius below configured warning radius {effective_settings.rotary_c_min_radius_mm:.3f} mm.")
    footer_settings = replace(effective_settings, safe_z_final_mm=max(effective_settings.safe_z_final_mm, height + effective_settings.z_hop_mm + 5.0))
    lines.extend(_gcode_footer(footer_settings))
    gcode = "\n".join(lines) + "\n"
    audit = audit_gcode(gcode, effective_settings)
    stats.update({
        "app_version": APP_VERSION,
        "rotational_path_strategy": "rotary_c_rings",
        "rotary_c_mode": "B_fixed_C_axis_rings",
        "rotary_c_center_x_mm": float(effective_settings.rotary_c_center_x_mm),
        "rotary_c_center_y_mm": float(effective_settings.rotary_c_center_y_mm),
        "rotary_c_direction": str(effective_settings.rotary_c_direction),
        "rotary_c_start_deg": float(effective_settings.rotary_c_start_deg),
        "rotary_c_b_angle_deg": float(effective_settings.rotary_c_b_angle_deg),
        "rotary_c_max_deg_min": float(effective_settings.rotary_c_max_deg_min),
        "rotary_c_min_radius_mm": float(effective_settings.rotary_c_min_radius_mm),
        "rotary_c_feed_limited_count": int(feed_limited_count),
        "rotary_c_small_radius_count": int(small_radius_count),
        "rotary_c_required_max_deg_min": float(max_c_required),
        "rotary_c_used_max_deg_min": float(max_c_used),
        "rotary_c_min_radius_used_mm": 0.0 if min_radius_used == float("inf") else float(min_radius_used),
        "rotary_c_max_radius_used_mm": float(max_radius_used),
        "rotary_c_warning_radius_below_mm": float(effective_settings.rotary_c_min_radius_mm),
        "rotary_c_real_radius_limit_note": "C feed limiting uses each real ring radius; warning radius is only a warning threshold",
        "min_x": float(effective_settings.rotary_c_center_x_mm) + (0.0 if min_radius_used == float("inf") else float(min_radius_used)),
        "max_x": float(effective_settings.rotary_c_center_x_mm) + float(max_radius_used),
        "min_y": float(effective_settings.rotary_c_center_y_mm),
        "max_y": float(effective_settings.rotary_c_center_y_mm),
        "rotational_radial_step_mm": radial_step,
        "layers_total": len(set(li.index for li in layer_infos)),
        "passes_total": len(layer_infos),
        "rings_total": int(total_rings),
        "layers_requested": n_layers,
        "layers_full_model": n_layers_full,
        "is_test_truncated": bool(n_layers < n_layers_full),
        "segments_total": int(total_rings),
        "contour_segments_total": 0,
        "contour_path_length_mm": 0.0,
        "max_segments_per_layer": max_rings_layer,
        "active_path_length_mm": total_active_len,
        "active_path_length_m": total_active_len / 1000.0,
        "estimated_active_time_s": sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos),
        "estimated_wire_length_mm": sum((li.path_length_mm / max(li.travel_speed_mm_s, 1e-9)) * li.wire_mm_s for li in layer_infos),
        "wire_min_calculated_mm_s": min((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_max_calculated_mm_s": max((li.wire_mm_s for li in layer_infos), default=0.0),
        "feed_min_mm_min": min((li.feed_mm_min for li in layer_infos), default=0.0),
        "feed_max_mm_min": max((li.feed_mm_min for li in layer_infos), default=0.0),
        "process_wire_warning_limit_mm_s": effective_settings.wire_max_mm_s,
        "wire_above_control_limit": any(li.wire_mm_s > effective_settings.wire_max_mm_s for li in layer_infos),
        "current_min_ma": effective_settings.current_min_ma,
        "current_low_warning_ma": effective_settings.current_low_warning_ma,
        "current_limit_ma": effective_settings.current_max_ma,
        "beam_current_mode": str(getattr(effective_settings, "beam_current_mode", "energy")),
        "beam_current_bottom_ma": float(getattr(effective_settings, "beam_current_bottom_ma", 0.0)),
        "beam_current_top_ma": float(getattr(effective_settings, "beam_current_top_ma", 0.0)),
        "current_required_min_ma": min((li.current_required_ma for li in layer_infos), default=0.0),
        "current_required_max_ma": max((li.current_required_ma for li in layer_infos), default=0.0),
        "current_clipped_by_min": any(li.current_clipped_by_min for li in layer_infos),
        "current_clipped_by_max": any(li.current_clipped_by_max for li in layer_infos),
        "current_clipped_by_limit": any(li.current_clipped_by_max for li in layer_infos),
        "current_below_low_warning": any((li.current_required_ma < effective_settings.current_low_warning_ma - 1e-9) for li in layer_infos) if effective_settings.current_low_warning_ma > 0 else False,
        "energy_target_min_j_mm": min((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_target_max_j_mm": max((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_actual_min_j_mm": min((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_actual_max_j_mm": max((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_volume_min_j_mm3": min((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "energy_volume_max_j_mm3": max((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "bormash_profile_enabled": bool(is_bormash_profile(effective_settings)),
        "gcode_lines": len(lines),
        "gcode_size_mb": len(gcode.encode("utf-8")) / (1024.0 * 1024.0),
        "deposition_strategy": "rotary_c_table_rings",
        "hatch_spacing_effective_mm": radial_step,
    })
    stats["estimated_active_time_h"] = stats["estimated_active_time_s"] / 3600.0
    layer_pause_time_s = sum(li.layer_pause_s for li in layer_infos)
    stats["estimated_pause_time_s"] = layer_pause_time_s
    stats.update(_strategy_aux_time_s(effective_settings, physical_passes=len(layer_infos), link_moves=0))
    stats["estimated_service_time_s"] = stats["estimated_pause_time_s"] + stats["estimated_aux_time_s"]
    stats["estimated_total_time_s"] = (stats["estimated_active_time_s"] + stats["estimated_service_time_s"]) * 1.12
    stats["estimated_total_time_h"] = stats["estimated_total_time_s"] / 3600.0
    report = audit_report(effective_settings, stats, audit, warnings, layer_infos)
    return GenerationResult(gcode=gcode, layer_csv=layer_table_csv(layer_infos), audit_text=report, stats=stats)


def _generate_rotational_shell_ring_spiral(params: Dict[str, Any], settings: ProcessSettings, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    """Generate dedicated ring/spiral G-code for rotational vessels."""
    thermal_dwell_count = 0
    thermal_dwell_total_s = 0.0
    validate_process_settings(settings, height=float(params.get("height_mm", params.get("height", 80.0))))
    stats = rotational_shell_summary(params)
    height = float(stats["size_z"])
    strategy = str(getattr(settings, "rotational_path_strategy", "rings")).strip().lower()
    radial_step = _effective_rotational_radial_step(settings)
    radial_step = max(radial_step, 0.1)
    effective_settings = replace(settings, hatch_spacing=radial_step)
    n_layers_full = int(math.ceil(height / float(effective_settings.layer_height)))
    if effective_settings.max_layers_to_generate and effective_settings.max_layers_to_generate > 0:
        n_layers = max(1, min(int(effective_settings.max_layers_to_generate), n_layers_full))
    else:
        n_layers = n_layers_full
    z_values = [min(i * effective_settings.layer_height, height) for i in range(n_layers)]
    z_sections = [min(z + effective_settings.layer_height * 0.5, height - 1e-4) for z in z_values]
    lines = _gcode_header(effective_settings, stats)
    lines.append(f"(ROTATIONAL_SPECIAL_PATH: {strategy}; radial_step={_fmt(radial_step,3)} mm; true circular/spiral layer paths)")
    if effective_settings.use_w_retract and effective_settings.w_retract_mm > 0:
        lines.append("(Initial W retract calibration)")
        lines.append("G10 L20 P0 W0")
        lines.extend(_relative_w_move(-effective_settings.w_retract_mm, effective_settings.w_retract_feed_mm_min))
    layer_infos: List[LayerInfo] = []
    warning_messages: List[str] = [
        f"Rotational vessel path strategy: {strategy}. This uses dedicated circular/spiral paths instead of XY hatch filling.",
        "Recommended for cups/vessels/balloons, but still requires single-bead TEST and short-height qualification on the real machine.",
    ]
    total_active_len = 0.0
    max_segments_layer = 0
    total_segments = 0
    for idx, (z, zsec) in enumerate(zip(z_values, z_sections), start=1):
        if progress_callback and ((idx == 1) or (idx == n_layers) or (idx % max(1, int(effective_settings.progress_update_every_layers)) == 0)):
            try:
                progress_callback(idx, n_layers, f"rotational/{strategy}")
            except Exception:
                pass
        layer = layer_parameters(z, height, effective_settings)
        layer.index = idx
        if strategy == "spiral":
            segs = rotational_spiral_segments_at_z(zsec, params, effective_settings, idx)
        else:
            segs = rotational_ring_segments_at_z(zsec, params, effective_settings, idx)
        segs = _dedupe_segments(segs)
        if not segs:
            warning_messages.append(f"Layer {idx}: no circular/spiral path at Z={zsec:.3f}; skipped")
            continue
        layer.segments_count = len(segs)
        layer.contour_segments_count = 0
        max_segments_layer = max(max_segments_layer, len(segs))
        path_len = sum(math.hypot(s[2]-s[0], s[3]-s[1]) for s in segs)
        lines.append(f"(--- LAYER {idx}/{n_layers} Z={_fmt(layer.z,3)} ROTATIONAL_{strategy.upper()} I={_fmt(layer.current_ma,3)} F={_fmt(layer.feed_mm_min,1)} WIRE={_fmt(layer.wire_mm_s,3)} SEG={len(segs)} ---)")
        lines.extend(_continuous_layer_gcode(segs, layer, effective_settings, {}, f"rot_{strategy}"))
        if layer.layer_pause_s > 0:
            lines.append(f"G4 P{_fmt(layer.layer_pause_s,3)} (layer thermal stabilization)")
        _tdw = _thermal_dwell_for_layer(effective_settings, path_len / max(float(layer.travel_speed_mm_s), 1e-9))
        if _tdw > 0:
            lines.append(f"G4 P{_fmt(_tdw,1)} (THERMAL_DWELL L{idx}: adaptive - layer cycle below minimum)")
            thermal_dwell_count += 1
            thermal_dwell_total_s += _tdw
        layer.path_length_mm = path_len
        layer.contour_length_mm = 0.0
        total_active_len += path_len
        total_segments += len(segs)
        layer_infos.append(layer)
    if not layer_infos:
        raise RuntimeError("No rotational ring/spiral layers were generated. Check vessel diameter, wall thickness, radial step and height.")
    if thermal_dwell_count:
        warning_messages.append(f"ТЕПЛОВЫЕ ВЫДЕРЖКИ: {thermal_dwell_count} шт, суммарно {thermal_dwell_total_s/60.0:.1f} мин "
                                f"(мин. цикл слоя {float(getattr(effective_settings, 'thermal_min_layer_cycle_min', 0.0)):.1f} мин); время включено в G-code (G4).")
    footer_settings = replace(effective_settings, safe_z_final_mm=max(effective_settings.safe_z_final_mm, height + effective_settings.z_hop_mm + 5.0))
    lines.extend(_gcode_footer(footer_settings))
    gcode = "\n".join(lines) + "\n"
    audit = audit_gcode(gcode, effective_settings)
    stats.update({
        "app_version": APP_VERSION,
        "rotational_path_strategy": strategy,
        "rotational_radial_step_mm": radial_step,
        "rotational_points_per_circle": int(getattr(effective_settings, "rotational_points_per_circle", 160)),
        "layers_total": len(layer_infos),
        "layers_requested": n_layers,
        "layers_full_model": n_layers_full,
        "is_test_truncated": bool(n_layers < n_layers_full),
        "layer_fraction": len(layer_infos) / max(n_layers, 1),
        "segments_total": total_segments,
        "contour_segments_total": 0,
        "contour_path_length_mm": 0.0,
        "max_segments_per_layer": max_segments_layer,
        "active_path_length_mm": total_active_len,
        "active_path_length_m": total_active_len / 1000.0,
        "estimated_active_time_s": sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos),
        "estimated_wire_length_mm": sum((li.path_length_mm / max(li.travel_speed_mm_s, 1e-9)) * li.wire_mm_s for li in layer_infos),
        "wire_min_calculated_mm_s": min((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_max_calculated_mm_s": max((li.wire_mm_s for li in layer_infos), default=0.0),
        "feed_min_mm_min": min((li.feed_mm_min for li in layer_infos), default=0.0),
        "feed_max_mm_min": max((li.feed_mm_min for li in layer_infos), default=0.0),
        "process_wire_warning_limit_mm_s": effective_settings.wire_max_mm_s,
        "wire_above_control_limit": any(li.wire_mm_s > effective_settings.wire_max_mm_s for li in layer_infos),
        "current_min_ma": effective_settings.current_min_ma,
        "current_low_warning_ma": effective_settings.current_low_warning_ma,
        "current_limit_ma": effective_settings.current_max_ma,
        "beam_current_mode": str(getattr(effective_settings, "beam_current_mode", "energy")),
        "beam_current_bottom_ma": float(getattr(effective_settings, "beam_current_bottom_ma", 0.0)),
        "beam_current_top_ma": float(getattr(effective_settings, "beam_current_top_ma", 0.0)),
        "current_required_min_ma": min((li.current_required_ma for li in layer_infos), default=0.0),
        "current_required_max_ma": max((li.current_required_ma for li in layer_infos), default=0.0),
        "current_clipped_by_min": any(li.current_clipped_by_min for li in layer_infos),
        "current_clipped_by_max": any(li.current_clipped_by_max for li in layer_infos),
        "current_clipped_by_limit": any(li.current_clipped_by_max for li in layer_infos),
        "current_below_low_warning": any((li.current_required_ma < effective_settings.current_low_warning_ma - 1e-9) for li in layer_infos) if effective_settings.current_low_warning_ma > 0 else False,
        "energy_target_min_j_mm": min((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_target_max_j_mm": max((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_actual_min_j_mm": min((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_actual_max_j_mm": max((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_volume_min_j_mm3": min((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "energy_volume_max_j_mm3": max((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "bormash_profile_enabled": bool(is_bormash_profile(effective_settings)),
        "gcode_lines": len(lines),
        "gcode_size_mb": len(gcode.encode("utf-8")) / (1024.0 * 1024.0),
        "deposition_strategy": "rotational_" + strategy,
        "hatch_spacing_effective_mm": radial_step,
        "bead_width_mm": float(getattr(effective_settings, "bead_width_mm", 0.0)),
        "overlap_model": str(getattr(effective_settings, "overlap_model", "tom")),
    })
    stats["estimated_active_time_h"] = stats["estimated_active_time_s"] / 3600.0
    passes = len(layer_infos)
    layer_pause_time_s = sum(li.layer_pause_s for li in layer_infos)
    stats["estimated_pause_time_s"] = layer_pause_time_s
    if strategy == "spiral":
        stats.update(_strategy_aux_time_s(effective_settings, physical_passes=passes, link_moves=0))
    else:
        stats.update(_strategy_aux_time_s(effective_settings, physical_passes=passes, link_moves=total_segments))
    stats["estimated_service_time_s"] = stats["estimated_pause_time_s"] + stats["estimated_aux_time_s"]
    stats["estimated_total_time_s"] = (stats["estimated_active_time_s"] + stats["estimated_service_time_s"]) * 1.12
    stats["estimated_total_time_h"] = stats["estimated_total_time_s"] / 3600.0
    report = audit_report(effective_settings, stats, audit, warning_messages, layer_infos)
    return GenerationResult(gcode=gcode, layer_csv=layer_table_csv(layer_infos), audit_text=report, stats=stats)


def generate_rotational_shell(params: Dict[str, Any], settings: Optional[ProcessSettings] = None, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    """Generate G-code for simple built-in bowl/cup/balloon rotational shells.

    v4.2.9.6 supports four modes:
    - hatch: old per-layer annulus fill;
    - rings: true concentric circular XY paths;
    - spiral: Archimedean XY spiral paths inside each layer;
    - rotary_c_rings: B=0 rotary C-table rings, X sets radius, Z sets layer.
    """
    if settings is None:
        settings = ProcessSettings()
    path_strategy = str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower()
    if path_strategy in ("rotary_c", "rotary_c_rings", "c_rings", "c_table"):
        return _generate_rotational_shell_rotary_c(params, settings, progress_callback=progress_callback)
    if path_strategy in ("rings", "spiral"):
        return _generate_rotational_shell_ring_spiral(params, settings, progress_callback=progress_callback)
    stats = rotational_shell_summary(params)
    stats["rotational_path_strategy"] = "hatch"
    stats["rotational_path_note"] = "legacy hatch/zigzag fill inside annular layer section"
    height = float(stats["size_z"])
    provider = lambda z: rotational_shell_polygons_at_z(z, params)
    return _generate_with_polygon_provider(provider, height, stats, settings, progress_callback=progress_callback)

def _transform_poly_to_xy(poly, transform):
    """Convert Path2D polygon coordinates back into original XY using section transform."""
    def conv(coords):
        out = []
        for u, v in coords:
            vec = np.array([float(u), float(v), 0.0, 1.0])
            xyz = transform.dot(vec)[:3]
            out.append((float(xyz[0]), float(xyz[1])))
        return out
    exterior = conv(poly.exterior.coords)
    interiors = [conv(r.coords) for r in poly.interiors]
    return Polygon(exterior, interiors)


def _fallback_polygons_from_closed(path2d, transform):
    """Build polygons from closed rings without requiring rtree.

    trimesh Path2D.polygons_full may require the optional rtree package to
    classify holes. On many Windows installs rtree is missing, which used to
    produce an empty toolpath for valid STL sections with inner holes (for
    example bowl/cup shapes). This fallback uses polygons_closed and rebuilds
    simple parent/child hole relationships with shapely only.
    """
    try:
        raw = list(path2d.polygons_closed)
    except Exception:
        raw = []
    rings = []
    for p in raw:
        try:
            q = _transform_poly_to_xy(p, transform)
        except Exception:
            q = p
        if q is None or q.is_empty:
            continue
        try:
            if not q.is_valid:
                q = q.buffer(0)
        except Exception:
            continue
        if isinstance(q, Polygon) and q.area > 1e-6:
            rings.append(q)
        elif isinstance(q, MultiPolygon):
            rings.extend([g for g in q.geoms if g.area > 1e-6])

    if not rings:
        return []

    # Sort by area: largest rings are probable exteriors. For each ring,
    # choose the smallest larger ring that contains it as a parent. Odd-even
    # nesting is simplified here; this handles typical STL slices with outer
    # loops and holes robustly enough for EBAM path generation.
    rings = sorted(rings, key=lambda p: p.area, reverse=True)
    parents = [None] * len(rings)
    for i, child in enumerate(rings):
        cpt = child.representative_point()
        best = None
        best_area = float('inf')
        for j, parent in enumerate(rings[:i]):
            try:
                if parent.contains(cpt) and parent.area < best_area:
                    best = j
                    best_area = parent.area
            except Exception:
                pass
        parents[i] = best

    result = []
    for i, ring in enumerate(rings):
        # Exterior rings have no parent. Rings with a parent become holes of
        # that parent. Nested islands are rare for our EBAM use case; if found
        # they will be treated as separate exteriors by even-depth rule.
        depth = 0
        pidx = parents[i]
        while pidx is not None:
            depth += 1
            pidx = parents[pidx]
        if depth % 2 != 0:
            continue
        holes = []
        for k, other in enumerate(rings):
            if parents[k] == i:
                try:
                    holes.append(list(other.exterior.coords))
                except Exception:
                    pass
        try:
            poly = Polygon(list(ring.exterior.coords), holes)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if isinstance(poly, Polygon) and poly.area > 1e-6:
                result.append(poly)
            elif isinstance(poly, MultiPolygon):
                result.extend([g for g in poly.geoms if g.area > 1e-6])
        except Exception:
            result.append(ring)
    return result



def _rings_to_polygons(rings: List[Polygon]) -> List[Polygon]:
    """Convert independent closed rings to polygons with holes using containment.

    This deliberately avoids rtree. It is slower than Trimesh's polygons_full,
    but robust for offline Windows installations where optional spatial-index
    wheels are absent.
    """
    clean = []
    for q in rings:
        if q is None or q.is_empty:
            continue
        try:
            if not q.is_valid:
                q = q.buffer(0)
        except Exception:
            continue
        if isinstance(q, Polygon) and q.area > 1e-6:
            clean.append(q)
        elif isinstance(q, MultiPolygon):
            clean.extend([g for g in q.geoms if g.area > 1e-6])
    if not clean:
        return []

    clean = sorted(clean, key=lambda p: p.area, reverse=True)
    parents = [None] * len(clean)
    for i, child in enumerate(clean):
        cpt = child.representative_point()
        best = None
        best_area = float("inf")
        for j, parent in enumerate(clean[:i]):
            try:
                if parent.contains(cpt) and parent.area < best_area:
                    best = j
                    best_area = parent.area
            except Exception:
                pass
        parents[i] = best

    result = []
    for i, ring in enumerate(clean):
        depth = 0
        pidx = parents[i]
        while pidx is not None:
            depth += 1
            pidx = parents[pidx]
        if depth % 2 != 0:
            continue
        holes = []
        for k, other in enumerate(clean):
            if parents[k] == i:
                try:
                    holes.append(list(other.exterior.coords))
                except Exception:
                    pass
        try:
            poly = Polygon(list(ring.exterior.coords), holes)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if isinstance(poly, Polygon) and poly.area > 1e-6:
                result.append(poly)
            elif isinstance(poly, MultiPolygon):
                result.extend([g for g in poly.geoms if g.area > 1e-6])
        except Exception:
            result.append(ring)
    return _clean_polygons(result)


def _fallback_polygons_from_discrete(path2d, transform) -> List[Polygon]:
    """Build polygons from Path2D.discrete without polygons_full/polygons_closed.

    This is the v3.1 offline fallback. It only needs Shapely and Numpy and
    avoids the optional rtree dependency entirely.
    """
    rings: List[Polygon] = []
    try:
        curves = list(path2d.discrete)
    except Exception:
        curves = []
    for arr in curves:
        try:
            pts = [(float(a[0]), float(a[1])) for a in arr]
        except Exception:
            continue
        if len(pts) < 4:
            continue
        if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 1e-4:
            pts.append(pts[0])
        try:
            p2 = Polygon(pts)
            q = _transform_poly_to_xy(p2, transform)
            if not q.is_valid:
                q = q.buffer(0)
            if isinstance(q, Polygon) and q.area > 1e-6:
                rings.append(q)
            elif isinstance(q, MultiPolygon):
                rings.extend([g for g in q.geoms if g.area > 1e-6])
        except Exception:
            continue
    return _rings_to_polygons(rings)


def _manual_section_polygons_at_z(mesh: trimesh.Trimesh, z: float) -> List[Polygon]:
    """Manual triangle-plane intersection fallback for STL slicing.

    Trimesh section polygonization may fail on some offline Windows machines
    depending on optional packages. This routine intersects mesh triangles
    with a horizontal plane directly, polygonizes resulting XY linework with
    Shapely, and reconstructs holes. It is slower but very robust.
    """
    try:
        verts = np.asarray(mesh.vertices, dtype=float)
        faces = np.asarray(mesh.faces, dtype=np.int64)
    except Exception:
        return []
    if len(faces) == 0:
        return []
    z = float(z)
    eps = 1e-7
    tri = verts[faces]
    zvals = tri[:, :, 2]
    zmin = zvals.min(axis=1)
    zmax = zvals.max(axis=1)
    mask = (zmin <= z + eps) & (zmax >= z - eps) & ((zmax - zmin) > eps)
    if not np.any(mask):
        return []
    lines = []
    for t in tri[mask]:
        pts = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            p0 = t[i]
            p1 = t[j]
            z0 = p0[2]
            z1 = p1[2]
            dz = z1 - z0
            # edge lies in the plane; keep both endpoints as a segment candidate
            if abs(z0 - z) <= eps and abs(z1 - z) <= eps:
                pts.append((float(p0[0]), float(p0[1])))
                pts.append((float(p1[0]), float(p1[1])))
                continue
            if abs(dz) <= eps:
                continue
            if (z0 - z) * (z1 - z) <= eps:
                u = (z - z0) / dz
                if -1e-7 <= u <= 1.0 + 1e-7:
                    x = p0[0] + u * (p1[0] - p0[0])
                    y = p0[1] + u * (p1[1] - p0[1])
                    pts.append((float(x), float(y)))
        # dedupe points from vertices exactly on plane
        uniq = []
        seen = set()
        for x, y in pts:
            key = (round(x, 5), round(y, 5))
            if key not in seen:
                seen.add(key)
                uniq.append((x, y))
        if len(uniq) >= 2:
            a, b = uniq[0], uniq[1]
            if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-5:
                try:
                    # Snap coordinates to 1 µm to help polygonize close loops.
                    a2 = (round(a[0], 6), round(a[1], 6))
                    b2 = (round(b[0], 6), round(b[1], 6))
                    lines.append(LineString([a2, b2]))
                except Exception:
                    pass
    if not lines:
        return []
    try:
        merged = unary_union(lines)
        polys = [p for p in polygonize(merged) if isinstance(p, Polygon) and p.area > 1e-6]
    except Exception:
        return []
    if not polys:
        return []

    # Shapely polygonize of an annulus often returns both the annulus polygon
    # and the filled inner hole polygon.  Drop polygons that sit inside a hole
    # of another polygon, otherwise a hollow cup slice becomes a filled disk.
    filtered: List[Polygon] = []
    for i, p in enumerate(polys):
        rp = p.representative_point()
        inside_some_hole = False
        for j, q in enumerate(polys):
            if i == j or not getattr(q, "interiors", None):
                continue
            try:
                for ring in q.interiors:
                    if Polygon(ring).contains(rp):
                        inside_some_hole = True
                        break
            except Exception:
                pass
            if inside_some_hole:
                break
        if not inside_some_hole:
            filtered.append(p)
    return _clean_polygons(filtered)

def _path2d_polygons(section: trimesh.path.Path3D):
    if section is None:
        return []
    try:
        path2d, transform = section.to_2D()
    except Exception:
        path2d, transform = section.to_planar()

    # First try Trimesh's full polygon reconstruction. On some Windows
    # combinations polygons_full may fail OR silently return an empty list
    # even when polygons_closed contains valid rings.  A silent empty list is
    # exactly what made the preview look blank for valid STL models.
    polys = []
    try:
        polys = list(path2d.polygons_full)
    except Exception:
        # Common on Windows if optional rtree dependency is absent.
        # Fallback keeps STL generation working without installing rtree.
        polys = _fallback_polygons_from_closed(path2d, transform)
        if not polys:
            polys = _fallback_polygons_from_discrete(path2d, transform)
        return _clean_polygons(polys)

    if not polys:
        polys = _fallback_polygons_from_closed(path2d, transform)
        if not polys:
            polys = _fallback_polygons_from_discrete(path2d, transform)
        return _clean_polygons(polys)

    converted = []
    for p in polys:
        try:
            converted.append(_transform_poly_to_xy(p, transform))
        except Exception:
            converted.append(p)
    cleaned = _clean_polygons(converted)
    if not cleaned:
        cleaned = _clean_polygons(_fallback_polygons_from_closed(path2d, transform))
    if not cleaned:
        cleaned = _clean_polygons(_fallback_polygons_from_discrete(path2d, transform))
    return cleaned


def _extract_lines(geom) -> List[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        lines: List[LineString] = []
        for g in geom.geoms:
            lines.extend(_extract_lines(g))
        return lines
    return []




def _dedupe_segments(segs: List[Segment], ndigits: int = 5) -> List[Segment]:
    """Remove exact/near exact duplicate segments while preserving order."""
    out: List[Segment] = []
    seen = set()
    for s in segs:
        key = tuple(round(float(v), ndigits) for v in s)
        rkey = (key[2], key[3], key[0], key[1])
        # Do not collapse reverse direction for hatch lines; direction matters
        # for side wire feeding.  Only exact duplicates are removed.
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out

def _section_polygons_at_z(mesh: trimesh.Trimesh, z: float, tolerance: float = 1e-6,
                           probe_radius: float = 0.0, probe_count: int = 7):
    """Return robust XY polygons for an STL section near Z.

    v2.8 improvement: a mathematically valid STL can still return an empty
    section exactly at a vertex/edge level or on a near-tangent slice.  For
    EBAM this used to create skipped layers.  We now probe nearby Z levels
    and choose the largest valid section.  This is a geometric robustness
    fallback, not a thermal correction.
    """
    zmin = float(mesh.bounds[0, 2])
    zmax = float(mesh.bounds[1, 2])
    eps = max(tolerance, 1e-5)
    z0 = min(max(float(z), zmin + eps), zmax - eps)

    candidates = [z0]
    if probe_radius > 1e-9 and probe_count > 1:
        # symmetric probing: z, +/- small, +/- medium, +/- max
        fractions = [0.18, 0.32, 0.50, 0.72, 1.00]
        for fr in fractions:
            dz = probe_radius * fr
            candidates.append(min(max(z0 + dz, zmin + eps), zmax - eps))
            candidates.append(min(max(z0 - dz, zmin + eps), zmax - eps))

    best_polys = []
    best_area = -1.0
    seen = set()
    for zz in candidates:
        key = round(float(zz), 8)
        if key in seen:
            continue
        seen.add(key)
        section = mesh.section(plane_origin=[0.0, 0.0, float(zz)], plane_normal=[0.0, 0.0, 1.0])
        polys = _path2d_polygons(section)
        if not polys:
            # v3.1: fully offline fallback independent of trimesh polygonization/rtree.
            polys = _manual_section_polygons_at_z(mesh, float(zz))
        if not polys:
            continue
        try:
            u = unary_union(polys)
            if isinstance(u, Polygon):
                polys = [u]
            elif isinstance(u, MultiPolygon):
                polys = list(u.geoms)
        except Exception:
            pass
        area = sum(float(p.area) for p in polys if p is not None and not p.is_empty)
        if area > best_area:
            best_area = area
            best_polys = polys
    return best_polys


def _clean_polygons(polys: Iterable[Polygon]) -> List[Polygon]:
    out = []
    for p in polys:
        if p is None or p.is_empty:
            continue
        try:
            if not p.is_valid:
                p = p.buffer(0)
        except Exception:
            continue
        if isinstance(p, Polygon) and p.area > 1e-6:
            out.append(p)
        elif isinstance(p, MultiPolygon):
            out.extend([g for g in p.geoms if g.area > 1e-6])
    if not out:
        return []
    try:
        u = unary_union(out)
        if isinstance(u, Polygon):
            return [u]
        if isinstance(u, MultiPolygon):
            return [g for g in u.geoms if g.area > 1e-6]
    except Exception:
        pass
    return out


def _hatch_segments_for_polygons(polys, settings: ProcessSettings, layer_index: int) -> List[Segment]:
    segments: List[Segment] = []
    if not polys:
        return segments

    shift_options = [settings.shift_fraction_a, settings.shift_fraction_b, settings.shift_fraction_c]
    shift = shift_options[layer_index % len(shift_options)] * settings.hatch_spacing if settings.alternate_hatch_shift else 0.0
    direction = settings.direction.upper().strip()
    axis_y = direction.startswith("Y")

    for poly in polys:
        if poly.is_empty:
            continue
        work_poly = poly
        if settings.edge_offset > 0:
            try:
                work_poly = poly.buffer(-settings.edge_offset)
            except Exception:
                work_poly = poly
            if work_poly.is_empty:
                work_poly = poly

        minx, miny, maxx, maxy = work_poly.bounds
        if maxx - minx < 1e-6 or maxy - miny < 1e-6:
            continue

        if axis_y:
            if maxy - miny < settings.min_segment_length:
                continue
            start = minx + settings.hatch_spacing / 2.0 + shift
            while start < minx:
                start += settings.hatch_spacing
            while start - settings.hatch_spacing >= minx:
                start -= settings.hatch_spacing
            x = start
            guard = 0
            while x <= maxx + 1e-9 and guard < 20000:
                guard += 1
                line = LineString([(x, miny - 5 * settings.hatch_spacing - 10), (x, maxy + 5 * settings.hatch_spacing + 10)])
                inter = work_poly.intersection(line)
                for ls in _extract_lines(inter):
                    coords = list(ls.coords)
                    if len(coords) < 2:
                        continue
                    yvals = [c[1] for c in coords]
                    y0, y1 = float(min(yvals)), float(max(yvals))
                    if abs(y1 - y0) < settings.min_segment_length:
                        continue
                    if direction == "Y+":
                        segments.append((float(x), y0, float(x), y1))
                    else:
                        segments.append((float(x), y1, float(x), y0))
                x += settings.hatch_spacing
        else:
            if maxx - minx < settings.min_segment_length:
                continue
            start = miny + settings.hatch_spacing / 2.0 + shift
            while start < miny:
                start += settings.hatch_spacing
            while start - settings.hatch_spacing >= miny:
                start -= settings.hatch_spacing
            y = start
            guard = 0
            while y <= maxy + 1e-9 and guard < 20000:
                guard += 1
                line = LineString([(minx - 5 * settings.hatch_spacing - 10, y), (maxx + 5 * settings.hatch_spacing + 10, y)])
                inter = work_poly.intersection(line)
                for ls in _extract_lines(inter):
                    coords = list(ls.coords)
                    if len(coords) < 2:
                        continue
                    xvals = [c[0] for c in coords]
                    x0, x1 = float(min(xvals)), float(max(xvals))
                    if abs(x1 - x0) < settings.min_segment_length:
                        continue
                    if direction == "X+":
                        segments.append((x0, float(y), x1, float(y)))
                    else:
                        segments.append((x1, float(y), x0, float(y)))
                y += settings.hatch_spacing

    if axis_y:
        segments.sort(key=lambda s: s[0])
    else:
        segments.sort(key=lambda s: s[1])

    if settings.thermal_ordering == "skip_neighbours":
        even = segments[0::2]
        odd = segments[1::2]
        return even + odd[::-1]
    return segments


def _line_ring_to_segments(coords: List[Tuple[float, float]], min_len: float) -> List[Segment]:
    if len(coords) < 2:
        return []
    segs: List[Segment] = []
    for a, b in zip(coords[:-1], coords[1:]):
        x0, y0 = float(a[0]), float(a[1])
        x1, y1 = float(b[0]), float(b[1])
        if math.hypot(x1 - x0, y1 - y0) >= min_len:
            segs.append((x0, y0, x1, y1))
    return segs


def _rotate_closed_coords(coords: List[Tuple[float, float]], direction: str) -> List[Tuple[float, float]]:
    # remove duplicated close point for rotation then close again
    if len(coords) > 1 and math.hypot(coords[0][0]-coords[-1][0], coords[0][1]-coords[-1][1]) < 1e-9:
        base = coords[:-1]
    else:
        base = coords[:]
    if not base:
        return coords
    d = direction.upper()
    if d == "Y-":
        idx = max(range(len(base)), key=lambda i: base[i][1])
    elif d == "Y+":
        idx = min(range(len(base)), key=lambda i: base[i][1])
    elif d == "X-":
        idx = max(range(len(base)), key=lambda i: base[i][0])
    else:
        idx = min(range(len(base)), key=lambda i: base[i][0])
    rot = base[idx:] + base[:idx]
    rot.append(rot[0])
    return rot


def _contour_segments_for_polygons(polys, settings: ProcessSettings, layer_index: int) -> List[Segment]:
    if settings.contour_passes <= 0:
        return []
    segs: List[Segment] = []
    for poly in polys:
        if poly.is_empty:
            continue
        for pidx in range(settings.contour_passes):
            offset = settings.edge_offset + pidx * max(settings.contour_offset_step, 0.01)
            try:
                work = poly.buffer(-offset) if offset > 0 else poly
            except Exception:
                work = poly
            if work.is_empty:
                continue
            geoms = [work] if isinstance(work, Polygon) else list(work.geoms) if isinstance(work, MultiPolygon) else []
            for g in geoms:
                coords = [(float(x), float(y)) for x, y in list(g.exterior.coords)]
                coords = _rotate_closed_coords(coords, settings.direction)
                segs.extend(_line_ring_to_segments(coords, settings.min_segment_length * 0.4))
                for interior in g.interiors:
                    icoords = [(float(x), float(y)) for x, y in list(interior.coords)]
                    icoords = _rotate_closed_coords(icoords, settings.direction)
                    segs.extend(_line_ring_to_segments(icoords, settings.min_segment_length * 0.4))
    return segs


def _ensure_finite_positive(name: str, value: float, min_value: float = 0.0) -> None:
    try:
        v = float(value)
    except Exception as exc:
        raise ValueError(f"{name} должен быть числом") from exc
    if not math.isfinite(v) or v <= min_value:
        raise ValueError(f"{name} должен быть > {min_value}")


def validate_process_settings(settings: ProcessSettings, height: Optional[float] = None) -> None:
    """Validate user/JSON settings before generation.

    UI widgets already constrain most values, but CLI and JSON import can bypass
    them. Failing early is safer than silently producing wrong G-code.
    """
    if height is not None:
        try:
            h = float(height)
        except Exception as exc:
            raise ValueError("Высота модели/построения должна быть числом") from exc
        if not math.isfinite(h) or h <= 0:
            raise ValueError("Высота модели/построения должна быть > 0 мм")
    _ensure_finite_positive("layer_height", settings.layer_height, 0.0)
    _ensure_finite_positive("hatch_spacing", settings.hatch_spacing, 0.0)
    _ensure_finite_positive("wire_diameter_mm", settings.wire_diameter_mm, 0.0)
    _ensure_finite_positive("deposition_efficiency", settings.deposition_efficiency, 0.0)
    if float(settings.deposition_efficiency) > 1.0:
        raise ValueError("deposition_efficiency должен быть <= 1.0")
    _ensure_finite_positive("voltage_kv", settings.voltage_kv, 0.0)
    _ensure_finite_positive("feed_bottom_mm_min", settings.feed_bottom_mm_min, 0.0)
    _ensure_finite_positive("feed_top_mm_min", settings.feed_top_mm_min, 0.0)
    _ensure_finite_positive("rapid_feed_z_mm_min", settings.rapid_feed_z_mm_min, 0.0)
    _ensure_finite_positive("work_z_feed_mm_min", settings.work_z_feed_mm_min, 0.0)
    _ensure_finite_positive("contour_feed_factor", settings.contour_feed_factor, 0.0)
    if float(settings.current_min_ma) < 0 or float(settings.current_max_ma) < 0:
        raise ValueError("current_min_ma/current_max_ma не могут быть отрицательными")
    if float(settings.current_min_ma) > float(settings.current_max_ma):
        raise ValueError("current_min_ma не может быть больше current_max_ma")
    for name in ["edge_offset", "min_segment_length", "z_hop_mm", "safe_initial_approach_z_mm", "safe_z_final_mm", "wire_min_mm_s", "wire_max_mm_s", "w_retract_mm", "w_retract_feed_mm_min"]:
        v = float(getattr(settings, name))
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} должен быть >= 0")
    if int(settings.contour_passes) < 0:
        raise ValueError("contour_passes не может быть отрицательным")
    if int(settings.contour_every_n_layers) < 1:
        raise ValueError("contour_every_n_layers должен быть >= 1")
    if int(settings.max_layers_to_generate) < 0:
        raise ValueError("max_layers_to_generate не может быть отрицательным")
    if float(getattr(settings, "bead_width_mm", 0.0)) < 0:
        raise ValueError("bead_width_mm не может быть отрицательным")
    if float(getattr(settings, "link_feed_factor", 1.3)) <= 0:
        raise ValueError("link_feed_factor должен быть > 0")
    if str(getattr(settings, "deposition_strategy", "continuous")).strip().lower() not in ("continuous", "segmented"):
        raise ValueError("deposition_strategy должен быть 'continuous' или 'segmented'")
    if str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower() not in ("hatch", "rings", "spiral", "xy_rings", "xy_spiral", "rotary_c", "rotary_c_rings", "c_rings", "c_table", "stl_rotary_c_rings", "mesh_rotary_c_rings", "generic_rotary_c_rings"):
        raise ValueError("rotational_path_strategy должен быть 'hatch', 'rings/xy_rings', 'spiral/xy_spiral', 'rotary_c_rings' или 'stl_rotary_c_rings'")
    # v4.2.9.31: volumetric energy density sanity (both directions).
    # Line energy alone (J/mm) is geometry-dependent; the invariant is J/mm^3.
    try:
        _qv_lh = float(getattr(settings, "layer_height", 0.0) or 0.0)
        _qv_hs = float(_effective_hatch_spacing(settings)) if "_effective_hatch_spacing" in globals() else float(getattr(settings, "hatch_spacing", 0.0) or 0.0)
    except Exception:
        _qv_lh = float(getattr(settings, "layer_height", 0.0) or 0.0)
        _qv_hs = float(getattr(settings, "hatch_spacing", 0.0) or 0.0)
    if _qv_lh > 0 and _qv_hs > 0 and str(getattr(settings, "beam_current_mode", "energy")).strip().lower().startswith("energy"):
        _qv_e0 = float(getattr(settings, "target_energy_bottom_j_per_mm", 0.0) or 0.0)
        if _qv_e0 > 0:
            _qv_val = qv_from_energy_j_mm(_qv_e0, _qv_lh, _qv_hs)
            if _qv_val >= QV_FAIL_HIGH_J_MM3:
                raise ValueError(
                    f"Плотность энергии QV = {_qv_val:.0f} Дж/мм³ — перегрев (порог {QV_FAIL_HIGH_J_MM3:.0f}). "
                    f"Энергия {_qv_e0:.0f} Дж/мм приходится на тонкий валик "
                    f"(слой {_qv_lh:.2f} × шаг {_qv_hs:.2f} = {_qv_lh*_qv_hs:.3f} мм²). "
                    f"Это режим испарения и разбрызгивания. Снизьте энергию до "
                    f"{energy_j_mm_from_qv(QV_MAX_J_MM3, _qv_lh, _qv_hs):.0f} Дж/мм или увеличьте "
                    f"высоту слоя/шаг дорожек. Рабочая полоса QV: {QV_MIN_J_MM3:.0f}–{QV_MAX_J_MM3:.0f} Дж/мм³."
                )
            if _qv_val <= QV_FAIL_LOW_J_MM3:
                raise ValueError(
                    f"Плотность энергии QV = {_qv_val:.0f} Дж/мм³ — ниже порога отказа "
                    f"{QV_FAIL_LOW_J_MM3:.0f} (проволока перестаёт плавиться и лезет из ванны). "
                    f"Поднимите энергию до {energy_j_mm_from_qv(QV_MIN_J_MM3, _qv_lh, _qv_hs):.0f} Дж/мм "
                    f"или уменьшите высоту слоя/шаг дорожек."
                )
    if float(getattr(settings, "rotational_radial_step_mm", 0.0)) < 0:
        raise ValueError("rotational_radial_step_mm должен быть >= 0")
    # v4.2.9.31 (ported from v4.2.9.16): radial step = 0 must NOT silently collapse to
    # hatch_spacing for a rotational strategy. Field case Flange_V1: radial_step=0
    # stacked all rings on one line -> a cup/cone instead of a flat disc, +6x layer
    # height. Also inflates ring count and wrecks the time estimate.
    _rot_need_step = ("rings", "spiral", "xy_rings", "xy_spiral", "rotary_c",
                      "rotary_c_rings", "c_rings", "c_table", "stl_rotary_c_rings",
                      "mesh_rotary_c_rings", "generic_rotary_c_rings")
    _strat = str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower()
    if _strat in _rot_need_step:
        _explicit = float(getattr(settings, "rotational_radial_step_mm", 0.0) or 0.0)
        _auto_ok = (bool(getattr(settings, "auto_hatch_from_bead", False))
                    and float(getattr(settings, "bead_width_mm", 0.0) or 0.0) > 0.0)
        if _explicit <= 0.0 and not _auto_ok:
            raise ValueError(
                "Радиальный шаг колец (rotational_radial_step_mm) равен 0 для ротационной "
                "стратегии '" + _strat + "'. Нулевой шаг схлопывает все кольца в одну линию "
                "(чаша/конус вместо плоского диска, высота слоя кратно превышает заданную, "
                "а оценка времени становится неверной). Задайте шаг > 0 (обычно 0.6-0.8 ширины "
                "валика — имеется в виду ДОЛЯ ширины: для валика 4 мм это 2.4-3.2 мм; чаще всего "
                "радиальный шаг ставят РАВНЫМ полю «Шаг дорожек») или включите авто-шаг от ширины "
                "валика (auto_hatch_from_bead + bead_width_mm > 0). "
                + f"Подсказка: сейчас «Шаг дорожек» (hatch_spacing) = {float(getattr(settings, 'hatch_spacing', 0.0)):.2f} мм."
            )
        _eff = _effective_rotational_radial_step(settings)
        if _eff < 0.3:
            raise ValueError("Эффективный радиальный шаг " + f"{_eff:.3f}" + " мм слишком мал: кольца совпадут, металл уйдёт в высоту.")
        if _eff > 20.0:
            raise ValueError("Эффективный радиальный шаг " + f"{_eff:.1f}" + " мм слишком велик: между кольцами останутся непроплавленные промежутки.")
    if int(getattr(settings, "rotational_points_per_circle", 160)) < 24:
        raise ValueError("rotational_points_per_circle должен быть >= 24")
    if float(getattr(settings, "rotary_c_max_deg_min", 2100.0)) <= 0:
        raise ValueError("rotary_c_max_deg_min должен быть > 0")
    if float(getattr(settings, "rotary_c_min_radius_mm", 18.0)) < 0:
        raise ValueError("rotary_c_min_radius_mm должен быть >= 0")
    if str(getattr(settings, "rotary_c_direction", "C+")).strip().upper() not in ("C+", "C-"):
        raise ValueError("rotary_c_direction должен быть C+ или C-")
    if str(getattr(settings, "rotary_c_motion_mode", "separate_rings")).strip().lower() not in ("separate_rings", "no_pause_flat_rings"):
        raise ValueError("rotary_c_motion_mode должен быть separate_rings или no_pause_flat_rings")
    if float(getattr(settings, "rotary_c_transition_angle_deg", 17.0)) < 0:
        raise ValueError("rotary_c_transition_angle_deg должен быть >= 0")
    if float(getattr(settings, "rotary_c_radius_variation_tolerance_mm", 0.05)) < 0:
        raise ValueError("rotary_c_radius_variation_tolerance_mm должен быть >= 0")
    path_mode = str(getattr(settings, "path_control_mode", "g64_tolerance")).strip().lower()
    if path_mode not in ("g64_tolerance", "machine_default", "g61", "g61_1", "g61.1"):
        raise ValueError("path_control_mode должен быть g64_tolerance, machine_default, g61 или g61_1")
    if float(getattr(settings, "g64_tolerance_mm", 0.08)) < 0 or float(getattr(settings, "g64_naive_cam_q_mm", 0.0)) < 0:
        raise ValueError("G64 P/Q должны быть >= 0")
    analog_mode = str(getattr(settings, "analog_output_mode", "m68_compatible")).strip().lower()
    if abs(float(getattr(settings, "rotary_c_seam_scatter_deg", 0.0) or 0.0)) >= 180.0:
        raise ValueError("rotary_c_seam_scatter_deg должен быть в диапазоне (-180, 180)")
    if analog_mode not in ("m68_compatible", "m67_synchronized"):
        raise ValueError("analog_output_mode должен быть m68_compatible или m67_synchronized")
    if str(getattr(settings, "wire_feed_mode", "auto")).strip().lower() not in ("auto", "manual_constant", "manual_bottom_top"):
        raise ValueError("wire_feed_mode должен быть auto, manual_constant или manual_bottom_top")


def layer_parameters(z: float, zmax: float, settings: ProcessSettings) -> LayerInfo:
    ratio = 0.0 if zmax <= 1e-9 else min(max(z / zmax, 0.0), 1.0)
    f = settings.feed_bottom_mm_min + (settings.feed_top_mm_min - settings.feed_bottom_mm_min) * ratio
    e_target, current_required, current, clipped_by_min, clipped_by_max = _beam_current_and_energy_for_layer(z, zmax, settings, f)
    travel = f / 60.0
    area = settings.wire_area_mm2()
    # Important: wire_max_mm_s is a warning threshold, not a hard clamp.
    # v4.2.9.7 can either auto-calculate E2 from geometry/F or use operator-set E2.
    wire = _wire_for_layer_ratio(settings, f, ratio)
    qmetal = area * wire * float(settings.deposition_efficiency)
    p = settings.voltage_kv * current
    e_actual = p / max(travel, 1e-9)
    e_vol = p / qmetal if qmetal > 0 else float("inf")
    pause = settings.layer_pause_bottom_s + (settings.layer_pause_top_s - settings.layer_pause_bottom_s) * ratio
    return LayerInfo(index=0, z=float(z), z_next=float(z + settings.layer_height), ratio=float(ratio),
                     current_ma=float(current), feed_mm_min=float(f), travel_speed_mm_s=float(travel),
                     wire_mm_s=float(wire), energy_j_mm=float(e_target), energy_actual_j_mm=float(e_actual),
                     current_required_ma=float(current_required), current_clipped_by_min=bool(clipped_by_min),
                     current_clipped_by_max=bool(clipped_by_max), energy_j_mm3=float(e_vol),
                     layer_pause_s=float(pause))


def _fmt(v: float, nd: int = 3) -> str:
    return f"{v:.{nd}f}"


def _path_control_gcode(settings: ProcessSettings) -> List[str]:
    """Return explicit path-control commands without changing operator overrides.

    LinuxCNC recommends an explicit path-control mode in the preamble. Q=0
    disables naive-CAM collapsing, which is safer for short C+Z transition
    segments. ``machine_default`` intentionally emits nothing.
    """
    mode = str(getattr(settings, "path_control_mode", "g64_tolerance") or "g64_tolerance").strip().lower()
    if mode == "machine_default":
        return []
    if mode == "g61":
        return ["G61 (exact path; may slow/stop at programmed points)"]
    if mode in ("g61_1", "g61.1"):
        return ["G61.1 (exact stop; stops at every segment)"]
    p = max(0.0, float(getattr(settings, "g64_tolerance_mm", 0.08)))
    q = max(0.0, float(getattr(settings, "g64_naive_cam_q_mm", 0.0)))
    return [f"G64 P{_fmt(p,3)} Q{_fmt(q,3)} (path blending; Q0 disables naive-CAM collapse)"]


def _deposition_analog_code(settings: ProcessSettings) -> str:
    """Choose M67 only after explicit machine/HAL confirmation.

    The shipped Bormash-compatible behaviour remains M68. This prevents an
    unverified software update from silently switching to an analog output
    command that may not be connected in the actual HAL configuration.
    """
    requested = str(getattr(settings, "analog_output_mode", "m68_compatible") or "m68_compatible").strip().lower()
    if requested == "m67_synchronized" and bool(getattr(settings, "machine_m67_confirmed", False)):
        return "M67"
    return "M68"


def _analog_setpoint_line(settings: ProcessSettings, channel: int, value: float, comment: str = "") -> str:
    code = _deposition_analog_code(settings)
    suffix = f" ({comment})" if comment else ""
    return f"{code} E{int(channel)} Q{_fmt(float(value),3)}{suffix}"


def beam_power_floor_analysis(settings: ProcessSettings, layers: List["LayerInfo"]) -> Dict[str, Any]:
    """Advisory beam-power (fusion) check, ported from the parallel branch.

    Beam power P = U[kV] * I[mA] (numerically watts). If the calculated regime is
    below settings.min_beam_power_w, the deposit risks lack-of-fusion / balling,
    exactly as observed on the real plate (160 J/mm at slow C-speed -> ~630 W ->
    balled bead; ~1.26 kW -> fused bead). Reports actual per-layer power and, when
    below the floor, what target energy or travel speed reaches the floor. It
    NEVER changes G-code. Calibrate the floor on a single-bead TEST.
    """
    u = max(float(settings.voltage_kv), 1e-9)
    floor = float(getattr(settings, "min_beam_power_w", 0.0) or 0.0)
    out: Dict[str, Any] = {"min_beam_power_w_floor": floor}
    if not layers:
        out.update({"beam_power_min_w": 0.0, "beam_power_max_w": 0.0, "beam_power_below_floor": False})
        return out
    powers = [u * float(li.current_ma) for li in layers]
    p_min, p_max = min(powers), max(powers)
    out["beam_power_min_w"] = p_min
    out["beam_power_max_w"] = p_max
    below = bool(getattr(settings, "power_floor_warning_enabled", True)) and floor > 0 and (p_min < floor - 1e-9)
    out["beam_power_below_floor"] = below
    if below:
        weak = min(layers, key=lambda li: u * float(li.current_ma))
        i_now = float(weak.current_ma)
        v_now = max(float(weak.travel_speed_mm_s), 1e-9)
        e_now = float(weak.energy_actual_j_mm if weak.energy_actual_j_mm else weak.energy_j_mm)
        out["beam_power_weak_layer_index"] = int(getattr(weak, "index", 0))
        out["beam_power_current_now_ma"] = i_now
        out["beam_power_current_needed_ma"] = floor / u
        out["beam_power_energy_now_j_mm"] = e_now
        out["beam_power_energy_needed_j_mm"] = floor / v_now
        out["beam_power_speed_now_mm_s"] = v_now
        out["beam_power_speed_for_floor_mm_s"] = floor / max(e_now, 1e-9)
    return out


def generate_calibration_beads(settings: ProcessSettings,
                               currents_ma: List[float],
                               feeds_mm_min: List[float],
                               bead_length_mm: float = 40.0,
                               bead_spacing_mm: float = 10.0,
                               col_gap_mm: float = 12.0,
                               origin_x_mm: float = 15.0,
                               origin_y_mm: float = 15.0,
                               wire_mode: str = "auto",
                               wire_fixed_mm_s: float = 3.5,
                               z_mm: float = 0.0) -> GenerationResult:
    """Single-bead calibration matrix (ported from the parallel branch).

    Straight test beads on a plate: rows = beam currents, columns = travel feeds.
    Every bead uses the proven safe per-segment emitter (beam off + Z-hop between
    beads, no G0 under beam, W balance, no overrides touched, M68 only). Measure
    real width/height/fusion per bead, then analyze_calibration_results() derives
    the machine's true fusion power floor, Z-step, hatch/radial step and eta.
    """
    currents = [float(c) for c in currents_ma if float(c) > 0]
    feeds = [float(f) for f in feeds_mm_min if float(f) > 0]
    if not currents or not feeds:
        raise ValueError("Калибровка: нужен хотя бы один ток > 0 и одна скорость > 0")
    if bead_length_mm <= 2.0:
        raise ValueError("Калибровка: длина валика должна быть больше 2 мм")
    eff = replace(settings, beam_current_mode="current")
    area = eff.wire_area_mm2()
    beads: List[Dict[str, Any]] = []
    bead_id = 0
    x_max_used = origin_x_mm
    y_max_used = origin_y_mm
    gcode_lines: List[str] = []
    for row, cur in enumerate(currents):
        for col, f in enumerate(feeds):
            bead_id += 1
            travel = f / 60.0
            if str(wire_mode).strip().lower() == "fixed":
                wire = max(float(wire_fixed_mm_s), 0.0)
            else:
                wire = eff.layer_height * travel * eff.hatch_spacing / max(area * eff.deposition_efficiency, 1e-9)
            x0 = origin_x_mm + col * (bead_length_mm + col_gap_mm)
            y0 = origin_y_mm + row * bead_spacing_mm
            x1 = x0 + bead_length_mm
            x_max_used = max(x_max_used, x1)
            y_max_used = max(y_max_used, y0)
            power_w = eff.voltage_kv * cur
            e_j_mm = power_w / max(travel, 1e-9)
            li = LayerInfo(index=bead_id, z=float(z_mm), z_next=float(z_mm + eff.layer_height), ratio=0.0,
                           current_ma=float(cur), feed_mm_min=float(f), travel_speed_mm_s=float(travel),
                           wire_mm_s=float(wire), energy_j_mm=float(e_j_mm), energy_actual_j_mm=float(e_j_mm),
                           current_required_ma=float(cur), current_clipped_by_min=False,
                           current_clipped_by_max=cur > eff.current_max_ma + 1e-9,
                           energy_j_mm3=float(power_w / max(area * wire, 1e-9)) if wire > 0 else float("inf"),
                           layer_pause_s=0.0, segments_count=1, path_length_mm=float(bead_length_mm))
            gcode_lines.append(f"(BEAD {bead_id}: I={cur:.2f} mA  F={f:.1f} mm/min  E2={wire:.3f} mm/s  P={power_w:.0f} W  E={e_j_mm:.0f} J/mm)")
            gcode_lines.extend(_beam_wire_segment_gcode((x0, y0, x1, y0), li, eff, 1.0, "hatch"))
            beads.append({"bead_id": bead_id, "row": row + 1, "col": col + 1,
                          "x0": x0, "y0": y0, "x1": x1, "y1": y0,
                          "current_ma": cur, "feed_mm_min": f, "wire_mm_s": wire,
                          "power_w": power_w, "energy_j_mm": e_j_mm})
    stats: Dict[str, Any] = {
        "source_type": "calibration_beads",
        "beads_total": len(beads),
        "currents_ma": currents, "feeds_mm_min": feeds,
        "bead_length_mm": bead_length_mm,
        "size_x": x_max_used + 5.0, "size_y": y_max_used + 5.0, "size_z": eff.layer_height,
        "min_x": 0.0, "min_y": 0.0, "min_z": 0.0,
        "max_x": x_max_used + 5.0, "max_y": y_max_used + 5.0, "max_z": eff.layer_height,
        "gcode_lines": 0, "gcode_size_mb": 0.0,
        "beam_power_min_w": min(b["power_w"] for b in beads),
        "beam_power_max_w": max(b["power_w"] for b in beads),
    }
    header = _gcode_header(eff, stats)
    body: List[str] = ["(CALIBRATION_BEAD_MATRIX: rows=currents, cols=feeds; measure width/height/fusion per bead)"]
    if eff.use_w_retract and eff.w_retract_mm > 0:
        body.append("(Initial W retract calibration)")
        body.append("G10 L20 P0 W0")
        body.extend(_relative_w_move(-eff.w_retract_mm, eff.w_retract_feed_mm_min))
    footer = _gcode_footer(replace(eff, safe_z_final_mm=max(eff.safe_z_final_mm, z_mm + eff.z_hop_mm + 5.0)))
    gcode = "\n".join(header + body + gcode_lines + footer) + "\n"
    stats["gcode_lines"] = gcode.count("\n")
    stats["gcode_size_mb"] = len(gcode.encode("utf-8")) / (1024.0 * 1024.0)
    audit = audit_gcode(gcode, eff)
    warnings: List[str] = [
        "Calibration matrix: beads intentionally span weak-to-strong regimes; some are EXPECTED to underfuse. Run on a spare plate only.",
        "Measure per bead: width (mm), height (mm), fused-to-plate (yes/no). Enter results in the app to derive the fusion power floor, Z-step and hatch.",
    ]
    header_csv = "bead_id,row,col,x0,y0,x1,y1,current_ma,feed_mm_min,wire_mm_s,power_w,energy_j_mm"
    rows_csv = [header_csv] + [
        f"{b['bead_id']},{b['row']},{b['col']},{b['x0']:.3f},{b['y0']:.3f},{b['x1']:.3f},{b['y1']:.3f},{b['current_ma']:.3f},{b['feed_mm_min']:.3f},{b['wire_mm_s']:.4f},{b['power_w']:.1f},{b['energy_j_mm']:.1f}"
        for b in beads]
    report = audit_report(replace(eff, power_floor_warning_enabled=False), stats, audit, warnings, [])
    return GenerationResult(gcode=gcode, layer_csv="\n".join(rows_csv) + "\n", audit_text=report, stats=stats)


def analyze_calibration_results(rows: List[Dict[str, Any]], voltage_kv: float,
                                wire_diameter_mm: float = 1.2,
                                overlap_model: str = "tom") -> Dict[str, Any]:
    """Derive the machine process window from measured calibration beads.

    Each row: current_ma, feed_mm_min, wire_mm_s, width_mm, height_mm, fused (bool).
    Returns fusion power floor bracketed by real data, recommended Z-step (median
    fused height), hatch TOM 0.738w / FOM 0.667w (median width), target J/mm
    (median of fused) and a deposition-efficiency estimate (parabolic bead area vs
    fed wire volume). Inconsistent data is flagged, not hidden.
    """
    u = max(float(voltage_kv), 1e-9)
    area_wire = math.pi * (float(wire_diameter_mm) * 0.5) ** 2
    clean: List[Dict[str, Any]] = []
    for r in rows:
        try:
            cur = float(r.get("current_ma", 0)); f = float(r.get("feed_mm_min", 0))
            if cur <= 0 or f <= 0:
                continue
            item = dict(r)
            item["power_w"] = u * cur
            item["travel_mm_s"] = f / 60.0
            item["energy_j_mm"] = item["power_w"] / max(item["travel_mm_s"], 1e-9)
            item["fused"] = bool(r.get("fused"))
            clean.append(item)
        except (TypeError, ValueError):
            continue
    out: Dict[str, Any] = {"beads_used": len(clean)}
    if not clean:
        out["error"] = "Нет пригодных строк: нужны ток и скорость > 0."
        return out
    fused = [r for r in clean if r["fused"]]
    unfused = [r for r in clean if not r["fused"]]
    out["fused_count"] = len(fused); out["unfused_count"] = len(unfused)

    def _med(vals: List[float]) -> Optional[float]:
        v = sorted(x for x in vals if x is not None and math.isfinite(x) and x > 0)
        if not v:
            return None
        n = len(v)
        return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])

    warnings: List[str] = []
    floor_lo = max((r["power_w"] for r in unfused), default=None)
    floor_hi = min((r["power_w"] for r in fused), default=None)
    if floor_lo is not None and floor_hi is not None and floor_hi < floor_lo - 1e-9:
        warnings.append(f"Данные противоречивы: проплав при {floor_hi:.0f} Вт и непроплав при {floor_lo:.0f} Вт. "
                        "Проплав зависит не только от мощности (фокус/скорость/чистота плиты); порог взят по верхней границе непроплава.")
        rec_floor = floor_lo * 1.1
    elif floor_hi is not None and floor_lo is not None:
        rec_floor = 0.5 * (floor_lo + floor_hi)
    elif floor_hi is not None:
        rec_floor = floor_hi
        warnings.append("Непроплавленных валиков нет — нижняя граница не найдена; порог = слабейший проплавленный (может быть завышен).")
    elif floor_lo is not None:
        rec_floor = floor_lo * 1.25
        warnings.append("Проплавленных валиков нет — порог оценён экстраполяцией (+25% к сильнейшему непроплаву). Нужны валики мощнее.")
    else:
        rec_floor = None
    out["power_unfused_max_w"] = floor_lo
    out["power_fused_min_w"] = floor_hi
    out["recommended_min_beam_power_w"] = rec_floor
    if fused:
        w_med = _med([float(r.get("width_mm") or 0) for r in fused])
        h_med = _med([float(r.get("height_mm") or 0) for r in fused])
        out["bead_width_median_mm"] = w_med
        out["bead_height_median_mm"] = h_med
        if h_med:
            out["recommended_z_step_mm"] = h_med
        if w_med:
            out["recommended_hatch_tom_mm"] = 0.738 * w_med
            out["recommended_hatch_fom_mm"] = 0.667 * w_med
            out["recommended_hatch_mm"] = (0.738 if str(overlap_model).lower() != "fom" else 0.667) * w_med
        out["recommended_energy_j_mm"] = _med([r["energy_j_mm"] for r in fused])
        effs = []
        for r in fused:
            w = float(r.get("width_mm") or 0); h = float(r.get("height_mm") or 0)
            e2 = float(r.get("wire_mm_s") or 0); v = float(r["travel_mm_s"])
            if w > 0 and h > 0 and e2 > 0 and v > 0:
                fed_area = area_wire * e2 / v
                if fed_area > 1e-9:
                    effs.append((2.0 / 3.0) * w * h / fed_area)
        eff_med = _med(effs)
        if eff_med:
            out["deposition_efficiency_estimate"] = min(eff_med, 1.5)
            if eff_med > 1.15:
                warnings.append(f"Оценка η {eff_med:.2f} > 1: проверьте измерения ширины/высоты или E2 — валик не может содержать больше металла, чем подано.")
    else:
        warnings.append("Нет проплавленных валиков — Z-step/шаг/энергия не рассчитаны.")
    out["warnings"] = warnings
    return out


def estimate_eta_from_wall(measured_wall_mm: float, z_step_mm: float,
                           feed_mm_min: float, wire_e2_mm_s: float,
                           wire_diameter_mm: float) -> Optional[float]:
    """Implied deposition efficiency from a real measured wall (handoff PDF 8.2 inverted).

    A_path = wall * z_step; fed area = A_wire * E2 / v  =>  eta = wall*z_step*v/(A_wire*E2).
    Returns None for non-physical inputs. This mass-side estimate must agree with
    the bead-matrix estimate from analyze_calibration_results()."""
    try:
        wall = float(measured_wall_mm); z = float(z_step_mm)
        v = float(feed_mm_min) / 60.0; e2 = float(wire_e2_mm_s); d = float(wire_diameter_mm)
    except (TypeError, ValueError):
        return None
    if min(wall, z, v, e2, d) <= 0:
        return None
    a_wire = math.pi * (d * 0.5) ** 2
    return (wall * z * v) / (a_wire * e2)


def eta_cross_check(eta_beads: Optional[float], eta_part: Optional[float],
                    rel_tol: float = 0.20) -> Dict[str, Any]:
    """Compare two independent eta estimates: bead matrix vs real part wall/mass.

    Agreement within rel_tol means both measurements support each other and the
    value can be trusted for E2 auto-calc. Disagreement is a DIAGNOSTIC, not an
    error to hide: usually measurement error, spatter/overflow losses, or the part
    regime differing thermally from the bead regime."""
    out: Dict[str, Any] = {"eta_beads": eta_beads, "eta_part": eta_part, "rel_tol": rel_tol}
    if not eta_beads or not eta_part or eta_beads <= 0 or eta_part <= 0:
        out["verdict"] = "insufficient"
        out["message"] = "Недостаточно данных: нужны обе оценки η (валики и деталь)."
        return out
    rel = abs(eta_beads - eta_part) / max(eta_part, 1e-9)
    out["relative_difference"] = rel
    out["eta_recommended"] = 0.5 * (eta_beads + eta_part)
    if rel <= rel_tol:
        out["verdict"] = "agree"
        out["message"] = (f"η сходится: валики {eta_beads:.2f} vs деталь {eta_part:.2f} "
                          f"(расхождение {rel*100:.0f}% ≤ {rel_tol*100:.0f}%). "
                          f"Рекомендуемое η: {out['eta_recommended']:.2f}.")
    else:
        out["verdict"] = "disagree"
        hint = ("η по валикам выше — на детали больше потерь (разбрызгивание/переливы) или ошибка измерения стенки."
                if eta_beads > eta_part else
                "η по детали выше — валики измерены заниженно или их режим не совпадает с режимом детали.")
        out["message"] = (f"η НЕ сходится: валики {eta_beads:.2f} vs деталь {eta_part:.2f} "
                          f"(расхождение {rel*100:.0f}% > {rel_tol*100:.0f}%). {hint} "
                          "Не усредняйте вслепую: проверьте измерения и повторите один валик.")
    return out


def generate_m67_hal_check_kit(settings: ProcessSettings) -> Dict[str, str]:
    """Dry-run kit to qualify synchronized M67 on the real HAL (handoff PDF 17.2 item 3).

    Returns {'gcode', 'checklist'}. Exercises M68 (immediate) vs M67 (applies at
    next motion) on E0 and E2 with tiny Q and short X moves. SAFETY: dry run ONLY:
    HV and wire drive must be hardware disabled/interlocked. The kit intentionally
    CONTAINS M67 - it IS the confirmation procedure; production generators still
    refuse M67 until machine_m67_confirmed is set after this kit passes. Final
    all-off is immediate M68, per project rule."""
    q_small = 1.0
    f_move = 300.0
    g: List[str] = []
    g.append("(=== M67/M68 HAL QUALIFICATION - DRY RUN ONLY ===)")
    g.append("(BEAM HV OFF + WIRE DRIVE DISABLED/INTERLOCKED BEFORE RUNNING)")
    g.append("(Watch halmeter/halscope on the analog pins mapped to E0 and E2)")
    g.append("G21")
    g.append("G90")
    g.append("G0 X10 Y10 Z50")
    g.append("(--- STEP 1: M68 E0 immediate: pin changes DURING the pause, before motion ---)")
    g.append(f"M68 E0 Q{q_small:.3f}")
    g.append("G4 P2.000 (pin must ALREADY be at Q here)")
    g.append("M68 E0 Q0.000")
    g.append("G4 P1.000")
    g.append("(--- STEP 2: M67 E0 synchronized: pin must NOT change during pause, only at motion start ---)")
    g.append(f"M67 E0 Q{q_small:.3f}")
    g.append("G4 P2.000 (pin must still be 0 here)")
    g.append(f"G1 X15 F{f_move:.1f} (pin steps to Q exactly when this move starts)")
    g.append("M68 E0 Q0.000 (immediate OFF)")
    g.append("G4 P1.000")
    g.append("(--- STEP 3: same for wire channel E2 ---)")
    g.append(f"M68 E2 Q{q_small:.3f}")
    g.append("G4 P2.000 (E2 pin at Q during pause = immediate OK)")
    g.append("M68 E2 Q0.000")
    g.append("G4 P1.000")
    g.append(f"M67 E2 Q{q_small:.3f}")
    g.append("G4 P2.000 (E2 pin must still be 0 here)")
    g.append(f"G1 X20 F{f_move:.1f} (E2 pin steps at motion start)")
    g.append("M68 E2 Q0.000")
    g.append("(--- STEP 4: M67 queueing across two motions ---)")
    g.append(f"M67 E0 Q{q_small:.3f}")
    g.append(f"G1 X25 F{f_move:.1f} (E0 -> Q at start of THIS move)")
    g.append("M67 E0 Q0.000")
    g.append(f"G1 X30 F{f_move:.1f} (E0 -> 0 at start of THIS move)")
    g.append("(--- FINAL: immediate all-off, per project rule ---)")
    g.append("M68 E0 Q0.000")
    g.append("M68 E2 Q0.000")
    g.append("G0 Z60")
    g.append("M30")
    checklist_lines = [
        "M67 HAL QUALIFICATION CHECKLIST (dry run)",
        "==========================================",
        "0. АППАРАТНО: HV выключен, привод проволоки отключён/заблокирован, interlocks активны.",
        "1. Открыть halmeter/halscope на аналоговых пинах E0 и E2 (motion.analog-out-NN по вашему HAL).",
        "2. Запустить kit в dry run; для каждого шага записать фактическое поведение пина.",
        "3. КРИТЕРИИ PASS:",
        "   - STEP1: пин E0 = Q УЖЕ во время паузы G4 (M68 немедленный).",
        "   - STEP2: пин E0 = 0 всю паузу; скачок в Q ровно на старте G1 X15 (M67 синхронный).",
        "   - STEP3: то же для E2.",
        "   - STEP4: каждое M67 применяется на старте СЛЕДУЮЩЕГО движения; без движения не применяется.",
        "   - Финальный M68 all-off немедленный.",
        "4. Любое несовпадение (не тот момент, не тот пин, задержки) = M67 НЕ подтверждён:",
        "   machine_m67_confirmed оставить ВЫКЛ, работать на M68, сохранить запись halscope.",
        "5. Все PASS: включить 'HAL подтверждён (M67)' — режим m67_synchronized станет доступен",
        "   для зонных обновлений E0/E2. Финальный OFF всегда остаётся M68.",
        "6. Повторять после ЛЮБОГО изменения HAL/INI.",
    ]
    return {"gcode": "\n".join(g) + "\n", "checklist": "\n".join(checklist_lines)}


def _gcode_header(settings: ProcessSettings, stats: Dict[str, float]) -> List[str]:
    lines = [
        f"(Generated by EBAM G-code Studio {APP_VERSION})",
        f"(Machine: {settings.machine_name})",
        f"(Units: mm, absolute coordinates)",
        f"(Model size X={stats['size_x']:.3f} Y={stats['size_y']:.3f} Z={stats['size_z']:.3f}; source={stats.get('source_type','unknown')})",
        f"(WARNING: calculated process only; verify with dry run and short test)",
        "G21 (mm)",
        "G90 (absolute XYZ)",
        "G94 (feed per minute)",
    ]
    lines.extend(_path_control_gcode(settings))
    lines.extend([
        "M429 (identity kinematics, if available)",
        "M68 E0 Q0.000 (beam current OFF; immediate safe state)",
        "M68 E2 Q0.000 (wire feed OFF; immediate safe state)",
        f"M68 E1 Q{_fmt(settings.focus_ma,3)} (focus)",
        "(EXTERNAL_OVERRIDES: this program does not enable, disable or reset operator overrides)",
        "(OPERATOR_NOTE: for calibrated comparison keep external WIRE override at 100%; adjustment remains available and machine-specific)",
    ])
    if bool(getattr(settings, "safe_initial_approach_enabled", False)):
        initial_z = float(getattr(settings, "safe_initial_approach_z_mm", 7.0))
        lines.append(
            f"(SAFE_INITIAL_APPROACH: enabled; absolute Z={_fmt(initial_z,3)}; "
            "machine/work-offset clearance must be dry-run verified)"
        )
    else:
        initial_z = float(settings.z_hop_mm)
    lines.append(f"G0 Z{_fmt(initial_z,3)}")
    return lines


def _gcode_footer(settings: ProcessSettings) -> List[str]:
    return [
        "(--- FINISH ---)",
        "M68 E2 Q0.000",
        "M68 E0 Q0.000",
        f"G0 Z{_fmt(settings.safe_z_final_mm,3)}",
        "M30",
    ]


def _relative_w_move(delta: float, feed: float) -> List[str]:
    return [
        "G91 (relative for W)",
        f"G1 W{_fmt(delta,3)} F{_fmt(feed,1)}",
        "G90 (absolute XYZ)",
    ]


def _beam_wire_segment_gcode(seg: Segment, layer: LayerInfo, settings: ProcessSettings, edge_factor: float = 1.0,
                             segment_kind: str = "hatch") -> List[str]:
    x0, y0, x1, y1 = seg
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    # Hard geometric guard: a zero/near-zero length segment has no direction and
    # would divide by zero below. This must not depend on min_segment_length,
    # which a CLI/JSON user is allowed to set to 0 and which can be defeated by
    # duplicate/degenerate points surviving dedup.
    if length <= 1e-9:
        return []
    if length < settings.min_segment_length * 0.35:
        return []
    ux = dx / length
    uy = dy / length

    max_each = max(length * 0.22, 0.05)
    lead = min(settings.lead_in_beam_mm, max_each)
    soft_s = min(settings.soft_start_mm, max_each)
    soft_f = min(settings.soft_finish_mm, max_each)
    tail = min(settings.tail_beam_mm, max_each)
    if lead + soft_s + soft_f + tail > length * 0.80:
        scale = (length * 0.80) / max(lead + soft_s + soft_f + tail, 1e-9)
        lead *= scale; soft_s *= scale; soft_f *= scale; tail *= scale

    def pt_at(dist: float) -> Tuple[float, float]:
        return (x0 + ux * dist, y0 + uy * dist)

    p_lead = pt_at(lead)
    p_soft = pt_at(lead + soft_s)
    p_soft_fin = pt_at(max(lead + soft_s, length - tail - soft_f))
    p_tail = pt_at(max(lead + soft_s, length - tail))

    wire_main = layer.wire_mm_s * edge_factor
    feed = layer.feed_mm_min
    if segment_kind == "contour":
        wire_main *= settings.contour_wire_factor
        feed *= settings.contour_feed_factor
    wire_soft = wire_main * settings.soft_wire_factor

    lines: List[str] = []
    z_safe = layer.z + settings.z_hop_mm
    if settings.include_comments:
        lines.append(f"({segment_kind.upper()} L{layer.index} X{_fmt(x0,3)} Y{_fmt(y0,3)} -> X{_fmt(x1,3)} Y{_fmt(y1,3)})")
    lines.append(f"G0 Z{_fmt(z_safe,3)}")
    lines.append(f"G0 X{_fmt(x0,3)} Y{_fmt(y0,3)}")
    lines.append(f"G1 Z{_fmt(layer.z,3)} F{_fmt(settings.work_z_feed_mm_min,1)}")
    lines.append(f"M68 E0 Q{_fmt(layer.current_ma,3)}")
    if settings.beam_preheat_s > 0:
        lines.append(f"G4 P{_fmt(settings.beam_preheat_s,3)}")
    if lead > 1e-6:
        lines.append(f"G1 X{_fmt(p_lead[0],3)} Y{_fmt(p_lead[1],3)} F{_fmt(feed,1)}")

    if settings.use_w_retract:
        lines.extend(_relative_w_move(settings.w_retract_mm, settings.w_retract_feed_mm_min))

    lines.append(f"M68 E2 Q{_fmt(wire_soft,3)}")
    if settings.wire_settle_s > 0:
        lines.append(f"G4 P{_fmt(settings.wire_settle_s,3)}")
    if math.hypot(p_soft[0]-p_lead[0], p_soft[1]-p_lead[1]) > 1e-6:
        lines.append(f"G1 X{_fmt(p_soft[0],3)} Y{_fmt(p_soft[1],3)} F{_fmt(feed,1)}")
    lines.append(f"M68 E2 Q{_fmt(wire_main,3)}")
    if math.hypot(p_soft_fin[0]-p_soft[0], p_soft_fin[1]-p_soft[1]) > 1e-6:
        lines.append(f"G1 X{_fmt(p_soft_fin[0],3)} Y{_fmt(p_soft_fin[1],3)} F{_fmt(feed,1)}")
    lines.append(f"M68 E2 Q{_fmt(wire_soft,3)}")
    if math.hypot(p_tail[0]-p_soft_fin[0], p_tail[1]-p_soft_fin[1]) > 1e-6:
        lines.append(f"G1 X{_fmt(p_tail[0],3)} Y{_fmt(p_tail[1],3)} F{_fmt(feed,1)}")
    lines.append("M68 E2 Q0.000")
    if math.hypot(x1-p_tail[0], y1-p_tail[1]) > 1e-6:
        lines.append(f"G1 X{_fmt(x1,3)} Y{_fmt(y1,3)} F{_fmt(feed,1)}")
    lines.append("M68 E0 Q0.000")
    if settings.beam_off_pause_s > 0:
        lines.append(f"G4 P{_fmt(settings.beam_off_pause_s,3)}")

    if settings.use_w_retract:
        lines.extend(_relative_w_move(-settings.w_retract_mm, settings.w_retract_feed_mm_min))
    elif settings.use_m68_speed_retract:
        lines.append(f"M68 E2 Q-{_fmt(settings.speed_retract_mm_s,3)}")
        lines.append(f"G4 P{_fmt(settings.speed_retract_time_s,3)}")
        lines.append("M68 E2 Q0.000")

    lines.append(f"G0 Z{_fmt(z_safe,3)}")
    return lines


def overlap_center_distance_factor(overlap_model: str) -> float:
    """Center-distance / bead-width ratio for multi-bead overlap.

    Literature (single material, parabolic bead):
      TOM (Ding et al. 2015) critical center distance d* = 0.738 * w
      FOM (Suryakumar et al. 2011) flat-top center distance d = 0.667 * w
    """
    m = (overlap_model or "tom").strip().lower()
    if m in ("fom", "flat", "flat_top", "0667", "0.667"):
        return 0.667
    if m in ("tom", "tangent", "0738", "0.738"):
        return 0.738
    return 0.738


def recommended_hatch_from_bead(bead_width_mm: float, overlap_model: str = "tom") -> float:
    """Recommended hatch (step-over) for a measured bead width, per overlap model."""
    bw = max(float(bead_width_mm), 0.0)
    return bw * overlap_center_distance_factor(overlap_model)


def estimate_bead_width_mm(settings: ProcessSettings, layer: "LayerInfo") -> float:
    """Approximate single-bead width from mass conservation + parabolic profile.

    In automatic E2 mode, deposition efficiency is already compensated in the
    commanded wire feed. The deposited target area is therefore the geometric
    lane area ``layer_height * hatch_spacing`` rather than that area divided by
    efficiency. For a parabolic bead A=(2/3)*w*h, so w=1.5*A/h. This remains an
    estimate; real width must be measured on a single-bead TEST.
    """
    h = max(settings.layer_height, 1e-9)
    area = settings.layer_height * settings.hatch_spacing
    return max(1.5 * area / h, 1e-9)


def _zigzag_order(segs: List[Segment], axis_y: bool) -> List[Segment]:
    """Order parallel hatch lines spatially and alternate their direction.

    Produces a serpentine (boustrophedon) sequence so the end of one line is
    adjacent to the start of the next, enabling continuous beam-on deposition.
    """
    if not segs:
        return []
    ordered = sorted(segs, key=(lambda s: s[0]) if axis_y else (lambda s: s[1]))
    out: List[Segment] = []
    for i, s in enumerate(ordered):
        x0, y0, x1, y1 = s
        if i % 2 == 1:
            out.append((x1, y1, x0, y0))  # reverse every other line
        else:
            out.append((x0, y0, x1, y1))
    return out


def _deposit_along_line_continuous(seg: Segment, layer: LayerInfo, settings: ProcessSettings,
                                   edge_factor: float, segment_kind: str) -> List[str]:
    """Wire-on deposition along one line, assuming beam is ALREADY on and Z is at
    work height. Used by the continuous layer emitter. No beam on/off, no Z-hop.
    """
    x0, y0, x1, y1 = seg
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 1e-9:
        return []
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    max_each = max(length * 0.22, 0.05)
    soft_s = min(settings.soft_start_mm, max_each)
    soft_f = min(settings.soft_finish_mm, max_each)
    if soft_s + soft_f > length * 0.8:
        scale = (length * 0.8) / max(soft_s + soft_f, 1e-9)
        soft_s *= scale; soft_f *= scale

    def pt(d):
        return (x0 + ux * d, y0 + uy * d)

    p_s = pt(soft_s)
    p_f = pt(max(soft_s, length - soft_f))
    wire_main = layer.wire_mm_s * edge_factor
    feed = layer.feed_mm_min
    if segment_kind == "contour":
        wire_main *= settings.contour_wire_factor
        feed *= settings.contour_feed_factor
    wire_soft = wire_main * settings.soft_wire_factor

    lines: List[str] = []
    if settings.include_comments:
        lines.append(f"({segment_kind.upper()} CONT L{layer.index} X{_fmt(x0,3)} Y{_fmt(y0,3)} -> X{_fmt(x1,3)} Y{_fmt(y1,3)})")
    if bool(getattr(settings, "simplify_wire_ramps", False)):
        # One setpoint for the whole line: fewer planner syncs, smoother motion.
        lines.append(f"M68 E2 Q{_fmt(wire_main,3)}")
        if settings.wire_settle_s > 0:
            lines.append(f"G4 P{_fmt(settings.wire_settle_s,3)}")
        lines.append(f"G1 X{_fmt(x1,3)} Y{_fmt(y1,3)} F{_fmt(feed,1)}")
        return lines
    lines.append(f"M68 E2 Q{_fmt(wire_soft,3)}")
    if settings.wire_settle_s > 0:
        lines.append(f"G4 P{_fmt(settings.wire_settle_s,3)}")
    if math.hypot(p_s[0]-x0, p_s[1]-y0) > 1e-6:
        lines.append(f"G1 X{_fmt(p_s[0],3)} Y{_fmt(p_s[1],3)} F{_fmt(feed,1)}")
    lines.append(f"M68 E2 Q{_fmt(wire_main,3)}")
    if math.hypot(p_f[0]-p_s[0], p_f[1]-p_s[1]) > 1e-6:
        lines.append(f"G1 X{_fmt(p_f[0],3)} Y{_fmt(p_f[1],3)} F{_fmt(feed,1)}")
    lines.append(f"M68 E2 Q{_fmt(wire_soft,3)}")
    lines.append(f"G1 X{_fmt(x1,3)} Y{_fmt(y1,3)} F{_fmt(feed,1)}")
    return lines


def _continuous_layer_gcode(ordered_segs: List[Segment], layer: LayerInfo, settings: ProcessSettings,
                            edge_factors: Dict[Tuple[float, float], float], segment_kind: str = "hatch") -> List[str]:
    """EBAM-compliant continuous (zigzag) deposition for one pass of a layer.

    The beam is switched ON once for the whole pass; adjacent lines are joined by
    short beam-on, wire-off link moves (all G1). G0 is used only to approach with
    the beam OFF and to lift at the end with the beam OFF. This matches the
    continuous-deposition practice in WAAM/EBAM literature and avoids a beam
    restrike and 2x Z-hop per hatch line.
    """
    if not ordered_segs:
        return []
    z_safe = layer.z + settings.z_hop_mm
    first = ordered_segs[0]
    lines: List[str] = []
    lines.append(f"(--- CONTINUOUS {segment_kind.upper()} PASS L{layer.index}: {len(ordered_segs)} lines, beam stays ON ---)")
    lines.append(f"G0 Z{_fmt(z_safe,3)}")
    lines.append(f"G0 X{_fmt(first[0],3)} Y{_fmt(first[1],3)}")
    lines.append(f"G1 Z{_fmt(layer.z,3)} F{_fmt(settings.work_z_feed_mm_min,1)}")
    lines.append(f"M68 E0 Q{_fmt(layer.current_ma,3)}")
    if settings.beam_preheat_s > 0:
        lines.append(f"G4 P{_fmt(settings.beam_preheat_s,3)}")
    if settings.use_w_retract:
        lines.extend(_relative_w_move(settings.w_retract_mm, settings.w_retract_feed_mm_min))

    link_feed = layer.feed_mm_min * max(settings.link_feed_factor, 1.0)
    prev_end = (first[0], first[1])
    for i, seg in enumerate(ordered_segs):
        x0, y0, x1, y1 = seg
        if i > 0 and math.hypot(x0 - prev_end[0], y0 - prev_end[1]) > 1e-6:
            # Beam-on, wire-off link to the next line (serpentine turn).
            lines.append("M68 E2 Q0.000")
            lines.append(f"G1 X{_fmt(x0,3)} Y{_fmt(y0,3)} F{_fmt(link_feed,1)}")
        factor = edge_factors.get((round(x0, 6), round(y0, 6)), 1.0)
        lines.extend(_deposit_along_line_continuous(seg, layer, settings, factor, segment_kind))
        prev_end = (x1, y1)

    lines.append("M68 E2 Q0.000")
    if settings.beam_off_pause_s > 0:
        lines.append(f"G4 P{_fmt(settings.beam_off_pause_s,3)}")
    lines.append("M68 E0 Q0.000")
    if settings.use_w_retract:
        lines.extend(_relative_w_move(-settings.w_retract_mm, settings.w_retract_feed_mm_min))
    elif settings.use_m68_speed_retract:
        lines.append(f"M68 E2 Q-{_fmt(settings.speed_retract_mm_s,3)}")
        lines.append(f"G4 P{_fmt(settings.speed_retract_time_s,3)}")
        lines.append("M68 E2 Q0.000")
    lines.append(f"G0 Z{_fmt(z_safe,3)}")
    return lines


def _edge_compensation_factors(segs: List[Segment], settings: ProcessSettings, layer: LayerInfo) -> Dict[Tuple[float, float], float]:
    axis_y = settings.direction.upper().startswith("Y")
    key_func = (lambda s: round(s[0], 6)) if axis_y else (lambda s: round(s[1], 6))
    vals = sorted({key_func(s) for s in segs})
    edge_vals = {vals[0], vals[-1]} if vals else set()
    near_vals = set()
    if len(vals) >= 4:
        near_vals = {vals[1], vals[-2]}
    edge_factor = settings.edge_wire_factor_bottom + (settings.edge_wire_factor_top - settings.edge_wire_factor_bottom) * layer.ratio
    near_factor = settings.near_edge_wire_factor_bottom + (settings.near_edge_wire_factor_top - settings.near_edge_wire_factor_bottom) * layer.ratio
    factors: Dict[Tuple[float, float], float] = {}
    for s in segs:
        v = key_func(s)
        f = 1.0
        if v in edge_vals:
            f = edge_factor
        elif v in near_vals:
            f = near_factor
        factors[(round(s[0], 6), round(s[1], 6))] = f
    return factors



def _polygons_bounds_center(polys: List[Polygon], fallback_xy: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
    """Return a stable XY centre for circular/spiral approximations of arbitrary sections."""
    try:
        u = unary_union(polys or [])
        minx, miny, maxx, maxy = u.bounds
        if math.isfinite(minx) and math.isfinite(maxx):
            return (float(minx + maxx) * 0.5, float(miny + maxy) * 0.5)
    except Exception:
        pass
    return float(fallback_xy[0]), float(fallback_xy[1])


def _xy_ring_segments_from_polygons(polys: List[Polygon], settings: ProcessSettings, layer_index: int, fallback_center: Tuple[float, float]) -> Tuple[List[Segment], Dict[str, float]]:
    """Approximate a 2D section by concentric XY circles around its section centre."""
    center = _polygons_bounds_center(polys, fallback_center)
    radii, rstats = _section_radii_from_polygons_for_rotary_c(polys, center, settings)
    npts = max(24, min(int(getattr(settings, "rotational_points_per_circle", 160)), 2048))
    segs: List[Segment] = []
    for i, r in enumerate(radii):
        start = (layer_index % 12) * (math.pi / 6.0) + i * (math.pi / 9.0)
        segs.extend(_circle_segments(center[0], center[1], r, npts, clockwise=((layer_index + i) % 2 == 0), start_angle=start))
    rstats["center_x"] = center[0]
    rstats["center_y"] = center[1]
    rstats["radii_count"] = len(radii)
    return _dedupe_segments(segs), rstats


def _xy_spiral_segments_from_polygons(polys: List[Polygon], settings: ProcessSettings, layer_index: int, fallback_center: Tuple[float, float]) -> Tuple[List[Segment], Dict[str, float]]:
    """Approximate a 2D section by an Archimedean XY spiral inside the section radius range."""
    center = _polygons_bounds_center(polys, fallback_center)
    radii, rstats = _section_radii_from_polygons_for_rotary_c(polys, center, settings)
    if not radii:
        return [], rstats
    ro = max(radii)
    ri = min(radii) if len(radii) > 1 else 0.0
    # If the section has a detected hole, use its inner radius. For solid sections start near centre.
    ri = max(0.0, float(rstats.get("inner_radius_mm", ri)))
    pitch = _effective_rotational_radial_step(settings)
    pitch = max(pitch, 0.1)
    npts_circle = max(48, min(int(getattr(settings, "rotational_points_per_circle", 160)), 2048))
    r_start = max(ri + pitch * 0.35, 0.35 if ri <= 0.05 else ri + 0.05)
    r_end = max(ro - pitch * 0.35, r_start + 0.1)
    radial_span = max(r_end - r_start, 0.1)
    turns = max(1.0, radial_span / pitch)
    samples = max(80, int(npts_circle * turns))
    theta0 = (layer_index % 16) * (math.pi / 8.0)
    clockwise = (layer_index % 2 == 0)
    pts: List[Tuple[float, float]] = []
    for i in range(samples + 1):
        u = i / max(samples, 1)
        r = r_start + radial_span * u
        a = theta0 + (-1.0 if clockwise else 1.0) * (2.0 * math.pi * turns * u)
        pts.append((center[0] + r * math.cos(a), center[1] + r * math.sin(a)))
    rstats["center_x"] = center[0]
    rstats["center_y"] = center[1]
    rstats["spiral_turns"] = turns
    return [(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) for i in range(len(pts)-1)], rstats


def _strategy_aux_time_s(settings: ProcessSettings, physical_passes: int, link_moves: int = 0, segmented_passes: int = 0) -> Dict[str, float]:
    """Estimate strategy-dependent non-deposition time: starts, Z approaches, W retracts and links."""
    physical_passes = max(0, int(physical_passes))
    link_moves = max(0, int(link_moves))
    segmented_passes = max(0, int(segmented_passes))
    z_descent_speed_mm_s = max(float(settings.work_z_feed_mm_min) / 60.0, 1e-9)
    z_ascent_speed_mm_s = max(float(settings.rapid_feed_z_mm_min) / 60.0, 1e-9)
    z_pair_s = float(settings.z_hop_mm) / z_descent_speed_mm_s + float(settings.z_hop_mm) / z_ascent_speed_mm_s
    restrike_s = float(settings.beam_preheat_s) + float(settings.wire_settle_s) + float(settings.beam_off_pause_s)
    if settings.use_w_retract and settings.w_retract_mm > 0 and settings.w_retract_feed_mm_min > 0:
        restrike_s += 2.0 * float(settings.w_retract_mm) / max(float(settings.w_retract_feed_mm_min) / 60.0, 1e-9)
    elif settings.use_m68_speed_retract:
        restrike_s += float(settings.speed_retract_time_s)
    link_s = float(settings.wire_settle_s) + 0.05
    total = physical_passes * (z_pair_s + restrike_s) + link_moves * link_s + segmented_passes * (z_pair_s + restrike_s)
    return {
        "estimated_aux_time_s": float(total),
        "estimated_segment_z_time_s": float((physical_passes + segmented_passes) * z_pair_s),
        "estimated_strategy_passes": float(physical_passes + segmented_passes),
        "estimated_strategy_link_moves": float(link_moves),
        "estimated_strategy_restrike_s": float(restrike_s),
        "estimated_strategy_z_pair_s": float(z_pair_s),
    }


def _generate_special_paths_from_polygon_provider(
    provider: Callable[[float], List[Polygon]],
    height: float,
    stats: Dict[str, Any],
    settings: ProcessSettings,
    source_label: str = "geometry",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> GenerationResult:
    """Generate unified special strategies for any polygon source: XY rings, XY spiral, or rotary C rings."""
    validate_process_settings(settings, height=height)
    path_strategy_raw = str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower()
    is_c = path_strategy_raw in ("rotary_c", "rotary_c_rings", "c_rings", "c_table", "stl_rotary_c_rings", "mesh_rotary_c_rings", "generic_rotary_c_rings")
    is_spiral = path_strategy_raw in ("spiral", "xy_spiral", "stl_xy_spiral", "mesh_xy_spiral")
    strategy = "rotary_c_rings" if is_c else ("spiral" if is_spiral else "rings")
    radial_step = _effective_rotational_radial_step(settings)
    radial_step = max(radial_step, 0.1)
    effective_settings = replace(settings, hatch_spacing=radial_step, deposition_strategy="continuous")
    n_layers_full = int(math.ceil(float(height) / float(effective_settings.layer_height)))
    if effective_settings.max_layers_to_generate and effective_settings.max_layers_to_generate > 0:
        n_layers = max(1, min(int(effective_settings.max_layers_to_generate), n_layers_full))
    else:
        n_layers = n_layers_full
    z_values = [min(i * effective_settings.layer_height, height) for i in range(n_layers)]
    z_sections = [min(z + effective_settings.layer_height * 0.5, height - 1e-4) for z in z_values]
    geo_cx = (float(stats.get("min_x", 0.0)) + float(stats.get("max_x", 0.0))) * 0.5
    geo_cy = (float(stats.get("min_y", 0.0)) + float(stats.get("max_y", 0.0))) * 0.5
    lines = _gcode_header(effective_settings, stats)
    warnings: List[str] = []
    if is_c:
        lines.append(f"(SPECIAL_PATH: {source_label}_rotary_c_rings; radial_step={_fmt(radial_step,3)} mm; B={_fmt(effective_settings.rotary_c_b_angle_deg,3)} deg; C-axis table rings from sections)")
        lines.append("(RING_ORDER: inner_to_outer; first pass uses inner/small radius and subsequent passes grow outward)")
        lines.append(f"(SPECIAL_PATH_NOTE: {source_label} sections are approximated as circular radii around section/model centre; use only for near-axisymmetric geometry)")
        lines.append(f"G0 B{_fmt(effective_settings.rotary_c_b_angle_deg,3)}")
        lines.append(f"G0 C{_fmt(effective_settings.rotary_c_start_deg,3)}")
        warnings.extend([
            f"{source_label} rotary C-table strategy: layer sections are approximated by circular radii.",
            "Use only for bodies close to revolution; for non-round sections this is an approximation, not exact geometry.",
        ])
        if abs(float(effective_settings.rotary_c_b_angle_deg)) > 1e-6:
            warnings.append("WARNING: rotary C mode is intended first with B=0. Non-zero B requires TCP/centre calibration and collision checks.")
    else:
        lines.append(f"(SPECIAL_PATH: {source_label}_xy_{strategy}; radial_step={_fmt(radial_step,3)} mm; true XY circular/spiral section paths)")
        if strategy == "rings":
            lines.append("(RING_ORDER: inner_to_outer; XY rings start from inner/small radius and grow outward)")
        warnings.extend([
            f"{source_label} XY {strategy} strategy: section is approximated by concentric circular/spiral paths around its centre.",
            "Best for round/axisymmetric geometry. For non-round geometry use normal snake/parallel paths for exact section filling.",
        ])
    if effective_settings.use_w_retract and effective_settings.w_retract_mm > 0:
        lines.append("(Initial W retract calibration)")
        lines.append("G10 L20 P0 W0")
        lines.extend(_relative_w_move(-effective_settings.w_retract_mm, effective_settings.w_retract_feed_mm_min))
    layer_infos: List[LayerInfo] = []
    max_roundness_pct = 0.0
    skipped_layers = 0
    total_active_len = 0.0
    total_segments = 0
    max_segments_layer = 0
    max_c_required = 0.0
    max_c_used = 0.0
    min_radius_used = float("inf")
    max_radius_used = 0.0
    feed_limited_count = 0
    thermal_dwell_count = 0
    thermal_dwell_total_s = 0.0
    small_radius_count = 0
    for idx, (z, zsec) in enumerate(zip(z_values, z_sections), start=1):
        if progress_callback and ((idx == 1) or (idx == n_layers) or (idx % max(1, int(effective_settings.progress_update_every_layers)) == 0)):
            try:
                progress_callback(idx, n_layers, f"{source_label}/{strategy}")
            except Exception:
                pass
        polys = provider(zsec)
        if not polys:
            skipped_layers += 1
            warnings.append(f"Layer {idx}: no section at Z={zsec:.3f}; skipped")
            continue
        center = _polygons_bounds_center(polys, (geo_cx, geo_cy))
        radii, rstats = _section_radii_from_polygons_for_rotary_c(polys, center, effective_settings)
        max_roundness_pct = max(max_roundness_pct, float(rstats.get("roundness_error_pct", 0.0)))
        if is_c:
            if not radii:
                skipped_layers += 1
                warnings.append(f"Layer {idx}: no C-radii at Z={zsec:.3f}; skipped")
                continue
            lines.append(f"(--- LAYER {idx}/{n_layers} Z={_fmt(z,3)} {source_label.upper()}_ROTARY_C rings={len(radii)} outerR={_fmt(rstats.get('outer_radius_mm',0.0),3)} innerR={_fmt(rstats.get('inner_radius_mm',0.0),3)} ---)")
            for j, radius in enumerate(radii, start=1):
                base = layer_parameters(z, height, effective_settings)
                required_c = rotary_c_speed_deg_min(base.feed_mm_min, radius)
                max_c_required = max(max_c_required, required_c)
                # v4.2.9.31: CV radial compensation now also covers the special
                # cup/balloon STL path (his Flange1 route) - previously this loop
                # ignored rotary_c_constant_velocity entirely.
                c_feed, actual_feed, pitch_factor = _rotary_c_ring_kinematics(
                    effective_settings, radius, base.feed_mm_min)
                if radius < float(effective_settings.rotary_c_min_radius_mm):
                    small_radius_count += 1
                if (not bool(getattr(effective_settings, "rotary_c_constant_velocity", False))
                        and required_c > float(effective_settings.rotary_c_max_deg_min) + 1e-9):
                    if bool(getattr(effective_settings, "rotary_c_auto_limit_feed", True)):
                        feed_limited_count += 1
                    else:
                        warnings.append(f"WARNING: layer {idx} radius {radius:.3f} requires C feed {required_c:.1f} deg/min above limit {effective_settings.rotary_c_max_deg_min:.1f}")
                layer = _layer_parameters_with_feed(z, height, effective_settings, actual_feed)
                if pitch_factor < 0.999:
                    layer.wire_mm_s = float(layer.wire_mm_s) * pitch_factor
                layer.index = idx
                layer.segments_count = 1
                layer.path_length_mm = 2.0 * math.pi * float(radius)
                lines.extend(_rotary_c_pass_gcode(radius, layer, effective_settings, j, len(radii), c_feed))
                layer_infos.append(layer)
                total_active_len += layer.path_length_mm
                total_segments += 1
                max_segments_layer = max(max_segments_layer, 1)
                min_radius_used = min(min_radius_used, float(radius))
                max_radius_used = max(max_radius_used, float(radius))
                max_c_used = max(max_c_used, c_feed)
            if layer_infos and layer_infos[-1].layer_pause_s > 0:
                lines.append(f"G4 P{_fmt(layer_infos[-1].layer_pause_s,3)} (layer thermal stabilization)")
            _tdw = _thermal_dwell_for_layer(effective_settings, sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos[-len(radii):]))
            if _tdw > 0:
                lines.append(f"G4 P{_fmt(_tdw,1)} (THERMAL_DWELL L{idx}: adaptive - layer cycle below minimum)")
                thermal_dwell_count += 1
                thermal_dwell_total_s += _tdw
        else:
            layer = layer_parameters(z, height, effective_settings)
            layer.index = idx
            if is_spiral:
                segs, _ = _xy_spiral_segments_from_polygons(polys, effective_settings, idx, center)
            else:
                segs, _ = _xy_ring_segments_from_polygons(polys, effective_settings, idx, center)
            segs = _dedupe_segments(segs)
            if not segs:
                skipped_layers += 1
                warnings.append(f"Layer {idx}: no XY {strategy} path at Z={zsec:.3f}; skipped")
                continue
            path_len = sum(math.hypot(s[2]-s[0], s[3]-s[1]) for s in segs)
            layer.segments_count = len(segs)
            layer.path_length_mm = path_len
            lines.append(f"(--- LAYER {idx}/{n_layers} Z={_fmt(layer.z,3)} XY_{strategy.upper()} I={_fmt(layer.current_ma,3)} F={_fmt(layer.feed_mm_min,1)} WIRE={_fmt(layer.wire_mm_s,3)} SEG={len(segs)} ---)")
            lines.extend(_continuous_layer_gcode(segs, layer, effective_settings, {}, f"xy_{strategy}"))
            if layer.layer_pause_s > 0:
                lines.append(f"G4 P{_fmt(layer.layer_pause_s,3)} (layer thermal stabilization)")
            _tdw = _thermal_dwell_for_layer(effective_settings, path_len / max(float(layer.travel_speed_mm_s), 1e-9))
            if _tdw > 0:
                lines.append(f"G4 P{_fmt(_tdw,1)} (THERMAL_DWELL L{idx}: adaptive - layer cycle below minimum)")
                thermal_dwell_count += 1
                thermal_dwell_total_s += _tdw
            layer_infos.append(layer)
            total_active_len += path_len
            total_segments += len(segs)
            max_segments_layer = max(max_segments_layer, len(segs))
    if not layer_infos:
        raise RuntimeError(f"No {source_label} special path layers were generated. Check geometry, sectioning, radial step and height.")
    if thermal_dwell_count:
        warnings.append(f"ТЕПЛОВЫЕ ВЫДЕРЖКИ: {thermal_dwell_count} шт, суммарно {thermal_dwell_total_s/60.0:.1f} мин "
                        f"(мин. цикл слоя {float(getattr(effective_settings, 'thermal_min_layer_cycle_min', 0.0)):.1f} мин, пол {float(getattr(effective_settings, 'thermal_min_dwell_s', 0.0)):.0f} с); время включено в G-code (G4).")
    if feed_limited_count:
        warnings.append(f"WARNING: {feed_limited_count} rotary C passes were feed-limited by C max speed; E0/E2 were recalculated for the reduced linear speed.")
    if small_radius_count:
        warnings.append(f"WARNING: {small_radius_count} rotary C passes use radius below configured warning radius {effective_settings.rotary_c_min_radius_mm:.3f} mm.")
    if max_roundness_pct > 8.0:
        warnings.append(f"WARNING: sections are not very round (estimated spread up to {max_roundness_pct:.1f}%). Circular/spiral approximation may not match the exact shape.")
    footer_settings = replace(effective_settings, safe_z_final_mm=max(effective_settings.safe_z_final_mm, height + effective_settings.z_hop_mm + 5.0))
    lines.extend(_gcode_footer(footer_settings))
    gcode = "\n".join(lines) + "\n"
    audit = audit_gcode(gcode, effective_settings)
    out_stats = dict(stats)
    out_stats.update({
        "app_version": APP_VERSION,
        "source_type": f"{source_label} special path",
        "rotational_path_strategy": path_strategy_raw,
        "special_path_strategy": strategy,
        "special_path_source_label": source_label,
        "roundness_error_max_pct": float(max_roundness_pct),
        "special_path_skipped_layers": int(skipped_layers),
        "rotational_radial_step_mm": radial_step,
        "rotational_points_per_circle": int(getattr(effective_settings, "rotational_points_per_circle", 160)),
        "layers_total": len(set(li.index for li in layer_infos)),
        "passes_total": len(layer_infos),
        "layers_requested": n_layers,
        "layers_full_model": n_layers_full,
        "is_test_truncated": bool(n_layers < n_layers_full),
        "segments_total": total_segments,
        "contour_segments_total": 0,
        "contour_path_length_mm": 0.0,
        "max_segments_per_layer": max_segments_layer,
        "active_path_length_mm": total_active_len,
        "active_path_length_m": total_active_len / 1000.0,
        "estimated_active_time_s": sum(li.path_length_mm / max(li.travel_speed_mm_s, 1e-9) for li in layer_infos),
        "estimated_wire_length_mm": sum((li.path_length_mm / max(li.travel_speed_mm_s, 1e-9)) * li.wire_mm_s for li in layer_infos),
        "wire_min_calculated_mm_s": min((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_max_calculated_mm_s": max((li.wire_mm_s for li in layer_infos), default=0.0),
        "feed_min_mm_min": min((li.feed_mm_min for li in layer_infos), default=0.0),
        "feed_max_mm_min": max((li.feed_mm_min for li in layer_infos), default=0.0),
        "process_wire_warning_limit_mm_s": effective_settings.wire_max_mm_s,
        "wire_above_control_limit": any(li.wire_mm_s > effective_settings.wire_max_mm_s for li in layer_infos),
        "current_min_ma": effective_settings.current_min_ma,
        "current_low_warning_ma": effective_settings.current_low_warning_ma,
        "current_limit_ma": effective_settings.current_max_ma,
        "beam_current_mode": str(getattr(effective_settings, "beam_current_mode", "energy")),
        "beam_current_bottom_ma": float(getattr(effective_settings, "beam_current_bottom_ma", 0.0)),
        "beam_current_top_ma": float(getattr(effective_settings, "beam_current_top_ma", 0.0)),
        "current_required_min_ma": min((li.current_required_ma for li in layer_infos), default=0.0),
        "current_required_max_ma": max((li.current_required_ma for li in layer_infos), default=0.0),
        "current_clipped_by_min": any(li.current_clipped_by_min for li in layer_infos),
        "current_clipped_by_max": any(li.current_clipped_by_max for li in layer_infos),
        "energy_target_min_j_mm": min((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_target_max_j_mm": max((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_actual_min_j_mm": min((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_actual_max_j_mm": max((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_volume_min_j_mm3": min((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "energy_volume_max_j_mm3": max((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "bormash_profile_enabled": bool(is_bormash_profile(effective_settings)),
        "gcode_lines": len(lines),
        "gcode_size_mb": len(gcode.encode("utf-8")) / (1024.0 * 1024.0),
    })
    if is_c:
        out_stats.update({
            "rotary_c_center_x_mm": float(effective_settings.rotary_c_center_x_mm),
            "rotary_c_center_y_mm": float(effective_settings.rotary_c_center_y_mm),
            "rotary_c_direction": str(effective_settings.rotary_c_direction),
            "rotary_c_start_deg": float(effective_settings.rotary_c_start_deg),
            "rotary_c_b_angle_deg": float(effective_settings.rotary_c_b_angle_deg),
            "rotary_c_max_deg_min": float(effective_settings.rotary_c_max_deg_min),
            "rotary_c_min_radius_mm": float(effective_settings.rotary_c_min_radius_mm),
            "rotary_c_feed_limited_count": int(feed_limited_count),
            "rotary_c_small_radius_count": int(small_radius_count),
            "rotary_c_required_max_deg_min": float(max_c_required),
            "rotary_c_used_max_deg_min": float(max_c_used),
            "rotary_c_min_radius_used_mm": 0.0 if min_radius_used == float("inf") else float(min_radius_used),
            "rotary_c_max_radius_used_mm": float(max_radius_used),
            "rotary_c_warning_radius_below_mm": float(effective_settings.rotary_c_min_radius_mm),
            "rotary_c_real_radius_limit_note": "C feed limiting uses each real ring radius; warning radius is only a warning threshold",
            "min_x": float(effective_settings.rotary_c_center_x_mm) + (0.0 if min_radius_used == float("inf") else float(min_radius_used)),
            "max_x": float(effective_settings.rotary_c_center_x_mm) + float(max_radius_used),
            "min_y": float(effective_settings.rotary_c_center_y_mm),
            "max_y": float(effective_settings.rotary_c_center_y_mm),
        })
    out_stats["estimated_active_time_h"] = out_stats["estimated_active_time_s"] / 3600.0
    out_stats["estimated_pause_time_s"] = sum(li.layer_pause_s for li in layer_infos)
    if is_c:
        timing = _strategy_aux_time_s(effective_settings, physical_passes=len(layer_infos), link_moves=0)
    elif is_spiral:
        timing = _strategy_aux_time_s(effective_settings, physical_passes=len(set(li.index for li in layer_infos)), link_moves=0)
    else:
        timing = _strategy_aux_time_s(effective_settings, physical_passes=len(set(li.index for li in layer_infos)), link_moves=total_segments)
    out_stats.update(timing)
    out_stats["estimated_total_time_s"] = (out_stats["estimated_active_time_s"] + out_stats["estimated_pause_time_s"] + out_stats["estimated_aux_time_s"]) * 1.12
    out_stats["estimated_total_time_h"] = out_stats["estimated_total_time_s"] / 3600.0
    out_stats["target_total_time_s"] = float(getattr(effective_settings, "target_total_time_s", 0.0) or 0.0)
    out_stats["target_time_mode"] = getattr(effective_settings, "target_time_mode", "off") if out_stats["target_total_time_s"] else "off"
    out_stats["estimated_wire_length_m"] = out_stats["estimated_wire_length_mm"] / 1000.0
    volume_wire_mm3 = out_stats["estimated_wire_length_mm"] * effective_settings.wire_area_mm2()
    out_stats["estimated_wire_mass_kg"] = volume_wire_mm3 / 1000.0 * effective_settings.density_g_cm3 / 1000.0
    layer_csv = layer_table_csv(layer_infos)
    audit_text = audit_report(effective_settings, out_stats, audit, warnings, layer_infos)
    if n_layers < n_layers_full:
        audit_text += f"\n\nTEST / TRUNCATED FILE:\n  Generated first {n_layers} of {n_layers_full} model layers only. Do not use as full-part program.\n"
    if progress_callback:
        try:
            progress_callback(n_layers, n_layers, "done")
        except Exception:
            pass
    return GenerationResult(gcode=gcode, layer_csv=layer_csv, audit_text=audit_text, stats=out_stats)

def _generate_with_polygon_provider(provider: Callable[[float], List[Polygon]], height: float, stats: Dict[str, Any], settings: ProcessSettings, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    validate_process_settings(settings, height=height)

    # EBAM bead-overlap step-over: if a measured bead width is given, set hatch
    # spacing from the overlap model (TOM 0.738w / FOM 0.667w) instead of an
    # arbitrary value. This is the WAAM/EBAM standard step-over criterion.
    bead_overlap_note = None
    thermal_dwell_count = 0
    thermal_dwell_total_s = 0.0
    if getattr(settings, "auto_hatch_from_bead", False) and float(getattr(settings, "bead_width_mm", 0.0)) > 0:
        rec = recommended_hatch_from_bead(settings.bead_width_mm, settings.overlap_model)
        if rec > 1e-6:
            bead_overlap_note = (f"Hatch set from bead width {settings.bead_width_mm:.3f} mm via "
                                 f"{settings.overlap_model.upper()} ({overlap_center_distance_factor(settings.overlap_model):.3f}w) = {rec:.3f} mm.")
            settings = replace(settings, hatch_spacing=rec)
    n_layers_full = int(math.ceil(float(height) / float(settings.layer_height)))
    if settings.max_layers_to_generate and settings.max_layers_to_generate > 0:
        n_layers = max(1, min(int(settings.max_layers_to_generate), n_layers_full))
    else:
        n_layers = n_layers_full
    z_values = [min(i * settings.layer_height, height) for i in range(n_layers)]
    z_sections = [min(z + settings.layer_height * 0.5, height - 1e-4) for z in z_values]
    if progress_callback:
        try:
            progress_callback(0, n_layers, "start")
        except Exception:
            pass

    lines = _gcode_header(settings, stats)
    if settings.use_w_retract and settings.w_retract_mm > 0:
        lines.append("(Initial W retract calibration)")
        lines.append("G10 L20 P0 W0")
        lines.extend(_relative_w_move(-settings.w_retract_mm, settings.w_retract_feed_mm_min))

    layer_infos: List[LayerInfo] = []
    total_active_len = 0.0
    total_contour_len = 0.0
    total_segments = 0
    total_contours = 0
    max_segments_layer = 0
    warning_messages: List[str] = []
    if thermal_dwell_count:
        warning_messages.append(f"ТЕПЛОВЫЕ ВЫДЕРЖКИ: {thermal_dwell_count} шт, суммарно {thermal_dwell_total_s/60.0:.1f} мин "
                 f"(мин. цикл слоя {float(getattr(settings, 'thermal_min_layer_cycle_min', 0.0)):.1f} мин); время включено в G-code (G4).")
    if bead_overlap_note:
        warning_messages.append(bead_overlap_note)
    _strategy = str(getattr(settings, "deposition_strategy", "continuous")).strip().lower()
    warning_messages.append(
        f"Deposition strategy: {_strategy}"
        + (" (EBAM continuous beam-on zigzag)" if _strategy == "continuous" else " (legacy beam-off per line)")
        + (f"; alternate orthogonal layers: ON" if getattr(settings, "alternate_layer_rotation", False) else "")
        + ". Process windows are calculated starting points and require a single-bead/TEST qualification on the real machine."
    )

    for idx, (z, zsec) in enumerate(zip(z_values, z_sections), start=1):
        if progress_callback and ((idx == 1) or (idx == n_layers) or (idx % max(1, int(settings.progress_update_every_layers)) == 0)):
            try:
                progress_callback(idx, n_layers, "slicing/gcode")
            except Exception:
                pass
        layer = layer_parameters(z, height, settings)
        layer.index = idx
        polys = provider(zsec)
        if not polys:
            warning_messages.append(f"Layer {idx}: no section/polygon at Z={zsec:.3f}; skipped")
            continue
        # Alternate orthogonal strategy: rotate the raster ~90 deg on odd layers
        # to improve geometric uniformity and reduce anisotropy (literature).
        layer_settings = settings
        if getattr(settings, "alternate_layer_rotation", False) and (idx % 2 == 0):
            rot_map = {"Y-": "X-", "Y+": "X+", "X-": "Y-", "X+": "Y+"}
            layer_settings = replace(settings, direction=rot_map.get(settings.direction.upper(), settings.direction))
        segs = _dedupe_segments(_hatch_segments_for_polygons(polys, layer_settings, idx))
        conts: List[Segment] = []
        if settings.contour_passes > 0 and settings.contour_every_n_layers > 0 and (idx - 1) % settings.contour_every_n_layers == 0:
            conts = _dedupe_segments(_contour_segments_for_polygons(polys, layer_settings, idx))

        # v2.7: adaptive recovery for thin STL layers.
        # A cup/bowl often has upper ring-like sections that become thinner than
        # the normal hatch spacing and edge offset. Older versions skipped such
        # layers. Here we retry with a smaller edge offset/spacing, and if hatch
        # still cannot be created we force a contour-only toolpath for that layer.
        if settings.adaptive_thin_wall and not segs:
            thin_settings = replace(
                settings,
                edge_offset=max(0.0, min(settings.edge_offset * settings.thin_wall_edge_offset_factor, settings.edge_offset, 0.35)),
                hatch_spacing=max(0.35, min(settings.hatch_spacing * settings.thin_wall_hatch_spacing_factor, settings.hatch_spacing)),
                min_segment_length=max(0.25, min(settings.thin_wall_min_segment_length, settings.min_segment_length)),
            )
            segs_retry = _dedupe_segments(_hatch_segments_for_polygons(polys, thin_settings, idx))
            if segs_retry:
                segs = segs_retry
                if settings.adaptive_wire_correction and settings.hatch_spacing > 1e-9:
                    wire_factor = max(0.25, min(1.0, thin_settings.hatch_spacing / settings.hatch_spacing))
                    layer = replace(layer, wire_mm_s=max(0.0, layer.wire_mm_s * wire_factor))
                warning_messages.append(f"Layer {idx}: adaptive thin-wall hatch used; wire corrected to {layer.wire_mm_s:.3f} mm/s")

        if settings.adaptive_thin_wall and settings.force_contour_on_empty_layers and not segs and not conts:
            contour_settings = replace(
                settings,
                contour_passes=max(1, settings.contour_passes),
                edge_offset=max(0.0, min(settings.edge_offset * settings.thin_wall_edge_offset_factor, settings.edge_offset, 0.35)),
                min_segment_length=max(0.25, min(settings.thin_wall_min_segment_length, settings.min_segment_length)),
            )
            conts_retry = _dedupe_segments(_contour_segments_for_polygons(polys, contour_settings, idx))
            if conts_retry:
                conts = conts_retry
                layer = replace(layer, wire_mm_s=max(0.0, layer.wire_mm_s * settings.thin_wall_wire_factor))
                warning_messages.append(f"Layer {idx}: contour-only thin-wall fallback used; wire corrected to {layer.wire_mm_s:.3f} mm/s")

        layer.segments_count = len(segs)
        layer.contour_segments_count = len(conts)
        max_segments_layer = max(max_segments_layer, len(segs) + len(conts))
        if not segs and not conts:
            warning_messages.append(f"Layer {idx}: no toolpath segments; shape may be too thin for spacing/offset")
            continue

        lines.append(f"(--- LAYER {idx}/{n_layers} Z={_fmt(layer.z,3)} I={_fmt(layer.current_ma,3)} F={_fmt(layer.feed_mm_min,1)}mm/min WIRE={_fmt(layer.wire_mm_s,3)}mm/s HATCH={len(segs)} CONTOUR={len(conts)} ---)")
        path_len = 0.0
        contour_len = 0.0
        factors = _edge_compensation_factors(segs, layer_settings, layer) if segs else {}
        axis_y = layer_settings.direction.upper().startswith("Y")
        continuous = str(getattr(settings, "deposition_strategy", "continuous")).strip().lower() == "continuous"

        def emit_hatch():
            nonlocal path_len
            if continuous:
                ordered = _zigzag_order(segs, axis_y)
                for seg in ordered:
                    path_len += math.hypot(seg[2]-seg[0], seg[3]-seg[1])
                lines.extend(_continuous_layer_gcode(ordered, layer, settings, factors, "hatch"))
            else:
                for seg in segs:
                    factor = factors.get((round(seg[0], 6), round(seg[1], 6)), 1.0)
                    path_len += math.hypot(seg[2]-seg[0], seg[3]-seg[1])
                    lines.extend(_beam_wire_segment_gcode(seg, layer, settings, factor, "hatch"))

        def emit_contour():
            nonlocal contour_len
            if not conts:
                return
            if continuous:
                for seg in conts:
                    contour_len += math.hypot(seg[2]-seg[0], seg[3]-seg[1])
                lines.extend(_continuous_layer_gcode(conts, layer, settings, {}, "contour"))
            else:
                for seg in conts:
                    contour_len += math.hypot(seg[2]-seg[0], seg[3]-seg[1])
                    lines.extend(_beam_wire_segment_gcode(seg, layer, settings, 1.0, "contour"))

        if settings.contour_first:
            emit_contour(); emit_hatch()
        else:
            emit_hatch(); emit_contour()

        if layer.layer_pause_s > 0:
            lines.append(f"G4 P{_fmt(layer.layer_pause_s,3)} (layer thermal stabilization)")
        _tdw = _thermal_dwell_for_layer(settings, (path_len + contour_len) / max(float(layer.travel_speed_mm_s), 1e-9))
        if _tdw > 0:
            lines.append(f"G4 P{_fmt(_tdw,1)} (THERMAL_DWELL L{idx}: adaptive - layer cycle below minimum)")
            thermal_dwell_count += 1
            thermal_dwell_total_s += _tdw
        layer.path_length_mm = path_len
        layer.contour_length_mm = contour_len
        total_active_len += path_len + contour_len
        total_contour_len += contour_len
        total_segments += len(segs)
        total_contours += len(conts)
        layer_infos.append(layer)

    if not layer_infos:
        raise RuntimeError("No toolpath layers were generated from the prepared geometry. For STL: check orientation, edge offset, hatch spacing, adaptive thin-wall settings and offline STL fallback. For CSV/DXF: check that the file contains a closed X,Y contour, not layers.csv/audit/report data. If this is a diagnostic STL run only, enable XY projection fallback.")
    layer_fraction = len(layer_infos) / max(n_layers, 1)
    if layer_fraction < settings.minimum_generated_layer_fraction:
        warning_messages.append(f"Only {len(layer_infos)}/{n_layers} layers generated ({layer_fraction*100:.1f}%). Geometry may be too thin or STL slicing still unstable.")

    footer_settings = replace(settings, safe_z_final_mm=max(settings.safe_z_final_mm, height + settings.z_hop_mm + 5.0))
    lines.extend(_gcode_footer(footer_settings))
    gcode = "\n".join(lines) + "\n"
    audit = audit_gcode(gcode, settings)

    stats.update({
        "app_version": APP_VERSION,
        "layers_total": len(layer_infos),
        "layers_requested": n_layers,
        "layers_full_model": n_layers_full,
        "is_test_truncated": bool(n_layers < n_layers_full),
        "layer_fraction": len(layer_infos) / max(n_layers, 1),
        "segments_total": total_segments,
        "contour_segments_total": total_contours,
        "contour_path_length_mm": total_contour_len,
        "max_segments_per_layer": max_segments_layer,
        "active_path_length_mm": total_active_len,
        "active_path_length_m": total_active_len / 1000.0,
        "estimated_active_time_s": sum(((li.path_length_mm / max(li.travel_speed_mm_s, 1e-9)) + (li.contour_length_mm / max(li.travel_speed_mm_s * max(settings.contour_feed_factor, 1e-9), 1e-9))) for li in layer_infos),
        "estimated_wire_length_mm": sum(((li.path_length_mm / max(li.travel_speed_mm_s,1e-9)) * li.wire_mm_s) + ((li.contour_length_mm / max(li.travel_speed_mm_s * max(settings.contour_feed_factor, 1e-9),1e-9)) * li.wire_mm_s * settings.contour_wire_factor) for li in layer_infos),
        "wire_min_calculated_mm_s": min((li.wire_mm_s for li in layer_infos), default=0.0),
        "wire_max_calculated_mm_s": max((li.wire_mm_s for li in layer_infos), default=0.0),
        "feed_min_mm_min": min((li.feed_mm_min for li in layer_infos), default=0.0),
        "feed_max_mm_min": max((li.feed_mm_min for li in layer_infos), default=0.0),
        "process_wire_warning_limit_mm_s": settings.wire_max_mm_s,
        "wire_above_control_limit": any(li.wire_mm_s > settings.wire_max_mm_s for li in layer_infos),
        "current_min_ma": settings.current_min_ma,
        "current_low_warning_ma": settings.current_low_warning_ma,
        "current_limit_ma": settings.current_max_ma,
        "beam_current_mode": str(getattr(settings, "beam_current_mode", "energy")),
        "beam_current_bottom_ma": float(getattr(settings, "beam_current_bottom_ma", 0.0)),
        "beam_current_top_ma": float(getattr(settings, "beam_current_top_ma", 0.0)),
        "current_required_min_ma": min((li.current_required_ma for li in layer_infos), default=0.0),
        "current_required_max_ma": max((li.current_required_ma for li in layer_infos), default=0.0),
        "current_clipped_by_min": any(li.current_clipped_by_min for li in layer_infos),
        "current_clipped_by_max": any(li.current_clipped_by_max for li in layer_infos),
        "current_clipped_by_limit": any(li.current_clipped_by_max for li in layer_infos),
        "current_below_low_warning": any((li.current_required_ma < settings.current_low_warning_ma - 1e-9) for li in layer_infos) if settings.current_low_warning_ma > 0 else False,
        "energy_target_min_j_mm": min((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_target_max_j_mm": max((li.energy_j_mm for li in layer_infos), default=0.0),
        "energy_actual_min_j_mm": min((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_actual_max_j_mm": max((li.energy_actual_j_mm for li in layer_infos), default=0.0),
        "energy_volume_min_j_mm3": min((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "energy_volume_max_j_mm3": max((li.energy_j_mm3 for li in layer_infos), default=0.0),
        "bormash_profile_enabled": bool(is_bormash_profile(settings)),
        "gcode_lines": len(lines),
        "gcode_size_mb": len(gcode.encode("utf-8")) / (1024.0 * 1024.0),
        "deposition_strategy": str(getattr(settings, "deposition_strategy", "continuous")),
        "alternate_layer_rotation": bool(getattr(settings, "alternate_layer_rotation", False)),
        "hatch_spacing_effective_mm": settings.hatch_spacing,
        "bead_width_mm": float(getattr(settings, "bead_width_mm", 0.0)),
        "overlap_model": str(getattr(settings, "overlap_model", "tom")),
    })
    stats["estimated_active_time_h"] = stats["estimated_active_time_s"] / 3600.0
    segment_count_for_timing = max(int(stats.get("segments_total", 0)) + int(stats.get("contour_segments_total", 0)), 0)
    layer_pause_time_s = sum(li.layer_pause_s for li in layer_infos)
    per_segment_delay_s = settings.beam_preheat_s + settings.wire_settle_s + settings.beam_off_pause_s
    if settings.use_w_retract and settings.w_retract_mm > 0 and settings.w_retract_feed_mm_min > 0:
        per_segment_delay_s += 2.0 * settings.w_retract_mm / (settings.w_retract_feed_mm_min / 60.0)
    elif settings.use_m68_speed_retract:
        per_segment_delay_s += settings.speed_retract_time_s
    # Each emitted segment does a controlled Z descent (work feed) to the work
    # plane and a rapid Z lift back to the safe plane. With many short segments
    # this Z travel dominates and was previously hidden inside the service
    # factor only, badly under-counting total time for real parts (cup/bowl).
    z_descent_speed_mm_s = max(settings.work_z_feed_mm_min / 60.0, 1e-9)
    z_ascent_speed_mm_s = max(settings.rapid_feed_z_mm_min / 60.0, 1e-9)
    per_segment_z_time_s = settings.z_hop_mm / z_descent_speed_mm_s + settings.z_hop_mm / z_ascent_speed_mm_s
    continuous_mode = str(getattr(settings, "deposition_strategy", "continuous")).strip().lower() == "continuous"
    if continuous_mode:
        # Beam stays on across the pass: one Z-hop per layer-pass, plus a small
        # per-segment wire-ramp/link overhead instead of a full restrike cycle.
        passes = len(layer_infos) + (sum(1 for li in layer_infos if li.contour_segments_count > 0))
        per_segment_link_s = settings.wire_settle_s + 0.05
        per_segment_delay_s = per_segment_link_s
        z_time_total_s = passes * per_segment_z_time_s
    else:
        per_segment_delay_s += per_segment_z_time_s
        z_time_total_s = segment_count_for_timing * per_segment_z_time_s
    service_factor = 1.12  # acceleration, XY repositioning, controller latency, operator margin
    stats["estimated_pause_time_s"] = layer_pause_time_s
    stats["estimated_aux_time_s"] = segment_count_for_timing * per_segment_delay_s + (z_time_total_s if continuous_mode else 0.0)
    stats["estimated_segment_z_time_s"] = z_time_total_s
    stats["estimated_total_time_s"] = (stats["estimated_active_time_s"] + stats["estimated_pause_time_s"] + stats["estimated_aux_time_s"]) * service_factor
    stats["estimated_total_time_h"] = stats["estimated_total_time_s"] / 3600.0
    if getattr(settings, "target_total_time_s", 0.0) and settings.target_total_time_s > 0:
        stats["target_total_time_s"] = float(settings.target_total_time_s)
        stats["target_total_time_h"] = float(settings.target_total_time_s) / 3600.0
        stats["target_time_mode"] = getattr(settings, "target_time_mode", "unknown")
        stats["target_time_error_s"] = float(stats["estimated_total_time_s"]) - float(settings.target_total_time_s)
        stats["target_time_error_pct"] = 100.0 * stats["target_time_error_s"] / max(float(settings.target_total_time_s), 1e-9)
        stats["target_time_within_15pct"] = abs(stats["target_time_error_pct"]) <= 15.0
    else:
        stats["target_total_time_s"] = 0.0
        stats["target_time_mode"] = "off"
    stats["estimated_wire_length_m"] = stats["estimated_wire_length_mm"] / 1000.0
    volume_wire_mm3 = stats["estimated_wire_length_mm"] * settings.wire_area_mm2()
    stats["estimated_wire_mass_kg"] = volume_wire_mm3 / 1000.0 * settings.density_g_cm3 / 1000.0

    layer_csv = layer_table_csv(layer_infos)
    audit_text = audit_report(settings, stats, audit, warning_messages, layer_infos)
    if n_layers < n_layers_full:
        audit_text += f"\n\nTEST / TRUNCATED FILE:\n  Generated first {n_layers} of {n_layers_full} model layers only. Do not use as full-part program.\n"
    if progress_callback:
        try:
            progress_callback(n_layers, n_layers, "done")
        except Exception:
            pass
    return GenerationResult(gcode=gcode, layer_csv=layer_csv, audit_text=audit_text, stats=stats)



def _xy_projection_polygons_from_mesh(mesh: trimesh.Trimesh) -> List[Polygon]:
    """Last-resort 2.5D XY projection of mesh faces.

    Used only when true layer slicing fails entirely. For complex hollow STL this
    is not as geometrically accurate as slicing, so generated audit carries a
    warning. It is still better than producing an empty G-code file silently.
    """
    try:
        tris = np.asarray(mesh.vertices, dtype=float)[np.asarray(mesh.faces, dtype=np.int64)]
    except Exception:
        return []
    polys = []
    for t in tris:
        pts = [(float(t[0,0]), float(t[0,1])), (float(t[1,0]), float(t[1,1])), (float(t[2,0]), float(t[2,1]))]
        try:
            p = Polygon(pts)
            if p.is_valid and p.area > 1e-6:
                polys.append(p)
        except Exception:
            pass
    if not polys:
        return []
    try:
        u = unary_union(polys)
        if isinstance(u, Polygon):
            return [u]
        if isinstance(u, MultiPolygon):
            return [g for g in u.geoms if g.area > 1e-6]
    except Exception:
        pass
    return _clean_polygons(polys)

def generate_from_mesh(mesh: trimesh.Trimesh, settings: Optional[ProcessSettings] = None, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    if settings is None:
        settings = ProcessSettings()
    mesh_n = normalize_mesh(mesh, settings)
    stats = mesh_summary(mesh_n)
    height = stats["size_z"]
    path_strategy = str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower()
    probe = settings.layer_height * settings.section_probe_fraction if settings.adaptive_section_probe else 0.0
    if path_strategy in ("stl_rotary_c_rings", "mesh_rotary_c_rings", "rotary_c", "rotary_c_rings", "c_rings", "c_table", "rings", "xy_rings", "spiral", "xy_spiral"):
        return _generate_special_paths_from_polygon_provider(lambda z: _section_polygons_at_z(mesh_n, z, probe_radius=probe), height, stats, settings, source_label="STL", progress_callback=progress_callback)

    try:
        return _generate_with_polygon_provider(lambda z: _section_polygons_at_z(mesh_n, z, probe_radius=probe), height, stats, settings, progress_callback=progress_callback)
    except RuntimeError as exc:
        # v3.1 last-resort protection against offline STL polygonization failures:
        # do not return a 24-line empty file; either generate a conservative
        # 2.5D projection fallback or raise a clear error.
        msg = str(exc)
        if (not settings.projection_fallback_if_empty) or ("No toolpath layers" not in msg):
            raise
        proj_polys = _xy_projection_polygons_from_mesh(mesh_n)
        if not proj_polys:
            raise
        fallback_settings = replace(
            settings,
            contour_passes=max(1, settings.contour_passes),
            adaptive_thin_wall=True,
            force_contour_on_empty_layers=True,
            edge_offset=max(0.0, min(settings.edge_offset, 0.35)),
            min_segment_length=max(0.25, min(settings.min_segment_length, 1.0)),
        )
        stats2 = polygon_summary(proj_polys, height)
        stats2.update({
            "source_type": "STL XY projection fallback",
            "projection_fallback_used": True,
            "projection_fallback_warning": "True STL slicing failed; 2.5D XY projection was used as a conservative fallback. Verify geometry carefully before real EBAM run.",
        })
        result = _generate_with_polygon_provider(lambda z: proj_polys, height, stats2, fallback_settings, progress_callback=progress_callback)
        result.audit_text += "\n\nCRITICAL WARNING:\n  STL slicing failed and 2.5D XY projection fallback was used.\n  This may overfill hollow or sloped geometry. Use only after visual verification and dry run.\n"
        result.stats["projection_fallback_used"] = True
        return result

def generate_from_polygons_2d(polys: List[Polygon], height: float, settings: Optional[ProcessSettings] = None, progress_callback: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
    if settings is None:
        settings = ProcessSettings()
    clean = _clean_polygons(polys)
    if not clean:
        raise ValueError("No valid polygons")
    # Normalize XY to non-negative if requested by center_xy=False
    if not settings.center_xy:
        u = unary_union(clean)
        minx, miny, _, _ = u.bounds
        if abs(minx) > 1e-9 or abs(miny) > 1e-9:
            from shapely.affinity import translate
            clean = [translate(p, xoff=-minx, yoff=-miny) for p in clean]
    elif settings.center_xy:
        u = unary_union(clean)
        minx, miny, maxx, maxy = u.bounds
        from shapely.affinity import translate
        clean = [translate(p, xoff=-(minx+maxx)/2.0, yoff=-(miny+maxy)/2.0) for p in clean]
    if abs(float(getattr(settings, "output_offset_x_mm", 0.0))) > 1e-12 or abs(float(getattr(settings, "output_offset_y_mm", 0.0))) > 1e-12:
        from shapely.affinity import translate
        clean = [translate(p, xoff=float(getattr(settings, "output_offset_x_mm", 0.0)), yoff=float(getattr(settings, "output_offset_y_mm", 0.0))) for p in clean]
    stats = polygon_summary(clean, height)
    path_strategy = str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower()
    if path_strategy in ("rotary_c", "rotary_c_rings", "c_rings", "c_table", "generic_rotary_c_rings", "rings", "xy_rings", "spiral", "xy_spiral"):
        return _generate_special_paths_from_polygon_provider(lambda z: clean, height, stats, settings, source_label="POLYGON", progress_callback=progress_callback)
    return _generate_with_polygon_provider(lambda z: clean, height, stats, settings, progress_callback=progress_callback)


def layer_table_csv(layer_infos: List[LayerInfo]) -> str:
    header = "layer,z,current_ma,current_required_ma,current_clipped_by_min,current_clipped_by_max,travel_mm_min,wire_mm_s,energy_target_j_mm,energy_actual_j_mm,energy_j_mm3,pause_s,hatch_segments,contour_segments,hatch_length_mm,contour_length_mm,actual_e0_command_ma,actual_e2_command_mm_s,analog_command_mode,analog_command_update"
    rows = [header]
    for li in layer_infos:
        e0_cmd = "" if li.commanded_e0_ma is None else f"{li.commanded_e0_ma:.3f}"
        e2_cmd = "" if li.commanded_e2_mm_s is None else f"{li.commanded_e2_mm_s:.3f}"
        rows.append(f"{li.index},{li.z:.3f},{li.current_ma:.3f},{li.current_required_ma:.3f},{int(li.current_clipped_by_min)},{int(li.current_clipped_by_max)},{li.feed_mm_min:.1f},{li.wire_mm_s:.3f},{li.energy_j_mm:.2f},{li.energy_actual_j_mm:.2f},{li.energy_j_mm3:.2f},{li.layer_pause_s:.3f},{li.segments_count},{li.contour_segments_count},{li.path_length_mm:.3f},{li.contour_length_mm:.3f},{e0_cmd},{e2_cmd},{li.analog_command_mode},{int(li.analog_command_update)}")
    return "\n".join(rows) + "\n"



def _strip_gcode_line(raw: str) -> str:
    """Remove common inline comments from one G-code line.

    Supports LinuxCNC-style parenthesized comments and semicolon comments.
    This helper is intentionally small and conservative: it is for static
    analysis, not for executing macro expressions.
    """
    no_paren = re.sub(r"\([^)]*\)", "", str(raw))
    return no_paren.split(";", 1)[0].strip()


def _parse_gcode_words(line: str) -> Dict[str, List[float]]:
    """Parse simple numeric G-code words from one line.

    Returns a dict of letter -> list of values to tolerate lines with both
    G and M words. Handles compact words like G1X.5Y-.25 and decimal comma.
    Macro expressions such as Q#<_val> are deliberately not interpreted.
    """
    words: Dict[str, List[float]] = {}
    clean = _strip_gcode_line(line).upper().replace(',', '.')
    pattern = r"([A-Z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))"
    for m in re.finditer(pattern, clean):
        key = m.group(1)
        try:
            val = float(m.group(2))
        except ValueError:
            continue
        words.setdefault(key, []).append(val)
    return words


def _gcode_comments(line: str) -> List[str]:
    return [m.group(1).strip() for m in re.finditer(r"\((.*?)\)", line)]


def analyze_gcode_reverse(gcode: str, settings: Optional[ProcessSettings] = None) -> Dict[str, Any]:
    """Reverse-analyze G-code and reconstruct toolpath information for UI preview.

    This is intentionally conservative: it does not claim to recover the original STL.
    It reconstructs the executed path from G0/G1 X/Y/Z/F and M68 E0/E1/E2 commands.
    v4.2.9.6 fixes: first dangerous G0 is detected even before a drawable segment exists,
    W moves are tracked separately, and status is based on structured danger codes.
    """
    settings = settings or ProcessSettings()
    segments: List[Dict[str, Any]] = []
    w_moves: List[Dict[str, Any]] = []
    comments: List[str] = []
    warnings: List[str] = []
    recommendations: List[str] = []
    issues: List[Dict[str, Any]] = []

    x = y = z = None
    w_pos = 0.0
    feed = None
    beam_current = 0.0
    focus_ma = None
    wire_feed = 0.0
    relative_mode = False
    motion_mode: Optional[str] = None
    g0_active = 0
    g91_count = 0
    g90_count = 0
    g0_count = 0
    g1_count = 0
    m68_e0_count = 0
    m68_e1_count = 0
    m68_e2_count = 0
    pause_count = 0
    line_count = 0
    w_move_count = 0
    w_move_while_beam_on = 0
    w_move_while_wire_on = 0
    w_total_abs_mm = 0.0
    arc_count = 0
    x_vals: List[float] = []
    y_vals: List[float] = []
    z_vals: List[float] = []
    feed_vals: List[float] = []
    current_vals: List[float] = []
    wire_vals: List[float] = []
    focus_vals: List[float] = []

    def _add_coord(v, lst):
        if v is not None and math.isfinite(float(v)):
            lst.append(float(v))

    def _issue(level: str, code: str, message: str, line: Optional[int] = None):
        item = {'level': level, 'code': code, 'message': message}
        if line is not None:
            item['line'] = line
        issues.append(item)
        if level == 'DANGER':
            prefix = 'Опасность'
        elif level == 'WARNING':
            prefix = 'Предупреждение'
        else:
            prefix = 'Информация'
        if line is not None:
            warnings.append(f'{prefix}: строка {line}: {message}')
        else:
            warnings.append(f'{prefix}: {message}')

    for ln, raw in enumerate(str(gcode).splitlines(), start=1):
        line_count += 1
        if len(comments) < 12:
            comments.extend(_gcode_comments(raw)[: max(0, 12-len(comments))])
        stripped = _strip_gcode_line(raw)
        if not stripped:
            continue
        up = stripped.upper().replace(',', '.')
        words = _parse_gcode_words(up)

        explicit_motion: Optional[str] = None
        non_motion_g = False
        arc_g = False
        has_g_word = 'G' in words
        if has_g_word:
            for gv in words['G']:
                gi = int(round(gv))
                if gi == 90:
                    relative_mode = False
                    g90_count += 1
                elif gi == 91:
                    relative_mode = True
                    g91_count += 1
                elif gi in (0, 1):
                    motion_mode = f"G{gi}"
                    explicit_motion = motion_mode
                elif gi in (2, 3):
                    # Arc moves are real motion but this analyzer is linear-only.
                    # Do not silently treat them as straight G1 deposition.
                    arc_g = True
                else:
                    # G10/G17/G18/G19/G20/G21/G64/G94/G4 etc. are not motion.
                    # Same guard as audit_gcode: a non-motion G word must not
                    # inherit a stale modal G0/G1 just because it carries coords
                    # (e.g. G10 L20 P0 W0 calibration after a G1 move).
                    non_motion_g = True

        if 'M' in words:
            mvals = [int(round(v)) for v in words.get('M', [])]
            if 68 in mvals and 'E' in words and 'Q' in words:
                # The generator writes one M68 per line. Use the first E/Q pair.
                e = int(round(words['E'][0]))
                q = float(words['Q'][0])
                if e == 0:
                    beam_current = q
                    m68_e0_count += 1
                    current_vals.append(abs(q))
                elif e == 1:
                    focus_ma = q
                    m68_e1_count += 1
                    focus_vals.append(q)
                elif e == 2:
                    wire_feed = q
                    m68_e2_count += 1
                    wire_vals.append(abs(q))
            if 0 in mvals:
                pause_count += 1
        if 'G' in words and any(int(round(gv)) == 4 for gv in words['G']):
            pause_count += 1

        if 'F' in words:
            feed = float(words['F'][-1])
            feed_vals.append(feed)

        axes_present = any(k in words for k in ('X', 'Y', 'Z', 'W', 'B', 'C'))
        if arc_g and axes_present:
            # Count the arc, warn once, and skip linear plotting for this line so
            # we do not report a wrong (chord) length or a false deposition path.
            arc_count += 1
            continue
        is_motion = False
        if axes_present:
            if explicit_motion in ('G0', 'G1'):
                is_motion = True
            elif (not has_g_word or not non_motion_g) and motion_mode in ('G0', 'G1'):
                is_motion = True
        if not is_motion:
            continue

        beam_on = abs(beam_current) > 1e-6
        wire_on = abs(wire_feed) > 1e-6

        # Safety check must trigger even when this is the first movement and there is no old point yet.
        if motion_mode == 'G0':
            g0_count += 1
            if beam_on or wire_on:
                g0_active += 1
        elif motion_mode == 'G1':
            g1_count += 1

        old_x, old_y, old_z = x, y, z
        nx, ny, nz = x, y, z
        if 'X' in words:
            val = float(words['X'][-1]); nx = (0.0 if nx is None else nx) + val if relative_mode else val
        if 'Y' in words:
            val = float(words['Y'][-1]); ny = (0.0 if ny is None else ny) + val if relative_mode else val
        if 'Z' in words:
            val = float(words['Z'][-1]); nz = (0.0 if nz is None else nz) + val if relative_mode else val

        # W is tracked independently from XY/Z plotting.
        if 'W' in words:
            old_w = w_pos
            val = float(words['W'][-1])
            new_w = old_w + val if relative_mode else val
            d_w = new_w - old_w
            w_pos = new_w
            if abs(d_w) > 1e-9:
                w_move_count += 1
                w_total_abs_mm += abs(d_w)
                if beam_on:
                    w_move_while_beam_on += 1
                if wire_on:
                    w_move_while_wire_on += 1
                w_moves.append({
                    'line': ln,
                    'motion': motion_mode,
                    'w_from': old_w,
                    'w_to': new_w,
                    'dW_mm': d_w,
                    'feed_mm_min': feed,
                    'current_ma': beam_current,
                    'wire_mm_s': wire_feed,
                    'relative_mode': relative_mode,
                })

        x, y, z = nx, ny, nz
        _add_coord(x, x_vals); _add_coord(y, y_vals); _add_coord(z, z_vals)

        if old_x is None or old_y is None or x is None or y is None:
            continue
        old_z_plot = 0.0 if old_z is None else float(old_z)
        new_z_plot = old_z_plot if z is None else float(z)
        dx = float(x) - float(old_x)
        dy = float(y) - float(old_y)
        dz = new_z_plot - old_z_plot
        length = math.sqrt(dx*dx + dy*dy + dz*dz)
        if length <= 1e-9:
            continue

        if motion_mode == 'G0':
            seg_type = 'rapid'
        else:
            if beam_on and wire_on:
                seg_type = 'deposition'
            elif beam_on:
                seg_type = 'beam_only'
            elif wire_on:
                seg_type = 'wire_only'
            else:
                seg_type = 'travel'
        segments.append({
            'line': ln,
            'type': seg_type,
            'x1': float(old_x), 'y1': float(old_y), 'z1': old_z_plot,
            'x2': float(x), 'y2': float(y), 'z2': new_z_plot,
            'length_mm': length,
            'feed_mm_min': feed,
            'current_ma': beam_current,
            'wire_mm_s': wire_feed,
            'focus_ma': focus_ma,
            'relative_mode': relative_mode,
        })

    dep_segments = [s for s in segments if s['type'] == 'deposition']
    beam_segments = [s for s in segments if s['type'] in ('deposition', 'beam_only')]
    path_segments = beam_segments or [s for s in segments if s['type'] != 'rapid'] or segments
    total_len = sum(s['length_mm'] for s in segments)
    deposition_len = sum(s['length_mm'] for s in dep_segments)
    beam_len = sum(s['length_mm'] for s in beam_segments)
    rapid_len = sum(s['length_mm'] for s in segments if s['type'] == 'rapid')
    travel_len = sum(s['length_mm'] for s in segments if s['type'] == 'travel')

    estimated_active_time_s = 0.0
    estimated_total_time_s = 0.0
    for s in segments:
        f = s.get('feed_mm_min')
        if f and f > 0:
            dt = s['length_mm'] / (float(f) / 60.0)
            estimated_total_time_s += dt
            if s['type'] in ('deposition', 'beam_only'):
                estimated_active_time_s += dt

    # Infer layers from deposition/beam path Z values, ignoring safe travel if possible.
    z_path_vals = []
    for s in path_segments:
        z_path_vals.extend([round(float(s['z1']), 3), round(float(s['z2']), 3)])
    unique_z = sorted(set(z_path_vals))
    if beam_segments or dep_segments:
        work_z = unique_z
    elif len(unique_z) > 3:
        cutoff = np.percentile(unique_z, 90)
        work_z = sorted(set(v for v in unique_z if v <= cutoff)) or unique_z
    else:
        work_z = unique_z

    bbox = None
    if x_vals and y_vals:
        bbox = {
            'x_min': min(x_vals), 'x_max': max(x_vals),
            'y_min': min(y_vals), 'y_max': max(y_vals),
            'z_min': min(z_vals) if z_vals else None,
            'z_max': max(z_vals) if z_vals else None,
            'size_x': max(x_vals) - min(x_vals),
            'size_y': max(y_vals) - min(y_vals),
            'size_z': (max(z_vals) - min(z_vals)) if z_vals else 0.0,
        }

    x_like = 0
    y_like = 0
    mixed_like = 0
    for s in dep_segments or beam_segments:
        dx = abs(s['x2'] - s['x1']); dy = abs(s['y2'] - s['y1'])
        if dx > dy * 3:
            x_like += 1
        elif dy > dx * 3:
            y_like += 1
        elif dx > 1e-6 and dy > 1e-6:
            mixed_like += 1
    if y_like > x_like * 1.5:
        path_pattern = 'преимущественно Y-направленные дорожки'
    elif x_like > y_like * 1.5:
        path_pattern = 'преимущественно X-направленные дорожки'
    elif mixed_like:
        path_pattern = 'много смешанных/контурных движений'
    else:
        path_pattern = 'траектория не распознана уверенно'

    process = 'Неизвестный G-code'
    if m68_e0_count and m68_e2_count:
        process = 'EBAM наплавка с проволокой'
    elif m68_e0_count:
        process = 'EBAM проход лучом без рабочей подачи проволоки'
    elif dep_segments:
        process = 'Активная траектория по G1'

    if not segments:
        _issue('WARNING', 'NO_SEGMENTS', 'В файле не найдено пригодных G0/G1 перемещений X/Y/Z для построения траектории.')
    if not beam_segments and not dep_segments:
        _issue('WARNING', 'NO_EBAM_ACTIVE_SEGMENTS', 'Не найдено участков с включённым E0/E2. Это может быть сухой прогон или G-code другого формата.')
    if arc_count:
        _issue('WARNING', 'ARC_UNSUPPORTED', f'Найдено {arc_count} дуг G2/G3. Линейный анализатор их не строит и не учитывает в длине/времени; предпросмотр и оценка времени приблизительны.')
    if g0_active:
        _issue('DANGER', 'G0_ACTIVE', f'Найдено G0 при включённом луче или проволоке: {g0_active} строк.')
    if relative_mode:
        _issue('DANGER', 'ENDS_IN_G91', 'Файл заканчивается в G91 относительном режиме.')
    if m68_e0_count == 0:
        _issue('WARNING', 'NO_M68_E0', 'Не найдены команды M68 E0: ток пучка не распознан.')
    if m68_e2_count == 0:
        _issue('WARNING', 'NO_M68_E2', 'Не найдены команды M68 E2: рабочая подача проволоки не распознана или отсутствует.')

    max_wire = max(wire_vals) if wire_vals else 0.0
    max_current = max(current_vals) if current_vals else 0.0
    min_nonzero_current = min([v for v in current_vals if v > 1e-9], default=0.0)
    if max_wire > settings.wire_max_mm_s:
        _issue('WARNING', 'E2_ABOVE_LIMIT', f'Подача проволоки до {max_wire:.3f} мм/с выше контрольной границы {settings.wire_max_mm_s:.3f} мм/с.')
    if max_current > settings.current_max_ma:
        _issue('WARNING', 'E0_ABOVE_LIMIT', f'Ток E0 до {max_current:.3f} мА выше заданного лимита {settings.current_max_ma:.3f} мА.')
    if min_nonzero_current and settings.current_low_warning_ma > 0 and min_nonzero_current < settings.current_low_warning_ma:
        _issue('WARNING', 'E0_LOW', f'Минимальный ненулевой ток E0 {min_nonzero_current:.3f} мА ниже порога предупреждения {settings.current_low_warning_ma:.3f} мА.')
    if w_move_while_wire_on:
        _issue('WARNING', 'W_WHILE_E2', f'W-перемещения при активной подаче E2: {w_move_while_wire_on} строк. Проверьте, не конфликтует ли W с M68 E2.')
    if w_move_while_beam_on:
        _issue('WARNING', 'W_WHILE_E0', f'W-перемещения при активном луче E0: {w_move_while_beam_on} строк. Для Бормаш это допустимо только после сухой проверки направления W.')

    if g0_active:
        recommendations.append('Перед запуском исправить G0 при активном луче/проволоке: быстрые перемещения допустимы только при E0=0 и E2=0.')
    if relative_mode:
        recommendations.append('Добавить возврат G90 после относительных перемещений G91, особенно после W-ретракта.')
    if not dep_segments and beam_segments:
        recommendations.append('Есть проходы лучом без рабочей подачи проволоки. Проверьте, не потерялись ли команды M68 E2.')
    if max_wire > settings.wire_max_mm_s:
        recommendations.append('Если механизм подачи не рассчитан на такую скорость, уменьшить Z-шаг, шаг дорожек или F.')
    if max_current > settings.current_max_ma:
        recommendations.append('Если ток выше допустимого, уменьшить энергию Дж/мм или скорость F, либо вручную увеличить лимит только после проверки источника и interlock.')
    if w_move_count:
        recommendations.append('Проверить W-перемещения отдельно: направление ретракта/возврата, конфликт W с E2 и отсутствие тычка проволоки в холодную ванну.')
    if deposition_len <= 0 and beam_len <= 0:
        recommendations.append('Для анализа технологического режима загрузите файл с M68 E0/E2 или проверьте формат G-code.')
    if not recommendations:
        recommendations.append('Критичных признаков по статическому анализу не найдено. Всё равно нужен viewer, audit и сухой прогон без луча/проволоки.')

    danger_count = sum(1 for i in issues if i['level'] == 'DANGER')
    warning_count = sum(1 for i in issues if i['level'] == 'WARNING')
    if danger_count:
        status_level = 'DANGER'
        status_text = '🔴 Не запускать с лучом/проволокой'
    elif warning_count:
        status_level = 'WARNING'
        status_text = '🟡 Только анализ/viewer и сухой прогон'
    else:
        status_level = 'OK'
        status_text = '🟢 Критичных признаков не найдено'

    stats = {
        'version': APP_VERSION,
        'line_count': line_count,
        'segment_count': len(segments),
        'deposition_segments': len(dep_segments),
        'beam_segments': len(beam_segments),
        'g0_count': g0_count,
        'g1_count': g1_count,
        'g0_active': g0_active,
        'g90_count': g90_count,
        'g91_count': g91_count,
        'ends_in_g91': relative_mode,
        'm68_e0_count': m68_e0_count,
        'm68_e1_count': m68_e1_count,
        'm68_e2_count': m68_e2_count,
        'max_current_ma': max_current,
        'min_nonzero_current_ma': min_nonzero_current,
        'max_wire_mm_s': max_wire,
        'max_focus_ma': max(focus_vals) if focus_vals else 0.0,
        'min_feed_mm_min': min(feed_vals) if feed_vals else 0.0,
        'max_feed_mm_min': max(feed_vals) if feed_vals else 0.0,
        'total_path_length_mm': total_len,
        'beam_path_length_mm': beam_len,
        'deposition_path_length_mm': deposition_len,
        'rapid_path_length_mm': rapid_len,
        'travel_path_length_mm': travel_len,
        'estimated_active_time_s': estimated_active_time_s,
        'estimated_total_motion_time_s': estimated_total_time_s,
        'layer_count_estimate': len(work_z),
        'z_values_sample': work_z[:20],
        'bbox': bbox,
        'path_pattern': path_pattern,
        'process_inferred': process,
        'comments_sample': comments[:12],
        'w_move_count': w_move_count,
        'w_move_while_beam_on': w_move_while_beam_on,
        'w_move_while_wire_on': w_move_while_wire_on,
        'w_total_abs_mm': w_total_abs_mm,
        'w_final_mm': w_pos,
        'arc_count': arc_count,
        'danger_count': danger_count,
        'warning_count': warning_count,
        'status_level': status_level,
        'status_text': status_text,
        'ok': danger_count == 0,
    }
    return {
        'stats': stats,
        'segments': segments,
        'w_moves': w_moves,
        'issues': issues,
        'warnings': warnings,
        'recommendations': recommendations,
        'summary_text': build_gcode_reverse_report({'stats': stats, 'warnings': warnings, 'recommendations': recommendations}),
    }

def build_gcode_reverse_report(analysis: Dict[str, Any]) -> str:
    stats = analysis.get('stats', {})
    warnings = analysis.get('warnings', [])
    recommendations = analysis.get('recommendations', [])
    bbox = stats.get('bbox') or {}
    lines = []
    lines.append(f'EBAM G-code Studio {APP_VERSION} reverse G-code analysis')
    lines.append('================================================')
    lines.append('')
    lines.append(f"Status: {stats.get('status_text', 'OK for review/dry-run preparation')}")
    lines.append(f"Inferred process: {stats.get('process_inferred', 'unknown')}")
    lines.append(f"Path pattern: {stats.get('path_pattern', 'unknown')}")
    lines.append(f"Lines: {stats.get('line_count', 0)}")
    lines.append(f"Segments: {stats.get('segment_count', 0)}; deposition={stats.get('deposition_segments', 0)}; beam={stats.get('beam_segments', 0)}")
    lines.append(f"Estimated layers: {stats.get('layer_count_estimate', 0)}")
    if bbox:
        lines.append(f"BBox X: {bbox.get('x_min', 0):.3f} ... {bbox.get('x_max', 0):.3f} mm, size {bbox.get('size_x', 0):.3f} mm")
        lines.append(f"BBox Y: {bbox.get('y_min', 0):.3f} ... {bbox.get('y_max', 0):.3f} mm, size {bbox.get('size_y', 0):.3f} mm")
        if bbox.get('z_min') is not None:
            lines.append(f"BBox Z: {bbox.get('z_min', 0):.3f} ... {bbox.get('z_max', 0):.3f} mm, size {bbox.get('size_z', 0):.3f} mm")
    lines.append(f"Current E0 max: {stats.get('max_current_ma', 0):.3f} mA")
    lines.append(f"Wire E2 max: {stats.get('max_wire_mm_s', 0):.3f} mm/s")
    lines.append(f"Feed F range: {stats.get('min_feed_mm_min', 0):.1f} ... {stats.get('max_feed_mm_min', 0):.1f} mm/min")
    lines.append(f"W moves: {stats.get('w_move_count', 0)}, W abs sum: {stats.get('w_total_abs_mm', 0.0):.3f} mm, W final: {stats.get('w_final_mm', 0.0):.3f} mm")
    lines.append(f"Active time estimate: {stats.get('estimated_active_time_s', 0)/60.0:.1f} min")
    lines.append(f"Motion time estimate: {stats.get('estimated_total_motion_time_s', 0)/60.0:.1f} min")
    lines.append('')
    lines.append('Warnings:')
    if warnings:
        for w in warnings:
            lines.append(f"  - {w}")
    else:
        lines.append('  - No critical static warnings found.')
    lines.append('')
    lines.append('Recommendations:')
    for r in recommendations:
        lines.append(f"  - {r}")
    lines.append('')
    lines.append('Note: reverse analysis reconstructs toolpath from G-code only. It cannot fully restore the original STL surface.')
    return '\n'.join(lines) + '\n'

def audit_gcode(gcode: str, settings: ProcessSettings) -> AuditResult:
    """Static safety audit for G-code.

    v4.2.9.6: parser is word-based instead of startswith-based. This avoids
    treating G10/G17/G18/G19 as G1 motion, supports compact commands without
    spaces, modal G0/G1 movement, and relative G91 coordinates.
    """
    messages: List[str] = []
    ok = True
    beam_on = False
    wire_on = False
    g0_active = 0
    active_x_transition = 0
    active_xy_lines = 0
    active_c_motion = 0
    g0_active_c = 0
    m0_count = 0
    cycle_count = 0
    m67_count = 0
    m68_count = 0
    m67_without_motion = 0
    m68_while_active = 0
    pending_analog: Dict[int, float] = {}
    path_control_modes_seen: List[str] = []
    last_x = None
    last_y = None
    last_z = None
    last_c = None
    max_i = 0.0
    max_wire = 0.0
    w_pos = 0.0
    w_move_count = 0
    w_move_while_beam_on = 0
    w_move_while_wire_on = 0
    g91_count = 0
    g90_count = 0
    relative_mode = False
    motion_mode: Optional[str] = None
    min_x = min_y = min_z = min_c = float("inf")
    max_x = max_y = max_z = max_c = float("-inf")

    for ln, raw in enumerate(str(gcode).splitlines(), start=1):
        stripped = _strip_gcode_line(raw)
        if not stripped:
            continue
        up = stripped.upper().replace(',', '.')
        words = _parse_gcode_words(up)
        if not words:
            continue

        if re.search(r"(?:^|\s)G61\.1(?:\s|$)", up):
            path_control_modes_seen.append("G61.1")
        elif re.search(r"(?:^|\s)G61(?:\s|$)", up):
            path_control_modes_seen.append("G61")
        elif re.search(r"(?:^|\s)G64(?:\s|$)", up):
            path_control_modes_seen.append("G64")

        has_g_word = 'G' in words
        explicit_motion: Optional[str] = None
        non_motion_g = False
        if has_g_word:
            for gv in words.get('G', []):
                gi = int(round(gv))
                if gi == 90:
                    relative_mode = False
                    g90_count += 1
                elif gi == 91:
                    relative_mode = True
                    g91_count += 1
                elif gi in (0, 1):
                    motion_mode = f"G{gi}"
                    explicit_motion = motion_mode
                else:
                    # G10/G17/G18/G19/G20/G21/G64/G94/G4 etc. are not motion.
                    # If such a code appears with coordinates, do not inherit old G1.
                    non_motion_g = True

        if 'M' in words:
            mvals = [int(round(v)) for v in words.get('M', [])]
            if 0 in mvals:
                m0_count += 1
            if 67 in mvals and 'E' in words and 'Q' in words:
                m67_count += 1
                e = int(round(words['E'][0]))
                q = float(words['Q'][0])
                pending_analog[e] = q
                if e == 0:
                    max_i = max(max_i, abs(q))
                elif e == 2:
                    max_wire = max(max_wire, abs(q))
            if 68 in mvals and 'E' in words and 'Q' in words:
                m68_count += 1
                e = int(round(words['E'][0]))
                q = float(words['Q'][0])
                if (beam_on or wire_on) and abs(q) > 1e-6:
                    m68_while_active += 1
                if e == 0:
                    beam_on = abs(q) > 1e-6
                    max_i = max(max_i, abs(q))
                elif e == 2:
                    wire_on = abs(q) > 1e-6
                    max_wire = max(max_wire, abs(q))

        if "WHILE" in up or "O<" in up or "CALL" in up or "SUB" in up:
            if "M429" not in up:
                cycle_count += 1

        axes_present = any(k in words for k in ('X', 'Y', 'Z', 'W', 'B', 'C'))
        is_motion = False
        if axes_present:
            if explicit_motion in ('G0', 'G1'):
                is_motion = True
            elif (not has_g_word or not non_motion_g) and motion_mode in ('G0', 'G1'):
                is_motion = True
        if not is_motion:
            continue

        if pending_analog:
            for e, q in list(pending_analog.items()):
                if e == 0:
                    beam_on = abs(q) > 1e-6
                elif e == 2:
                    wire_on = abs(q) > 1e-6
            pending_analog.clear()

        old_x, old_y, old_z, old_c = last_x, last_y, last_z, last_c
        x, y, z, c = last_x, last_y, last_z, last_c

        def axis_value(letter: str, current: Optional[float]) -> Optional[float]:
            if letter not in words:
                return current
            val = float(words[letter][-1])
            if relative_mode:
                return (0.0 if current is None else current) + val
            return val

        x = axis_value('X', x)
        y = axis_value('Y', y)
        z = axis_value('Z', z)
        c = axis_value('C', c)

        if 'W' in words:
            old_w = w_pos
            val = float(words['W'][-1])
            new_w = old_w + val if relative_mode else val
            d_w = new_w - old_w
            w_pos = new_w
            if abs(d_w) > 1e-9:
                w_move_count += 1
                if beam_on:
                    w_move_while_beam_on += 1
                if wire_on:
                    w_move_while_wire_on += 1

        if x is not None:
            min_x = min(min_x, x); max_x = max(max_x, x)
        if y is not None:
            min_y = min(min_y, y); max_y = max(max_y, y)
        if z is not None:
            min_z = min(min_z, z); max_z = max(max_z, z)
        if c is not None:
            min_c = min(min_c, c); max_c = max(max_c, c)

        is_g0 = motion_mode == 'G0'
        dc = 0.0 if old_c is None or c is None else abs(c - old_c)
        if is_g0 and (beam_on or wire_on):
            g0_active += 1
            if dc > 1e-6:
                g0_active_c += 1
            ok = False
        if (not is_g0) and (beam_on or wire_on):
            dx = 0.0 if old_x is None or x is None else abs(x - old_x)
            dy = 0.0 if old_y is None or y is None else abs(y - old_y)
            if dx > 1e-6:
                active_x_transition += 1
            if dx > 1e-6 and dy > 1e-6:
                active_xy_lines += 1
            if dc > 1e-6:
                active_c_motion += 1

        last_x, last_y, last_z, last_c = x, y, z, c

    if pending_analog:
        m67_without_motion = len(pending_analog)
        ok = False
        messages.append(f"DANGER: {m67_without_motion} queued M67 analog changes have no following motion command")
    if g0_active:
        ok = False
        messages.append(f"DANGER: {g0_active} rapid G0 moves with beam/wire active")
    if active_x_transition and settings.contour_passes <= 0 and settings.direction.upper().startswith("Y"):
        messages.append(f"WARNING: {active_x_transition} active X motions detected in Y-hatch mode")
    elif active_x_transition:
        messages.append(f"NOTE: {active_x_transition} active X motions detected; expected for contour or X-hatch modes")
    if active_c_motion:
        messages.append(f"NOTE: {active_c_motion} active C-axis deposition moves detected; expected for rotary table mode")
    if g0_active_c:
        messages.append(f"DANGER: {g0_active_c} rapid C moves with beam/wire active")
    if m0_count:
        messages.append(f"WARNING: M0 count = {m0_count}")
    if cycle_count:
        messages.append(f"WARNING: possible cycles/subprogram calls = {cycle_count}")
    if max_i > float(settings.current_max_ma) + 1e-9:
        messages.append(f"WARNING: E0 reaches {max_i:.3f} mA, above selected limit {settings.current_max_ma:.3f} mA")
    if max_wire > float(settings.wire_max_mm_s) + 1e-9:
        messages.append(f"WARNING: E2 reaches {max_wire:.3f} mm/s, above control threshold {settings.wire_max_mm_s:.3f} mm/s")
    if w_move_count and abs(w_pos + settings.w_retract_mm) > max(0.05, settings.w_retract_mm * 0.25):
        messages.append(f"WARNING: final W estimate is {w_pos:.3f} mm; expected about {-settings.w_retract_mm:.3f} mm if using initial retract")
    if w_move_while_beam_on:
        messages.append(f"NOTE: {w_move_while_beam_on} W moves while beam is ON; this is expected for W recover before wire feed, but verify dry")
    if w_move_while_wire_on:
        messages.append(f"WARNING: {w_move_while_wire_on} W moves while wire feed E2 is ON; verify there is no W/M68 E2 conflict")
    if relative_mode:
        ok = False
        messages.append("DANGER: program ends in G91 relative mode; check G90 recovery after W moves")
    if m67_count:
        messages.append(f"NOTE: synchronized M67 commands = {m67_count}; each must be followed by motion and requires HAL analog-out wiring")
    if m68_while_active:
        messages.append(f"WARNING: {m68_while_active} immediate M68 updates occurred while deposition outputs were active; these may break blending")
    if any(m in ("G61", "G61.1") for m in path_control_modes_seen) and active_c_motion:
        messages.append("WARNING: exact-path/exact-stop mode is present with active C motion; segment-boundary slowdown/stop is possible")
    if not messages:
        messages.append("No obvious dangerous G0/active transitions found")
    stats = {
        "g0_active": g0_active,
        "active_x_transition": active_x_transition,
        "active_xy_lines": active_xy_lines,
        "active_c_motion": active_c_motion,
        "g0_active_c": g0_active_c,
        "m0_count": m0_count,
        "cycle_count": cycle_count,
        "m67_count": m67_count,
        "m68_count": m68_count,
        "m67_without_following_motion": m67_without_motion,
        "m68_updates_while_active": m68_while_active,
        "path_control_modes_seen": list(dict.fromkeys(path_control_modes_seen)),
        "max_current_ma": max_i,
        "max_wire_mm_s": max_wire,
        "w_move_count": w_move_count,
        "w_final_estimate_mm": w_pos,
        "w_moves_while_beam_on": w_move_while_beam_on,
        "w_moves_while_wire_on": w_move_while_wire_on,
        "g91_count": g91_count,
        "g90_count": g90_count,
        "x_min": None if min_x == float("inf") else min_x,
        "x_max": None if max_x == float("-inf") else max_x,
        "y_min": None if min_y == float("inf") else min_y,
        "y_max": None if max_y == float("-inf") else max_y,
        "z_min": None if min_z == float("inf") else min_z,
        "z_max": None if max_z == float("-inf") else max_z,
        "c_min": None if min_c == float("inf") else min_c,
        "c_max": None if max_c == float("-inf") else max_c,
    }
    return AuditResult(ok=ok, messages=messages, stats=stats)


def audit_report(settings: ProcessSettings, stats: Dict[str, Any], audit: AuditResult, warnings: List[str], layers: List[LayerInfo]) -> str:
    lines = []
    lines.append(f"EBAM G-code Studio {APP_VERSION} audit")
    lines.append("============================")
    lines.append("")
    lines.append("Process settings:")
    for k, v in asdict(settings).items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("Model / generation stats:")
    for k in sorted(stats):
        lines.append(f"  {k}: {stats[k]}")
    trace_changes = stats.get("requested_effective_changes")
    if isinstance(trace_changes, dict):
        lines.append("")
        lines.append("Requested -> effective settings trace:")
        if not trace_changes:
            lines.append("  no implicit setting changes")
        else:
            for field_name in sorted(trace_changes):
                change = trace_changes[field_name]
                lines.append(
                    f"  {field_name}: requested={change.get('requested')} -> "
                    f"effective={change.get('effective')} ({change.get('reason', 'generator rule')})"
                )
    bormash_ok, bormash_msgs = bormash_limits_report(stats, settings)
    stats["bormash_limits_ok"] = bool(bormash_ok)
    stats["bormash_limits_messages"] = bormash_msgs
    if not bormash_ok:
        audit.ok = False
        warnings.extend(bormash_msgs)
    elif is_bormash_profile(settings):
        warnings.extend([m for m in bormash_msgs if m.startswith("OK:")])

    # --- Fusion power floor (advisory), ported from parallel branch ---
    pf = beam_power_floor_analysis(settings, layers)
    stats.update(pf)
    if pf.get("beam_power_below_floor"):
        warnings.insert(0, (
            f"LOW BEAM POWER: weakest layer runs {pf['beam_power_current_now_ma']:.2f} mA -> "
            f"{pf['beam_power_min_w']:.0f} W, below the fusion floor {pf['min_beam_power_w_floor']:.0f} W. "
            f"Risk of lack-of-fusion / balling (real case: 160 J/mm + slow C = ~630 W balled). "
            f"To reach the floor at current speed raise target energy to ~{pf['beam_power_energy_needed_j_mm']:.0f} J/mm "
            f"(now ~{pf['beam_power_energy_now_j_mm']:.0f}), i.e. ~{pf['beam_power_current_needed_ma']:.1f} mA; "
            f"or raise travel speed to ~{pf['beam_power_speed_for_floor_mm_s']:.2f} mm/s. "
            f"G-code is NOT changed. Calibrate the floor on a single-bead TEST."
        ))

    # --- Manual wire-feed volumetric sanity (advisory), v4.2.9.13 ---
    # Field case Cylindr_V5: manual_constant E2=10.0 mm/s vs volumetric need 4.96
    # (x2.01) passed silently and produced metal the bead lane cannot hold.
    # Compare each layer's commanded E2 against the volumetric requirement for the
    # DECLARED lane (z_step x effective step). Advisory only: manual mode stays
    # fully manual (including E2=0 dry runs); we just force intent reconciliation.
    wire_mode_chk = str(getattr(settings, "wire_feed_mode", "auto") or "auto").strip().lower()
    if wire_mode_chk.startswith("manual") and layers:
        try:
            strategy_chk = str(getattr(settings, "rotational_path_strategy", "hatch")).lower()
            is_rot_chk = ("rotary" in strategy_chk) or strategy_chk in ("rings", "spiral")
            step_eff_chk = float(_effective_rotational_radial_step(settings)) if is_rot_chk else float(settings.hatch_spacing)
            area_chk = settings.wire_area_mm2()
            eta_chk = max(float(settings.deposition_efficiency), 1e-9)
            RATIO_LO, RATIO_HI = 0.6, 1.5
            worst = None  # (abs_log_ratio, layer, ratio, needed)
            r_min = r_max = None
            for li in layers:
                wire_cmd = float(li.wire_mm_s)
                if wire_cmd <= 1e-9:
                    continue  # dry / no-wire pass is intentional in manual mode
                z_step_li = li.z_next - li.z
                if z_step_li <= 1e-6:
                    z_step_li = float(settings.layer_height)
                needed = z_step_li * float(li.travel_speed_mm_s) * step_eff_chk / (area_chk * eta_chk)
                if needed <= 1e-9 or step_eff_chk <= 1e-9:
                    continue
                ratio = wire_cmd / needed
                r_min = ratio if r_min is None else min(r_min, ratio)
                r_max = ratio if r_max is None else max(r_max, ratio)
                score = abs(math.log(max(ratio, 1e-9)))
                if worst is None or score > worst[0]:
                    worst = (score, li, ratio, needed)
            if r_min is not None:
                stats["wire_feed_ratio_min"] = r_min
                stats["wire_feed_ratio_max"] = r_max
                flag = (r_max > RATIO_HI + 1e-9) or (r_min < RATIO_LO - 1e-9)
                stats["wire_manual_deviation_flag"] = bool(flag)
                if flag and worst is not None:
                    _, li_w, ratio_w, needed_w = worst
                    implied_wall = area_chk * float(li_w.wire_mm_s) * eta_chk / max(float(li_w.travel_speed_mm_s), 1e-9)
                    z_step_w = (li_w.z_next - li_w.z) if (li_w.z_next - li_w.z) > 1e-6 else float(settings.layer_height)
                    implied_wall = implied_wall / max(z_step_w, 1e-9)
                    direction = ("ПЕРЕПОДАЧА: лишний металл переполняет валик - наплывы/капли/уход геометрии"
                                 if ratio_w > 1.0 else
                                 "НЕДОПОДАЧА: металла меньше дорожки - тонкая стенка, оплавление кончика назад, шарики")
                    warnings.insert(0, (
                        f"MANUAL E2 x{ratio_w:.2f} от объёмной потребности (слой {getattr(li_w,'index','?')}: "
                        f"задано {li_w.wire_mm_s:.2f} мм/с, для дорожки {step_eff_chk:.2f} мм x Z {z_step_w:.2f} мм "
                        f"при F={li_w.feed_mm_min:.0f} нужно ~{needed_w:.2f} мм/с при η={eta_chk:.2f}). "
                        f"Фактическая E2 подразумевает стенку ~{implied_wall:.2f} мм. {direction}. "
                        f"Если стенка ~{implied_wall:.1f} мм - ЦЕЛЬ, приведите радиальный шаг/геометрию в соответствие; "
                        f"иначе установите E2 ~{needed_w:.2f} мм/с или режим auto. G-code НЕ изменён."
                    ))
        except Exception:
            pass  # advisory must never break generation

    # --- Low-Z approach advisory, v4.2.9.13 ---
    # With z_hop ~0 (his V5: header G0 Z0.000) the initial XY rapid runs at work
    # height across the plate - clamp/tailstock collision risk. Advisory only.
    safe_initial_enabled = bool(getattr(settings, "safe_initial_approach_enabled", False))
    if float(getattr(settings, "z_hop_mm", 0.0)) < 1.0 and not safe_initial_enabled:
        warnings.append(
            f"ПОДВОД НА МАЛОЙ ВЫСОТЕ: z_hop={float(settings.z_hop_mm):.2f} мм - начальный XY-переезд к точке старта "
            f"пройдёт практически на рабочей высоте. Проверьте зазор с оснасткой/прижимами по трассе подвода "
            f"(сухой прогон), либо поднимите z_hop для подвода."
        )
    elif safe_initial_enabled:
        warnings.append(
            f"БЕЗОПАСНЫЙ НАЧАЛЬНЫЙ ПОДВОД ВКЛЮЧЁН: B/C и XY позиционируются на абсолютном "
            f"Z={float(getattr(settings, 'safe_initial_approach_z_mm', 7.0)):.2f} мм до рабочего спуска. "
            f"Подтвердите этот зазор и систему координат сухим прогоном на реальном станке."
        )

    if stats.get("gcode_lines", 0) > settings.max_gcode_lines_warning:
        lines.append(f"  WARNING_LARGE_GCODE_LINES: {stats.get('gcode_lines')} lines; LinuxCNC preview/loading can be slow")
    if stats.get("gcode_size_mb", 0.0) > settings.max_gcode_size_mb_warning:
        lines.append(f"  WARNING_LARGE_GCODE_SIZE_MB: {stats.get('gcode_size_mb'):.2f} MB; consider compact output or larger layer/hatch")
    lines.append("")
    lines.append("Static G-code audit:")
    lines.append(f"  OK: {audit.ok}")
    for k, v in audit.stats.items():
        lines.append(f"  {k}: {v}")
    for msg in audit.messages:
        lines.append(f"  - {msg}")
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings[:250]:
            lines.append(f"  - {w}")
        if len(warnings) > 250:
            lines.append(f"  ... {len(warnings)-250} more warnings")
    if is_bormash_profile(settings):
        lines.append("")
        lines.append("Bormash profile check:")
        ok_limits, limit_msgs = bormash_limits_report(stats, settings)
        lines.append(f"  limits_ok: {ok_limits}")
        for m in limit_msgs:
            lines.append(f"  - {m}")

    if layers:
        lines.append("")
        lines.append("Layer range:")
        lines.append(f"  first: Z={layers[0].z:.3f}, I={layers[0].current_ma:.3f}, speed={layers[0].feed_mm_min:.1f} mm/min, wire={layers[0].wire_mm_s:.3f} mm/s")
        lines.append(f"  last:  Z={layers[-1].z:.3f}, I={layers[-1].current_ma:.3f}, speed={layers[-1].feed_mm_min:.1f} mm/min, wire={layers[-1].wire_mm_s:.3f} mm/s")
    if stats.get("target_total_time_s", 0.0):
        lines.append("")
        lines.append("Target-time check:")
        lines.append(f"  target total time: {stats.get('target_total_time_s', 0.0)/3600.0:.2f} h")
        lines.append(f"  estimated total time after real toolpath: {stats.get('estimated_total_time_h', 0.0):.2f} h")
        lines.append(f"  deviation: {stats.get('target_time_error_pct', 0.0):+.1f}%")
        if not stats.get("target_time_within_15pct", True):
            lines.append("  NOTE: target time is not matched closely after real toolpath generation; adjust feed/layer/hatch or use a different target.")

    if stats.get("wire_above_control_limit"):
        lines.append("")
        lines.append("Wire-feed control limit note:")
        lines.append(f"  calculated wire feed reaches {stats.get('wire_max_calculated_mm_s', 0.0):.3f} mm/s; user control limit is {settings.wire_max_mm_s:.3f} mm/s.")
        lines.append("  v4.2 does not clamp this value automatically. If the real feeder allows it, raise the control limit; otherwise reduce Z-step, hatch spacing or feed speed.")
    if stats.get("current_clipped_by_limit"):
        lines.append("")
        lines.append("Beam-current limit note:")
        lines.append(f"  required current reaches {stats.get('current_required_max_ma', 0.0):.3f} mA; selected current limit is {settings.current_max_ma:.3f} mA.")
        lines.append("  G-code current is clipped to the selected limit, so actual J/mm will be lower than requested. Raise the limit only if the real EBAM source, cooling, vacuum and interlocks allow it.")
    if stats.get("current_clipped_by_min"):
        lines.append("")
        lines.append("Beam-current minimum note:")
        lines.append(f"  required current falls to {stats.get('current_required_min_ma', 0.0):.3f} mA; selected current minimum is {settings.current_min_ma:.3f} mA.")
        lines.append("  G-code current is raised to the selected minimum, so actual J/mm will be higher than requested. Check overheating risk on slow/thin sections.")
    elif stats.get("current_below_low_warning"):
        lines.append("")
        lines.append("Low-current advisory note:")
        lines.append(f"  required current falls to {stats.get('current_required_min_ma', 0.0):.3f} mA; advisory warning threshold is {settings.current_low_warning_ma:.3f} mA.")
        lines.append("  G-code is NOT changed by this advisory threshold. Verify the real EBAM source is stable at such low current.")
    if layers and (stats.get("current_clipped_by_limit") or stats.get("current_clipped_by_min")):
        lines.append("")
        lines.append("Energy target/actual note:")
        lines.append("  layers.csv separates energy_target_j_mm and energy_actual_j_mm. Actual energy is calculated after current clipping.")
    lines.append("")
    lines.append("Beam power (fusion) check:")
    lines.append(f"  beam power range: {stats.get('beam_power_min_w', 0.0):.0f}..{stats.get('beam_power_max_w', 0.0):.0f} W  (P = U*I, U={settings.voltage_kv:.1f} kV)")
    lines.append(f"  advisory fusion floor: {stats.get('min_beam_power_w_floor', 0.0):.0f} W (calibrate per machine/material on a single-bead TEST)")
    if stats.get("beam_power_below_floor"):
        lines.append(f"  RESULT: BELOW FLOOR at weakest layer ({stats.get('beam_power_current_now_ma', 0.0):.2f} mA = {stats.get('beam_power_min_w', 0.0):.0f} W).")
        lines.append(f"    -> lack-of-fusion / balling risk. Raise target energy to ~{stats.get('beam_power_energy_needed_j_mm', 0.0):.0f} J/mm (~{stats.get('beam_power_current_needed_ma', 0.0):.1f} mA)")
        lines.append(f"       or raise travel speed to ~{stats.get('beam_power_speed_for_floor_mm_s', 0.0):.2f} mm/s. G-code is NOT auto-changed.")
    else:
        lines.append("  RESULT: OK (all layers at or above the advisory fusion floor).")
    lines.append("")
    lines.append("Important engineering notes:")
    lines.append("  1. This generator uses calculated start regimes, not real closed-loop thermal control.")
    lines.append("  2. Always run dry simulation and a short Z10-15 mm test before full build.")
    lines.append("  3. Verify W retract direction and that W does not conflict with M68 E2 wire speed control.")
    lines.append("  4. If top becomes shiny/liquid early, reduce target energy or add cooling pause.")
    lines.append("  5. If wire knocks/stubs, increase energy or reduce wire feed/layer height.")
    lines.append("  6. Contour passes improve shape but may violate ideal side-wire direction on some segments.")
    return "\n".join(lines) + "\n"


def save_result(result: GenerationResult, out_prefix: str | Path, settings: Optional[ProcessSettings] = None) -> Dict[str, Path]:
    p = Path(out_prefix)
    p.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "gcode": p.with_suffix(".ngc"),
        "layers": p.with_name(p.name + "_layers.csv"),
        "audit": p.with_name(p.name + "_audit.txt"),
    }
    if settings is not None:
        files["settings"] = p.with_name(p.name + "_settings.json")
    files["gcode"].write_text(result.gcode, encoding="utf-8")
    files["layers"].write_text(result.layer_csv, encoding="utf-8")
    files["audit"].write_text(result.audit_text, encoding="utf-8")
    if settings is not None:
        files["settings"].write_text(settings_to_json(settings), encoding="utf-8")
    return files


def settings_to_json(settings: ProcessSettings) -> str:
    """User-facing JSON stores movement feed speeds in mm/min.

    This matches LinuxCNC/G-code F words (G94 feed per minute). Wire feed
    remains in mm/s because it is sent through M68 E2 as a process speed.
    The loader still accepts older *_mm_s movement-speed keys for backward
    compatibility and converts them to *_mm_min.
    """
    data = asdict(settings)
    data["speed_unit_note"] = "Movement speeds are mm/min and are written to G-code F directly. Wire feed remains mm/s."
    return json.dumps(data, indent=2, ensure_ascii=False)


def _coerce_setting_value(value: Any, default: Any) -> Any:
    """Coerce JSON/user values to the dataclass field type.

    This keeps imported JSON predictable: strings like "0,30" become floats
    and "false" becomes False instead of a truthy string.
    """
    if isinstance(default, bool):
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "y", "да", "истина", "on"):
                return True
            if v in ("0", "false", "no", "n", "нет", "ложь", "off", ""):
                return False
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(round(float(str(value).strip().replace(",", "."))))
    if isinstance(default, float):
        return float(str(value).strip().replace(",", "."))
    if isinstance(default, str):
        return str(value)
    return value


def settings_from_dict(d: Dict[str, Any]) -> ProcessSettings:
    base = asdict(ProcessSettings())
    speed_map = {
        "feed_bottom_mm_s": "feed_bottom_mm_min",
        "feed_top_mm_s": "feed_top_mm_min",
        "w_retract_feed_mm_s": "w_retract_feed_mm_min",
        "rapid_feed_z_mm_s": "rapid_feed_z_mm_min",
        "work_z_feed_mm_s": "work_z_feed_mm_min",
    }
    converted = dict(d or {})
    for src, dst in speed_map.items():
        if src in converted and dst not in converted:
            converted[dst] = float(str(converted[src]).strip().replace(",", ".")) * 60.0
    for k, v in converted.items():
        if k in base:
            try:
                base[k] = _coerce_setting_value(v, base[k])
            except Exception as exc:
                raise ValueError(f"Некорректное значение настройки {k}: {v!r}") from exc
    settings = ProcessSettings(**base)
    # Validate independent parameters immediately; geometry height is checked at generation time.
    validate_process_settings(settings, height=1.0)
    return settings


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def recommend_settings_from_summary(summary: Dict[str, float], mode: str = "balanced", material_key: str = "stainless_steel_12_wire") -> ProcessSettings:
    m = mode.lower()
    s = ProcessSettings()
    mat = MATERIAL_LIBRARY.get(material_key, MATERIAL_LIBRARY["stainless_steel_12_wire"])
    s.density_g_cm3 = float(mat.get("density_g_cm3", s.density_g_cm3))
    s.wire_diameter_mm = float(mat.get("wire_diameter_mm", s.wire_diameter_mm))
    base_e0 = float(mat.get("energy_bottom_j_mm", s.target_energy_bottom_j_per_mm))
    base_e1 = float(mat.get("energy_top_j_mm", s.target_energy_top_j_per_mm))
    # v4.2.9.31: volumetric targets are primary; legacy J/mm values are only a
    # fallback for profiles that predate the QV fields.
    base_qv0 = float(mat.get("qv_bottom_j_mm3", 0.0) or 0.0)
    base_qv1 = float(mat.get("qv_top_j_mm3", 0.0) or 0.0)
    if base_qv0 <= 0.0:
        base_qv0 = qv_from_energy_j_mm(base_e0, 0.5, 2.35)
    if base_qv1 <= 0.0:
        base_qv1 = qv_from_energy_j_mm(base_e1, 0.5, 2.35)
    sx = float(summary.get("size_x", 20.0) or 20.0)
    sy = float(summary.get("size_y", 100.0) or 100.0)
    sz = float(summary.get("size_z", 100.0) or 100.0)
    min_xy = max(min(sx, sy), 1.0)
    slender = sz / max(min_xy, 1.0)

    if "quality" in m or "кач" in m:
        s.layer_height = _clamp(sz / 420.0, 0.20, 0.32)
        s.hatch_spacing = _clamp(min_xy / 10.0, 1.4, 2.0)
        s.feed_bottom_mm_min = 610.0
        s.feed_top_mm_min = 670.0
        qv0_mode, qv1_mode = base_qv0 * 1.04, base_qv1 * 1.02
        s.contour_passes = 1
        s.contour_every_n_layers = 2
    elif "speed" in m or "скор" in m:
        s.layer_height = _clamp(sz / 230.0, 0.35, 0.60)
        s.hatch_spacing = _clamp(min_xy / 8.0, 2.0, 3.0)
        s.feed_bottom_mm_min = 700.0
        s.feed_top_mm_min = 790.0
        qv0_mode, qv1_mode = base_qv0 * 1.06, base_qv1 * 1.06
        s.contour_passes = 0
    else:
        s.layer_height = _clamp(sz / 330.0, 0.25, 0.42)
        s.hatch_spacing = _clamp(min_xy / 9.0, 1.7, 2.35)
        s.feed_bottom_mm_min = 650.0
        s.feed_top_mm_min = 730.0
        qv0_mode, qv1_mode = base_qv0, base_qv1
        s.contour_passes = 1
        s.contour_every_n_layers = 3

    max_xy = max(sx, sy, 1.0)
    aspect_xy = sx / max(sy, 1e-9)
    round_tall = (0.80 <= aspect_xy <= 1.25) and (sz > 0.35 * max_xy)
    broad_part = max_xy > 120.0 or (sx * sy) > 12000.0

    if slender > 4.0:
        # Tall thin objects accumulate heat upward: reduce top energy and add pause.
        qv1_mode -= 3.0
        s.edge_wire_factor_top = 0.85
        s.near_edge_wire_factor_top = 0.92
        s.layer_pause_top_s = max(s.layer_pause_top_s, 0.55)
    if round_tall:
        # Bowl/balloon-like geometry: bottom needs stable fusion, upper bulb/throat overheats easier.
        qv0_mode += 1.5
        qv1_mode -= 4.0
        s.feed_top_mm_min = max(s.feed_bottom_mm_min, s.feed_top_mm_min - 20.0)
        s.layer_pause_bottom_s = max(s.layer_pause_bottom_s, 0.25)
        s.layer_pause_top_s = max(s.layer_pause_top_s, 0.85)
        s.hatch_spacing = min(s.hatch_spacing, 2.25)
    if broad_part and not round_tall:
        # Large flat-ish parts can dissipate heat better at the bottom, but still need top monitoring.
        s.feed_top_mm_min += 10.0
        s.layer_pause_top_s = max(s.layer_pause_top_s, 0.25)
    if min_xy < 8.0:
        s.hatch_spacing = _clamp(min_xy / 4.0, 0.6, s.hatch_spacing)
        s.layer_height = min(s.layer_height, 0.25)
    if sx > 80 or sy > 80:
        s.feed_top_mm_min += 20.0
    # v4.2.9.31: convert the volumetric targets into line energy using the FINAL
    # bead cross-section (layer_height x hatch_spacing). This is what makes the
    # material profile portable: the same QV yields the correct J/mm on a thin
    # block bead and on a thick flange bead alike.
    qv0_mode = _clamp(qv0_mode, QV_MIN_J_MM3, QV_MAX_J_MM3)
    qv1_mode = _clamp(qv1_mode, QV_MIN_J_MM3, QV_MAX_J_MM3)

    # v4.2.9.31: physical consistency with the fusion power floor.
    # Beam power P = QV * bead_section * v. A thin layer at the CORRECT volumetric
    # energy simply cannot deliver enough power to fuse: the 30x70x30 block was
    # recommended a 0.25 mm layer, which at QV=72 and F=650 yields only ~460 W
    # against the 900 W floor. Grow the layer height (thicker bead) until the
    # floor is met, within physically sane bounds tied to the wire diameter.
    # v4.2.9.31: everything that depends on wire diameter is derived here, so that
    # picking a thicker wire automatically rescales feed limits and layer bounds.
    _lay_min_d, _lay_max_d = layer_height_bounds_for_wire(s.wire_diameter_mm)
    _e2_ceiling = max_wire_feed_for_beam(s.wire_diameter_mm, s.current_max_ma,
                                         s.voltage_kv, QV_MIN_J_MM3, s.deposition_efficiency)
    s.wire_max_mm_s = round(_e2_ceiling, 1)
    s.wire_min_mm_s = round(min(float(s.wire_min_mm_s), _e2_ceiling * 0.02), 3)

    _v_bottom = max(float(s.feed_bottom_mm_min), 1.0) / 60.0
    _p_floor = max(float(s.min_beam_power_w), 0.0)
    if _p_floor > 0.0 and qv0_mode > 0.0:
        _section_needed = (_p_floor * 1.08) / (qv0_mode * _v_bottom)
        _layer_needed = _section_needed / max(s.hatch_spacing, 1e-9)
        # Sane band for a given wire: not thinner than ~0.3*d, not thicker than ~1.4*d.
        _layer_floor_wire = _lay_min_d
        _layer_cap = _lay_max_d
        _target_layer = max(s.layer_height, _layer_needed, _layer_floor_wire)
        s.layer_height = round(_clamp(_target_layer, 0.08, _layer_cap), 3)
    s.target_qv_bottom_j_mm3 = float(qv0_mode)
    s.target_qv_top_j_mm3 = float(qv1_mode)
    s.target_energy_bottom_j_per_mm = energy_j_mm_from_qv(qv0_mode, s.layer_height, s.hatch_spacing)
    s.target_energy_top_j_per_mm = energy_j_mm_from_qv(qv1_mode, s.layer_height, s.hatch_spacing)
    # Absolute guard rails remain, but they should not normally bind now.
    s.target_energy_bottom_j_per_mm = _clamp(s.target_energy_bottom_j_per_mm, 30.0, 400.0)
    s.target_energy_top_j_per_mm = _clamp(s.target_energy_top_j_per_mm, 25.0, 380.0)
    s.center_xy = False
    s.z_to_zero = True
    return s


def generate_from_stl_file(stl_path: str | Path, settings: Optional[ProcessSettings] = None) -> GenerationResult:
    mesh = load_mesh_any(stl_path)
    return generate_from_mesh(mesh, settings)


def _normalize_csv_header_name(name: str) -> str:
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


def _detect_csv_delimiter(text: str, default: str = ",") -> str:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except Exception:
        return default


def _csv_looks_like_ebam_layers(headers: List[str]) -> bool:
    h = {_normalize_csv_header_name(x) for x in headers}
    layer_markers = {
        "layer", "z", "current_ma", "travel_mm_min", "wire_mm_s",
        "energy_target_j_mm", "energy_actual_j_mm", "hatch_segments",
        "contour_segments", "hatch_length_mm", "contour_length_mm"
    }
    # EBAM layers.csv has many of these fields. A normal geometry CSV should not.
    return len(h.intersection(layer_markers)) >= 4


def _find_xy_columns(headers: List[str]) -> Optional[Tuple[int, int]]:
    norm = [_normalize_csv_header_name(x) for x in headers]
    x_candidates = ("x", "x_mm", "coord_x", "coordinate_x", "point_x")
    y_candidates = ("y", "y_mm", "coord_y", "coordinate_y", "point_y")
    x_idx = next((i for i, v in enumerate(norm) if v in x_candidates), None)
    y_idx = next((i for i, v in enumerate(norm) if v in y_candidates), None)
    if x_idx is not None and y_idx is not None:
        return int(x_idx), int(y_idx)
    return None


def load_polygons_from_csv(path_or_text: str | Path, delimiter: str = ",") -> List[Polygon]:
    """Load a 2D contour CSV.

    Accepted input: polygon points, either first two numeric columns or named X/Y columns.
    Rejected input: EBAM layers.csv reports generated by this app. layers.csv is an
    analysis/report file, not source geometry for a new G-code calculation.
    """
    if isinstance(path_or_text, (str, Path)) and Path(str(path_or_text)).exists():
        text = Path(str(path_or_text)).read_text(encoding="utf-8-sig")
    else:
        text = str(path_or_text)
    if not text.strip():
        raise ValueError("CSV пустой. Нужен файл с точками контура X,Y.")

    delim = _detect_csv_delimiter(text, default=delimiter)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise ValueError("CSV пустой. Нужен файл с точками контура X,Y.")

    first = rows[0]
    xy_cols: Optional[Tuple[int, int]] = None
    start_row = 0
    first_two_are_numbers = False
    if len(first) >= 2:
        try:
            float(str(first[0]).replace(",", ".")); float(str(first[1]).replace(",", "."))
            first_two_are_numbers = True
        except Exception:
            first_two_are_numbers = False

    if not first_two_are_numbers:
        if _csv_looks_like_ebam_layers(first):
            raise ValueError(
                "Загружен EBAM layers.csv — это отчёт по слоям, а не геометрия. "
                "Его нельзя использовать как источник 'CSV X,Y + высота'. "
                "Для повторной генерации загрузите исходный STL/DXF/контурный CSV X,Y или импортируйте settings.json во вкладке JSON."
            )
        xy_cols = _find_xy_columns(first)
        if xy_cols is None:
            raise ValueError(
                "CSV должен содержать точки контура: колонки X,Y или первые две числовые колонки. "
                "Если это файл *_layers.csv, он нужен только для анализа слоёв и не является входной геометрией."
            )
        start_row = 1
    else:
        xy_cols = (0, 1)

    pts: List[Tuple[float, float]] = []
    bad_numeric = 0
    for row in rows[start_row:]:
        if len(row) <= max(xy_cols):
            continue
        try:
            x = float(str(row[xy_cols[0]]).strip().replace(",", "."))
            y = float(str(row[xy_cols[1]]).strip().replace(",", "."))
            pts.append((x, y))
        except Exception:
            bad_numeric += 1
            continue
    if len(pts) < 3:
        raise ValueError(
            "CSV должен содержать минимум 3 числовые точки X,Y для замкнутого контура. "
            "Проверьте, что вы загрузили контурный CSV, а не layers.csv/audit/таблицу отчёта."
        )
    if math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) > 1e-6:
        pts.append(pts[0])
    p = Polygon(pts)
    clean = _clean_polygons([p])
    if not clean:
        raise ValueError(
            "Точки CSV не образуют пригодный замкнутый полигон. "
            "Нужен внешний контур детали в колонках X,Y. Файл layers.csv после генерации G-code не подходит."
        )
    return clean


def _load_polygons_from_dxf_basic(path: str | Path) -> List[Polygon]:
    """Very small fallback DXF reader for offline cases without ezdxf.

    Supports the most common 2D entities used for simple contours: LINE,
    LWPOLYLINE, POLYLINE/VERTEX and CIRCLE. It is not a full DXF parser, but it
    lets basic closed contours work even when ezdxf was not installed yet.
    """
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    raw = [ln.rstrip("\n\r") for ln in txt.splitlines()]
    pairs = []
    i = 0
    while i + 1 < len(raw):
        code = raw[i].strip()
        value = raw[i + 1].strip()
        pairs.append((code, value))
        i += 2

    polys: List[Polygon] = []
    lines: List[LineString] = []
    idx = 0

    def _float(v: str) -> float:
        return float(str(v).replace(",", "."))

    while idx < len(pairs):
        code, val = pairs[idx]
        if code != "0":
            idx += 1
            continue
        ent = val.upper()
        idx += 1
        data = []
        while idx < len(pairs) and pairs[idx][0] != "0":
            data.append(pairs[idx])
            idx += 1

        try:
            if ent == "LINE":
                d = dict(data)
                if all(k in d for k in ["10", "20", "11", "21"]):
                    lines.append(LineString([(_float(d["10"]), _float(d["20"])), (_float(d["11"]), _float(d["21"]))]))
            elif ent == "CIRCLE":
                d = dict(data)
                if all(k in d for k in ["10", "20", "40"]):
                    polys.append(ShapelyPoint(_float(d["10"]), _float(d["20"])).buffer(abs(_float(d["40"])), resolution=96))
            elif ent == "LWPOLYLINE":
                pts: List[Point] = []
                closed = False
                j = 0
                while j < len(data):
                    c, v = data[j]
                    if c == "70":
                        try:
                            closed = bool(int(float(v)) & 1)
                        except Exception:
                            pass
                    if c == "10" and j + 1 < len(data):
                        # next 20 is expected, but tolerate intervening width/bulge codes by scanning forward
                        x = _float(v)
                        y = None
                        for k in range(j + 1, min(j + 8, len(data))):
                            if data[k][0] == "20":
                                y = _float(data[k][1])
                                break
                        if y is not None:
                            pts.append((x, y))
                    j += 1
                if len(pts) >= 3:
                    if closed or math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) < 1e-6:
                        if math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) > 1e-6:
                            pts.append(pts[0])
                        polys.append(Polygon(pts))
                    else:
                        lines.append(LineString(pts))
            # POLYLINE/VERTEX is harder in pair-isolated form; LINE/LWPOLYLINE covers our main offline test cases.
        except Exception:
            continue

    if not polys and lines:
        try:
            polys = list(polygonize(lines))
        except Exception:
            pass
    clean = _clean_polygons(polys)
    if not clean:
        raise ValueError(
            "DXF не дал замкнутых 2D-контуров. Нужны закрытые LWPOLYLINE/POLYLINE, "
            "круги или набор LINE, который можно замкнуть в полигон."
        )
    return clean


def load_polygons_from_dxf(path: str | Path) -> List[Polygon]:
    try:
        import ezdxf  # type: ignore
    except Exception:
        return _load_polygons_from_dxf_basic(path)
    try:
        doc = ezdxf.readfile(str(path))
    except Exception:
        return _load_polygons_from_dxf_basic(path)
    msp = doc.modelspace()
    polys: List[Polygon] = []
    lines: List[LineString] = []
    for e in msp:
        t = e.dxftype().upper()
        try:
            if t == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
                if len(pts) >= 3 and (e.closed or math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) < 1e-6):
                    if math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) > 1e-6:
                        pts.append(pts[0])
                    polys.append(Polygon(pts))
                elif len(pts) >= 2:
                    lines.append(LineString(pts))
            elif t == "POLYLINE":
                pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in e.vertices]
                if len(pts) >= 3 and (e.is_closed or math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) < 1e-6):
                    if math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1]) > 1e-6:
                        pts.append(pts[0])
                    polys.append(Polygon(pts))
                elif len(pts) >= 2:
                    lines.append(LineString(pts))
            elif t == "LINE":
                s = e.dxf.start; en = e.dxf.end
                lines.append(LineString([(float(s.x), float(s.y)), (float(en.x), float(en.y))]))
            elif t == "CIRCLE":
                c = e.dxf.center; r = float(e.dxf.radius)
                polys.append(ShapelyPoint(float(c.x), float(c.y)).buffer(r, resolution=96))
        except Exception:
            continue
    # Try polygonize open linework if no closed polylines found
    if not polys and lines:
        try:
            from shapely.ops import polygonize
            polys = list(polygonize(lines))
        except Exception:
            pass
    clean = _clean_polygons(polys)
    if not clean:
        raise ValueError(
            "DXF не дал замкнутых 2D-контуров. Нужны закрытые LWPOLYLINE/POLYLINE, "
            "круги или набор LINE, который можно замкнуть в полигон."
        )
    return clean
