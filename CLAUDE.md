# NHIS YOD-DM Analyzer — 프로젝트 지침

## 에이전트 역할 및 오케스트레이션

1. 각 에이전트는 본인이 담당하는 작업에 자동으로 역할을 수행한다.
1. Claude Code는 에이전트에게 맡겨진 업무를 오케스트레이션한다.

## 실행 환경

1. 프로젝트는 **Windows OS + Python 3.12** 에서 작동한다.
   - `build.bat`, `requirements.txt` 등 패키지/종속성 관련 파일은 **UTF-8 + CRLF** 인코딩을 적용하여 Windows CMD에서 한글이 정상 인식되도록 한다.
   - `pathlib`, `open(..., encoding='utf-8')` 등 크로스플랫폼 방식을 사용한다.

## build.bat 유지보수 규칙

1. **코드 수정 시 항상 `build.bat`을 함께 업데이트한다.**
   - 새 Python 모듈 추가 → `--hidden-import <모듈명>` 항목 추가
   - 새 패키지 의존성 추가 → `--collect-all <패키지명>` 항목 추가
   - `NHIS_YOD_DM_Analyzer.spec`도 `build.bat`와 동기화 상태를 유지한다.
   - exe 빌드는 Windows에서 `build.bat` 실행으로 수행하며, macOS에서는 직접 빌드하지 않는다.

## 데이터 추출 구조

1. **대상자 우선 선정 후 매칭 추출** 구조를 따른다.
   - 사용자의 조건(진입기간, 연령, 상병코드 등)에 따라 대상자를 먼저 선정하고, 선정된 대상자의 `INDI_DSCM_NO`로 각 테이블에서 매칭되는 레코드를 추출한다.
   - 구현체: `CohortIDExtractor` (HHDT_DSES_YY + T20 → cohort_ids.parquet) → `MonthlyHanaExtractor` / `load_table_to_duckdb` (INDI_DSCM_NO IN 필터 적용).
