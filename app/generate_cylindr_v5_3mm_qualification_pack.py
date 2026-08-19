from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from ebam_gcode_studio.core import (
    APP_VERSION,
    GenerationResult,
    _wire_for_target_wall,
    audit_gcode,
    generate_rotational_shell,
    save_result,
    settings_from_dict,
)


CYLINDER_V5 = {
    "profile_type": "straight_cup",
    "height_mm": 100.0,
    "bottom_diameter_mm": 80.0,
    "top_diameter_mm": 80.0,
    "max_diameter_mm": 80.0,
    "wall_thickness_mm": 3.0,
    "bottom_solid_mm": 0.0,
    "resolution": 160,
}

TARGET_WALL_MM = 3.0
TEST_HEIGHT_MM = 10.0
SPREAD_PCT = 10.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _qualification_cases(nominal_e2: float) -> list[dict[str, Any]]:
    return [
        {"case_id": "LOW", "multiplier": 0.90, "e2_mm_s": nominal_e2 * 0.90},
        {"case_id": "NOMINAL", "multiplier": 1.00, "e2_mm_s": nominal_e2},
        {"case_id": "HIGH", "multiplier": 1.10, "e2_mm_s": nominal_e2 * 1.10},
    ]


def _tag(value: float) -> str:
    return f"{value:.3f}".replace(".", "p")


def _mark_result(result: GenerationResult, *, case_id: str, e0_ma: float, e2_mm_s: float) -> None:
    banner = [
        f"(QUALIFICATION_PACK {APP_VERSION}: {case_id})",
        f"(TRUNCATED TEST HEIGHT {TEST_HEIGHT_MM:.1f} mm; NOT A FULL PART PROGRAM)",
        f"(PLANNED E0={e0_ma:.3f} mA E2={e2_mm_s:.3f} mm/s; EXTERNAL OVERRIDES ARE NOT RESET)",
        "(HOT RUN NOT AUTHORIZED BY GENERATOR; MACHINE DRY-RUN CONFIRMATIONS REQUIRED)",
    ]
    result.gcode = "\n".join(banner) + "\n" + result.gcode
    audit_text = (
        f"QUALIFICATION CASE: {case_id}\n"
        f"HOT RUN AUTHORIZED: NO\n"
        f"TARGET WALL MODEL: {TARGET_WALL_MM:.3f} mm\n"
        f"COMMAND E0/E2: {e0_ma:.3f} mA / {e2_mm_s:.3f} mm/s\n"
        f"TEST HEIGHT: {TEST_HEIGHT_MM:.1f} mm\n\n"
        + result.audit_text
    )
    result.audit_text = "\n".join(line.rstrip() for line in audit_text.splitlines()) + "\n"


