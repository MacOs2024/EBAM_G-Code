# PyInstaller spec for EBAM G-code Studio offline desktop build.
# Build command:
#   pyinstaller --clean --noconfirm EBAM_Gcode_Studio.spec

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None
root = Path.cwd()

datas = []
# Application files
datas += [(str(root / "app.py"), ".")]
datas += [(str(root / "ebam_gcode_studio"), "ebam_gcode_studio")]
if (root / "samples").exists():
    datas += [(str(root / "samples"), "samples")]
for name in [
    "settings_balanced_example.json",
    "README_RU.md",
    "README_OFFLINE_RU.md",
    "README_V28.md",
    "V28_10M_AUDIT_REPORT.txt",
]:
    p = root / name
    if p.exists():
        datas += [(str(p), ".")]

# Streamlit and scientific stack data/metadata
for pkg in ["streamlit", "altair", "pydeck", "pandas", "numpy", "scipy", "matplotlib", "trimesh", "shapely", "ezdxf"]:
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

hiddenimports = []
for pkg in ["streamlit", "altair", "pydeck", "pandas", "numpy", "scipy", "matplotlib", "trimesh", "shapely", "ezdxf"]:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# Common hidden imports used by trimesh/scipy/shapely/streamlit
hiddenimports += [
    'flange_family_generator',  # v4.2.9.22: exe build was missing this module
    "scipy.spatial._ckdtree",
    "scipy.interpolate",
    "scipy.sparse.csgraph._validation",
    "matplotlib.backends.backend_agg",
    "shapely.geometry",
    "trimesh.path.polygons",
]

excludes = ["tkinter", "pytest", "IPython", "jupyter", "notebook"]

a = Analysis(
    ["desktop_launcher.py"],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EBAM_Gcode_Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EBAM_Gcode_Studio",
)
