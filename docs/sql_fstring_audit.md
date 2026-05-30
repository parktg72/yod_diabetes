# SQL f-string 안전성 감사 보고서

작성일: 2026-05-30
범위: `dist/`, `tests/`, 가상환경 제외한 애플리케이션 Python 코드의 `execute/query/fetch_*` 호출 중 f-string SQL 사용부.

## 결론

즉시 수정이 필요한 신규 SQL 인젝션 회귀는 발견하지 못했다. 다만 f-string SQL은 연구 설정값, 코드 상수, 내부 테이블명/컬럼명 조합에 넓게 사용되고 있어, 사용자 입력 또는 외부 파일 설정이 직접 문자열 리터럴/식별자로 들어가는 지점은 지속적으로 검증해야 한다.

이번 개선에서 P1 위험으로 분류된 `cohort_ids` 기반 `INDI_DSCM_NO IN (...)` 조건은 다음 방어가 적용되어 있음:

- `_validate_cohort_ids()`가 전체 ID를 사전 검증해 숫자 문자열(`^\d+$`) 외 값을 거부한다.
- `_iter_cohort_id_where_parts()`가 정렬/전체 복사 없이 chunk 단위로 검증·생성한다.
- `HANAConnector.load_table_to_duckdb()`, `MonthlyHanaExtractor.extract_all_months()`, `MonthlyJKExtractor.extract_all_months()`는 DuckDB mutation 또는 HANA fetch 전에 `cohort_ids`를 검증한다.
- JK chunk 조회용 `cohort_id_where_part`는 내부 생성 형식(`INDI_DSCM_NO IN ('숫자', ...)`)만 허용하도록 추가 검증한다.

## 주요 분류

### A. 검증 완료 또는 내부 상수 기반으로 낮은 위험

- `analysis_runner.py`, `statistical_analysis.py`, `tabs.py`
  - `SELECT setseed({seed_float})`
  - `SAMPLING_SEED`는 `config.load_settings()`와 `_validate_study_settings()`에서 0-99 정수로 검증됨.

- `cohort_builder.py`의 ICD/약물 코드 조건 생성
  - `icd_like(...)`, `DM_CODES`, `DEMENTIA_CODES`, 약물 코드 상수 기반.
  - 외부 사용자 자유 입력이 아니라 코드 상수와 연구 설정값 기반.

- `variable_generator.py`의 comorbidity/complication/CCI 동적 SELECT
  - 코드 상수 dict에서 생성되는 컬럼/조건 조합.

### B. 식별자 동적 SQL — helper 인용 또는 내부 후보 테이블 기반

- `db_connector.py`
  - `_quote_identifier()` 사용부: row count, drop table 등은 식별자 인용 적용.
  - `_build_chunk_select_sql()` 기반 `CREATE/INSERT`: 임시 등록 테이블과 내부 로직으로 생성된 select SQL.
  - GJ 통합 테이블 생성부: `_quote_identifier()` 또는 스키마 매핑으로 선별된 테이블/컬럼명을 사용.

### C. 주의 유지 필요

- `cohort_builder.py:251` `_inpatient_keys` 생성
  - `INPATIENT_FORM_CD` 설정값이 SQL 문자열 리터럴로 직접 삽입됨.
  - 현재 기본값은 `'02'`이고 프로젝트 설정에서 관리되지만, 향후 UI/외부 설정으로 열릴 경우 화이트리스트 검증 또는 SQL literal escaping helper를 적용하는 것이 안전함.

- `cohort_builder.py:227`, `cohort_builder.py:452`, `cohort_builder.py:751`
  - `table_name`, `outcome_{oname.lower()}`, suffix 기반 테이블명이 동적으로 삽입됨.
  - 현재 값은 내부 호출/상수에서 오지만, public method 인자로 확장될 경우 `_quote_identifier()` 계열 helper 사용 권장.

## P2 SQL helper 확장 inventory (2026-05-30)

AST 기반으로 `dist/`, `tests/`, `.venv/venv`, `build`, `__pycache__`를 제외한 애플리케이션 Python 코드의 f-string SQL 후보를 재점검했다. 총 87개 후보 중 대부분은 내부 상수/숫자 검증/이미 적용된 helper 기반이다.

### P2-완료

- `cohort_builder.py` 약물/질병 코드 IN-list
  - `DM_CODES`, `DEMENTIA_CODES`, `OHA_CODES`, `INSULIN_CODES`, `INSULIN_EFMDC`, `DEMENTIA_DRUG_CODES`는 `icd_like()`, `sql_in_list()`, `sql_literal()`, `sql_identifier()` 경로로 정리됨.