def _write_matrix(path: Path, cases: list[dict[str, Any]], nominal_e2: float) -> None:
    fields = [
        "case_id", "e2_mm_s", "relative_to_nominal_pct", "model_wall_mm_eta_source",
        "run_selected", "operator", "date", "result", "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for case in cases:
            writer.writerow({
                "case_id": case["case_id"],
                "e2_mm_s": f"{case['e2_mm_s']:.3f}",
                "relative_to_nominal_pct": f"{case['multiplier'] * 100.0:.1f}",
                "model_wall_mm_eta_source": f"{TARGET_WALL_MM * case['multiplier']:.3f}",
                "run_selected": "NO",
                "operator": "",
                "date": "",
                "result": "NOT_RUN",
                "notes": "",
            })


def _write_journal(path: Path, cases: list[dict[str, Any]]) -> None:
    fields = [
        "case_id", "e2_command_mm_s", "wire_override_pct", "current_override_pct",
        "feed_override_pct", "measured_height_mm", "wall_bottom_mm", "wall_middle_mm",
        "wall_top_mm", "outer_diameter_mm", "inner_diameter_mm", "mass_g",
        "surface_observation", "wire_behavior", "vacuum_events", "abort_reason",
        "photos_or_log_refs", "operator_decision",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for case in cases:
            writer.writerow({
                "case_id": case["case_id"],
                "e2_command_mm_s": f"{case['e2_mm_s']:.3f}",
                "wire_override_pct": "100.0",
                "current_override_pct": "100.0",
                "feed_override_pct": "100.0",
                "operator_decision": "NOT_RUN",
            })


def _readme_text(nominal_e2: float, cases: list[dict[str, Any]]) -> str:
    case_lines = "\n".join(
        f"- {case['case_id']}: E2={case['e2_mm_s']:.3f} мм/с, модельная стенка≈{TARGET_WALL_MM * case['multiplier']:.2f} мм при исходном η."
        for case in cases
    )
    return f"""CYLINDR V5 — 3-MM QUALIFICATION PACK, {APP_VERSION}
====================================================

СТАТУС: HOT RUN НЕ РАЗРЕШЁН ГЕНЕРАТОРОМ. Это подготовленные TEST-файлы.
Номинальная E2 по прозрачной объёмной модели: {nominal_e2:.3f} мм/с.

Содержимое:
- 00_DRY_RUN_MOTION: E0=0, E2=0, 20 слоёв / {TEST_HEIGHT_MM:.1f} мм.
{case_lines}

Обязательная последовательность:
1. Сверить MANIFEST_SHA256.txt.
2. Аппаратно отключить HV и привод проволоки; выполнить только 00_DRY_RUN_MOTION.
3. Подтвердить абсолютный Z=7 мм с текущими рабочими смещениями и оснасткой.
4. Подтвердить C+, G91 C360, плавный C+Z переход 17°, финальный G90 и M68 E0/E2=0.
5. Подтвердить M68↔HAL каналы E0/E2; внешние overrides программа не сбрасывает.
6. Только оператор после пунктов 1–5 выбирает ОДИН короткий горячий TEST. Не запускать три файла подряд автоматически.
7. Для сопоставимого опыта зафиксировать overrides; стартовая рекомендация журнала — 100/100/100%.
8. Немедленно остановить тест при касании оснастки, неверном направлении C/Z, потере вакуума,
   нестабильном пучке, тыкании/обрыве проволоки, шарообразовании или неконтролируемой ванне.
9. После охлаждения измерить стенку минимум внизу/середине/сверху и заполнить EXPERIMENT_JOURNAL.csv.

Модельные ограничения:
- η исходного JSON = 1.0 — некалиброванная верхняя оценка.
- Фото от 2026-07-06 показывают другой выбранный файл (V2), поэтому scan 8%/300 Гц не перенесён в V5 автоматически.
- Ток и скорость оставлены от исходного V5; внешние overrides не закодированы.
- Без измеренной детали нельзя выбирать полный производственный режим.
"""


def build_pack(source_settings_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    source = Path(source_settings_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    source_data = json.loads(source.read_text(encoding="utf-8"))
    base = settings_from_dict(source_data)
    test_layers = int(round(TEST_HEIGHT_MM / base.layer_height))
    nominal_e2 = _wire_for_target_wall(
        TARGET_WALL_MM,
        base.layer_height,
        base.feed_bottom_mm_min,
        base.wire_diameter_mm,
        base.deposition_efficiency,
    )
    cases = _qualification_cases(nominal_e2)
    generated: list[dict[str, Any]] = []

    common = replace(
        base,
        rotational_radial_step_mm=(base.rotational_radial_step_mm or base.hatch_spacing),
        max_layers_to_generate=test_layers,
        safe_initial_approach_enabled=True,
        safe_initial_approach_z_mm=max(7.0, base.z_hop_mm),
        analog_output_mode="m68_compatible",
        machine_m67_confirmed=False,
        include_comments=True,
    )

    dry_settings = replace(
        common,
        beam_current_mode="current",
        beam_current_bottom_ma=0.0,
        beam_current_top_ma=0.0,
        min_beam_power_w=0.0,
        power_floor_warning_enabled=False,
        wire_feed_mode="manual_constant",
        wire_feed_manual_mm_s=0.0,
    )
    dry_result = generate_rotational_shell(CYLINDER_V5, dry_settings)
    _mark_result(dry_result, case_id="DRY_RUN_MOTION", e0_ma=0.0, e2_mm_s=0.0)
    dry_audit = audit_gcode(dry_result.gcode, dry_settings)
    if not dry_audit.ok or dry_audit.stats.get("g0_active", 0):
        raise RuntimeError("dry-run static audit failed")
    dry_prefix = output / "00_DRY_RUN_MOTION_E0_0_E2_0"
    save_result(dry_result, dry_prefix, dry_settings)
    generated.append({"case_id": "DRY_RUN_MOTION", "prefix": dry_prefix.name, "stats": dry_result.stats})

    for order, case in enumerate(cases, start=1):
        settings = replace(
            common,
            wire_feed_mode="manual_constant",
            wire_feed_manual_mm_s=float(case["e2_mm_s"]),
        )
        result = generate_rotational_shell(CYLINDER_V5, settings)
        _mark_result(
            result,
            case_id=case["case_id"],
            e0_ma=float(result.stats["current_commanded_once_ma"]),
            e2_mm_s=float(result.stats["wire_commanded_once_mm_s"]),
        )
        static = audit_gcode(result.gcode, settings)
        if not static.ok or static.stats.get("g0_active", 0):
            raise RuntimeError(f"static audit failed for {case['case_id']}")
        prefix = output / f"{order * 10:02d}_TEST_{case['case_id']}_E2_{_tag(case['e2_mm_s'])}"
        save_result(result, prefix, settings)
        generated.append({"case_id": case["case_id"], "prefix": prefix.name, "stats": result.stats})

    shutil.copy2(source, output / "SOURCE_Cylindr_V5_settings_v4298.json")
    _write_matrix(output / "QUALIFICATION_MATRIX.csv", cases, nominal_e2)
    _write_journal(output / "EXPERIMENT_JOURNAL.csv", cases)
    (output / "README_FIRST_RU.txt").write_text(_readme_text(nominal_e2, cases), encoding="utf-8")

    metadata = {
        "app_version": APP_VERSION,
        "source_settings_sha256": _sha256(source),
        "target_wall_mm": TARGET_WALL_MM,
        "test_height_mm": TEST_HEIGHT_MM,
        "test_layers": test_layers,
        "nominal_e2_mm_s": round(nominal_e2, 6),
        "spread_pct": SPREAD_PCT,
        "hot_run_authorized": False,
        "machine_confirmations": {
            "safe_initial_z_and_work_offset": False,
            "c_direction_and_single_turn": False,
            "c_plus_z_transition": False,
            "m68_e0_e2_hal_mapping": False,
            "hardware_interlocks": False,
        },
        "generated_cases": [
            {
                "case_id": item["case_id"],
                "prefix": item["prefix"],
                "layers": item["stats"].get("layers_total"),
                "is_test_truncated": item["stats"].get("is_test_truncated"),
                "initial_positioning_z_mm": item["stats"].get("initial_positioning_z_mm"),
            }
            for item in generated
        ],
    }
    (output / "PACK_METADATA.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    files_for_manifest = sorted(path for path in output.iterdir() if path.is_file() and path.name != "MANIFEST_SHA256.txt")
    manifest = "\n".join(f"{_sha256(path)}  {path.name}" for path in files_for_manifest) + "\n"
    (output / "MANIFEST_SHA256.txt").write_text(manifest, encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Cylindr_V5 3-mm qualification TEST pack")
    parser.add_argument("--source-settings", required=True, help="Original Cylindr_V5 settings JSON")
    parser.add_argument("--output-dir", required=True, help="Destination directory")
    args = parser.parse_args()
    metadata = build_pack(args.source_settings, args.output_dir)
    print(json.dumps(metadata, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
