@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul
echo === NHIS YOD-DM Analyzer v2.1 Build ===

REM --- Python 설치 확인 ---
python --version > /dev/null 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되지 않았거나 PATH에 없습니다.
    pause & exit /b 1
)

REM --- 가상환경 생성 (없으면) ---
if not exist venv (
    echo [INFO] 가상환경 생성 중...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] 가상환경 생성 실패
        pause & exit /b 1
    )
)

call venv\Scripts\activate.bat

REM --- pip 업그레이드 ---
echo [INFO] pip 업그레이드 중...
python -m pip install --no-cache-dir --upgrade pip

REM --- 의존 패키지 설치 ---
echo [INFO] 패키지 설치 중 (requirements.txt)...
python -m pip install --no-cache-dir -r requirements.txt
if errorlevel 1 (
    echo [ERROR] requirements.txt 패키지 설치 실패
    pause & exit /b 1
)

REM --- PyInstaller 설치 ---
echo [INFO] PyInstaller 설치 중...
python -m pip install --no-cache-dir pyinstaller
if errorlevel 1 (
    echo [ERROR] PyInstaller 설치 실패
    pause & exit /b 1
)

REM --- 이전 빌드 정리 ---
if exist build rmdir /s /q build
if exist dist\NHIS_YOD_DM_Analyzer rmdir /s /q dist\NHIS_YOD_DM_Analyzer

REM --- PyInstaller 빌드 ---
echo [INFO] PyInstaller 빌드 중...
pyinstaller --noconfirm --onedir --windowed^
 --name NHIS_YOD_DM_Analyzer^
 --hidden-import PyQt5.sip^
 --hidden-import PyQt5.QtCore^
 --hidden-import PyQt5.QtGui^
 --hidden-import PyQt5.QtWidgets^
 --hidden-import duckdb^
 --hidden-import hdbcli^
 --hidden-import hdbcli.dbapi^
 --hidden-import pyreadstat^
 --hidden-import lifelines^
 --hidden-import lifelines.statistics^
 --hidden-import lifelines.fitters^
 --hidden-import lifelines.fitters.coxph_fitter^
 --hidden-import lifelines.fitters.kaplan_meier_fitter^
 --hidden-import lifelines.utils^
 --hidden-import formulaic^
 --hidden-import autograd^
 --hidden-import autograd_gamma^
 --hidden-import sklearn^
 --hidden-import sklearn.linear_model^
 --hidden-import sklearn.linear_model._logistic^
 --hidden-import sklearn.neighbors^
 --hidden-import sklearn.neighbors._ball_tree^
 --hidden-import sklearn.neighbors._kd_tree^
 --hidden-import sklearn.utils._typedefs^
 --hidden-import sklearn.utils._heap^
 --hidden-import sklearn.utils._sorting^
 --hidden-import scipy.stats^
 --hidden-import scipy.linalg^
 --hidden-import scipy.special^
 --hidden-import scipy.sparse^
 --hidden-import scipy.optimize^
 --hidden-import scipy._lib.messagestream^
 --hidden-import matplotlib^
 --hidden-import matplotlib.backends.backend_agg^
 --hidden-import matplotlib.backends.backend_pdf^
 --hidden-import matplotlib.figure^
 --hidden-import matplotlib.patches^
 --hidden-import matplotlib.font_manager^
 --hidden-import pandas^
 --hidden-import pandas.io.formats.excel^
 --hidden-import openpyxl^
 --hidden-import openpyxl.workbook^
 --hidden-import psutil^
 --hidden-import numpy^
 --collect-all lifelines^
 --collect-all duckdb^
 --collect-all sklearn^
 --collect-all scipy^
 --collect-all formulaic^
 --collect-all pyreadstat^
 --collect-all matplotlib^
 --collect-all hdbcli^
 main_app.py

if errorlevel 1 (
    echo [ERROR] PyInstaller 빌드 실패
    pause & exit /b 1
)

echo.
echo [SUCCESS] 빌드 완료: dist\NHIS_YOD_DM_Analyzer\
endlocal
pause
