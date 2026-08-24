"""Parametric axisymmetric flange-family generator for EBAM Studio v4.2.9.19.

The module intentionally keeps the Flange1 R6 process structure while taking
all geometry from the supplied STL:

* auto-detect the build axis (or accept an explicit X/Y/Z choice);
* recover inner and outer radii from horizontal mesh sections;
* detect radial steps and preserve them as exact layer boundaries;
* allocate layers and concentric tracks from configurable height/pitch limits;
* run every layer from the inner ring to the outer ring with positive C;
* calculate G93 time, C speed, wire feed and beam current for every segment;
* refuse a hot release unless geometry, limits, time and LinuxCNC comments pass.

The generated hot programs remain experimental qualification candidates.  A
successful static audit is not a metallurgical process qualification.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import trimesh


APP_VERSION = "v4.2.9.19"


@dataclass
class FlangeFamilySettings:
    build_axis: str = "AUTO"
    target_layer_height_mm: float = 1.5
    flange_max_pitch_mm: float = 2.6
    hub_max_pitch_mm: float = 4.0
    wide_section_width_threshold_mm: float = 40.0
    manual_breakpoints_mm: list[float] = field(default_factory=list)

    voltage_kv: float = 60.0
    current_min_ma: float = 25.0
    current_hard_max_ma: float = 40.0
    current_command_max_ma: float = 39.5
    base_current_min_ma: float = 20.0
    base_current_max_ma: float = 34.5
    scan_current_reserve_factor: float = 1.10

    scan_pattern: str = "CONCENTRIC_CIRCLES"
    scan_frequency_hz: float = 300.0
    scan_scale_x_percent: float = 10.0
    scan_scale_y_percent: float = 10.0

    wire_diameter_mm: float = 1.2
    deposition_efficiency: float = 0.97
    wire_hard_max_mm_s: float = 50.0
    wire_command_max_mm_s: float = 49.5

    c_min_deg_min: float = 450.0
    c_hard_max_deg_min: float = 600.0
    c_command_max_deg_min: float = 599.5
    radial_link_c_deg: float = 5.0
    layer_link_c_deg: float = 17.0

    hub_min_layer_cycle_min: float = 5.0
    planned_hmi_verify_min: float = 5.0
    min_total_time_h: float = 4.0
    max_total_time_h: float = 6.0
    short_build_target_time_h: float = 4.25

    focus_q: float = 1030.0
    safe_clearance_mm: float = 3.0
    edge_current_factor: float = 0.92
    section_tolerance_mm: float = 0.02
    step_detection_threshold_mm: float = 0.5
    axisymmetry_tolerance_fraction: float = 0.025

    def wire_area_mm2(self) -> float:
        return math.pi * (self.wire_diameter_mm / 2.0) ** 2

    def validate(self) -> None:
        errors: list[str] = []
        if self.build_axis.upper() not in {"AUTO", "X", "Y", "Z"}:
            errors.append("build_axis must be AUTO, X, Y or Z")
        if self.target_layer_height_mm <= 0:
            errors.append("target_layer_height_mm must be positive")
        if min(self.flange_max_pitch_mm, self.hub_max_pitch_mm) <= 0:
            errors.append("track pitches must be positive")
        if not (0 < self.current_min_ma <= self.current_command_max_ma < self.current_hard_max_ma + 1e-12):
            errors.append("current limits are inconsistent")
        if not (0 < self.wire_command_max_mm_s <= self.wire_hard_max_mm_s):
            errors.append("wire limits are inconsistent")
        if not (0 < self.c_min_deg_min <= self.c_command_max_deg_min <= self.c_hard_max_deg_min):
            errors.append("C limits are inconsistent")
        if self.scan_current_reserve_factor <= 0:
            errors.append("scan current reserve must be positive")
        if not (0 < self.deposition_efficiency <= 1.0):
            errors.append("deposition efficiency must be in 0..1")
        if not (0 < self.min_total_time_h < self.max_total_time_h):
            errors.append("time window is inconsistent")
        if errors:
            raise ValueError("; ".join(errors))


@dataclass
class GeometryProfile:
    source_name: str
    build_axis_original: str
    original_size_mm: list[float]
    oriented_size_mm: list[float]
    height_mm: float
    center_original_mm: list[float]
    r_inner_bottom_mm: float
    r_outer_bottom_mm: float
    r_inner_top_mm: float
    r_outer_top_mm: float
    breakpoints_mm: list[float]
    zones: list[dict[str, float | str | int]]
    mesh_volume_mm3: float
    axisymmetry_error_fraction: float
    is_watertight: bool
    oriented_mesh: trimesh.Trimesh = field(repr=False)

    @property
    def inner_diameter_bottom_mm(self) -> float:
        return 2.0 * self.r_inner_bottom_mm

    @property
    def outer_diameter_bottom_mm(self) -> float:
        return 2.0 * self.r_outer_bottom_mm


@dataclass
class Segment:
    layer: int
    zone: str
    kind: str
    index: int
    radius_start_mm: float
    radius_end_mm: float
    c_deg: float
    length_mm: float
    time_min: float
    c_speed_deg_min: float
    linear_speed_mm_s: float
    e0_ma: float
    e2_mm_s: float
    deposit_area_mm2: float
    line_energy_j_mm: float
    volume_energy_j_mm3: float
    deposited_volume_mm3: float
    contour: bool


@dataclass
class Layer:
    index: int
    zone_index: int
    zone: str
    z_bottom_mm: float
    z_top_mm: float
    height_mm: float
    r_inner_bottom_mm: float
    r_inner_top_mm: float
    r_inner_mid_mm: float
    r_outer_bottom_mm: float
    r_outer_top_mm: float
    r_outer_mid_mm: float
    direction: str
    tracks: int
    pitch_mm: float
    centres_mm: list[float]
    exact_volume_mm3: float
    deposit_area_mm2: float
    target_energy_j_mm3: float
    active_time_min: float
    cold_link_time_min: float
    dwell_s: float
    cycle_time_min: float
    segments: list[Segment]


@dataclass
class PlanResult:
    profile: GeometryProfile
    settings: FlangeFamilySettings
    layers: list[Layer]
    summary: dict[str, Any]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _as_mesh(loaded: Any) -> trimesh.Trimesh:
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise ValueError("STL scene has no geometry")
        loaded = trimesh.util.concatenate(tuple(loaded.geometry.values()))
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("file did not produce a triangle mesh")
    mesh = loaded.copy()
    if len(mesh.faces) == 0:
        raise ValueError("mesh has no triangles")
    # Binary STL stores three independent vertices per triangle.  Merging
    # numerically identical vertices restores the intended topological mesh
    # without changing any coordinate or model dimension.
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    return mesh


def load_stl_bytes(data: bytes) -> trimesh.Trimesh:
    return _as_mesh(trimesh.load(io.BytesIO(data), file_type="stl", force="mesh", process=True))


def load_stl_file(path: str | Path) -> tuple[bytes, trimesh.Trimesh]:
    data = Path(path).read_bytes()
    return data, load_stl_bytes(data)


def _auto_build_axis(mesh: trimesh.Trimesh) -> tuple[int, float]:
    ext = np.asarray(mesh.extents, dtype=float)
    scores: list[tuple[float, int]] = []
    for axis in range(3):
        radial = [ext[i] for i in range(3) if i != axis]
        mismatch = abs(radial[0] - radial[1]) / max(radial)
        scores.append((float(mismatch), axis))
    scores.sort()
    return scores[0][1], scores[0][0]


def orient_axisymmetric_mesh(mesh: trimesh.Trimesh, build_axis: str = "AUTO") -> tuple[trimesh.Trimesh, str, float, list[float]]:
    original_size = [float(x) for x in mesh.extents]
    axis_names = "XYZ"
    if build_axis.upper() == "AUTO":
        axis, mismatch = _auto_build_axis(mesh)
    else:
        axis = axis_names.index(build_axis.upper())
        radial = [original_size[i] for i in range(3) if i != axis]
        mismatch = abs(radial[0] - radial[1]) / max(radial)
    radial_axes = [i for i in range(3) if i != axis]
    vertices = np.asarray(mesh.vertices, dtype=float)
    mapped = np.column_stack((vertices[:, radial_axes[0]], vertices[:, radial_axes[1]], vertices[:, axis]))
    out = trimesh.Trimesh(vertices=mapped, faces=np.asarray(mesh.faces), process=False)
    bounds = out.bounds
    cx = (bounds[0, 0] + bounds[1, 0]) / 2.0
    cy = (bounds[0, 1] + bounds[1, 1]) / 2.0
    out.apply_translation((-cx, -cy, -bounds[0, 2]))
    out.remove_unreferenced_vertices()
    return out, axis_names[axis], float(mismatch), original_size


def _cluster_values(values: Sequence[float], gap: float) -> list[list[float]]:
    if not values:
        return []
    ordered = sorted(float(v) for v in values)
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] > gap:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return clusters


def section_radii(mesh: trimesh.Trimesh, z_mm: float, settings: FlangeFamilySettings) -> tuple[float, float, dict[str, float]]:
    height = float(mesh.extents[2])
    eps = max(1e-5, min(0.01, height * 1e-5))
    z = min(max(float(z_mm), eps), height - eps)
    segments = trimesh.intersections.mesh_plane(
        mesh,
        plane_normal=np.array([0.0, 0.0, 1.0]),
        plane_origin=np.array([0.0, 0.0, z]),
    )
    if segments is None or len(segments) == 0:
        raise ValueError(f"no closed section at Z={z_mm:.6f} mm")
    points = np.asarray(segments, dtype=float).reshape(-1, 3)
    radii = np.hypot(points[:, 0], points[:, 1])
    span = float(np.max(radii) - np.min(radii))
    gap = max(settings.section_tolerance_mm, span * 0.015)
    clusters = _cluster_values(radii.tolist(), gap)
    if len(clusters) < 2:
        raise ValueError(
            f"section Z={z_mm:.4f} does not contain separate inner and outer contours; "
            "Flange-family mode requires a through annular section"
        )
    inner_cluster = clusters[0]
    outer_cluster = clusters[-1]
    r_inner = max(inner_cluster)
    r_outer = max(outer_cluster)
    if r_outer <= r_inner + 1e-6:
        raise ValueError(f"invalid annulus at Z={z_mm:.4f}")
    stats = {
        "inner_spread_mm": max(inner_cluster) - min(inner_cluster),
        "outer_spread_mm": max(outer_cluster) - min(outer_cluster),
        "segments": float(len(segments)),
    }
    return float(r_inner), float(r_outer), stats


def _candidate_vertex_levels(mesh: trimesh.Trimesh, height: float) -> list[float]:
    raw = sorted(float(v) for v in np.asarray(mesh.vertices)[:, 2])
    tol = max(0.002, height * 1e-5)
    groups: list[list[float]] = []
    for value in raw:
        if not groups or value - groups[-1][-1] > tol:
            groups.append([value])
        else:
            groups[-1].append(value)
    levels = [sum(g) / len(g) for g in groups]
    return [z for z in levels if tol < z < height - tol]


def detect_breakpoints(mesh: trimesh.Trimesh, settings: FlangeFamilySettings) -> list[float]:
    height = float(mesh.extents[2])
    delta = max(0.01, min(0.05, height * 0.0005))
    found: list[float] = []
    for z in _candidate_vertex_levels(mesh, height):
        try:
            ri0, ro0, _ = section_radii(mesh, z - delta, settings)
            ri1, ro1, _ = section_radii(mesh, z + delta, settings)
        except ValueError:
            continue
        if max(abs(ri1 - ri0), abs(ro1 - ro0)) >= settings.step_detection_threshold_mm:
            found.append(z)
    found.extend(float(z) for z in settings.manual_breakpoints_mm if 0 < float(z) < height)
    found.sort()
    merged: list[float] = []
    for value in found:
        if not merged or abs(value - merged[-1]) > max(0.05, height * 1e-4):
            merged.append(value)
        else:
            merged[-1] = (merged[-1] + value) / 2.0
    return merged


def analyze_stl_bytes(data: bytes, source_name: str, settings: FlangeFamilySettings) -> GeometryProfile:
    settings.validate()
    raw = load_stl_bytes(data)
    original_center = [float(x) for x in raw.bounds.mean(axis=0)]
    mesh, axis_name, axis_error, original_size = orient_axisymmetric_mesh(raw, settings.build_axis)
    height = float(mesh.extents[2])
    if height <= 0:
        raise ValueError("model height is zero")
    breakpoints = detect_breakpoints(mesh, settings)
    bounds = [0.0] + breakpoints + [height]
    zones: list[dict[str, float | str | int]] = []
    for zi, (z0, z1) in enumerate(zip(bounds, bounds[1:]), 1):
        zm = (z0 + z1) / 2.0
        ri, ro, stats = section_radii(mesh, zm, settings)
        width = ro - ri
        zone_type = "FLANGE_NARROW" if width >= settings.wide_section_width_threshold_mm else "HUB_WIDE"
        zones.append(
            {
                "zone_index": zi,
                "zone": zone_type,
                "z_bottom_mm": z0,
                "z_top_mm": z1,
                "height_mm": z1 - z0,
                "r_inner_mid_mm": ri,
                "r_outer_mid_mm": ro,
                "radial_width_mm": width,
                "section_inner_spread_mm": stats["inner_spread_mm"],
                "section_outer_spread_mm": stats["outer_spread_mm"],
            }
        )
    ri0, ro0, _ = section_radii(mesh, max(1e-4, height * 1e-5), settings)
    rit, rot, _ = section_radii(mesh, height - max(1e-4, height * 1e-5), settings)
    oriented_size = [float(x) for x in mesh.extents]
    radial_extent_error = abs(oriented_size[0] - oriented_size[1]) / max(oriented_size[0], oriented_size[1])
    return GeometryProfile(
        source_name=source_name,
        build_axis_original=axis_name,
        original_size_mm=original_size,
        oriented_size_mm=oriented_size,
        height_mm=height,
        center_original_mm=original_center,
        r_inner_bottom_mm=ri0,
        r_outer_bottom_mm=ro0,
        r_inner_top_mm=rit,
        r_outer_top_mm=rot,
        breakpoints_mm=breakpoints,
        zones=zones,
        mesh_volume_mm3=float(abs(raw.volume)),
        axisymmetry_error_fraction=max(axis_error, radial_extent_error),
        is_watertight=bool(raw.is_watertight),
        oriented_mesh=mesh,
    )


def analyze_stl_file(path: str | Path, settings: FlangeFamilySettings | None = None) -> tuple[bytes, GeometryProfile]:
    settings = settings or FlangeFamilySettings()
    data = Path(path).read_bytes()
    return data, analyze_stl_bytes(data, Path(path).name, settings)


def _allocate_layer_counts(profile: GeometryProfile, settings: FlangeFamilySettings) -> list[int]:
    zones = profile.zones
    total = max(len(zones), int(round(profile.height_mm / settings.target_layer_height_mm)))
    exact = [float(z["height_mm"]) / profile.height_mm * total for z in zones]
    counts = [max(1, int(math.floor(v))) for v in exact]
    while sum(counts) < total:
        idx = max(range(len(zones)), key=lambda i: exact[i] - counts[i])
        counts[idx] += 1
    while sum(counts) > total:
        candidates = [i for i, count in enumerate(counts) if count > 1]
        if not candidates:
            break
        idx = min(candidates, key=lambda i: exact[i] - counts[i])
        counts[idx] -= 1
    return counts


def _slice_volume(mesh: trimesh.Trimesh, z0: float, z1: float, settings: FlangeFamilySettings) -> tuple[float, tuple[float, float, float, float, float, float]]:
    h = z1 - z0
    eps = min(max(1e-5, h * 1e-5), h * 0.01)
    ri0, ro0, _ = section_radii(mesh, z0 + eps, settings)
    rim, rom, _ = section_radii(mesh, (z0 + z1) / 2.0, settings)
    ri1, ro1, _ = section_radii(mesh, z1 - eps, settings)
    area0 = math.pi * (ro0 * ro0 - ri0 * ri0)
    aream = math.pi * (rom * rom - rim * rim)
    area1 = math.pi * (ro1 * ro1 - ri1 * ri1)
    volume = h * (area0 + 4.0 * aream + area1) / 6.0
    return volume, (ri0, rim, ri1, ro0, rom, ro1)


def _target_energy(layer_index: int) -> float:
    return {1: 42.0, 2: 40.0, 3: 38.0, 4: 36.5}.get(layer_index, 35.0)


C_AXIS_ACCEL_DEG_S2 = 100.0  # ebam.ini, [AXIS_C] MAX_ACCELERATION


def _accel_overhead_min(segments: list["Segment"]) -> float:
    """Насколько разгон стола удлиняет наплавку сверх плана, мин.

    План считает движение идеально, как путь/скорость. Реально линейное ускорение
    точки на радиусе R равно a_C * pi * R / 180: на малом радиусе кольцо целиком
    укладывается в разгон и торможение, и время растёт в разы. Возвращается именно
    надбавка, чтобы её можно было прибавить к плану, не пересчитывая выдержки.
    """
    total_s = 0.0
    for s in segments:
        d = abs(float(s.length_mm))
        v = float(s.linear_speed_mm_s)
        r = max(abs(float(s.radius_start_mm)), abs(float(s.radius_end_mm)))
        a = C_AXIS_ACCEL_DEG_S2 * math.pi * r / 180.0
        if d <= 0.0 or v <= 0.0:
            continue
        ideal_s = d / v
        if a <= 0.0:
            real_s = ideal_s
        elif d >= v * v / a:
            real_s = ideal_s + v / a
        else:
            real_s = 2.0 * math.sqrt(d / a)
        total_s += max(0.0, real_s - ideal_s)
    return total_s / 60.0


def _polar_link_length(r0: float, r1: float, c_deg: float) -> float:
    n = 64
    theta = math.radians(c_deg)
    total = 0.0
    for i in range(n):
        a0, a1 = i / n, (i + 1) / n
        ra, rb = r0 + (r1 - r0) * a0, r0 + (r1 - r0) * a1
        ta, tb = theta * a0, theta * a1
        total += math.hypot(rb * math.cos(tb) - ra * math.cos(ta), rb * math.sin(tb) - ra * math.sin(ta))
    return total


def build_plan(profile: GeometryProfile, settings: FlangeFamilySettings) -> PlanResult:
    settings.validate()
    counts = _allocate_layer_counts(profile, settings)
    layers: list[Layer] = []
    layer_index = 0
    for zone, count in zip(profile.zones, counts):
        z_start = float(zone["z_bottom_mm"])
        z_end = float(zone["z_top_mm"])
        for local in range(count):
            layer_index += 1
            z0 = z_start + (z_end - z_start) * local / count
            z1 = z_start + (z_end - z_start) * (local + 1) / count
            volume, radii = _slice_volume(profile.oriented_mesh, z0, z1, settings)
            ri0, rim, ri1, ro0, rom, ro1 = radii
            width = rom - rim
            max_pitch = settings.flange_max_pitch_mm if str(zone["zone"]) == "FLANGE_NARROW" else settings.hub_max_pitch_mm
            tracks = max(1, int(math.ceil(width / max_pitch - 1e-12)))
            pitch = width / tracks
            centres = [rim + pitch * (j + 0.5) for j in range(tracks)]
            ring_lengths = [2.0 * math.pi * r for r in centres]
            link_lengths = [_polar_link_length(centres[j], centres[j + 1], settings.radial_link_c_deg) for j in range(tracks - 1)]
            hot_length = sum(ring_lengths) + sum(link_lengths)
            deposit_area = volume / hot_length
            e_target = _target_energy(layer_index)
            segments: list[Segment] = []

            def add(kind: str, idx: int, r0: float, r1: float, cdeg: float, length: float, contour: bool) -> None:
                max_linear_wire = (
                    settings.wire_command_max_mm_s
                    * settings.wire_area_mm2()
                    * settings.deposition_efficiency
                    / deposit_area
                )
                time_c = abs(cdeg) / settings.c_command_max_deg_min
                time_wire = length / max_linear_wire / 60.0
                time_min = max(time_c, time_wire)
                linear_speed = length / (time_min * 60.0)
                c_speed = abs(cdeg) / time_min
                if c_speed < settings.c_min_deg_min - 1e-8:
                    raise ValueError(
                        f"layer {layer_index} {kind} {idx}: C={c_speed:.3f} deg/min below "
                        f"{settings.c_min_deg_min:.3f}; reduce pitch or layer height"
                    )
                wire = deposit_area * linear_speed / (settings.wire_area_mm2() * settings.deposition_efficiency)
                edge = settings.edge_current_factor if contour else 1.0
                desired = e_target * deposit_area * linear_speed / settings.voltage_kv * edge
                base = min(settings.base_current_max_ma, max(settings.base_current_min_ma, desired))
                current = min(
                    settings.current_command_max_ma,
                    max(settings.current_min_ma, base * settings.scan_current_reserve_factor),
                )
                line_energy = settings.voltage_kv * current / linear_speed
                segments.append(
                    Segment(
                        layer=layer_index,
                        zone=str(zone["zone"]),
                        kind=kind,
                        index=idx,
                        radius_start_mm=r0,
                        radius_end_mm=r1,
                        c_deg=cdeg,
                        length_mm=length,
                        time_min=time_min,
                        c_speed_deg_min=c_speed,
                        linear_speed_mm_s=linear_speed,
                        e0_ma=current,
                        e2_mm_s=wire,
                        deposit_area_mm2=deposit_area,
                        line_energy_j_mm=line_energy,
                        volume_energy_j_mm3=line_energy / deposit_area,
                        deposited_volume_mm3=deposit_area * length,
                        contour=contour,
                    )
                )

            for j, radius in enumerate(centres):
                add("RING", j + 1, radius, radius, 360.0, ring_lengths[j], j in (0, tracks - 1))
                if j < tracks - 1:
                    add(
                        "RADIAL_LINK",
                        j + 1,
                        radius,
                        centres[j + 1],
                        settings.radial_link_c_deg,
                        link_lengths[j],
                        False,
                    )
            active = sum(s.time_min for s in segments)
            cold = 0.0 if layer_index == sum(counts) else settings.layer_link_c_deg / settings.c_command_max_deg_min
            dwell = 0.0
            if str(zone["zone"]) == "HUB_WIDE" and layer_index < sum(counts):
                dwell = max(0.0, (settings.hub_min_layer_cycle_min - active - cold) * 60.0)
            layers.append(
                Layer(
                    index=layer_index,
                    zone_index=int(zone["zone_index"]),
                    zone=str(zone["zone"]),
                    z_bottom_mm=z0,
                    z_top_mm=z1,
                    height_mm=z1 - z0,
                    r_inner_bottom_mm=ri0,
                    r_inner_top_mm=ri1,
                    r_inner_mid_mm=rim,
                    r_outer_bottom_mm=ro0,
                    r_outer_top_mm=ro1,
                    r_outer_mid_mm=rom,
                    direction="inner_to_outer",
                    tracks=tracks,
                    pitch_mm=pitch,
                    centres_mm=centres,
                    exact_volume_mm3=volume,
                    deposit_area_mm2=deposit_area,
                    target_energy_j_mm3=e_target,
                    active_time_min=active,
                    cold_link_time_min=cold,
                    dwell_s=dwell,
                    cycle_time_min=active + cold + dwell / 60.0,
                    segments=segments,
                )
            )

    # Very small variants are padded only with explicit beam/wire-off dwell.
    # This never hides an overlong path: a path above max_total_time_h fails.
    path_min = sum(l.cycle_time_min for l in layers)
    planned_min = path_min + settings.planned_hmi_verify_min
    if planned_min < settings.min_total_time_h * 60.0 and len(layers) > 1:
        target_min = min(settings.short_build_target_time_h, settings.max_total_time_h) * 60.0
        extra_s = max(0.0, (target_min - planned_min) * 60.0)
        per_layer = extra_s / (len(layers) - 1)
        for layer in layers[:-1]:
            layer.dwell_s += per_layer
            layer.cycle_time_min += per_layer / 60.0

    summary = collect_summary(profile, settings, layers)
    validate_plan(profile, settings, layers, summary)
    return PlanResult(profile=profile, settings=settings, layers=layers, summary=summary)


def collect_summary(profile: GeometryProfile, settings: FlangeFamilySettings, layers: list[Layer]) -> dict[str, Any]:
    segs = [s for layer in layers for s in layer.segments]
    deterministic_min = sum(l.cycle_time_min for l in layers)
    planned_min = deterministic_min + settings.planned_hmi_verify_min
    analytic_volume = sum(l.exact_volume_mm3 for l in layers)
    mesh_volume = profile.mesh_volume_mm3
    volume_error = abs(analytic_volume - mesh_volume) / max(mesh_volume, 1e-9)
    return {
        "app_version": APP_VERSION,
        "classification": "EXPERIMENTAL_QUALIFICATION_CANDIDATE",
        "status": "PENDING",
        "geometry": {
            "source_name": profile.source_name,
            "build_axis_original": profile.build_axis_original,
            "original_size_mm": profile.original_size_mm,
            "oriented_size_mm": profile.oriented_size_mm,
            "outer_diameter_bottom_mm": profile.outer_diameter_bottom_mm,
            "inner_diameter_bottom_mm": profile.inner_diameter_bottom_mm,
            "height_mm": profile.height_mm,
            "breakpoints_mm": profile.breakpoints_mm,
            "zones": [{k: v for k, v in z.items()} for z in profile.zones],
            "mesh_volume_mm3": mesh_volume,
            "planned_profile_volume_mm3": analytic_volume,
            "profile_volume_relative_error": volume_error,
            "axisymmetry_error_fraction": profile.axisymmetry_error_fraction,
            "is_watertight": profile.is_watertight,
        },
        "constraints": {
            "current_min_ma": settings.current_min_ma,
            "current_hard_max_ma": settings.current_hard_max_ma,
            "current_command_max_ma": settings.current_command_max_ma,
            "wire_hard_max_mm_s": settings.wire_hard_max_mm_s,
            "wire_command_max_mm_s": settings.wire_command_max_mm_s,
            "c_min_deg_min": settings.c_min_deg_min,
            "c_hard_max_deg_min": settings.c_hard_max_deg_min,
            "c_command_max_deg_min": settings.c_command_max_deg_min,
            "time_window_h": [settings.min_total_time_h, settings.max_total_time_h],
            "scan_frequency_hz": settings.scan_frequency_hz,
            "scan_scale_x_percent": settings.scan_scale_x_percent,
            "scan_scale_y_percent": settings.scan_scale_y_percent,
            "scan_current_reserve_factor": settings.scan_current_reserve_factor,
        },
        "process": {
            "layers": len(layers),
            "rings_total": sum(l.tracks for l in layers),
            "hot_segments_total": len(segs),
            "current_range_hot_ma": [min(s.e0_ma for s in segs), max(s.e0_ma for s in segs)],
            "wire_feed_range_hot_mm_s": [min(s.e2_mm_s for s in segs), max(s.e2_mm_s for s in segs)],
            "c_speed_range_hot_deg_min": [min(s.c_speed_deg_min for s in segs), max(s.c_speed_deg_min for s in segs)],
            "pitch_range_mm": [min(l.pitch_mm for l in layers), max(l.pitch_mm for l in layers)],
            "volume_energy_range_j_mm3": [min(s.volume_energy_j_mm3 for s in segs), max(s.volume_energy_j_mm3 for s in segs)],
            "deterministic_program_time_h_excluding_M0": deterministic_min / 60.0,
            "planned_hmi_verification_pause_min": settings.planned_hmi_verify_min,
            "planned_total_time_h": planned_min / 60.0,
            # План задаёт скорости, ток и подачу, поэтому считается по идеальному
            # «путь/скорость» и не меняется. Но стол разгоняется всего 100 °/с²
            # (ebam.ini, [AXIS_C]), и фактическая наплавка выходит длиннее — тем
            # заметнее, чем меньше радиус кольца. Эта величина показывает реальное
            # время, ничего не меняя в самой программе.
            "estimated_total_time_with_accel_h": (planned_min + _accel_overhead_min(segs)) / 60.0,
            "all_layers_direction": "inner_to_outer",
            "c_direction": "positive_only",
        },
        "checks": {},
    }


def validate_plan(
    profile: GeometryProfile,
    settings: FlangeFamilySettings,
    layers: list[Layer],
    summary: dict[str, Any],
) -> dict[str, bool]:
    segs = [s for l in layers for s in l.segments]
    checks = {
        "mesh_is_watertight": profile.is_watertight,
        "axisymmetry_within_tolerance": profile.axisymmetry_error_fraction <= settings.axisymmetry_tolerance_fraction,
        "annular_profile_has_positive_width": all(l.r_outer_mid_mm > l.r_inner_mid_mm for l in layers),
        "profile_volume_matches_mesh_within_2_percent": summary["geometry"]["profile_volume_relative_error"] <= 0.02,
        "all_layers_inner_to_outer": all(
            l.direction == "inner_to_outer" and all(b > a for a, b in zip(l.centres_mm, l.centres_mm[1:]))
            for l in layers
        ),
        "ring_envelopes_match_profile": all(
            abs((l.centres_mm[0] - l.pitch_mm / 2.0) - l.r_inner_mid_mm) < 1e-6
            and abs((l.centres_mm[-1] + l.pitch_mm / 2.0) - l.r_outer_mid_mm) < 1e-6
            for l in layers
        ),
        "current_within_command_limits": all(settings.current_min_ma <= s.e0_ma <= settings.current_command_max_ma for s in segs),
        "current_below_hard_limit": max(s.e0_ma for s in segs) < settings.current_hard_max_ma + 1e-9,
        "wire_within_command_limit": all(0 < s.e2_mm_s <= settings.wire_command_max_mm_s + 1e-8 for s in segs),
        "c_within_command_limits": all(settings.c_min_deg_min - 1e-8 <= s.c_speed_deg_min <= settings.c_command_max_deg_min + 1e-8 for s in segs),
        "c_positive_only": all(s.c_deg > 0 for s in segs),
        "volume_balance_exact": abs(sum(s.deposited_volume_mm3 for s in segs) - sum(l.exact_volume_mm3 for l in layers)) < 1e-5,
        "planned_time_between_4_and_6_hours": settings.min_total_time_h <= summary["process"]["planned_total_time_h"] <= settings.max_total_time_h,
    }
    summary["checks"] = checks
    summary["status"] = "PASS" if all(checks.values()) else "FAIL"
    return checks


def linuxcnc_comment_errors_text(text: str) -> list[str]:
    errors: list[str] = []
    depth = 0
    opening: tuple[int, int] | None = None
    for line_no, line in enumerate(text.splitlines(), 1):
        for column, char in enumerate(line, 1):
            if char == "(":
                if depth:
                    errors.append(f"nested opening parenthesis at {line_no}:{column}")
                else:
                    opening = (line_no, column)
                depth += 1
            elif char == ")":
                if depth == 0:
                    errors.append(f"unmatched closing parenthesis at {line_no}:{column}")
                else:
                    depth -= 1
                    if depth == 0:
                        opening = None
    if depth:
        errors.append(f"unclosed parenthesis comment opened at {opening}")
    return errors


def _inverse_feed(time_min: float) -> float:
    return 1.0 / time_min


def _linuxcnc_comment_safe(value: Any) -> str:
    """Return text which cannot create nested LinuxCNC round comments."""
    text = str(value).replace("(", "[").replace(")", "]")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def render_gcode(
    plan: PlanResult,
    *,
    hot: bool,
    source_sha256: str,
    title: str,
    layers_override: list[Layer] | None = None,
) -> str:
    layers = layers_override or plan.layers
    s = plan.settings
    p = plan.profile
    total_layers = len(layers)
    safe_z = layers[-1].z_top_mm + s.safe_clearance_mm
    lines = [
        f"(EBAM G-CODE STUDIO {APP_VERSION} - {_linuxcnc_comment_safe(title)})",
        f"(SOURCE: {_linuxcnc_comment_safe(p.source_name)})",
        f"(STL_SHA256: {source_sha256})",
        f"(GEOMETRY: OD_BOTTOM={p.outer_diameter_bottom_mm:.3f} ID_BOTTOM={p.inner_diameter_bottom_mm:.3f} H={p.height_mm:.3f})",
        f"(BUILD AXIS IN SOURCE STL: {p.build_axis_original})",
        f"(HARD LIMITS: HOT E0={s.current_min_ma:.1f} THROUGH {s.current_hard_max_ma:.1f}mA; E2=0 THROUGH {s.wire_hard_max_mm_s:.1f}mm/s; C={s.c_min_deg_min:.0f} THROUGH {s.c_hard_max_deg_min:.0f}deg/min)",
        f"(COMMAND MARGINS: E0 LE {s.current_command_max_ma:.1f}mA; E2 LE {s.wire_command_max_mm_s:.1f}mm/s; C LE {s.c_command_max_deg_min:.1f}deg/min)",
        f"(HMI SCAN REQUIRED: {s.scan_pattern}; {s.scan_frequency_hz:.0f}Hz; SCALE_X={s.scan_scale_x_percent:.1f}percent; SCALE_Y={s.scan_scale_y_percent:.1f}percent)",
        f"(SCAN CURRENT MODEL: BASE {s.base_current_min_ma:.1f} THROUGH {s.base_current_max_ma:.1f}mA; FACTOR {s.scan_current_reserve_factor:.3f}; FINAL {s.current_min_ma:.1f} THROUGH {s.current_command_max_ma:.1f}mA)",
        "(SCAN FACTOR IS AN EXPERIMENTAL COMMISSIONING ASSUMPTION, NOT A CALIBRATED POWER LAW)",
        "(REQUIRED: CURR WIRE FEED OVERRIDES AT 100 PERCENT; VERIFY HMI SCAN BEFORE START)",
        "(FULL DRY RUN AND SHORT COUPON TEST REQUIRED BEFORE FULL HOT BUILD)",
        "(G93 F IS INVERSE MOVE TIME; G4 P IS SECONDS)",
        "(C ALWAYS POSITIVE; EVERY LAYER RUNS INNER TO OUTER)",
        f"(HOT OUTPUTS: {'ENABLED' if hot else 'FORCED ZERO DRY RUN'})",
        "G21 (mm)",
        "G90 (absolute coordinates)",
        "G94 (feed per minute)",
        "G64 P0.080 Q0.000",
        "M429 (identity kinematics if available)",
        "M68 E0 Q0.000 (beam OFF safe state)",
        "M68 E2 Q0.000 (wire OFF safe state)",
        f"M68 E1 Q{s.focus_q:.3f} (focus verify on machine)",
        f"G0 Z{s.safe_clearance_mm:.3f} (initial clearance above substrate)",
        "G0 B0.000",
        f"G0 X{layers[0].centres_mm[0]:.4f} Y0.000",
        "G1 Z0.000 F240.0",
        "M0 (MANDATORY HMI CHECK SCAN 300HZ X10 Y10 CURR WIRE OVERRIDES 100 PERCENT)",
        "G93",
        "G91",
    ]
    current_radius = layers[0].centres_mm[0]
    previous_zone = layers[0].zone_index
    for idx, layer in enumerate(layers):
        lines.append(
            f"(LAYER {layer.index}/{total_layers} Z={layer.z_bottom_mm:.4f}..{layer.z_top_mm:.4f} "
            f"ZONE={layer.zone} DIR={layer.direction} TRACKS={layer.tracks} "
            f"PITCH={layer.pitch_mm:.4f} ADEP={layer.deposit_area_mm2:.4f} "
            f"ACTIVE={layer.active_time_min:.4f}min DWELL={layer.dwell_s:.1f}s)"
        )
        if idx > 0:
            previous = layers[idx - 1]
            dx = layer.centres_mm[0] - current_radius
            dz = layer.z_bottom_mm - previous.z_bottom_mm
            link_t = s.layer_link_c_deg / s.c_command_max_deg_min
            lines.extend(
                [
                    "M68 E2 Q0.000 (cold interlayer transition)",
                    "M68 E0 Q0.000",
                    f"G1 X{dx:.4f} Z{dz:.4f} C{s.layer_link_c_deg:.3f} F{_inverse_feed(link_t):.6f}",
                ]
            )
            if previous.dwell_s > 1e-9:
                lines.append(f"G4 P{previous.dwell_s:.3f} (calculated thermal dwell beam and wire off)")
        if layer.zone_index != previous_zone:
            lines.extend(
                [
                    "G90",
                    f"M0 (MANDATORY PROFILE STEP CHECK AT Z{layer.z_bottom_mm:.3f}; VERIFY SCAN POOL WIRE FOCUS)",
                    "G91",
                ]
            )
            previous_zone = layer.zone_index
        first = layer.segments[0]
        e0 = first.e0_ma if hot else 0.0
        e2 = first.e2_mm_s if hot else 0.0
        lines.append(f"M67 E0 Q{e0:.3f} (synchronized layer hot state)")
        lines.append(f"M67 E2 Q{e2:.3f}")
        for si, seg in enumerate(layer.segments):
            e0 = seg.e0_ma if hot else 0.0
            e2 = seg.e2_mm_s if hot else 0.0
            if si:
                lines.append(f"M67 E0 Q{e0:.3f}")
                lines.append(f"M67 E2 Q{e2:.3f}")
            if seg.kind == "RING":
                lines.append(
                    f"G1 C360.000 F{_inverse_feed(seg.time_min):.6f} "
                    f"(RING {seg.index}/{layer.tracks} R={seg.radius_start_mm:.4f} "
                    f"CSPD={seg.c_speed_deg_min:.2f} E0={e0:.3f} E2={e2:.3f} "
                    f"QV={seg.volume_energy_j_mm3:.2f})"
                )
            else:
                dx = seg.radius_end_mm - seg.radius_start_mm
                lines.append(
                    f"G1 X{dx:.4f} C{seg.c_deg:.3f} F{_inverse_feed(seg.time_min):.6f} "
                    f"(HOT LINK {seg.index} CSPD={seg.c_speed_deg_min:.2f} E0={e0:.3f} E2={e2:.3f})"
                )
            current_radius = seg.radius_end_mm
    lines.extend(
        [
            "M68 E2 Q0.000 (wire OFF)",
            "M68 E0 Q0.000 (beam OFF)",
            "G90",
            "G94",
            f"G0 Z{safe_z:.4f} (safe retract)",
            "M2",
        ]
    )
    text = "\n".join(lines) + "\n"
    errors = linuxcnc_comment_errors_text(text)
    if errors:
        raise ValueError(f"LinuxCNC comment syntax failed: {errors}")
    return text


def _shift_test_layers(source: list[Layer]) -> list[Layer]:
    shifted: list[Layer] = []
    z = 0.0
    for new_index, layer in enumerate(source, 1):
        dz = z - layer.z_bottom_mm
        segs = [replace(seg, layer=new_index) for seg in layer.segments]
        shifted.append(
            replace(
                layer,
                index=new_index,
                z_bottom_mm=layer.z_bottom_mm + dz,
                z_top_mm=layer.z_top_mm + dz,
                dwell_s=0.0,
                cycle_time_min=layer.active_time_min + layer.cold_link_time_min,
                segments=segs,
            )
        )
        z += layer.height_mm
    return shifted


def _write_schedule_csvs(out: Path, plan: PlanResult) -> None:
    with (out / "flange_family_layers.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "index", "zone_index", "zone", "z_bottom_mm", "z_top_mm", "height_mm",
            "r_inner_mid_mm", "r_outer_mid_mm", "tracks", "pitch_mm", "exact_volume_mm3",
            "deposit_area_mm2", "active_time_min", "dwell_s", "cycle_time_min",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for l in plan.layers:
            w.writerow({k: getattr(l, k) for k in fields})
    with (out / "flange_family_segments.csv").open("w", newline="", encoding="utf-8") as f:
        fields = list(asdict(plan.layers[0].segments[0]).keys())
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for layer in plan.layers:
            for seg in layer.segments:
                w.writerow(asdict(seg))


def algorithm_markdown() -> str:
    return """# Алгоритм Flange-family v4.2.9.19

