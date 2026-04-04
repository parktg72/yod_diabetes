"""
tests/test_stage_g.py - Stage G 분석 견고성 강화 테스트
"""

import pytest
import duckdb
import pandas as pd
from unittest.mock import patch
from utils import format_error_for_user, InsufficientDataError
from statistical_analysis import StatisticalAnalyzer, SamplingInfo


def _make_analyzer_with_conn(conn):
    class MockStorage:
        def get_row_count(self, t):
            return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    class MockDM:
        storage = MockStorage()
        def query(self, sql):
            return conn.execute(sql).df()
        def execute(self, sql):
            conn.execute(sql)
    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer.dm = MockDM()
    analyzer.results = {}
    analyzer._cached_df = None
    analyzer._sampling_info = SamplingInfo(applied=False, total_rows=0, sampled_rows=0)
    return analyzer


def test_format_error_for_user_insufficient_data_error():
    """InsufficientDataError 가 사용자 친화적 메시지로 변환되어야 한다."""
    exc = InsufficientDataError(valid_rows=10, min_rows=30)
    msg = format_error_for_user(exc)
    assert "10" in msg or "30" in msg or "최소" in msg, \
        f"InsufficientDataError 전용 메시지 없음: {msg!r}"
    # ValueError 일반 분기("입력값 오류:")로 떨어지면 안 됨
    assert "입력값 오류" not in msg, \
        f"InsufficientDataError 가 일반 ValueError 로 처리됨: {msg!r}"


def test_check_min_rows_raises_on_small_df():
    """_check_min_rows() 가 기준 미달 DataFrame 에서 InsufficientDataError 를 발생시킨다."""
    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer.results = {}
    small_df = pd.DataFrame({'a': range(5)})
    with pytest.raises(InsufficientDataError):
        analyzer._check_min_rows(small_df, context="테스트")


def test_check_min_rows_passes_on_sufficient_df():
    """_check_min_rows() 가 기준 이상 DataFrame 에서 예외 없이 반환한다."""
    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer.results = {}
    ok_df = pd.DataFrame({'a': range(30)})
    analyzer._check_min_rows(ok_df, context="테스트")  # 예외 없음


def test_run_cox_raises_on_insufficient_events():
    """run_cox() 에서 이벤트 수가 MIN_EVENTS 미만이면 InsufficientDataError 발생."""
    conn = duckdb.connect(':memory:')
    # 30건이지만 치매 이벤트 0건
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group,
               365 AS follow_up_days,
               1.0 AS follow_up_years,
               0 AS dementia_event
        FROM range(30)
    """)
    analyzer = _make_analyzer_with_conn(conn)
    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 200
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        with pytest.raises(InsufficientDataError):
            analyzer.run_cox()


def test_load_data_raises_on_invalid_min_valid_rows():
    """MIN_VALID_ROWS 가 0 이하이면 ValueError 가 발생해야 한다."""
    conn = duckdb.connect(':memory:')
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range(50)
    """)
    analyzer = _make_analyzer_with_conn(conn)
    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 200
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        with patch('statistical_analysis.STUDY_SETTINGS', {'MIN_VALID_ROWS': 0, 'SAMPLING_SEED': 42}):
            with pytest.raises(ValueError, match="MIN_VALID_ROWS"):
                analyzer._load_data()
