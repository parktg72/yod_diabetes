@echo off
chcp 65001 > nul
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] 스크립트 폴더로 이동 실패: %~dp0
    pause & exit /b 1
)
setlocal enabledelayedexpansion
set PYTHONHOME=
set PYTHONPATH=
set "VENV_DIR=venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo ============================================================
echo  NHIS YOD-DM Analyzer v2.1 - 간편 설치 (개발자 모드)
echo  Python 3.12 + 가상환경 + 패키지 자동 설치
echo ============================================================

REM --- Python 설치 확인 ---
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되지 않았거나 PATH에 없습니다.
    echo         https://www.python.org/downloads/ 에서
    echo         Python 3.12 를 설치한 후 다시 실행하세요.
    pause & exit /b 1
)

REM --- Python 3.12 버전 확인 ---
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [INFO] 감지된 Python 버전: %PY_VER%
echo %PY_VER% | findstr /b "3.12" > nul
if errorlevel 1 (
    echo [ERROR] Python 3.12.x 가 필요합니다. 현재 버전: %PY_VER%
    echo         https://www.python.org/downloads/ 에서 Python 3.12 를 설치하세요.
    pause & exit /b 1
)

REM --- 기존 가상환경 검증 ---
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" > nul 2>&1
    if errorlevel 1 (
        echo [WARN] 기존 가상환경이 현재 PC/경로와 맞지 않아 재생성합니다.
        rmdir /s /q "%VENV_DIR%"
        if exist "%VENV_DIR%" (
            echo [ERROR] 기존 가상환경 삭제 실패. venv 폴더를 닫고 다시 실행하세요.
            pause & exit /b 1
        )
    )
)

REM --- 가상환경 생성 ---
if not exist "%VENV_PY%" (
    echo [INFO] 가상환경 생성 중...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] 가상환경 생성 실패. 권한 문제나 디스크 공간을 확인하세요.
        pause & exit /b 1
    )
) else (
    echo [INFO] 기존 가상환경을 사용합니다: %VENV_DIR%\
)

REM --- 가상환경 Python 실행 확인 ---
"%VENV_PY%" -c "import sys" > nul 2>&1
if errorlevel 1 (
    echo [ERROR] 가상환경 Python 실행 실패. venv 폴더를 삭제 후 다시 실행하세요.
    pause & exit /b 1
)

REM --- pip 최신화 ---
echo [INFO] pip 업그레이드 중...
"%VENV_PY%" -m pip install --no-cache-dir --upgrade pip

REM --- 핵심 패키지 설치 ---
echo [INFO] requirements.txt 패키지 설치 중 (시간이 걸릴 수 있습니다)...
"%VENV_PY%" -m pip install --no-cache-dir -r requirements.txt
if errorlevel 1 (
    echo [ERROR] 패키지 설치 실패. 인터넷 연결과 방화벽 설정을 확인하세요.
    echo         회사 네트워크: pip install --index-url http://내부미러/simple -r requirements.txt
    pause & exit /b 1
)

REM --- SAP HANA 드라이버 선택 설치 ---
if exist requirements-hana.txt (
    echo.
    echo SAP HANA DB에 접속하려면 HANA 드라이버를 설치해야 합니다.
    set /p HANA_CHOICE="HANA 드라이버를 설치하시겠습니까? [Y/N]: "
    if /i "!HANA_CHOICE!"=="Y" (
        echo [INFO] HANA 드라이버 설치 중...
        "%VENV_PY%" -m pip install --no-cache-dir -r requirements-hana.txt
        if errorlevel 1 (
            echo [WARN] HANA 드라이버 설치 실패. HANA 없이 실행 가능합니다.
        ) else (
            echo [INFO] HANA 드라이버 설치 완료.
        )
    ) else (
        echo [INFO] HANA 드라이버 설치 건너뜀. 로컬 파일^(.parquet/.sas7bdat^)만 사용 가능합니다.
    )
)

echo.
echo ============================================================
echo  [SUCCESS] 설치 완료!
echo  실행하려면: run.bat 을 더블클릭하거나 아래 명령을 사용하세요.
echo    venv\Scripts\python.exe main_app.py
echo ============================================================
endlocal
pause
