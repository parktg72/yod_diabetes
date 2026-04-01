@echo off
echo === NHIS YOD-DM Analyzer v2.0 Build ===
if not exist venv (python -m venv venv)
call venv\Scripts\activate.bat
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --noconfirm --onedir --windowed ^
    --name "NHIS_YOD_DM_Analyzer" ^
    --hidden-import "hdbcli" --hidden-import "pyreadstat" --hidden-import "duckdb" ^
    --hidden-import "lifelines" --hidden-import "sklearn" --hidden-import "sklearn.neighbors" ^
    --hidden-import "sklearn.linear_model" ^
    --collect-all "lifelines" --collect-all "duckdb" ^
    main_app.py
echo Build complete: dist\NHIS_YOD_DM_Analyzer\
pause
