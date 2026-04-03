# Stage A: Safety & Error Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CohortBuilder 7단계 안전 실행(재시도+fail-fast), 구체적 예외 처리, 샘플링 UI 경고를 구현하여 분석 결과 신뢰성과 사용자 인지성을 높인다.

**Architecture:** A-1(CohortStepError + _run_step) → A-2(format_error_for_user + 예외 구체화) → A-3(SamplingInfo dataclass + UI 다이얼로그) 순으로 구현. utils.py가 공통 기반.

**Tech Stack:** Python 3.12, DuckDB, PyQt5, pandas, lifelines, openpyxl

---

## 파일 변경 맵

| 파일 | 역할 | 변경 유형 |
|------|------|---------|
| `utils.py` | CohortStepError, format_error_for_user 추가 | Modify |
| `cohort_builder.py` | _run_step 헬퍼 + build_cohort 안전 실행 | Modify |
| `statistical_analysis.py` | SamplingInfo 반환 + 예외 구체화 | Modify |
| `tabs.py` | 샘플링 다이얼로그 + 예외 처리 구체화 | Modify |
| `results_exporter.py` | Excel 헤더에 샘플링 정보 포함 | Modify |
| `tests/test_cohort_safety.py` | A-1 테스트 | Create |
| `tests/test_utils_errors.py` | A-2 테스트 | Create |
| `tests/test_sampling_info.py` | A-3 테스트 | Create |

---

## Task 1: CohortStepError 커스텀 예외 추가

**Files:**
- Modify: `utils.py` (현재 70줄 끝에 추가)
- Create: `tests/test_utils_errors.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_utils_errors.py
import pytest
from utils import CohortStepError


def test_cohort_step_error_stores_attributes():
    cause = ValueError("table not found")
    err = CohortStepError(step=3, step_name="투약 분류", cause=cause)
    assert err.step == 3
    assert err.step_name == "투약 분류"
    assert err.cause is cause


def test_cohort_step_error_message_contains_step_info():
    cause = RuntimeError("duckdb error")
    err = CohortStepError(step=2, step_name="당뇨 청구 식별", cause=cause)
    assert "2단계" in str(err)
    assert "당뇨 청구 식별" in str(err)
    assert "duckdb error" in str(err)


def test_cohort_step_error_is_exception():
    err = CohortStepError(step=1, step_name="기본 인구", cause=ValueError("x"))
    assert isinstance(err, Exception)
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
cd /Volumes/model/yod_diabetes_app
python3 -m pytest tests/test_utils_errors.py -v
```
Expected: `ERROR` (CohortStepError not defined)

- [ ] **Step 3: utils.py에 CohortStepError 추가**

`utils.py` 끝에 추가:
```python
class CohortStepError(Exception):
    """CohortBuilder 단계 실패 예외.

    step: 실패한 단계 번호 (1-7)
    step_name: 단계 이름 (예: '기본 인구 정의')
    cause: 원인 예외
    """
    def __init__(self, step: int, step_name: str, cause: Exception):
        self.step = step
        self.step_name = step_name
        self.cause = cause
        super().__init__(
            f"코호트 {step}단계({step_name}) 실패: {cause}"
        )
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_utils_errors.py -v
```
Expected: 3 passed

- [ ] **Step 5: 커밋**

```bash
git add utils.py tests/test_utils_errors.py
git commit -m "feat: CohortStepError 커스텀 예외 추가"
```

---

## Task 2: _run_step 헬퍼 구현

**Files:**
- Modify: `cohort_builder.py` (CohortBuilder 클래스에 추가)
- Modify: `tests/test_cohort_safety.py` (신규 생성)

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_cohort_safety.py
import time
import pytest
import duckdb
from unittest.mock import MagicMock, patch, call
from cohort_builder import CohortBuilder
from utils import CohortStepError


class MockStorage:
    def __init__(self):
        self.conn = duckdb.connect(':memory:')

    def get_row_count(self, table_name):
        try:
            return self.conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]
        except Exception:
            return 0

    def table_exists(self, table_name):
        try:
            self.conn.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
            return True
        except Exception:
            return False


class MockDM:
    def __init__(self):
        self.storage = MockStorage()
        self._execute_count = 0
        self._fail_on = None  # None = always succeed

    def execute(self, sql):
        self._execute_count += 1
        if self._fail_on is not None and self._execute_count >= self._fail_on:
            raise duckdb.Error("simulated duckdb error")
        self.storage.conn.execute(sql)

    def query(self, sql):
        import pandas as pd
        return pd.read_sql(sql, self.storage.conn)