1. STL загружается как замкнутая треугольная сетка. В автоматическом режиме осью
   построения выбирается та исходная ось, у которой две поперечные габаритные
   величины наиболее близки. Оператор может принудительно выбрать X, Y или Z.
2. Модель центрируется относительно оси вращения, нижняя точка переносится на
   Z=0. Проверяются замкнутость и осесимметрия.
3. На характерных уровнях STL строятся горизонтальные сечения. По двум крайним
   кластерам радиусов восстанавливаются внутренний и наружный профили.
4. Радиальные скачки профиля становятся точными границами зон. Поэтому изменение
   диаметра отверстия, наружного диаметра, высоты, конуса или положения плеча не
   требует правки исходного кода приложения.
5. Общее число слоёв равно приблизительно H / заданная_высота_слоя. Слои
   пропорционально распределяются между зонами так, чтобы каждая обнаруженная
   ступень оставалась точной границей.
6. В каждом слое ширина кольца делится на N=ceil((Rout-Rin)/макс_шаг) дорожек.
   Центры дорожек полностью покрывают annulus от Rin до Rout. Порядок всегда
   изнутри наружу, C всегда положительная.
7. Объём слоя рассчитывается интегрированием площади annulus по Z. Этот объём
   делится на суммарную длину кольцевых проходов и коротких горячих связок — так
   получается требуемая площадь осаждения на единицу длины.
