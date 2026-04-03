# Stage A: Safety & Error Handling Improvements
**Date:** 2026-04-03  
**Project:** NHIS YOD-DM Analyzer  
**Priority:** Immediate (안전성 우선 > 사용자경험 우선)  
**Scope:** cohort_builder.py, statistical_analysis.py, tabs.py, utils.py, results_exporter.py

---

## Overview

세 가지 즉시 개선 사항을 순서대로 구현한다. A-1(CohortBuilder 안전 실행)이 가장 독립적이며, A-3(샘플링 UI 경고)은 A-2(예외 처리)의 헬퍼를 활용한다.

구현 순서: **A-1 → A-2 → A-3**

---

## A-1. CohortBuilder 단계별 안전 실행

### 목적
7단계 코호트 파이프라인에서 한 단계 실패 시 후속 단계가 없는 테이블을 참조하여 잘못된 결과를 생성하는 문제를 방지한다.

### 설계

**커스텀 예외 (`utils.py`)**
```python
class CohortStepError(Exception):
    """CohortBuilder 단계 실패 예외."""
    def __init__(self, step: int, step_name: str, cause: Exception):
        self.step = step
        self.step_name = step_name
        self.cause = cause
        super().__init__(f"코호트 {step}단계({step_name}) 실패: {cause}")
```

**단계 실행 패턴 (`cohort_builder.py`)**
```
_run_step(step_num, step_name, sql, result_table):
  1회 시도
    성공 → 행 수 조회
      0건 → CohortStepError(step_num, step_name, "결과 0건") 발생
      n건 → logger.info(f"[{step_num}/7] {step_name}: {n:,}건") → 반환
    실패(duckdb.Error) → 1초 대기 후 1회 재시도
      재시도 성공 → 행 수 검증 후 반환
      재시도 실패 → CohortStepError(step_num, step_name, cause) 발생
```

**적용 대상**: `build_cohort()`의 7개 단계 모두

**실패 메시지 형식**:
```
[코호트 구성 실패] 3단계(투약 분류)에서 오류가 발생했습니다.
원인: Table 'dm_claims' does not exist
재시도 1회 후에도 실패. 데이터 적재 상태를 확인해 주세요.
```

### 변경 파일
- `utils.py`: `CohortStepError` 추가
- `cohort_builder.py`: `_run_step()` 헬퍼 추가, 7개 단계에 적용

### 테스트
- 단계 실패 시 `CohortStepError` 발생 검증
- 재시도 후 성공 시 정상 진행 검증
- 결과 0건 시 `CohortStepError` 발생 검증

---

## A-2. 예외 처리 개선

### 목적
`except Exception as e` 광범위 처리를 구체적 예외 타입으로 교체하여 오류 원인을 명확히 하고 디버깅을 용이하게 한다.

### 설계

**`format_error_for_user(exc) -> str` 헬퍼 (`utils.py`)**

| 예외 타입 | 사용자 메시지 |
|----------|------------|
| `duckdb.Error` | "데이터베이스 오류: {detail} — 재시도하거나 데이터를 다시 적재해 주세요." |
| `pd.errors.EmptyDataError` | "분석 대상 데이터가 없습니다. 코호트 구성 단계를 확인해 주세요." |
| `ValueError` | "입력값 오류: {detail}" |
| `MemoryError` | "메모리 부족 — 청크 크기를 줄이거나 데이터 범위를 축소하세요." |
| `CohortStepError` | f"{exc}" (이미 포맷된 메시지 사용) |
| `Exception` (최후) | "예기치 않은 오류가 발생했습니다. 로그를 확인해 주세요: {type(exc).__name__}" |

**로깅 개선**
- `logger.error(str(e))` → `logger.exception(msg)` 교체 (stack trace 자동 포함)
- 사용자 표시 메시지와 로그 메시지 분리

**적용 대상**
- `tabs.py`: 7곳의 `except Exception`
- `statistical_analysis.py`: 8곳 이상의 `except Exception`

### 변경 파일
- `utils.py`: `format_error_for_user()` 추가
- `tabs.py`: 예외 처리 구체화
- `statistical_analysis.py`: 예외 처리 구체화

### 테스트
- `format_error_for_user(duckdb.Error(...))` 반환값 검증
- `format_error_for_user(pd.errors.EmptyDataError())` 반환값 검증
- 각 예외 타입별 메시지 포맷 검증

---

## A-3. 샘플링 UI 경고

### 목적
데이터가 메모리 한계 초과 시 자동 샘플링이 적용될 때 사용자에게 명확히 알려 연구 결과 해석 오류를 방지한다.

### 설계

**샘플링 메타데이터 (`statistical_analysis.py`)**

`load_analysis_data()`가 샘플링 적용 시 메타데이터 반환:
```python
@dataclass
class SamplingInfo:
    applied: bool
    total_rows: int
    sampled_rows: int

    @property
    def ratio_pct(self) -> float:
        return self.sampled_rows / self.total_rows * 100
```

**모달 다이얼로그 (`tabs.py`)**

분석 시작 전 `SamplingInfo.applied == True`이면:
```
┌─────────────────────────────────────────────┐
│ ⚠️ 데이터 샘플링 적용                         │
│                                             │
│ 전체 데이터: 1,234,567건                     │
│ 분석 대상:    500,000건 (40.5% 층화 샘플링)  │
│                                             │
│ 메모리 한계(8GB)로 층화 샘플링이 적용됩니다.  │
│ 결과 해석 시 이 점을 반드시 고려하세요.       │
│                                             │
│   [계속 진행]              [취소]            │
└─────────────────────────────────────────────┘
```
사용자가 [취소] 선택 시 분석 중단.

**결과 표시**
- 모든 결과 테이블 제목: `"Cox 회귀 결과 (샘플링 40.5% 적용)"`
- Excel 내보내기 헤더: `"분석일: 2026-04-03 | 샘플링: 40.5% (500,000/1,234,567건)"`

**샘플링 없을 때**: 다이얼로그 표시 안 함, 결과 제목 변경 없음

### 변경 파일
- `statistical_analysis.py`: `SamplingInfo` dataclass 추가, `load_analysis_data()` 반환값 변경
- `tabs.py`: 샘플링 다이얼로그 + 결과 제목 업데이트
- `results_exporter.py`: Excel 헤더에 샘플링 정보 포함

### 테스트
- 샘플링 임계값 초과 시 `SamplingInfo.applied == True` 검증
- 샘플링 미적용 시 `SamplingInfo.applied == False` 검증
- `ratio_pct` 계산 정확성 검증
- Excel 헤더 포맷 검증

---

## 구현 순서 및 의존 관계

```
A-1 (CohortStepError, _run_step)
  └─→ A-2 (format_error_for_user — CohortStepError 활용)
        └─→ A-3 (SamplingInfo — tabs.py에서 format_error_for_user 이미 사용 중)
```

## 비기능 요구사항

- 기존 API 시그니처 변경 최소화 (하위 호환)
- PyQt5 모달 다이얼로그는 Worker 스레드가 아닌 Main 스레드에서 실행
- 모든 새 예외/클래스에 docstring 필수
- 새로 추가되는 모든 로직에 단위 테스트 필수
