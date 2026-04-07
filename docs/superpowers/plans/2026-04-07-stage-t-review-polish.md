# Stage T: 리뷰 후속 폴리시 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage S 종합 리뷰에서 발견된 Important 2건(run_interaction 무음 스킵, test_stage_s.py 실효 커버리지 부재)과 Minor 3건(docstring, keyword 인수, 테스트 안정성)을 수정한다.

**Architecture:** Task 1(run_interaction cb emit 추가) → Task 2(test_stage_s.py 실질 테스트 보강) → Task 3(코드 폴리시 3건) 순서로 독립 구현.

**Tech Stack:** Python 3.12, pandas, lifelines (CoxPHFitter), pytest, unittest.mock

---

## 파일 변경 맵

| 파일 | 변경 유형 | 내용 |
|------|-----------|------|
| `statistical_analysis.py` | Modify | `run_interaction` 무음 스킵에 cb emit 추가; `run_selected` 위치 인수 → 키워드 인수 |
| `tests/test_stage_qr.py` | Modify | `run_interaction` 스킵 cb 테스트 2개 추가; docstring 수정; competing_risks skip 테스트 동적 n 사용 |
| `tests/test_stage_s.py` | Modify | run_competing_risks/generate_table1 fallback cb 테스트 2개 추가 |

---

## Task 1: `run_interaction` 무음 스킵에 cb emit 추가

**Files:**
- Modify: `statistical_analysis.py:474-498` (2곳 emit 추가)
- Modify: `tests/test_stage_qr.py` (끝에 테스트 2개 추가)

### 배경

`run_interaction`은 두 가지 조건에서 `return None`으로 무음 스킵한다:
1. 라인 474: `dm_duration_cat` 컬럼이 없을 때
2. 라인 493-498: 유효 행/이벤트 수 부족

두 경우 모두 `cb` emit 없이 종료되어 사용자는 왜 결과가 없는지 알 수 없다.

현재 코드 (`statistical_analysis.py:467-498`):
```python
def run_interaction(self, cb=None, df_prepared=None):
    if cb: cb("상호작용 분석 중...")
    if df_prepared is None:
        raw, _ = self._load_data(cb=cb)
        df_prepared = self._prepare(raw, cb=cb)

    df_dm = df_prepared[df_prepared['exposure_group'] != 'NON_DM']
    if 'dm_duration_cat' not in df_dm.columns:
        return None           # ← cb emit 없음

    ...

    if len(d) < _min_rows or int(d['dementia_event'].sum()) < _min_events:
        logger.warning(...)
        return None           # ← cb emit 없음
```

- [ ] **Step 1: 실패하는 테스트 추가 (`tests/test_stage_qr.py` 끝에 추가)**

```python
def test_run_interaction_emits_skip_when_no_dm_duration_cat():
    """dm_duration_cat 컬럼 없을 때 스킵 메시지를 emit 해야 한다."""
    import pandas as pd
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)

    n = 40
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * n,
        'is_t1dm': [1] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
        'income_q': [3] * n,
        'cci_score': [1] * n,
        'follow_up_years': [2.0] * n,
        'dementia_event': [0] * 30 + [1] * 10,
        # dm_duration_cat 컬럼 의도적으로 누락
    })
    messages = []
    analyzer.run_interaction(cb=messages.append, df_prepared=df)

    skip_msgs = [m for m in messages if '스킵' in m or 'dm_duration_cat' in m]
    assert skip_msgs, f"dm_duration_cat 없을 때 스킵 메시지 없음. 실제: {messages}"


def test_run_interaction_emits_skip_when_insufficient_data():
    """행/이벤트 수 부족 시 스킵 메시지를 emit 해야 한다."""
    import pandas as pd
    from config import STUDY_SETTINGS
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)

    min_rows = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
    n = min_rows - 1  # 최소 행 미달
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * n,
        'is_t1dm': [1] * n,
        'dm_duration_cat': ['<5yr'] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
        'income_q': [3] * n,
        'cci_score': [1] * n,
        'follow_up_years': [2.0] * n,
        'dementia_event': [0] * n,  # 이벤트 0개 — 이중 조건 위반
    })
    messages = []
    analyzer.run_interaction(cb=messages.append, df_prepared=df)

    skip_msgs = [m for m in messages if '스킵' in m or '부족' in m]
    assert skip_msgs, f"데이터 부족 시 스킵 메시지 없음. 실제: {messages}"
```

