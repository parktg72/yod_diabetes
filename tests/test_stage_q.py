"""tests/test_stage_q.py — Stage Q: progress emit 커버리지"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statistical_analysis import StatisticalAnalyzer


_MIN_VALID_ROWS = 30  # config.py STUDY_SETTINGS['MIN_VALID_ROWS'] 와 동일


def _make_dm(total_rows=_MIN_VALID_ROWS):
    dm = MagicMock()
    dm.storage.get_row_count.return_value = total_rows
    sample_df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * total_rows,
        'follow_up_days': [365] * total_rows,
        'follow_up_years': [1.0] * total_rows,
        'age_at_index': [55.0] * total_rows,
        'SEX_TYPE': ['1'] * total_rows,
    })
    dm.query.return_value = sample_df
    return dm


def test_load_data_emits_start_message():
    dm = _make_dm()
    analyzer = StatisticalAnalyzer(dm)
    messages = []
    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 10_000
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        analyzer._load_data(cb=messages.append)
    assert any("분석 데이터 로딩 중" in m for m in messages), \
        f"'분석 데이터 로딩 중' 메시지 없음. 실제: {messages}"


def test_load_data_emits_completion_message():
    dm = _make_dm()
    analyzer = StatisticalAnalyzer(dm)
    messages = []
    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 10_000
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        analyzer._load_data(cb=messages.append)
    assert any("데이터 로드 완료" in m for m in messages), \
        f"'데이터 로드 완료' 메시지 없음. 실제: {messages}"


def test_prepare_emits_progress_message():
    dm = _make_dm()
    analyzer = StatisticalAnalyzer(dm)
    df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * 5,
        'SEX_TYPE': ['1'] * 5,
        'follow_up_years': [1.0] * 5,
        'age_at_index': [55.0] * 5,
    })
    messages = []
    analyzer._prepare(df, cb=messages.append)
    assert any("전처리" in m for m in messages), \
        f"'전처리' 메시지 없음. 실제: {messages}"


def test_run_selected_passes_cb_to_load_data(monkeypatch):
    dm = _make_dm()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_selected(cb=cb)
    except pd.errors.EmptyDataError:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        "_load_data에 cb가 전달되지 않음"