def make_builder(dm):
    builder = CohortBuilder.__new__(CohortBuilder)
    builder.dm = dm
    builder.settings = {
        'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2016,
        'WASHOUT_YEARS': 1, 'MIN_AGE': 40, 'MAX_AGE': 64,
        'MIN_DM_CLAIMS_OUTPATIENT': 2, 'MIN_DM_CLAIMS_INPATIENT': 1,
        'STUDY_END_YEAR': 2024, 'YOD_AGE_CUTOFF': 65,
    }
    return builder


def test_run_step_succeeds_and_returns_row_count():
    dm = MockDM()
    builder = make_builder(dm)

    dm.storage.conn.execute("CREATE TABLE test_tbl (id INTEGER)")
    dm.storage.conn.execute("INSERT INTO test_tbl VALUES (1), (2), (3)")

    count = builder._run_step(
        step_num=1,
        step_name="테스트",
        sql="SELECT 1",   # no-op (table already exists)
        result_table="test_tbl",
    )
    assert count == 3


def test_run_step_raises_cohort_step_error_after_retry_on_duckdb_error():
    dm = MockDM()
    dm._fail_on = 1  # fail immediately on first execute
    builder = make_builder(dm)

    with pytest.raises(CohortStepError) as exc_info:
        builder._run_step(
            step_num=2,
            step_name="실패 단계",
            sql="CREATE TABLE fail_tbl AS SELECT 1 AS x",
            result_table="fail_tbl",
        )

    err = exc_info.value
    assert err.step == 2
    assert err.step_name == "실패 단계"
    assert isinstance(err.cause, duckdb.Error)


def test_run_step_raises_on_zero_rows():
    dm = MockDM()
    builder = make_builder(dm)

    dm.storage.conn.execute("CREATE TABLE empty_tbl (id INTEGER)")
    # table exists but 0 rows

    with pytest.raises(CohortStepError) as exc_info:
        builder._run_step(
            step_num=3,
            step_name="빈 결과",
            sql="SELECT 1",  # no-op
            result_table="empty_tbl",
        )
    assert exc_info.value.step == 3
    assert "0건" in str(exc_info.value)


