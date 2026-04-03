import pytest
import duckdb
import pandas as pd
from utils import CohortStepError, format_error_for_user


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
