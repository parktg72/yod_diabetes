# AGENTS.md — NHIS YOD 당뇨병 분석 앱

> Codex, OpenCode 등 AI 에이전트가 이 프로젝트에서 작업할 때 따라야 하는 규칙입니다.

---

## 프로젝트 개요

국민건강보험공단(NHIS) 데이터를 활용한 **당뇨병 코호트 통계 분석 데스크톱 앱**.

- **배포 대상**: Windows OS + Python 3.12 (PyInstaller 단일 실행파일)
- **DB**: DuckDB (로컬 분석용) + SAP HANA (선택, `requirements-hana.txt`)
- **분석**: PSM 매칭, Cox 회귀, 경쟁 위험 모델(CIF), 서브그룹 분석, 인터랙션 분석
- **UI**: Tkinter 탭 기반 GUI (`tabs.py`, `main_app.py`)

---

## 핵심 파일 및 실제 클래스명

| 파일 | 주요 클래스/함수 | 비고 |
|------|----------------|------|
| `statistical_analysis.py` | `StatisticalAnalyzer` | ⚠️ `YODAnalyzer` 아님 |
| `db_connector.py` | `DuckDBStorage`, `HANAConnector` | ⚠️ `HANAStorage` 아님 |
| `cohort_builder.py` | `CohortBuilder`, `_safe_step()` | Step 3: `allow_zero=True` |
| `config.py` | `STUDY_SETTINGS`, `DUCKDB_SETTINGS` | frozen/unfrozen 경로 분기 |
| `analysis_runner.py` | `AnalysisRunner` | 분석 파이프라인 조율 |
| `tabs.py` | 각 탭 클래스 | GUI 탭 정의 |
| `nhis_schema.py` | 스키마 상수 | NHIS 컬럼명 정의 |
| `variable_generator.py` | 변수 생성 로직 | |
| `results_exporter.py` | 결과 내보내기 | |
| `visualization.py` | 시각화 | |

### 주요 경로 패턴

```python
# frozen(PyInstaller) / unfrozen 환경 모두 동작해야 함
_BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
```

### STUDY_SETTINGS 키 목록

`MIN_VALID_ROWS`, `MIN_EVENTS`, `MIN_SUBGROUP_EVENTS`, `PH_ALPHA`, `PSM_CALIPER`, `PSM_SMD_THRESHOLD`, `SAMPLING_SEED`, `PSM_RATIO`

---

## 코딩 규칙

### 1. 코드 수정 전 필수 확인

```bash
# 클래스/함수명 반드시 grep으로 확인 후 작업
grep -n "class HANAConnector" db_connector.py
grep -n "def run_cox" statistical_analysis.py
```

- 플랜에 적힌 이름과 실제 코드가 다를 수 있음 → **항상 grep/Read 먼저**
- `statistical_analysis.py`는 대형 파일 → 관련 섹션만 Read

### 2. TDD 필수

1. 실패 테스트 먼저 작성
2. 구현
3. `pytest tests/ -q` — **403 passed, 0 failed** 기준선 유지
4. 기준선 미달 시 작업 중단, 원인 파악 후 보고

### 3. 권한 (표준 B)

| 작업 | 허용 여부 |
|------|-----------|
| 소스 코드 읽기/분석 | ✅ 자유롭게 |
| 테스트 파일 작성/수정 | ✅ 테스트 통과 확인 후 |
| 소스 코드 수정 | ✅ 테스트 통과 확인 후 |
| `config.py` STUDY_SETTINGS 값 변경 | ⚠️ 사용자 확인 필요 |
| `db_connector.py` 연결 설정 변경 | ⚠️ 사용자 확인 필요 |
| `build.bat` / `.spec` 파일 수정 | ⚠️ 사용자 확인 필요 |
| `requirements*.txt` 패키지 추가/제거 | ⚠️ 사용자 확인 필요 |
| 스키마/DB 구조 변경 | ⚠️ 사용자 확인 필요 |

### 4. Windows 호환성

- 모든 경로: `pathlib.Path` 사용 (`os.path` 지양)
- 파일 읽기: `encoding='utf-8'` 명시
- `pywin32` 의존 코드 있음 (frozen 환경 전용)

### 5. HANA 선택 설치

- `hdbcli`는 `requirements-hana.txt`에만 있음
- import 시 `try/except ImportError`로 처리, 없으면 안내 메시지 출력

---

## 테스트 구조

```
tests/
  test_stage_j.py   # CIF per-group 이벤트 수 가드
  test_stage_n.py   # Stage N 핵심 로직 (4개)
  test_stage_o.py   # Stage O+P 통합 (7개)
  ...
```

내부 가드(early return) 우회 시 mock 주입 방식 사용.

---

## 커밋 메시지 형식

```
<type>: <한국어 설명> (Stage X)

예시:
fix: pooled_sd NaN 가드 추가 + 경고 로그 커버리지 테스트 (Stage P)
feat: HANA 월별 추출 구현 (Stage V)
chore: requirements.txt pywin32 추가
```

---

## 금지 사항

- `--no-verify` 옵션으로 pre-commit 훅 우회 금지
- 테스트 통과 전 커밋 금지
- `hdbcli`를 `requirements.txt`에 추가 금지 (선택 의존성)
- 클래스명/함수명 추측 작성 금지 → grep 확인 필수