def test_run_step_retries_once_and_succeeds():
    """첫 번째 실행 실패 → 재시도 성공 시 CohortStepError 발생하지 않아야 함."""
    dm = MockDM()
    builder = make_builder(dm)
    attempt = {'count': 0}

    original_execute = dm.execute

    def flaky_execute(sql):
        attempt['count'] += 1
        if attempt['count'] == 1:
            raise duckdb.Error("transient error")
        dm.storage.conn.execute(sql)

    dm.execute = flaky_execute
    dm.storage.conn.execute(
        "CREATE TABLE retry_tbl (id INTEGER)"
    )
    dm.storage.conn.execute("INSERT INTO retry_tbl VALUES (1)")

    # Should NOT raise — second attempt succeeds
    count = builder._run_step(
        step_num=1,
        step_name="재시도 성공",
        sql="SELECT 1",
        result_table="retry_tbl",
    )
    assert count == 1
    assert attempt['count'] == 2
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
python3 -m pytest tests/test_cohort_safety.py -v
```
Expected: `AttributeError: '_run_step' not found`

- [ ] **Step 3: cohort_builder.py에 _run_step 추가**

`cohort_builder.py`의 import 섹션 맨 위에 추가:
```python
import time
import duckdb
```

`CohortBuilder` 클래스의 `_flat_oha_codes` 메서드 바로 위(line 22 앞)에 추가:
```python
    def _run_step(self, step_num: int, step_name: str, sql: str, result_table: str) -> int:
        """단계 SQL 실행 + 1회 재시도 + 행 수 검증.

        성공 시 result_table의 행 수 반환.
        실패(duckdb.Error) 또는 결과 0건 시 CohortStepError 발생.
        """
        from utils import CohortStepError
        for attempt in range(2):
            try:
                self.dm.execute(sql)
                break
            except duckdb.Error as e:
                if attempt == 0:
                    logger.warning(
                        f"[{step_num}/7] {step_name} 1차 실패, 1초 후 재시도: {e}"
                    )
                    time.sleep(1)
                else:
                    raise CohortStepError(step_num, step_name, e)

        n = self.dm.storage.get_row_count(result_table)
        if n == 0:
            raise CohortStepError(
                step_num, step_name,
                ValueError(f"{result_table} 결과 0건 — 데이터 적재 상태를 확인하세요.")
            )
        logger.info(f"[{step_num}/7] {step_name}: {n:,}건")
        return n
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_cohort_safety.py -v
```
Expected: 4 passed

- [ ] **Step 5: 커밋**

```bash
git add cohort_builder.py tests/test_cohort_safety.py
git commit -m "feat: CohortBuilder._run_step 재시도+행수검증 헬퍼 추가"
```

---

## Task 3: build_cohort에 _run_step 적용

**Files:**
- Modify: `cohort_builder.py:445-468` (build_cohort 메서드)

> Note: step1~step6은 내부적으로 `self.dm.execute()` 여러 번 호출. 각 단계 함수가 CREATE OR REPLACE TABLE을 수행하므로, `_run_step`의 sql 인자에는 빈 문자열("")을 전달하고 step 함수 자체를 호출한 뒤 result_table 행 수만 검증하는 래퍼 패턴 사용.

- [ ] **Step 1: 테스트 추가 (build_cohort 실패 전파 검증)**

`tests/test_cohort_safety.py` 끝에 추가:
```python
def test_build_cohort_stops_on_step_failure():
    """step1이 실패하면 step2가 호출되지 않아야 한다."""
    dm = MockDM()
    builder = make_builder(dm)

    step1_called = {'v': False}
    step2_called = {'v': False}

    def fake_step1(cb=None):
        step1_called['v'] = True
        raise duckdb.Error("step1 fail")

    def fake_step2(cb=None):
        step2_called['v'] = True
        return 0

    builder.step1_base_population = fake_step1
    builder.step2_dm_claims = fake_step2

    with pytest.raises(CohortStepError) as exc_info:
        builder.build_cohort()

    assert step1_called['v'] is True
    assert step2_called['v'] is False
    assert exc_info.value.step == 1
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
python3 -m pytest tests/test_cohort_safety.py::test_build_cohort_stops_on_step_failure -v
```
Expected: FAIL (build_cohort does not propagate errors yet)

- [ ] **Step 3: build_cohort 수정**

`cohort_builder.py`의 `build_cohort` 메서드(line 445~468)를 교체:
```python
    def build_cohort(self, cb=None):
        """7단계 코호트 파이프라인 실행.

        각 단계는 duckdb.Error 발생 시 1회 재시도 후 CohortStepError를 발생시킨다.
        단계 결과가 0건이어도 CohortStepError를 발생시켜 후속 단계 실행을 막는다.
        """
        from utils import CohortStepError
        import duckdb as _duckdb

        results = {}

        def _safe_step(step_num, step_name, step_fn, result_table):
            """단계 함수를 실행하고 CohortStepError로 감싼다."""
            for attempt in range(2):
                try:
                    ret = step_fn(cb)
                    break
                except CohortStepError:
                    raise  # 이미 래핑된 예외는 그대로 전파
                except _duckdb.Error as e:
                    if attempt == 0:
                        logger.warning(
                            f"[{step_num}/7] {step_name} 1차 실패, 1초 후 재시도: {e}"
                        )
                        time.sleep(1)
                    else:
                        raise CohortStepError(step_num, step_name, e)
                except Exception as e:
                    raise CohortStepError(step_num, step_name, e)

            n = self.dm.storage.get_row_count(result_table)
            if n == 0:
                raise CohortStepError(
                    step_num, step_name,
                    ValueError(f"{result_table} 결과 0건 — 데이터 적재 상태를 확인하세요.")
                )
            logger.info(f"[{step_num}/7] {step_name} 완료: {n:,}건")
            return ret, n

        results['base_n'], _ = _safe_step(
            1, "기본 대상 인구 정의",
            self.step1_base_population, "base_population"
        )
        mem_manager.cleanup_after_step('step1')

        results['dm_claims'], _ = _safe_step(
            2, "당뇨 진단 청구 식별",
            self.step2_dm_claims, "dm_claims"
        )
        mem_manager.cleanup_after_step('step2')

        results['dm_meds'], _ = _safe_step(
            3, "당뇨 약물 처방 식별",
            self.step3_dm_medications, "dm_medications"
        )
        mem_manager.cleanup_after_step('step3')

        results['groups'], _ = _safe_step(
            4, "노출군 분류",
            self.step4_classify_groups, "exposure_groups"
        )
        mem_manager.cleanup_after_step('step4')

        (n, excl), _ = _safe_step(
            5, "기존 치매 및 항치매약 제외",
            self.step5_exclude_dementia, "study_cohort"
        )
        results['cohort_n'] = n
        results['excluded'] = excl
        mem_manager.cleanup_after_step('step5')

        results['events'], _ = _safe_step(
            6, "결과변수 및 추적기간 산출",
            self.step6_outcomes, "analysis_data"
        )
        mem_manager.cleanup_after_step('step6')

        results['final_n'], _ = _safe_step(
            7, "최종 분석 테이블 생성",
            self.step7_final_table, "final_analysis"
        )
        mem_manager.cleanup_after_step('step7')

        return results
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_cohort_safety.py -v
```
Expected: 5 passed

- [ ] **Step 5: 커밋**

```bash
git add cohort_builder.py tests/test_cohort_safety.py
git commit -m "feat: build_cohort 단계별 안전 실행 적용 (재시도+fail-fast)"
```

---

## Task 4: format_error_for_user 헬퍼 구현

**Files:**
- Modify: `utils.py`
- Modify: `tests/test_utils_errors.py`

- [ ] **Step 1: 테스트 추가**

`tests/test_utils_errors.py` 끝에 추가:
```python
import duckdb
import pandas as pd
from utils import format_error_for_user, CohortStepError


