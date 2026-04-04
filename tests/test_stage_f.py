"""
tests/test_stage_f.py - Stage F 최소 유효 행 수 하한 검증 테스트
"""

import pytest
import duckdb
import pandas as pd
from unittest.mock import patch
from statistical_analysis import StatisticalAnalyzer, SamplingInfo
from utils import InsufficientDataError

# 테스트 데이터 상수
_MIN_VALID_ROWS = 30   # config.py STUDY_SETTINGS['MIN_VALID_ROWS'] 와 동일


def _make_analyzer_with_conn(conn):
    """테스트용 StatisticalAnalyzer — 실제 DuckDB 연결 사용."""
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


def test_nonsampling_path_below_min_rows_raises_insufficient_data_error():
    """비샘플링 경로에서 유효 행이 MIN_VALID_ROWS 미만이면 InsufficientDataError 발생."""
    conn = duckdb.connect(':memory:')
    # 유효 행 10건 < MIN_VALID_ROWS(30) → InsufficientDataError
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range(10)
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 200  # total(10) <= 200 → 비샘플링
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        with pytest.raises(InsufficientDataError) as exc_info:
            analyzer._load_data()

    assert exc_info.value.valid_rows == 10
    assert exc_info.value.min_rows == _MIN_VALID_ROWS


def test_nonsampling_path_exactly_min_rows_succeeds():
    """비샘플링 경로에서 유효 행이 정확히 MIN_VALID_ROWS 이면 성공해야 한다."""
    conn = duckdb.connect(':memory:')
    conn.execute(f"""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range({_MIN_VALID_ROWS})
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 200
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        df, info = analyzer._load_data()  # 예외 없이 성공

    assert len(df) == _MIN_VALID_ROWS


def test_sampling_path_below_min_rows_raises_insufficient_data_error():
    """샘플링 경로에서 유효 행 합계가 MIN_VALID_ROWS 미만이면 InsufficientDataError 발생."""
    conn = duckdb.connect(':memory:')
    # total(500) > max_rows(50) → 샘플링 분기, 하지만 유효 행 10건 < 30
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range(10)
        UNION ALL
        SELECT 'T2DM_OHA', 0, 0.0, 0 FROM range(490)
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 50  # total(500) > 50 → 샘플링
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        with pytest.raises(InsufficientDataError):
            analyzer._load_data()
