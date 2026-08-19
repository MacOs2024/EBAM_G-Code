from __future__ import annotations
import argparse
import json
from pathlib import Path

from ebam_gcode_studio.core import (
    ProcessSettings, settings_from_dict, generate_from_stl_file, generate_from_polygons_2d,
    load_polygons_from_csv, load_polygons_from_dxf, recommend_settings_from_summary, load_mesh_any,
    mesh_summary, polygon_summary, save_result
)


def main():
    ap = argparse.ArgumentParser(description="EBAM STL/DXF/CSV to G-code generator")
    ap.add_argument("input", help="input STL/DXF/CSV file")
    ap.add_argument("output_prefix", help="output prefix, e.g. out/part")
    ap.add_argument("--settings", help="JSON settings file", default=None)
    ap.add_argument("--height", type=float, default=100.0, help="height for 2D DXF/CSV, mm")
    ap.add_argument("--layer", type=float, default=None, help="layer height mm")
    ap.add_argument("--spacing", type=float, default=None, help="hatch spacing mm")
    ap.add_argument("--quality", choices=["quality", "balanced", "speed"], default="balanced")
    ap.add_argument("--material", default="stainless_steel_12_wire", help="material key")
    ap.add_argument("--contours", type=int, default=None, help="contour passes per layer")
    args = ap.parse_args()

    inp = Path(args.input)
    ext = inp.suffix.lower()

    if args.settings:
        with open(args.settings, "r", encoding="utf-8") as f:
            settings = settings_from_dict(json.load(f))
    else:
        if ext == ".stl":
            mesh = load_mesh_any(inp)
            summary = mesh_summary(mesh)
        elif ext == ".dxf":
            polys = load_polygons_from_dxf(inp)
            summary = polygon_summary(polys, args.height)
        elif ext in [".csv", ".txt"]:
            polys = load_polygons_from_csv(inp)
            summary = polygon_summary(polys, args.height)
        else:
            raise SystemExit("Unsupported input type. Use STL, DXF, CSV, TXT.")
        settings = recommend_settings_from_summary(summary, args.quality, material_key=args.material)

    if args.layer is not None:
        settings.layer_height = args.layer
    if args.spacing is not None:
        settings.hatch_spacing = args.spacing
    if args.contours is not None:
        settings.contour_passes = args.contours

    if ext == ".stl":
        result = generate_from_stl_file(inp, settings)
    elif ext == ".dxf":
        result = generate_from_polygons_2d(load_polygons_from_dxf(inp), args.height, settings)
    elif ext in [".csv", ".txt"]:
        result = generate_from_polygons_2d(load_polygons_from_csv(inp), args.height, settings)
    else:
        raise SystemExit("Unsupported input type. Use STL, DXF, CSV, TXT.")

    files = save_result(result, args.output_prefix, settings=settings)
    print("Generated:")
    for k, p in files.items():
        print(f"  {k}: {p}")
    print(f"Layers: {result.stats.get('layers_total')}")
    print(f"Hatch segments: {result.stats.get('segments_total')}")
    print(f"Contour segments: {result.stats.get('contour_segments_total')}")
    print(f"Active path: {result.stats.get('active_path_length_m'):.2f} m")

if __name__ == "__main__":
    main()