def test_format_duckdb_error():
    msg = format_error_for_user(duckdb.Error("table not found"))
    assert "데이터베이스 오류" in msg
    assert "table not found" in msg


def test_format_empty_data_error():
    msg = format_error_for_user(pd.errors.EmptyDataError())
    assert "데이터가 없습니다" in msg
    assert "코호트" in msg


def test_format_value_error():
    msg = format_error_for_user(ValueError("invalid range"))
    assert "입력값 오류" in msg
    assert "invalid range" in msg


def test_format_memory_error():
    msg = format_error_for_user(MemoryError())
    assert "메모리 부족" in msg


def test_format_cohort_step_error():
    err = CohortStepError(3, "투약 분류", ValueError("x"))
    msg = format_error_for_user(err)
    assert "3단계" in msg
    assert "투약 분류" in msg


def test_format_unknown_error():
    msg = format_error_for_user(RuntimeError("unexpected"))
    assert "RuntimeError" in msg
    assert "로그" in msg
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
python3 -m pytest tests/test_utils_errors.py -v
```
Expected: 6 new FAILs (format_error_for_user not defined)

- [ ] **Step 3: utils.py에 format_error_for_user 추가**

`utils.py`의 `CohortStepError` 클래스 바로 뒤에 추가:
```python
def format_error_for_user(exc: Exception) -> str:
    """예외를 사용자 친화적 메시지로 변환한다.

    tabs.py, statistical_analysis.py 등에서 except 블록에 사용.
    로그에는 별도로 logger.exception()으로 스택 트레이스를 남길 것.
    """
    import duckdb as _duckdb
    import pandas as _pd

    if isinstance(exc, CohortStepError):
        return str(exc)
    if isinstance(exc, _duckdb.Error):
        return (
            f"데이터베이스 오류: {exc}\n"
            "재시도하거나 데이터를 다시 적재해 주세요."
        )
    if isinstance(exc, _pd.errors.EmptyDataError):
        return "분석 대상 데이터가 없습니다. 코호트 구성 단계를 확인해 주세요."
    if isinstance(exc, ValueError):
        return f"입력값 오류: {exc}"
    if isinstance(exc, MemoryError):
        return "메모리 부족 — 청크 크기를 줄이거나 데이터 범위를 축소하세요."
    return (
        f"예기치 않은 오류가 발생했습니다. 로그를 확인해 주세요: "
        f"{type(exc).__name__}: {exc}"
    )
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_utils_errors.py -v
```
Expected: 9 passed

- [ ] **Step 5: 커밋**

```bash
git add utils.py tests/test_utils_errors.py
git commit -m "feat: format_error_for_user 헬퍼 추가"
```

---

## Task 5: tabs.py / statistical_analysis.py 예외 처리 구체화

**Files:**
- Modify: `tabs.py` (광범위 except Exception 교체)
- Modify: `statistical_analysis.py` (광범위 except Exception 교체)

- [ ] **Step 1: tabs.py의 except Exception 패턴 검색**

```bash
grep -n "except Exception" /Volumes/model/yod_diabetes_app/tabs.py
```

- [ ] **Step 2: tabs.py 각 except Exception 블록 교체**

각 `except Exception as e:` 블록을 아래 패턴으로 교체:
```python
# 기존
except Exception as e:
    self.log(f"오류: {e}")

