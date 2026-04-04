"""
tests/test_stage_c.py - Stage C Codex 발견 수정 테스트
"""

import pytest
import duckdb
import pandas as pd
from unittest.mock import MagicMock, patch
from statistical_analysis import StatisticalAnalyzer, SamplingInfo


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


def test_zero_budget_stratum_excluded_from_sample():
    """non_dm_budget == 0 일 때 NON_DM 이 샘플에 포함되지 않아야 한다."""
    conn = duckdb.connect(':memory:')
    # DM 그룹 600건 (max_rows=500 이므로 DM 전수 > max_rows → non_dm_budget=0)
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range(600)
        UNION ALL
        SELECT 'NON_DM', 1, 1.0, 0 FROM range(200)
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 500  # DM 600 > 500 → budget 초과
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        df, info = analyzer._load_data()

    # non_dm_budget = max(500 - 600, 0) = 0 → NON_DM 0건 할당
    # 수정 전: max(1, 0) = 1 → NON_DM 1건 포함 (버그)
    # 수정 후: NON_DM 0건 → 샘플에 없어야 함
    non_dm_rows = df[df['exposure_group'] == 'NON_DM']
    assert len(non_dm_rows) == 0, \
        f"non_dm_budget=0 인데 NON_DM {len(non_dm_rows)}건이 샘플에 포함됨"


def test_nonzero_budget_stratum_included():
    """정상 예산 할당 시 그룹이 샘플에 포함된다."""
    conn = duckdb.connect(':memory:')
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range(300)
        UNION ALL
        SELECT 'NON_DM', 1, 1.0, 0 FROM range(300)
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 400  # 총 600 > 400 → 샘플링
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        df, info = analyzer._load_data()

    non_dm_rows = df[df['exposure_group'] == 'NON_DM']
    # non_dm_budget = max_rows(400) - dm_total(300) = 100
    assert len(non_dm_rows) == 100, \
        f"NON_DM 샘플 건수가 예산(100)과 다름: {len(non_dm_rows)}건"
    dm_rows = df[df['exposure_group'] == 'T2DM_OHA']
    assert len(dm_rows) == 300, \
        f"DM 그룹 전수 포함되어야 하나 {len(dm_rows)}건"


def test_worker_thread_logs_exception_to_file_logger():
    """WorkerThread.run() 예외 시 logger.exception 이 호출된다."""
    from main_app import WorkerThread

    def failing_func(progress_callback=None):
        raise ValueError("test error for audit")

    thread = WorkerThread(failing_func)
    thread.error = MagicMock()

    with patch('main_app.logger') as mock_logger:
        thread.run()

    mock_logger.exception.assert_called_once_with("WorkerThread 분석 중 예외 발생")
    # error signal 도 여전히 emit 되어야 함
    thread.error.emit.assert_called_once()
