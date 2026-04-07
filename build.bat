@echo off
chcp 65001 > nul
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] 스크립트 폴더로 이동 실패: %~dp0
    pause & exit /b 1
)
setlocal enabledelayedexpansion
echo === NHIS YOD-DM Analyzer v2.1 Build ===

REM --- Python 3.12 설치 확인 ---
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되지 않았거나 PATH에 없습니다.
    pause & exit /b 1
)

REM --- Python 버전 3.12 확인 ---
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [INFO] 감지된 Python 버전: %PY_VER%
echo %PY_VER% | findstr /b "3.12" > nul
if errorlevel 1 (
    echo [ERROR] Python 3.12.x 가 필요합니다. 현재 버전: %PY_VER%
    echo        https://www.python.org/downloads/ 에서 Python 3.12 를 설치하세요.
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
if not exist venv\Scripts\python.exe (
    echo [ERROR] 가상환경 활성화 실패 - venv\Scripts\python.exe 를 찾을 수 없습니다.
    pause & exit /b 1
)

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
python -m pip install --no-cache-dir "pyinstaller>=6.11,<7"
if errorlevel 1 (
    echo [ERROR] PyInstaller 설치 실패
    pause & exit /b 1
)

REM --- 이전 빌드 정리 ---
if exist build rmdir /s /q build
if exist dist\NHIS_YOD_DM_Analyzer rmdir /s /q dist\NHIS_YOD_DM_Analyzer

REM --- PyInstaller 빌드 ---
echo [INFO] PyInstaller 빌드 중...
python -m PyInstaller --noconfirm --onedir --windowed^
 --name NHIS_YOD_DM_Analyzer^
 --hidden-import PyQt5.sip^
 --hidden-import PyQt5.QtCore^
 --hidden-import PyQt5.QtGui^
 --hidden-import PyQt5.QtWidgets^
 --hidden-import duckdb^
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
 --hidden-import sklearn.utils._param_validation^
 --hidden-import scipy.stats^
 --hidden-import scipy.linalg^
 --hidden-import scipy.special^
 --hidden-import scipy.sparse^
 --hidden-import scipy.optimize^
 --hidden-import scipy._lib.messagestream^
 --hidden-import scipy._lib.array_api_compat^
 --hidden-import scipy._lib.array_api_compat.numpy^
 --hidden-import matplotlib^
 --hidden-import matplotlib.backends.backend_agg^
 --hidden-import matplotlib.backends.backend_pdf^
 --hidden-import matplotlib.figure^
 --hidden-import matplotlib.patches^
 --hidden-import matplotlib.font_manager^
 --hidden-import pandas^
 --hidden-import pandas.io.formats.excel^
 --hidden-import pandas.io.excel._openpyxl^
 --hidden-import openpyxl^
 --hidden-import openpyxl.workbook^
 --hidden-import openpyxl.styles^
 --hidden-import openpyxl.styles.differential^
 --hidden-import openpyxl.cell^
 --hidden-import openpyxl.utils^
 --hidden-import openpyxl.utils.dataframe^
 --hidden-import psutil^
 --hidden-import win32timezone^
 --hidden-import numpy^
 --collect-all lifelines^
 --collect-all duckdb^
 --collect-all sklearn^
 --collect-all scipy^
 --collect-all formulaic^
 --collect-all pyreadstat^
 --collect-all matplotlib^
 --collect-all pandas^
 --collect-all openpyxl^
 main_app.py

if errorlevel 1 (
    echo [ERROR] PyInstaller 빌드 실패
    pause & exit /b 1
)

echo.
echo [SUCCESS] 빌드 완료: dist\NHIS_YOD_DM_Analyzer\
endlocal
pause
