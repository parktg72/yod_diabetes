@echo off
chcp 65001 > nul
REM Phase 2 Final Report 실행 스크립트 (Windows CMD)
REM 사용: phase2_run.bat [db_path] [output_dir]
REM 예시: phase2_run.bat nhis_analysis.duckdb phase2_output

cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] 스크립트 폴더로 이동 실패: %~dp0
    pause & exit /b 1
)
setlocal enabledelayedexpansion

REM --- 가상환경 존재 확인 ---
if not exist venv\Scripts\activate.bat (
    echo [ERROR] 가상환경이 없습니다.
    echo         먼저 setup.bat 를 실행하여 패키지를 설치하세요.
    pause & exit /b 1
)

REM --- 가상환경 활성화 ---
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] 가상환경 활성화 실패. setup.bat 를 다시 실행하세요.
    pause & exit /b 1
)

REM --- 매개변수 설정 ---
set DB_PATH=%1
set OUTPUT_DIR=%2

if "!DB_PATH!"=="" (
    set DB_PATH=nhis_analysis.duckdb
    echo [INFO] DB 경로 기본값 사용: !DB_PATH!
)

if "!OUTPUT_DIR!"=="" (
    set OUTPUT_DIR=phase2_output
    echo [INFO] 출력 디렉토리 기본값 사용: !OUTPUT_DIR!
)

echo ============================================================
echo  Phase 2 Final Report 생성 시작
echo ============================================================
echo [INFO] 데이터베이스: !DB_PATH!
echo [INFO] 출력 디렉토리: !OUTPUT_DIR!
echo.

REM --- Phase 2 Report 실행 ---
python phase2_final_report.py "!DB_PATH!" "!OUTPUT_DIR!"
if errorlevel 1 (
    echo.
    echo [ERROR] Phase 2 보고서 생성 실패
    pause & exit /b 1
)

echo.
echo ============================================================
echo  [SUCCESS] Phase 2 보고서 생성 완료!
echo  출력 파일: !OUTPUT_DIR!\
echo ============================================================
pause
endlocal