- [ ] **Step 2: 테스트 실행 — FAIL 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_stage_qr.py::test_run_interaction_emits_skip_when_no_dm_duration_cat tests/test_stage_qr.py::test_run_interaction_emits_skip_when_insufficient_data -v 2>&1 | tail -15
```

기대: 2개 FAIL

- [ ] **Step 3: `statistical_analysis.py` 수정 — 2곳에 emit 추가**

라인 474 앞뒤 (컬럼 없을 때):
```python
        if 'dm_duration_cat' not in df_dm.columns:
            if cb: cb("상호작용 분석 스킵: dm_duration_cat 컬럼 없음")
            return None
```

라인 493-498 (데이터 부족 시):
```python
        if len(d) < _min_rows or int(d['dementia_event'].sum()) < _min_events:
            logger.warning(
                "run_interaction: 데이터 부족 — 행 수 %d (최소 %d), 이벤트 수 %d (최소 %d) — 분석 스킵",
                len(d), _min_rows, int(d['dementia_event'].sum()), _min_events,
            )
            if cb: cb(
                f"상호작용 분석 스킵: 데이터 부족 "
                f"({len(d)}행/{int(d['dementia_event'].sum())}이벤트, "
                f"최소 {_min_rows}행/{_min_events}이벤트 필요)"
            )
            return None
```

- [ ] **Step 4: 테스트 PASS + 전체 회귀 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_stage_qr.py -v 2>&1 | tail -20
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: test_stage_qr.py 전체 PASSED, 전체 206+ passed (pre-existing 4 failures 그대로)

- [ ] **Step 5: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add statistical_analysis.py tests/test_stage_qr.py
git commit -m "fix: run_interaction 무음 스킵에 cb emit 추가 (Stage T T1)"
```

---

## Task 2: `test_stage_s.py` 실질 테스트 보강

**Files:**
- Modify: `tests/test_stage_s.py` (run_competing_risks/generate_table1 fallback cb 테스트 추가)

### 배경

현재 `test_stage_s.py`의 유일한 테스트 `test_on_post_analysis_result_none_guard`는 Python 언어 기능(`x or {}`)만 검증하고 실제 프로젝트 코드를 전혀 실행하지 않는다. Stage S T1에서 수정한 `run_competing_risks`와 `generate_table1`의 fallback cb 전달은 `test_stage_qr.py`에서 `run_interaction`, `run_subgroup`만 테스트되고 이 두 함수는 테스트가 없다.

- [ ] **Step 1: `tests/test_stage_s.py` 파일 끝에 테스트 2개 추가**

기존 `test_on_post_analysis_result_none_guard` 아래에 추가:

```python
from unittest.mock import MagicMock
import pandas as pd
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from statistical_analysis import StatisticalAnalyzer


def test_run_competing_risks_standalone_passes_cb_to_load_data(monkeypatch):
    """run_competing_risks(cb=..., df_prepared=None) 시 _load_data 에 cb 전달."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_competing_risks(cb=cb, df_prepared=None)
    except Exception:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        f"run_competing_risks fallback: cb 미전달. received={load_cb_received}"


def test_generate_table1_standalone_passes_cb_to_load_data(monkeypatch):
    """generate_table1(cb=..., df_prepared=None) 시 _load_data 에 cb 전달."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.generate_table1(cb=cb, df_prepared=None)
    except Exception:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        f"generate_table1 fallback: cb 미전달. received={load_cb_received}"
```

