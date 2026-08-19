EBAM G-code Studio - Windows start

Use run_windows_ASCII_SAFE.bat or run_windows.bat.
This launcher is ASCII-only, so Windows CMD will not break Russian text inside BAT.

Recommended Python: 3.12 or 3.13.
If requirements fail on Python 3.14, install Python 3.12/3.13, delete .venv folder, and run again.

Manual start:
1) cd to this folder
2) python -m venv .venv
3) .venv\Scripts\activate
4) python -m pip install --upgrade pip
5) python -m pip install -r requirements.txt
6) python -m streamlit run app.py
