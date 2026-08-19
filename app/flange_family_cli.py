"""Command-line Flange-family package generator for EBAM G-code Studio v4.2.9.19."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from flange_family_generator import FlangeFamilySettings, generate_release


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate a validated inner-to-outer rotary-C Flange-family qualification package."
    )
    p.add_argument("stl", type=Path, help="axisymmetric STL input")
    p.add_argument("--out", type=Path, default=Path("FlangeFamily_generated"), help="output directory")
    p.add_argument("--axis", choices=["AUTO", "X", "Y", "Z"], default="AUTO")
    p.add_argument("--layer-height", type=float, default=1.5)
    p.add_argument("--flange-pitch", type=float, default=2.6)
    p.add_argument("--hub-pitch", type=float, default=4.0)
    p.add_argument("--wide-threshold", type=float, default=40.0)
    p.add_argument("--break-z", type=float, action="append", default=[], help="extra exact Z boundary; repeatable")
    p.add_argument("--scan-reserve", type=float, default=1.10)
    p.add_argument("--current-min", type=float, default=25.0)
    p.add_argument("--current-command-max", type=float, default=39.5)
    p.add_argument("--wire-command-max", type=float, default=49.5)
    p.add_argument("--c-min", type=float, default=450.0)
    p.add_argument("--c-command-max", type=float, default=599.5)
    p.add_argument("--hub-cycle-min", type=float, default=5.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    data = args.stl.read_bytes()
    settings = FlangeFamilySettings(
        build_axis=args.axis,
        target_layer_height_mm=args.layer_height,
        flange_max_pitch_mm=args.flange_pitch,
        hub_max_pitch_mm=args.hub_pitch,
        wide_section_width_threshold_mm=args.wide_threshold,
        manual_breakpoints_mm=args.break_z,
        scan_current_reserve_factor=args.scan_reserve,
        current_min_ma=args.current_min,
        current_command_max_ma=args.current_command_max,
        wire_command_max_mm_s=args.wire_command_max,
        c_min_deg_min=args.c_min,
        c_command_max_deg_min=args.c_command_max,
        hub_min_layer_cycle_min=args.hub_cycle_min,
    )
    plan, zip_path = generate_release(data, args.stl.name, args.out, settings)
    print(json.dumps(plan.summary, ensure_ascii=False, indent=2))
    print(f"ZIP: {zip_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
