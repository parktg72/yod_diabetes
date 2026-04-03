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