# 교체 후
except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
        MemoryError, CohortStepError) as e:
    from utils import format_error_for_user
    logger.exception("분석 오류")
    self.log(f"오류: {format_error_for_user(e)}")
except Exception as e:
    from utils import format_error_for_user
    logger.exception("예기치 않은 오류")
    self.log(f"오류: {format_error_for_user(e)}")
```

tabs.py 맨 위 import에 추가 (없을 경우):
```python
import duckdb
import pandas as pd
from utils import CohortStepError, format_error_for_user
```

- [ ] **Step 3: statistical_analysis.py except Exception 패턴 검색**

```bash
grep -n "except Exception" /Volumes/model/yod_diabetes_app/statistical_analysis.py
```

- [ ] **Step 4: statistical_analysis.py PH 검정 제외 나머지 except 블록 교체**

PH 검정 except(line 190)는 `logger.info`로 정보성 처리이므로 유지.
나머지 `except Exception as e:`를 아래 패턴으로 교체:
```python
except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
    from utils import format_error_for_user
    logger.exception(f"분석 오류 ({context_name})")
    raise
except Exception as e:
    from utils import format_error_for_user
    logger.exception(f"예기치 않은 오류 ({context_name})")
    raise
```

- [ ] **Step 5: 문법 검증**

```bash
python3 -c "import ast; ast.parse(open('tabs.py').read()); print('tabs.py OK')"
python3 -c "import ast; ast.parse(open('statistical_analysis.py').read()); print('statistical_analysis.py OK')"
```
Expected: 두 파일 모두 `OK`

- [ ] **Step 6: 커밋**

```bash
git add tabs.py statistical_analysis.py
git commit -m "refactor: 예외 처리 구체화 (duckdb.Error, EmptyDataError 등 명시)"
```

---

## Task 6: SamplingInfo dataclass 구현

**Files:**
- Modify: `statistical_analysis.py` (`_load_data` 반환값 변경)
- Create: `tests/test_sampling_info.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_sampling_info.py
import pytest
import duckdb
import pandas as pd
from statistical_analysis import SamplingInfo


def test_sampling_info_applied_false_when_no_sampling():
    info = SamplingInfo(applied=False, total_rows=1000, sampled_rows=1000)
    assert info.applied is False
    assert info.ratio_pct == pytest.approx(100.0)


def test_sampling_info_applied_true_when_sampled():
    info = SamplingInfo(applied=True, total_rows=1_000_000, sampled_rows=400_000)
    assert info.applied is True
    assert info.ratio_pct == pytest.approx(40.0)


def test_sampling_info_ratio_pct_rounds_correctly():
    info = SamplingInfo(applied=True, total_rows=3, sampled_rows=1)
    assert info.ratio_pct == pytest.approx(33.333, rel=1e-3)


def test_sampling_info_label():
    info = SamplingInfo(applied=True, total_rows=1_000_000, sampled_rows=400_000)
    label = info.label
    assert "400,000" in label
    assert "1,000,000" in label
    assert "40.0%" in label


def test_sampling_info_label_not_applied():
    info = SamplingInfo(applied=False, total_rows=500, sampled_rows=500)
    assert info.label == ""
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
python3 -m pytest tests/test_sampling_info.py -v
```
Expected: `ImportError: cannot import name 'SamplingInfo'`

- [ ] **Step 3: statistical_analysis.py에 SamplingInfo 추가**

`statistical_analysis.py` import 섹션 바로 아래(line 18 이후)에 추가:
```python
from dataclasses import dataclass