8. Для каждого сегмента время выбирается как максимум времени по пределу C и
   времени по пределу подачи проволоки. Затем рассчитываются фактические C, E2,
   линейная скорость, E0, линейная и объёмная энергия.
9. Ток при развёртке считается как clamp(clamp(I_energy, Ibase_min,
   Ibase_max) * reserve, Imin, Icommand_max). Параметры 300 Гц, X10%, Y10%
   записываются как обязательная настройка HMI; приложение не выдаёт вымышленные
   M-коды управления развёрткой.
10. Перед выдачей горячего файла проверяются: геометрия, баланс объёма, направление
    колец, E0/E2/C, время 4–6 ч и отсутствие вложенных комментариев LinuxCNC.
    Комплект всегда содержит полный dry-run и короткие квалификационные тесты.

Статус PASS означает только статическую согласованность. После изменения детали или
режима обязательны dry-run, пробные слои, контроль ванны, измерение валиков и
металлографическая квалификация до запуска полного горячего файла.
"""


def readme_text(plan: PlanResult) -> str:
    g = plan.summary["geometry"]
    p = plan.summary["process"]
    return f"""# EBAM G-code Studio {APP_VERSION} — Flange-family

Сформирован параметрический комплект для `{g['source_name']}`.

- нижний OD: {g['outer_diameter_bottom_mm']:.3f} мм;
- нижний ID: {g['inner_diameter_bottom_mm']:.3f} мм;
- высота: {g['height_mm']:.3f} мм;
- границы зон Z: {g['breakpoints_mm']};
- слоёв: {p['layers']}, колец: {p['rings_total']};
- E0: {p['current_range_hot_ma'][0]:.3f}–{p['current_range_hot_ma'][1]:.3f} мА;
- E2: {p['wire_feed_range_hot_mm_s'][0]:.3f}–{p['wire_feed_range_hot_mm_s'][1]:.3f} мм/с;
- C: {p['c_speed_range_hot_deg_min'][0]:.3f}–{p['c_speed_range_hot_deg_min'][1]:.3f} град/мин;
- плановое время: {p['planned_total_time_h']:.3f} ч;
- направление: каждое кольцо изнутри наружу, только C+;
- HMI-развёртка: 300 Гц, X=10%, Y=10%.