- `statistical_analysis.py:run_sensitivity()` 항치매약 코드 IN-list
  - 기존 수동 join(`"'" + "','".join(...) + "'"`)을 `sql_in_list(DEMENTIA_DRUG_CODES)`로 교체.
  - 작은따옴표 포함 코드가 SQL 리터럴로 escape되는 회귀 테스트 추가.
- `db_connector.py` 식별자 경로
  - `_quote_identifier()` 적용부와 `_validate_table_name()` 적용부는 현 상태 유지.

### P2-추적 유지(현재는 내부 상수/검증 경로)

- `cohort_builder.py:_create_t40_filtered()`
  - `LOOKBACK_YEARS`는 정수 변환 후 산술 표현식에 삽입됨.
- `variable_generator.py` comorbidity/complication/CCI SELECT 생성
  - P3-2 완료: config dict key에서 파생되는 alias(`comor_*`, `comp_*`, `cci_*`)는 SQL 생성 전 `sql_identifier(..., allow_qualified=False)`로 검증한다.
- `variable_generator.py:_apply_complete_case_strategy()`
  - P3-2 완료: complete-case 핵심 변수 목록은 `complete_case_critical_vars`로 분리하고 SQL 생성 전 `sql_identifier(..., allow_qualified=False)`로 검증한다.
- `analysis_runner.py`, `statistical_analysis.py`, `tabs.py`의 `SELECT setseed(...)`
  - `SAMPLING_SEED`는 설정 검증 후 0-99 정수/실수 표현으로만 삽입됨.

### P3 후보(기능 변경 없이 별도 TDD 권장)

- `db_connector.py`의 대형 HANA/DuckDB 적재 SQL 생성부
  - `read_parquet(...)`, HANA schema/table/column 조합, GJ 통합 테이블 생성은 기존 테스트가 있으나 코드 크기가 커 별도 회귀 스캐너/리팩터링 단위로 분리 권장.
  - P3-1 시작: `DuckDBStorage.create_index()`가 DuckDB 키워드와 겹치는 내부 식별자도 `_quote_identifier()`로 인용하도록 보강했고 회귀 테스트를 추가함.
  - P3-2 완료: `variable_generator.py`의 config dict key 기반 alias와 complete-case 핵심 변수명을 `sql_identifier(..., allow_qualified=False)`로 사전 검증하고 회귀 테스트를 추가함.
  - P3-3 완료: `CohortBuilder.sensitivity_analysis()`의 lookback day 목록을 전체 사전 검증해 unsafe suffix 또는 비정수 입력이 부분 테이블 생성/동적 SQL 생성으로 진행되지 않도록 보강함.
- `cohort_builder.py` 동적 outcome/exposure/med_pattern 테이블명
  - 현재 내부 enum/suffix 기반이며 `sql_identifier(..., allow_qualified=False)`가 적용된 주요 경로는 유지. 공개 인자로 확장 시 추가 테스트 필요.

## 권장 후속 개선

1. SQL helper 적용 규칙 유지
   - 값: `sql_literal()` 또는 `sql_in_list()`.
   - 식별자: `sql_identifier()` 또는 DuckDB 전용 `_quote_identifier()`.
   - 가능하면 DB 파라미터 바인딩 우선.

2. 공통 SQL identifier helper 적용 범위 확대
   - 이미 `db_connector.py`에 `_quote_identifier()`가 있음.
   - `variable_generator.py`의 동적 컬럼 alias/critical vars가 외부 설정화되면 즉시 `sql_identifier()` 검증을 추가.

3. 회귀 스캐너 유지
   - f-string SQL 호출 목록은 AST 기반으로 주기 점검.
   - `dist/`, `tests/`, `.venv/venv`는 제외하고 애플리케이션 코드만 감사.

## 이번 P1 처리 상태

- `cohort_ids` SQL 조건은 사전 검증과 streaming chunk 생성으로 개선 완료.
- JK chunk 조회용 raw SQL fragment는 내부 cohort-id IN절 형식만 허용하도록 검증 완료.
- locked final parquet는 force 재추출 전에 RuntimeError로 중단되도록 개선 완료.
- config JSON 로드는 전체 연구 설정 검증 실패 시 기존 설정으로 rollback되도록 개선 완료.