@dataclass
class SamplingInfo:
    """층화 샘플링 적용 여부 및 규모 정보.

    applied: 샘플링이 적용되었으면 True
    total_rows: 원본 전체 행 수
    sampled_rows: 실제 분석에 사용된 행 수
    """
    applied: bool
    total_rows: int
    sampled_rows: int

    @property
    def ratio_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return self.sampled_rows / self.total_rows * 100

    @property
    def label(self) -> str:
        """UI 및 Excel 헤더용 한줄 요약. 샘플링 없으면 빈 문자열."""
        if not self.applied:
            return ""
        return (
            f"층화 샘플링: {self.sampled_rows:,}/{self.total_rows:,}건 "
            f"({self.ratio_pct:.1f}%)"
        )
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_sampling_info.py -v
```
Expected: 5 passed

- [ ] **Step 5: 커밋**

```bash
git add statistical_analysis.py tests/test_sampling_info.py
git commit -m "feat: SamplingInfo dataclass 추가"
```

---

## Task 7: _load_data가 SamplingInfo 반환하도록 수정

**Files:**
- Modify: `statistical_analysis.py` (`_load_data` 시그니처 및 반환값)

- [ ] **Step 1: 테스트 추가**

`tests/test_sampling_info.py` 끝에 추가:
```python
from unittest.mock import MagicMock, patch
from statistical_analysis import StatisticalAnalyzer, SamplingInfo


