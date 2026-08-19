EBAM G-code Studio v2.5
========================

Исправление v2.5: добавлен fallback-слайсинг STL без rtree. Теперь чаши/кольца/полые модели не должны давать пустой G-code, если в сечении есть внутренний контур.

EBAM G-code Studio v2.4

Fix in this build:
- Added scipy to requirements.txt.
- Reason: trimesh may need scipy during STL section -> 2D polygon conversion.
- Symptom before fix: generation error "No module named 'scipy'" after uploading an STL and pressing Generate.

Windows use:
1. Close the old Streamlit/app terminal.
2. Unpack this archive into a new folder.
3. Run run_windows.bat or run_windows_ROBUST.bat.
4. The launcher will install/update requirements in the .venv.

If you unpack over an old folder and the error remains:
- close the app;
- delete the .venv folder;
- run run_windows.bat again.
