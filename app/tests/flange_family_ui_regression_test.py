"""End-to-end Streamlit smoke test for the v4.2.9.19 Flange-family page."""
from __future__ import annotations

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parent
REFERENCE_STL = Path("/workspace/scratch/39793d1130d4/upload/Flange1(2).STL")


def one(items, label: str):
    matches = [item for item in items if item.label == label]
    if len(matches) != 1:
        raise AssertionError(f"expected one widget {label!r}, got {len(matches)}")
    return matches[0]


def main() -> None:
    checks: list[str] = []
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run(timeout=60)
    one(at.radio, "Что нужно сделать?").set_value("Flange-family R6 (STL → C-кольца)").run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    checks.append("specialized_page_loads")

    uploader = one(at.file_uploader, "Осесимметричная STL")
    uploader.upload(REFERENCE_STL.name, REFERENCE_STL.read_bytes(), "model/stl").run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    metrics = {item.label: str(item.value) for item in at.metric}
    assert metrics["OD снизу"].startswith("164.000")
    assert metrics["ID снизу"].startswith("40.000")
    assert metrics["Высота"].startswith("47.500")
    assert metrics["Плановое время"].startswith("5.107")
    assert metrics["Слои"] == "32" and metrics["Кольца"] == "426"
    checks.append("reference_geometry_and_schedule_visible")

    one(at.button, "Сформировать проверенный комплект").click().run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    labels = {item.label for item in at.download_button}
    assert {
        "Скачать полный комплект ZIP",
        "Скачать полный dry-run",
        "Скачать горячий кандидат",
        "Скачать отчёт проверки",
    }.issubset(labels)
    checks.append("validated_release_downloads_created")

    result = {
        "version": "v4.2.9.19",
        "cases_total": len(checks),
        "cases_ok": len(checks),
        "cases_failed": 0,
        "checks": checks,
    }
    (ROOT / "flange_family_ui_regression_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