**주의**: 파일 상단에 `from unittest.mock import MagicMock`, `import pandas as pd` 등 import가 없다면 파일 맨 위에 추가한다.

- [ ] **Step 2: 테스트 실행 — PASS 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_stage_s.py -v 2>&1 | tail -10
```

기대: 3개 PASSED (기존 1개 + 신규 2개)

- [ ] **Step 3: 전체 회귀 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: 208+ passed, no new failures

- [ ] **Step 4: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add tests/test_stage_s.py
git commit -m "test: test_stage_s.py run_competing_risks/generate_table1 fallback cb 테스트 추가 (Stage T T2)"
```

---

## Task 3: 코드 폴리시 (Minor M1~M3)

**Files:**
- Modify: `tests/test_stage_qr.py:1` (docstring 수정)
- Modify: `statistical_analysis.py:944-972` (run_selected 위치 인수 → 키워드 인수)
- Modify: `tests/test_stage_qr.py:215-216` (n 하드코딩 → 동적 계산)

### M1: test_stage_qr.py docstring 수정

- [ ] **Step 1: 라인 1 수정**

현재:
```python
"""tests/test_stage_q.py — Stage Q: progress emit 커버리지"""
```

변경 후:
```python
"""tests/test_stage_qr.py — Stage Q+R+S: progress emit 커버리지"""
```

### M2: run_selected 위치 인수 → 키워드 인수

현재 (`statistical_analysis.py` 라인 944-972):
```python
        self.generate_table1(cb, df_prepared)
        ...
        self.run_cox(oc, cb, df_prepared)
        ...
        self.run_psm(cb, df_prepared)
        ...
        self.run_interaction(cb, df_prepared)
        ...
        self.run_subgroup(cb, df_prepared)
        ...
        self.run_competing_risks(cb, df_prepared)
        ...
        self.run_sensitivity(cb)
```

변경 후:
```python
        self.generate_table1(cb=cb, df_prepared=df_prepared)
        ...
        self.run_cox(oc, cb=cb, df_prepared=df_prepared)
        ...
        self.run_psm(cb=cb, df_prepared=df_prepared)
        ...
        self.run_interaction(cb=cb, df_prepared=df_prepared)
        ...
        self.run_subgroup(cb=cb, df_prepared=df_prepared)
        ...
        self.run_competing_risks(cb=cb, df_prepared=df_prepared)
        ...
        self.run_sensitivity(cb=cb)
```

### M3: competing_risks skip 테스트 n 동적 계산

현재 (`tests/test_stage_qr.py` 라인 215-216):
```python
    # MIN_VALID_ROWS=30 — 29행으로 스킵 유도
    n = 29
```

변경 후:
```python
    from config import STUDY_SETTINGS
    n = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30)) - 1  # 최소 행보다 1 적게
```

- [ ] **Step 2: 문법 확인 + 전체 테스트**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m py_compile statistical_analysis.py && echo "OK"
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: OK, 208+ passed

- [ ] **Step 3: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add statistical_analysis.py tests/test_stage_qr.py
git commit -m "refactor: run_selected keyword 인수 + 테스트 docstring/안정성 개선 (Stage T T3)"
```

---

## 자체 점검

### 스펙 커버리지

| 이슈 | Task | 상태 |
|------|------|------|
| Important I1: run_interaction 무음 스킵 cb emit | Task 1 | ✅ |
| Important I2: test_stage_s.py 실효 커버리지 | Task 2 | ✅ |
| Minor M1: test_stage_qr.py docstring | Task 3 | ✅ |
| Minor M2: run_selected keyword 인수 | Task 3 | ✅ |
| Minor M3: competing_risks skip 테스트 안정성 | Task 3 | ✅ |

### 시그니처 일관성

- `run_interaction(self, cb=None, df_prepared=None)` — Task 1 변경 없음, 시그니처 유지
- `run_selected`의 `self.run_cox(oc, cb=cb, ...)` — `run_cox(self, outcome='dementia_event', cb=None, df_prepared=None)` 시그니처와 일치
