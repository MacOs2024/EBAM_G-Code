from __future__ import annotations

import importlib
import re
from importlib.metadata import PackageNotFoundError, version

MODULES = ["streamlit", "numpy", "scipy", "trimesh", "shapely", "matplotlib", "pandas", "ezdxf"]
MIN_VERSIONS = {"streamlit": (1, 54, 0)}


def numeric_version_tuple(text: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", text)[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


missing: list[str] = []
for module_name in MODULES:
    try:
        importlib.import_module(module_name)
        try:
            installed = version(module_name)
        except PackageNotFoundError:
            installed = "unknown"
        minimum = MIN_VERSIONS.get(module_name)
        if minimum and installed != "unknown" and numeric_version_tuple(installed) < minimum:
            print(
                f"TOO OLD: {module_name} {installed}; required >= "
                + ".".join(str(x) for x in minimum)
            )
            missing.append(module_name)
        else:
            print(f"OK: {module_name} {installed}")
    except Exception as exc:
        print(f"MISSING/ERROR: {module_name}: {exc}")
        missing.append(module_name)

if missing:
    raise SystemExit("Missing or incompatible modules: " + ", ".join(missing))
print("All dependencies are installed and compatible.")
