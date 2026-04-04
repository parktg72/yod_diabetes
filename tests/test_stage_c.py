"""
tests/test_stage_c.py - Stage C Codex 발견 수정 테스트
"""

import pytest
import duckdb
import pandas as pd
from unittest.mock import MagicMock, patch
from statistical_analysis import StatisticalAnalyzer, SamplingInfo

# 테스트 데이터 상수
_DM_COUNT = 300
_NON_DM_COUNT = 300
_MAX_ROWS = 400          # DM 전수 포함 후 NON_DM 예산 = 400 - 300 = 100
_NON_DM_BUDGET = _MAX_ROWS - _DM_COUNT   # 100


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
    """정상 예산 할당 시 그룹이 샘플에 포함되고 SamplingInfo 가 정확하다."""
    conn = duckdb.connect(':memory:')
    conn.execute(f"""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 1 AS follow_up_days, 1.0 AS follow_up_years, 0 AS dementia_event
        FROM range({_DM_COUNT})
        UNION ALL
        SELECT 'NON_DM', 1, 1.0, 0 FROM range({_NON_DM_COUNT})
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = _MAX_ROWS  # 총 600 > 400 → 샘플링
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        df, info = analyzer._load_data()

    # 행 수 검증: non_dm_budget = _MAX_ROWS(400) - _DM_COUNT(300) = 100
    non_dm_rows = df[df['exposure_group'] == 'NON_DM']
    assert len(non_dm_rows) == _NON_DM_BUDGET, \
        f"NON_DM 샘플 건수가 예산({_NON_DM_BUDGET})과 다름: {len(non_dm_rows)}건"
    dm_rows = df[df['exposure_group'] == 'T2DM_OHA']
    assert len(dm_rows) == _DM_COUNT, \
        f"DM 그룹 전수 포함되어야 하나 {len(dm_rows)}건"

    # SamplingInfo 메타데이터 계약 검증
    assert info.applied is True, "샘플링 적용 시 info.applied=True 여야 함"
    assert info.sampled_rows == _DM_COUNT + _NON_DM_BUDGET, \
        f"info.sampled_rows={info.sampled_rows}, 예상={_DM_COUNT + _NON_DM_BUDGET}"


def test_no_valid_rows_raises_empty_data_error():
    """follow_up_days > 0 인 행이 없으면 EmptyDataError 가 발생해야 한다."""
    conn = duckdb.connect(':memory:')
    # 모든 행의 follow_up_days = 0 → WHERE follow_up_days > 0 필터에 전부 제외
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 0 AS follow_up_days, 0.0 AS follow_up_years, 0 AS dementia_event
        FROM range(100)
        UNION ALL
        SELECT 'NON_DM', 0, 0.0, 0 FROM range(100)
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        # total(200) > max_rows(50) → 샘플링 분기 진입 → valid_total=0 → EmptyDataError
        mock_mm.get_safe_analysis_rows.return_value = 50
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        with pytest.raises(pd.errors.EmptyDataError):
            analyzer._load_data()


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
    # error signal 도 여전히 emit 되어야 하고 예외 메시지가 포함되어야 함
    thread.error.emit.assert_called_once()
    emit_arg = thread.error.emit.call_args[0][0]
    assert "test error for audit" in emit_arg, \
        f"error.emit 인자에 예외 메시지 누락: {emit_arg!r}"


def test_nonsampling_path_no_valid_rows_raises_empty_data_error():
    """비샘플링 경로(total <= max_rows)에서 유효 행이 없으면 EmptyDataError 가 발생해야 한다.

    비샘플링 경로에서 빈 DataFrame 을 반환하면 하위 run_cox() 에서
    lifelines cph.fit() 이 LinAlgError/ValueError 를 발생시켜 디버깅이 어렵다.
    EmptyDataError 로 조기 실패해야 명확한 오류 메시지를 제공할 수 있다.
    """
    conn = duckdb.connect(':memory:')
    # total(50) <= max_rows(200) → 비샘플링 경로
    # follow_up_days=0 → WHERE follow_up_days > 0 필터 후 유효 행 0건
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'T2DM_OHA' AS exposure_group, 0 AS follow_up_days, 0.0 AS follow_up_years, 0 AS dementia_event
        FROM range(50)
    """)

    analyzer = _make_analyzer_with_conn(conn)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 200  # total(50) <= 200 → 비샘플링
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        with pytest.raises(pd.errors.EmptyDataError, match="follow_up_days > 0"):
            analyzer._load_data()