Это экспериментальный квалификационный кандидат. Начинать только с полного dry-run
и короткого теста. Горячий полный файл не является готовой производственной картой.
"""


def generate_release(
    stl_data: bytes,
    source_name: str,
    out_dir: str | Path,
    settings: FlangeFamilySettings | None = None,
) -> tuple[PlanResult, Path]:
    settings = settings or FlangeFamilySettings()
    profile = analyze_stl_bytes(stl_data, source_name, settings)
    plan = build_plan(profile, settings)
    if plan.summary["status"] != "PASS":
        failed = [k for k, v in plan.summary["checks"].items() if not v]
        raise ValueError(f"hot release blocked; failed checks: {failed}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe_source_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_name) or "source.stl"
    source_path = out / safe_source_name
    source_path.write_bytes(stl_data)
    source_hash = sha256_bytes(stl_data)
    hot = render_gcode(plan, hot=True, source_sha256=source_hash, title="FULL FLANGE FAMILY EXPERIMENTAL")
    dry = render_gcode(plan, hot=False, source_sha256=source_hash, title="FULL DRY RUN")
    first_test = plan.layers[: min(3, len(plan.layers))]
    last_zone_index = max(l.zone_index for l in plan.layers)
    hub_source = [l for l in plan.layers if l.zone_index == last_zone_index][:6]
    hub_test_layers = _shift_test_layers(hub_source)
    test_a = render_gcode(plan, hot=True, source_sha256=source_hash, title="FIRST ZONE 3 LAYER TEST", layers_override=first_test)
    test_b = render_gcode(plan, hot=True, source_sha256=source_hash, title="LAST ZONE 6 LAYER TEST", layers_override=hub_test_layers)
    names = {
        "FlangeFamily_FULL_EXPERIMENTAL.ngc": hot,
        "FlangeFamily_FULL_DRY_RUN_E0E2_ZERO.ngc": dry,
        "FlangeFamily_TEST_FIRST_ZONE_3_LAYERS.ngc": test_a,
        "FlangeFamily_TEST_LAST_ZONE_6_LAYERS.ngc": test_b,
    }
    comment_check: dict[str, list[str]] = {}
    for name, text in names.items():
        errors = linuxcnc_comment_errors_text(text)
        comment_check[name] = errors
        if errors:
            raise ValueError(f"comment syntax failed in {name}: {errors}")
        (out / name).write_text(text, encoding="utf-8")
    plan.summary["checks"]["linuxcnc_comments_balanced_and_not_nested"] = not any(comment_check.values())
    plan.summary["linuxcnc_comment_syntax"] = comment_check
    plan.summary["status"] = "PASS" if all(plan.summary["checks"].values()) else "FAIL"
    (out / "flange_family_validation.json").write_text(
        json.dumps(plan.summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "flange_family_settings.json").write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Do not call dataclasses.asdict(profile): it recursively deep-copies the
    # trimesh object.  The public geometry report is deliberately explicit.
    profile_data = {
        "source_name": profile.source_name,
        "build_axis_original": profile.build_axis_original,
        "original_size_mm": profile.original_size_mm,
        "oriented_size_mm": profile.oriented_size_mm,
        "height_mm": profile.height_mm,
        "center_original_mm": profile.center_original_mm,
        "r_inner_bottom_mm": profile.r_inner_bottom_mm,
        "r_outer_bottom_mm": profile.r_outer_bottom_mm,
        "r_inner_top_mm": profile.r_inner_top_mm,
        "r_outer_top_mm": profile.r_outer_top_mm,
        "breakpoints_mm": profile.breakpoints_mm,
        "zones": profile.zones,
        "mesh_volume_mm3": profile.mesh_volume_mm3,
        "axisymmetry_error_fraction": profile.axisymmetry_error_fraction,
        "is_watertight": profile.is_watertight,
    }
    (out / "flange_family_profile.json").write_text(
        json.dumps(profile_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_schedule_csvs(out, plan)
    (out / "README_FIRST_RU.md").write_text(readme_text(plan), encoding="utf-8")
    (out / "ALGORITHM_FLANGE_FAMILY_RU.md").write_text(algorithm_markdown(), encoding="utf-8")
    manifest = []
    for path in sorted(out.iterdir()):
        if path.is_file() and path.name != "MANIFEST_SHA256.txt":
            manifest.append(f"{sha256_file(path)}  {path.name}")
    (out / "MANIFEST_SHA256.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    zip_path = out.parent / f"{out.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out.iterdir()):
            if path.is_file():
                zf.write(path, arcname=f"{out.name}/{path.name}")
    return plan, zip_path


def render_streamlit_page() -> None:
    """Render the specialized Flange-family workflow inside the main app."""
    import pandas as pd
    import streamlit as st

    st.header("Flange-family: STL → слои → C-кольца")
    st.warning(
        "Режим формирует экспериментальный квалификационный комплект. PASS — это статическая проверка, "
        "а не разрешение на горячий производственный запуск."
    )
    with st.sidebar:
        st.header("Flange-family")
        uploaded = st.file_uploader("Осесимметричная STL", type=["stl"], key="flange_family_stl")
        axis = st.selectbox("Ось высоты в STL", ["AUTO", "X", "Y", "Z"], index=0)
        layer_h = st.number_input("Целевая высота слоя, мм", 0.2, 10.0, 1.5, 0.1)
        flange_pitch = st.number_input("Макс. шаг широкой зоны, мм", 0.5, 10.0, 2.6, 0.1)
        hub_pitch = st.number_input("Макс. шаг узкой зоны, мм", 0.5, 10.0, 4.0, 0.1)
        threshold = st.number_input("Граница широкой зоны, мм", 1.0, 100.0, 40.0, 1.0)
        manual_breaks = st.text_input("Доп. границы Z, мм", value="", help="Разделитель — пробел или точка с запятой. Например: 23; 31.5")
        st.caption("Развёртка HMI: концентрические окружности, 300 Гц, X10%, Y10%")
        reserve = st.number_input("Экспериментальный запас тока", 0.5, 2.0, 1.10, 0.01)
        current_min = st.number_input("Мин. горячий ток, мА", 0.0, 100.0, 25.0, 0.5)
        current_max = st.number_input("Командный максимум тока, мА", 0.1, 100.0, 39.5, 0.5)
        wire_max = st.number_input("Командный максимум E2, мм/с", 0.1, 100.0, 49.5, 0.5)
        c_min = st.number_input("Минимум C, град/мин", 1.0, 5000.0, 450.0, 10.0)
        c_max = st.number_input("Командный максимум C, град/мин", 1.0, 5000.0, 599.5, 10.0)
        hub_cycle = st.number_input("Мин. цикл узкого слоя, мин", 0.0, 30.0, 5.0, 0.25)
    if uploaded is None:
        st.info("Загрузите STL слева. Приложение покажет найденные размеры и зоны до генерации.")
        st.stop()
    try:
        breaks = [float(x.strip().replace(",", ".")) for x in re.split(r"[;\s]+", manual_breaks) if x.strip()]
        settings = FlangeFamilySettings(
            build_axis=axis,
            target_layer_height_mm=float(layer_h),
            flange_max_pitch_mm=float(flange_pitch),
            hub_max_pitch_mm=float(hub_pitch),
            wide_section_width_threshold_mm=float(threshold),
            manual_breakpoints_mm=breaks,
            scan_current_reserve_factor=float(reserve),
            current_min_ma=float(current_min),
            current_command_max_ma=float(current_max),
            wire_command_max_mm_s=float(wire_max),
            c_min_deg_min=float(c_min),
            c_command_max_deg_min=float(c_max),
            hub_min_layer_cycle_min=float(hub_cycle),
        )
        data = uploaded.getvalue()
        profile = analyze_stl_bytes(data, uploaded.name, settings)
        plan = build_plan(profile, settings)
    except Exception as exc:
        st.error(f"Flange-family: расчёт остановлен: {exc}")
        st.stop()
    g = plan.summary["geometry"]
    p = plan.summary["process"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OD снизу", f"{g['outer_diameter_bottom_mm']:.3f} мм")
    c2.metric("ID снизу", f"{g['inner_diameter_bottom_mm']:.3f} мм")
    c3.metric("Высота", f"{g['height_mm']:.3f} мм")
    c4.metric("Плановое время", f"{p['planned_total_time_h']:.3f} ч")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Слои", p["layers"])
    c6.metric("Кольца", p["rings_total"])
    c7.metric("E0", f"{p['current_range_hot_ma'][0]:.2f}…{p['current_range_hot_ma'][1]:.2f} мА")
    c8.metric("E2", f"{p['wire_feed_range_hot_mm_s'][0]:.2f}…{p['wire_feed_range_hot_mm_s'][1]:.2f} мм/с")
    st.subheader("Найденные зоны профиля")
    st.dataframe(pd.DataFrame(profile.zones), hide_index=True, width="stretch")
    st.subheader("Проверки")
    check_df = pd.DataFrame(
        [{"проверка": k, "результат": "PASS" if v else "FAIL"} for k, v in plan.summary["checks"].items()]
    )
    st.dataframe(check_df, hide_index=True, width="stretch")
    if plan.summary["status"] != "PASS":
        st.error("Горячий комплект заблокирован. Исправьте геометрию или параметры, отмеченные FAIL.")
        st.download_button(
            "Скачать отчёт расчёта JSON",
            json.dumps(plan.summary, ensure_ascii=False, indent=2),
            file_name="flange_family_blocked_report.json",
            mime="application/json",
        )
        st.stop()
    st.success("Статические проверки PASS. Генерация всё равно требует dry-run и квалификационных тестов.")
    if st.button("Сформировать проверенный комплект", type="primary"):
        try:
            root = Path(tempfile.mkdtemp(prefix="ebam_flange_family_"))
            out = root / "FlangeFamily_generated"
            generated_plan, zip_path = generate_release(data, uploaded.name, out, settings)
            st.session_state["flange_family_release"] = {
                "zip": zip_path.read_bytes(),
                "hot": (out / "FlangeFamily_FULL_EXPERIMENTAL.ngc").read_bytes(),
                "dry": (out / "FlangeFamily_FULL_DRY_RUN_E0E2_ZERO.ngc").read_bytes(),
                "validation": (out / "flange_family_validation.json").read_bytes(),
                "time": generated_plan.summary["process"]["planned_total_time_h"],
            }
        except Exception as exc:
            st.error(f"Не удалось сформировать комплект: {exc}")
    release = st.session_state.get("flange_family_release")
    if release:
        st.download_button("Скачать полный комплект ZIP", release["zip"], "FlangeFamily_v42919_release.zip", "application/zip")
        st.download_button("Скачать полный dry-run", release["dry"], "FlangeFamily_FULL_DRY_RUN_E0E2_ZERO.ngc", "text/plain")
        st.download_button("Скачать горячий кандидат", release["hot"], "FlangeFamily_FULL_EXPERIMENTAL.ngc", "text/plain")
        st.download_button("Скачать отчёт проверки", release["validation"], "flange_family_validation.json", "application/json")


__all__ = [
    "APP_VERSION",
    "FlangeFamilySettings",
    "GeometryProfile",
    "Segment",
    "Layer",
    "PlanResult",
    "analyze_stl_bytes",
    "analyze_stl_file",
    "build_plan",
    "generate_release",
    "render_gcode",
    "linuxcnc_comment_errors_text",
    "algorithm_markdown",
    "render_streamlit_page",
]
