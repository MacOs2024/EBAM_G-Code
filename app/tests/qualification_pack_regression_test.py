from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from pathlib import Path

from ebam_gcode_studio.core import APP_VERSION, analyze_gcode_reverse
from generate_cylindr_v5_3mm_qualification_pack import build_pack


def must(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    source = Path("qualification/Cylindr_V5_source_settings_v4298.json")
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ebam_qual_pack_") as tmp:
        output = Path(tmp)
        metadata = build_pack(source, output)
        must(metadata["app_version"] == APP_VERSION, "version mismatch")
        must(metadata["hot_run_authorized"] is False, "pack must not authorize hot run")
        must(all(value is False for value in metadata["machine_confirmations"].values()), "machine confirmations must start false")
        checks.extend(["version_metadata", "hot_run_not_authorized", "machine_confirmations_false"])

        cases = metadata["generated_cases"]
        must(len(cases) == 4 and cases[0]["case_id"] == "DRY_RUN_MOTION", "dry + three TEST cases expected")
        must(all(case["layers"] == 20 and case["is_test_truncated"] is True for case in cases), "all files must be 20-layer truncated TESTs")
        must(all(case["initial_positioning_z_mm"] == 7.0 for case in cases), "safe initial Z missing")
        checks.extend(["dry_plus_three_cases", "twenty_layer_truncation", "safe_initial_z_in_all_cases"])

        matrix = list(csv.DictReader((output / "QUALIFICATION_MATRIX.csv").open(encoding="utf-8")))
        must([row["case_id"] for row in matrix] == ["LOW", "NOMINAL", "HIGH"], "matrix order mismatch")
        must([row["e2_mm_s"] for row in matrix] == ["5.957", "6.619", "7.281"], "unexpected E2 matrix")
        checks.append("e2_matrix_exact")

        for case in cases:
            gcode = (output / f"{case['prefix']}.ngc").read_text(encoding="utf-8")
            must("HOT RUN NOT AUTHORIZED" in gcode and "TRUNCATED TEST HEIGHT" in gcode, "qualification banner missing")
            must("SAFE_INITIAL_APPROACH: enabled" in gcode, "safe approach marker missing")
            must(gcode.rstrip().endswith("M30"), "program does not end with M30")
            reverse = analyze_gcode_reverse(gcode)
            must(reverse["stats"].get("g0_active", 0) == 0, "G0 while E0/E2 active")
        checks.append("all_gcodes_static_safe")

        journal_rows = list(csv.DictReader((output / "EXPERIMENT_JOURNAL.csv").open(encoding="utf-8")))
        must(len(journal_rows) == 3 and all(row["operator_decision"] == "NOT_RUN" for row in journal_rows), "journal defaults mismatch")
        checks.append("journal_ready_not_run")

        for line in (output / "MANIFEST_SHA256.txt").read_text(encoding="utf-8").splitlines():
            expected, name = line.split("  ", 1)
            must(sha256(output / name) == expected, f"manifest mismatch: {name}")
        checks.append("manifest_all_files_match")

    payload = {
        "version": APP_VERSION,
        "cases_total": len(checks),
        "cases_ok": len(checks),
        "cases_failed": 0,
        "checks": checks,
    }
    Path("qualification_pack_regression_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
