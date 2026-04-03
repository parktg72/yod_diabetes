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


from unittest.mock import MagicMock, patch
from statistical_analysis import StatisticalAnalyzer, SamplingInfo


def _make_analyzer(rows, max_rows=500_000):
    """테스트용 StatisticalAnalyzer with in-memory DuckDB."""
    conn = duckdb.connect(':memory:')
    # 최소 스키마의 final_analysis 테이블 생성
    # NON_DM만 사용: _load_data의 샘플링 로직이 NON_DM을 예산 내로 제한하므로
    # 순수 NON_DM 데이터로 sampling 경로를 검증한다.
    conn.execute("""
        CREATE TABLE final_analysis AS
        SELECT 'NON_DM' AS exposure_group,
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