def _make_analyzer(rows, max_rows=500_000):
    """테스트용 StatisticalAnalyzer with in-memory DuckDB."""
    conn = duckdb.connect(':memory:')
    # 최소 스키마의 final_analysis 테이블 생성
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T1DM' AS exposure_group,
               1 AS follow_up_days,
               1.0 AS follow_up_years,
               0 AS dementia_event
        FROM range(?)
    """, [rows])

    class MockStorage:
        def get_row_count(self, t):
            return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    class MockDM:
        storage = MockStorage()
        def query(self, sql):
            return conn.execute(sql).df()

    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer.dm = MockDM()
    analyzer.results = {}
    analyzer._cached_df = None

    with patch('statistical_analysis.mem_manager') as mock_mem:
        mock_mem.get_safe_analysis_rows.return_value = max_rows
        mock_mem.optimize_dtypes.side_effect = lambda df: df
        df, info = analyzer._load_data()

    return df, info


def test_load_data_no_sampling_when_within_limit():
    df, info = _make_analyzer(rows=100, max_rows=500_000)
    assert info.applied is False
    assert info.total_rows == 100
    assert info.sampled_rows == 100


def test_load_data_sampling_applied_when_over_limit():
    df, info = _make_analyzer(rows=1000, max_rows=500)
    assert info.applied is True
    assert info.total_rows == 1000
    assert info.sampled_rows <= 500
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
python3 -m pytest tests/test_sampling_info.py::test_load_data_no_sampling_when_within_limit -v
```
Expected: FAIL (`_load_data` returns single value, not tuple)

- [ ] **Step 3: _load_data 반환값을 (DataFrame, SamplingInfo) 튜플로 변경**

`statistical_analysis.py`의 `_load_data` 메서드를 수정:

1. 샘플링 적용 분기(line 68 근처)에 `SamplingInfo` 생성:
```python
            self._cached_df = self.dm.query(f"""...""")  # 기존 코드 유지
            self._sampling_info = SamplingInfo(
                applied=True,
                total_rows=total,
                sampled_rows=len(self._cached_df),
            )
```

2. 샘플링 미적용 분기(line 87 근처):
```python
            self._cached_df = self.dm.query(
                "SELECT * FROM final_analysis WHERE follow_up_days > 0"
            )
            self._sampling_info = SamplingInfo(
                applied=False,
                total_rows=total,
                sampled_rows=len(self._cached_df),
            )
```

3. 메서드 마지막 return 수정:
```python
        return self._cached_df, self._sampling_info
```

4. `_load_data`를 호출하는 `run_cox`, `run_psm` 등의 코드에서 튜플 언패킹 적용:
```python
# 기존
df_prepared = self._prepare(self._load_data())
# 교체 후
_df, _sampling_info = self._load_data()
df_prepared = self._prepare(_df)
```

5. `StatisticalAnalyzer.__init__`에 `_sampling_info` 초기화 추가:
```python
        self._sampling_info = SamplingInfo(applied=False, total_rows=0, sampled_rows=0)
```

- [ ] **Step 4: 문법 검증**

```bash
python3 -c "import ast; ast.parse(open('statistical_analysis.py').read()); print('OK')"
```

- [ ] **Step 5: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_sampling_info.py -v
```
Expected: 7 passed

- [ ] **Step 6: 커밋**

```bash
git add statistical_analysis.py tests/test_sampling_info.py
git commit -m "feat: _load_data가 (DataFrame, SamplingInfo) 반환하도록 수정"
```

---

## Task 8: 샘플링 UI 다이얼로그 + 결과 제목 표시

**Files:**
- Modify: `tabs.py` (분석 시작 전 다이얼로그, 결과 제목)

> Note: PyQt5 다이얼로그는 Main 스레드에서만 실행 가능. Worker 스레드에서 SamplingInfo를 수신한 후 signal로 Main 스레드에 전달해야 한다. 기존 tabs.py의 Worker/signal 패턴을 따른다.

- [ ] **Step 1: tabs.py에서 분석 워커 signal 패턴 확인**

```bash
grep -n "emit\|Signal\|signal\|worker\|Worker" /Volumes/model/yod_diabetes_app/tabs.py | head -30
```

- [ ] **Step 2: SamplingInfo를 전달하는 signal 추가**

tabs.py의 분석 워커 클래스(또는 분석 완료 콜백)에 `sampling_info_ready` signal 추가:
```python
# 기존 signal 선언부 근처에 추가
from PyQt5.QtCore import pyqtSignal
from statistical_analysis import SamplingInfo

# 워커 클래스 내부
sampling_info_ready = pyqtSignal(object)  # SamplingInfo 전달
```

- [ ] **Step 3: 분석 시작 후 SamplingInfo 수신 시 다이얼로그 표시**

tabs.py의 분석 버튼 핸들러(또는 signal 연결부)에 아래 슬롯 추가:
```python
def _on_sampling_info_ready(self, info: 'SamplingInfo'):
    """샘플링 적용 시 사용자에게 확인 요청."""
    if not info.applied:
        return  # 샘플링 없으면 무시

    from PyQt5.QtWidgets import QMessageBox
    msg = QMessageBox(self)
    msg.setIcon(QMessageBox.Warning)
    msg.setWindowTitle("데이터 샘플링 적용")
    msg.setText(
        f"<b>⚠️ 층화 샘플링이 적용됩니다</b><br><br>"
        f"전체 데이터: <b>{info.total_rows:,}건</b><br>"
        f"분석 대상: <b>{info.sampled_rows:,}건</b> "
        f"({info.ratio_pct:.1f}% 샘플링)<br><br>"
        f"메모리 한계로 인해 비례 층화 샘플링이 적용됩니다.<br>"
        f"결과 해석 시 이 점을 반드시 고려하세요."
    )
    msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    msg.button(QMessageBox.Ok).setText("계속 진행")
    msg.button(QMessageBox.Cancel).setText("취소")

    if msg.exec_() == QMessageBox.Cancel:
        self._cancel_analysis()  # 기존 분석 취소 메서드 호출
        return

    # 결과 섹션 제목에 샘플링 표시
    self._sampling_label = info.label  # 이후 결과 표시 시 활용
```

- [ ] **Step 4: 결과 테이블 제목에 샘플링 라벨 추가**

결과를 표시하는 탭 섹션의 제목(QLabel 또는 GroupBox 텍스트)에:
```python
# 기존
title = "Cox 회귀 결과"
# 수정 후
sampling_suffix = f" ({self._sampling_label})" if getattr(self, '_sampling_label', '') else ""
title = f"Cox 회귀 결과{sampling_suffix}"
```

- [ ] **Step 5: 문법 검증**

```bash
python3 -c "import ast; ast.parse(open('tabs.py').read()); print('tabs.py OK')"
```

- [ ] **Step 6: 커밋**

```bash
git add tabs.py
git commit -m "feat: 샘플링 UI 경고 다이얼로그 + 결과 제목 표시"
```

---

## Task 9: results_exporter.py Excel 헤더에 샘플링 정보 포함

**Files:**
- Modify: `results_exporter.py`
- Create: `tests/test_sampling_export.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_sampling_export.py
import pytest
import tempfile
from pathlib import Path
import pandas as pd
import openpyxl
from results_exporter import ResultsExporter
from statistical_analysis import SamplingInfo


def _make_sampling_info(applied=True, total=1_000_000, sampled=400_000):
    return SamplingInfo(applied=applied, total_rows=total, sampled_rows=sampled)


def test_excel_header_contains_sampling_info_when_applied(tmp_path):
    exporter = ResultsExporter(output_dir=str(tmp_path))
    info = _make_sampling_info(applied=True, total=1_000_000, sampled=400_000)

    df = pd.DataFrame({'HR': [1.2], 'p': [0.03]})
    cox_results = {'model1': {'summary': df}}

    path = exporter.export_cox_results(cox_results, sampling_info=info)

    wb = openpyxl.load_workbook(path)
    ws = wb.active
    # 첫 번째 행이 샘플링 정보 포함 여부 확인
    first_row_values = [ws.cell(1, c).value for c in range(1, 5)]
    assert any("400,000" in str(v) for v in first_row_values if v)


def test_excel_header_no_sampling_row_when_not_applied(tmp_path):
    exporter = ResultsExporter(output_dir=str(tmp_path))
    info = _make_sampling_info(applied=False, total=500, sampled=500)

    df = pd.DataFrame({'HR': [1.1], 'p': [0.05]})
    cox_results = {'model1': {'summary': df}}

    path = exporter.export_cox_results(cox_results, sampling_info=info)

    wb = openpyxl.load_workbook(path)
    ws = wb.active
    first_row_values = [ws.cell(1, c).value for c in range(1, 5)]
    # 샘플링 정보 행 없어야 함
    assert not any("층화" in str(v) for v in first_row_values if v)
```

- [ ] **Step 2: 테스트 실행 → FAIL 확인**

```bash
python3 -m pytest tests/test_sampling_export.py -v
```
Expected: FAIL (`export_cox_results` does not accept `sampling_info` parameter)

- [ ] **Step 3: results_exporter.py 수정**

`export_cox_results` 시그니처 및 구현 수정:
```python
def export_cox_results(self, cox_results, filename='cox_regression.xlsx',
                       sampling_info=None):
    summaries = {k: v for k, v in cox_results.items() if 'summary' in v}
    if not summaries:
        logger.warning(f"Cox 결과 내보내기 생략: 저장할 모델 요약 없음 ({filename})")
        return None
    path = self.output_dir / filename
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        for name, data in summaries.items():
            df_out = data['summary'].copy()
            if sampling_info is not None and sampling_info.applied:
                # 샘플링 정보 행을 데이터 위에 삽입
                info_row = pd.DataFrame([[
                    f"⚠️ {sampling_info.label}",
                    f"분석일: {__import__('datetime').date.today()}",
                    "", ""
                ]])
                df_out = pd.concat(
                    [info_row, df_out], ignore_index=True
                )
            df_out.to_excel(writer, sheet_name=name[:31], index=False)
    return str(path)
```

동일 패턴을 `export_psm_results`, `export_subgroup_results`에도 `sampling_info=None` 파라미터 추가 (헤더 삽입 로직 동일하게 적용).

- [ ] **Step 4: 테스트 통과 확인**

```bash
python3 -m pytest tests/test_sampling_export.py -v
```
Expected: 2 passed

- [ ] **Step 5: 커밋**

```bash
git add results_exporter.py tests/test_sampling_export.py
git commit -m "feat: Excel 결과 헤더에 샘플링 정보 포함"
```

---

## Task 10: 전체 A단계 테스트 실행 및 최종 검증

- [ ] **Step 1: 전체 테스트 실행**

```bash
python3 -m pytest tests/test_cohort_safety.py tests/test_utils_errors.py \
    tests/test_sampling_info.py tests/test_sampling_export.py \
    tests/test_db_connector_decimal_chunks.py tests/test_cohort_builder.py \
    -v --tb=short
```
Expected: 모든 테스트 통과

- [ ] **Step 2: 문법 전체 검증**

```bash
python3 -c "
import ast, pathlib
for f in pathlib.Path('.').glob('*.py'):
    try:
        ast.parse(f.read_text())
        print(f'OK: {f}')
    except SyntaxError as e:
        print(f'ERROR: {f}: {e}')
"
```
Expected: 모든 파일 `OK`

- [ ] **Step 3: 최종 커밋**

```bash
git add .
git commit -m "feat: Stage A 완료 — 안전성·UX 개선 (CohortBuilder 재시도, 예외 구체화, 샘플링 경고)"
```

---

## 완료 기준

| 항목 | 검증 방법 |
|------|---------|
| CohortBuilder 단계 실패 시 즉시 중단 | `test_build_cohort_stops_on_step_failure` |
| 1회 재시도 후 성공 시 계속 진행 | `test_run_step_retries_once_and_succeeds` |
| 결과 0건 시 CohortStepError | `test_run_step_raises_on_zero_rows` |
| 예외 타입별 사용자 메시지 | `test_utils_errors.py` 전체 |
| 샘플링 SamplingInfo 정확성 | `test_sampling_info.py` 전체 |
| Excel 헤더 샘플링 정보 | `test_sampling_export.py` 전체 |
| 기존 테스트 회귀 없음 | `test_cohort_builder.py`, `test_db_connector_decimal_chunks.py` |
