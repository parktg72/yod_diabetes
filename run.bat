@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] 스크립트 폴더로 이동 실패: %~dp0
    pause & exit /b 1
)
set PYTHONHOME=
set PYTHONPATH=

REM --- 가상환경 존재 확인 ---
set "VENV_PY=venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] 가상환경이 없습니다.
    echo         먼저 setup.bat 를 실행하여 패키지를 설치하세요.
    pause & exit /b 1
)

REM --- 가상환경 Python 실행 확인 ---
"%VENV_PY%" -c "import sys" > nul 2>&1
if errorlevel 1 (
    echo [ERROR] 가상환경 Python 실행 실패. setup.bat 를 다시 실행하세요.
    pause & exit /b 1
)

REM --- 필수 GUI 패키지 확인/복구 ---
"%VENV_PY%" -c "import PyQt5" > nul 2>&1
if errorlevel 1 (
    echo [WARN] 필수 GUI 패키지^(PyQt5^)가 누락되었습니다.
    echo        requirements.txt 기준으로 패키지를 다시 설치합니다...
    "%VENV_PY%" -m pip install --no-cache-dir -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] 패키지 재설치 실패. 인터넷 연결과 방화벽 설정을 확인하세요.
        pause & exit /b 1
    )
)

REM --- 앱 실행 ---
echo [INFO] NHIS YOD-DM Analyzer 시작...
"%VENV_PY%" main_app.py
if errorlevel 1 (
    echo.
    echo [ERROR] 앱이 오류로 종료되었습니다.
    echo         로그 파일을 확인하세요: %LOCALAPPDATA%\NHIS_YOD_DM_Analyzer\logs\
    pause
)
endlocal
