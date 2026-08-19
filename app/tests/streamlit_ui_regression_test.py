from __future__ import annotations

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest
from ebam_gcode_studio.core import APP_VERSION


def by_label(items, label: str):
    matches = [item for item in items if item.label == label]
    if not matches:
        raise AssertionError(f"Widget not found: {label}")
    return matches[0]


def messages(items) -> list[str]:
    return [str(item.value) for item in items]


def main() -> None:
    checks: list[str] = []
    at = AppTest.from_file("app.py", default_timeout=60).run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert any(APP_VERSION in str(t.value) for t in at.title)
    checks.append("simple_mode_loads")

    by_label(at.radio, "Интерфейс").set_value("Расширенный").run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    planner = by_label(at.selectbox, "Режим планировщика траектории")
    analog = by_label(at.selectbox, "Команды E0/E2 в непрерывном C-режиме")
    confirm = by_label(at.checkbox, "Подтверждаю поддержку M67 в HAL")
    assert planner.value.startswith("G64")
    assert analog.value.startswith("M68")
    assert confirm.disabled is True
    assert by_label(at.number_input, "Допуск G64 P, мм").value == 0.08
    assert by_label(at.number_input, "Naive CAM G64 Q, мм").value == 0.0
    assert by_label(at.number_input, "Допуск изменения радиуса в no-pause, мм").value == 0.05
    checks.append("industrial_controls_visible")

    analog.set_value("M67 — синхронно со следующим движением").run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    confirm = by_label(at.checkbox, "Подтверждаю поддержку M67 в HAL")
    assert confirm.disabled is False
    assert any("итоговый G-code безопасно останется на M68" in text for text in messages(at.info))
    checks.append("m67_requires_confirmation_ui")

    confirm.check().run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    confirm = by_label(at.checkbox, "Подтверждаю поддержку M67 в HAL")
    assert confirm.value is True
    checks.append("m67_confirmation_available")

    # Exact-path warning is relevant specifically to the no-pause C mode.
    by_label(at.selectbox, "Режим C-колец").set_value(
        "Одно кольцо, C без остановки, переход C+Z"
    ).run(timeout=60)
    planner = by_label(at.selectbox, "Режим планировщика траектории")
    planner.set_value("G61 — точная траектория").run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert any("G61/G61.1 конфликтует" in text for text in messages(at.warning))
    checks.append("g61_warning_visible")

    assert any("не вставляет M49/M50" in text for text in messages(at.info))
    checks.append("override_recommendation_only")

    result = {
        "version": APP_VERSION,
        "cases_total": len(checks),
        "cases_ok": len(checks),
        "cases_failed": 0,
        "checks": checks,
    }
    Path("streamlit_ui_regression_results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
