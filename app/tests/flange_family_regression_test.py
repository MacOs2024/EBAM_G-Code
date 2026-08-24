"""Regression checks for the v4.2.9.19 parameterized Flange-family workflow."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import numpy as np
import trimesh

from flange_family_generator import (
    FlangeFamilySettings,
    analyze_stl_file,
    build_plan,
    generate_release,
    linuxcnc_comment_errors_text,
)


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_STL = ROOT / "qualification" / "FlangeFamily_v42919_REFERENCE_ID40" / "Flange1_2_.STL"
# Историческое имя эталона содержит круглые скобки. В комментарии LinuxCNC они
# недопустимы, генератор обязан заменить их на [2] — проверяем это на копии,
# названной по-исходному.
REFERENCE_NAME = "Flange1(2).STL"


def close(a: float, b: float, tol: float) -> None:
    assert abs(a - b) <= tol, (a, b, tol)


def command_values(text: str, channel: int) -> list[float]:
    return [float(v) for v in re.findall(rf"\bM6[78]\s+E{channel}\s+Q([-+0-9.]+)", text)]


def check_package(stl: Path, name: str) -> dict[str, float | int | str]:
    settings = FlangeFamilySettings()
    data, profile = analyze_stl_file(stl, settings)
    plan = build_plan(profile, settings)
    assert plan.summary["status"] == "PASS", plan.summary["checks"]
    assert profile.is_watertight
    assert plan.summary["process"]["all_layers_direction"] == "inner_to_outer"
    assert all(all(b > a for a, b in zip(layer.centres_mm, layer.centres_mm[1:])) for layer in plan.layers)
    assert 4.0 <= plan.summary["process"]["planned_total_time_h"] <= 6.0
    with tempfile.TemporaryDirectory(prefix="flange_family_test_") as td:
        out = Path(td) / name
        generated, zip_path = generate_release(data, stl.name, out, settings)
        assert zip_path.is_file() and zip_path.stat().st_size > 0
        hot = (out / "FlangeFamily_FULL_EXPERIMENTAL.ngc").read_text(encoding="utf-8")
        dry = (out / "FlangeFamily_FULL_DRY_RUN_E0E2_ZERO.ngc").read_text(encoding="utf-8")
        assert not linuxcnc_comment_errors_text(hot)
        assert "(SOURCE: Flange1[2].STL)" in hot if stl.name == REFERENCE_NAME else True
        assert "M0 (MANDATORY HMI CHECK SCAN 300HZ X10 Y10" in hot
        assert hot.index("M0 (MANDATORY HMI CHECK SCAN 300HZ X10 Y10") < hot.index("M67 E0 Q")
        hot_e0, hot_e2 = command_values(hot, 0), command_values(hot, 2)
        dry_e0, dry_e2 = command_values(dry, 0), command_values(dry, 2)
        assert min(v for v in hot_e0 if v > 0) >= settings.current_min_ma
        assert max(hot_e0) <= settings.current_command_max_ma
        assert max(hot_e2) <= settings.wire_command_max_mm_s
        assert max(dry_e0) == 0.0 and max(dry_e2) == 0.0
        assert generated.summary["checks"]["linuxcnc_comments_balanced_and_not_nested"]
    return {
        "status": plan.summary["status"],
        "od_mm": profile.outer_diameter_bottom_mm,
        "id_mm": profile.inner_diameter_bottom_mm,
        "height_mm": profile.height_mm,
        "layers": len(plan.layers),
        "rings": plan.summary["process"]["rings_total"],
        "time_h": plan.summary["process"]["planned_total_time_h"],
    }


def main() -> None:
    assert REFERENCE_STL.is_file(), REFERENCE_STL
    with tempfile.TemporaryDirectory(prefix="flange_family_reference_") as td:
        named = Path(td) / REFERENCE_NAME
        named.write_bytes(REFERENCE_STL.read_bytes())
        reference = check_package(named, "reference")
    close(float(reference["od_mm"]), 164.0, 0.02)
    close(float(reference["id_mm"]), 40.0, 0.02)
    close(float(reference["height_mm"]), 47.5, 0.02)
    assert int(reference["layers"]) == 32
    assert int(reference["rings"]) == 426

    with tempfile.TemporaryDirectory(prefix="flange_family_variant_") as td:
        mesh = trimesh.load(REFERENCE_STL, force="mesh", process=True)
        center = mesh.bounds.mean(axis=0)
        mesh.apply_translation(-center)
        # Source STL build axis is Y: change radial X/Z by +2% and height Y by -2%.
        mesh.vertices = np.asarray(mesh.vertices) * np.array([1.02, 0.98, 1.02])
        mesh.apply_translation(center)
        variant_path = Path(td) / "Flange1_variant(geometry).STL"
        mesh.export(variant_path)
        variant = check_package(variant_path, "variant")
        close(float(variant["od_mm"]), 167.28, 0.05)
        close(float(variant["id_mm"]), 40.80, 0.05)
        close(float(variant["height_mm"]), 46.55, 0.05)

    app = (ROOT / "app.py").read_text(encoding="utf-8")
    core = (ROOT / "ebam_gcode_studio" / "core.py").read_text(encoding="utf-8")
    assert "Flange-family R6 (STL → C-кольца)" in app
    # Версия ядра растёт с каждым релизом: проверяем формат, а не конкретный номер
    # (жёсткая привязка к v4.2.9.19 ломала этот набор начиная с v4.2.9.20).
    assert re.search(r'APP_VERSION = "v\d+(?:\.\d+)+"', core)
    results = {"reference": reference, "changed_geometry": variant, "status": "PASS"}
    (ROOT / "flange_family_regression_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
