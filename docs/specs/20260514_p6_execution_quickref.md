# P6 폐쇄망 실행 퀵레퍼런스

## 실행 전 체크
1. 프로젝트 폴더 위치 확인
   - `phase2_run.bat`가 있는 프로젝트 루트에서 실행해야 합니다.
   - 예: `D:\NHIS\yod_diabetes_app\`
2. Python 가상환경/패키지 확인
   - 가상환경 활성화 후 아래 명령으로 필수 패키지 import가 되는지 확인합니다.
   - `python -c "import pandas, duckdb, lifelines; print('OK')"`
3. DuckDB 파일 경로/권한 확인
   - 대상 DB 파일(예: `D:\NHIS\data\nhis_yod.duckdb`)이 존재하고 읽기 권한이 있어야 합니다.
4. 출력 디렉토리 쓰기 권한 확인
   - 결과 폴더(예: `D:\NHIS\output\phase2`)가 존재하거나 생성 가능해야 하며 쓰기 권한이 있어야 합니다.

## 실행 명령 (Windows Batch)
```bat
REM 1) 프로젝트 루트로 이동
cd /d D:\NHIS\yod_diabetes_app

REM 2) 가상환경 활성화
call venv\Scripts\activate.bat

REM 3) 기본 실행
phase2_run.bat
```

```bat
REM 사용자 지정 DB/출력 경로 예시
cd /d D:\NHIS\yod_diabetes_app
call venv\Scripts\activate.bat
phase2_run.bat "D:\NHIS\data\nhis_yod.duckdb" "D:\NHIS\output\phase2"
```

## 정상 산출물 (생성 기준)
| 파일명 패턴/이름 | 정상 여부 | 비고 |
|---|---|---|
| `phase2_report_*.log` | 생성되어야 함 | 실행 로그 |
| `km_t2dm_oha_switch.png` | 생성되어야 함 | KM 곡선 |
| `table_baseline.csv` | 생성되어야 함 | 기저 특성표 |
| `table_cox_results.csv` | 생성되어야 함 | Cox 결과표 |
| `INTERPRETATION_GUIDE.md` | 생성되어야 함 | 해석 가이드 |
| `forest_t2dm_oha_switch.png` | **미생성도 정상** | Forest plot 비활성화 상태 기준 |

## 실패 시 수집할 정보
1. 실행 명령어 원문, 실행 시각, DB 경로(마스킹 가능)
2. `phase2_report_*.log` 원본
3. `python --version` 결과
4. `pip list` 중 주요 패키지 버전
   - 예: `pandas`, `duckdb`, `lifelines`, `numpy`, `scipy`
5. HANA 연동 사용 시
   - 오류 코드/메시지 전문
   - 마지막 성공 스키마·테이블 조회 기록

## 통계 해석 주의
- `had_insulin_switch`는 **post-index 변수**입니다.
- **immortal time bias** 가능성이 있으므로 인과 해석을 금지합니다.
- 결과 해석은 `table_cox_results.csv`(Cox results table) 중심으로 수행합니다.
- Forest plot은 현재 비활성화 전제이므로 해석 근거로 사용하지 않습니다.
