"""tests/test_stage_q.py — Stage Q: progress emit 커버리지"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

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
    assert any("데이터 전처리 중" in m for m in messages), \
        f"'데이터 전처리 중' 메시지 없음. 실제: {messages}"


def test_load_data_emits_sampling_message():
    """total > max_rows 샘플링 분기에서 '샘플링' 관련 메시지를 emit 해야 한다."""
    dm = MagicMock()
    # total=100, max_rows=5 → 샘플링 분기 강제 진입
    dm.storage.get_row_count.return_value = 100
    group_df = pd.DataFrame({'exposure_group': ['NON_DM'], 'cnt': [100]})
    sample_df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * 5,
        'follow_up_days': [365] * 5,
        'follow_up_years': [1.0] * 5,
        'age_at_index': [55.0] * 5,
        'SEX_TYPE': ['1'] * 5,
    })
    dm.query.side_effect = [group_df, sample_df]
    dm.execute.return_value = None

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 5
        mock_mm.optimize_dtypes.side_effect = lambda df: df

        analyzer = StatisticalAnalyzer(dm)
        messages = []
        analyzer._load_data(cb=messages.append)

    assert any("샘플링" in m or "층화" in m for m in messages), \
        f"샘플링 메시지 없음. 실제: {messages}"


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


def test_run_cox_emits_per_model_progress():
    """run_cox 가 각 모델(model1/2/3) 피팅 전 메시지를 emit 해야 한다."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)

    df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * 40,
        'is_t1dm': [0] * 40, 'is_t2dm_oha': [0] * 40,
        'is_t2dm_insulin': [0] * 40, 'is_t2dm_nomed': [0] * 40,
        'age_at_index': [55.0] * 40,
        'male': [1] * 40,
        'follow_up_years': [1.0] * 40,
        'dementia_event': [0] * 30 + [1] * 10,
    })
    messages = []
    try:
        analyzer.run_cox('dementia_event', cb=messages.append, df_prepared=df)
    except Exception:
        pass

    model_msgs = [m for m in messages if 'model' in m.lower() or '모델' in m.lower()]
    assert len(model_msgs) >= 3, \
        f"모델별 진행 메시지 3개 미만. 실제: {messages}"


def test_run_competing_risks_emits_per_outcome_progress():
    """run_competing_risks 가 각 outcome 시작 시 메시지를 emit 해야 한다."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)

    df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * 40,
        'is_t1dm': [0] * 40, 'is_t2dm_oha': [0] * 40,
        'is_t2dm_insulin': [0] * 40, 'is_t2dm_nomed': [0] * 40,
        'age_at_index': [55.0] * 40, 'male': [1] * 40,
        'follow_up_years': [1.0] * 40,
        'dementia_event': [0] * 35 + [1] * 5,
        'ad_event': [0] * 38 + [1] * 2,
        'vad_event': [0] * 39 + [1] * 1,
        'competing_death_event': [0] * 36 + [1] * 4,
    })
    messages = []
    try:
        analyzer.run_competing_risks(cb=messages.append, df_prepared=df)
    except Exception:
        pass

    assert any('dementia_event' in m for m in messages), f"dementia_event 메시지 없음: {messages}"
    assert any('ad_event' in m for m in messages), f"ad_event 메시지 없음: {messages}"
    assert any('vad_event' in m for m in messages), f"vad_event 메시지 없음: {messages}"


def test_run_cox_standalone_passes_cb_to_load_data(monkeypatch):
    """run_cox(cb=..., df_prepared=None) 시 _load_data 에 cb 가 전달되어야 한다."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_cox('dementia_event', cb=cb, df_prepared=None)
    except (pd.errors.EmptyDataError, Exception):
        pass

    assert load_cb_received and load_cb_received[0] is cb, \
        f"run_cox fallback: _load_data 에 cb 미전달. received={load_cb_received}"


def test_run_psm_standalone_passes_cb_to_load_data(monkeypatch):
    """run_psm(cb=..., df_prepared=None) 시 _load_data 에 cb 가 전달되어야 한다."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_psm(cb=cb, df_prepared=None)
    except (pd.errors.EmptyDataError, Exception):
        pass

    assert load_cb_received and load_cb_received[0] is cb, \
        f"run_psm fallback: _load_data 에 cb 미전달. received={load_cb_received}"


def test_run_competing_risks_emits_skip_message_when_insufficient_rows():
    """유효 행 부족으로 스킵될 때 스킵 메시지를 emit 해야 한다."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)

    # MIN_VALID_ROWS=30 — 29행으로 스킵 유도
    n = 29
    df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * n,
        'is_t1dm': [0] * n, 'is_t2dm_oha': [0] * n,
        'is_t2dm_insulin': [0] * n, 'is_t2dm_nomed': [0] * n,
        'age_at_index': [55.0] * n, 'male': [1] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * 24 + [1] * 5,
        'competing_death_event': [0] * 25 + [1] * 4,
    })
    messages = []
    analyzer.run_competing_risks(cb=messages.append, df_prepared=df)

    skip_msgs = [m for m in messages if '스킵' in m or 'skip' in m.lower()]
    assert skip_msgs, f"스킵 메시지 없음. 실제: {messages}"


def test_main_app_on_error_hides_progress_bar():
    """_on_error 가 progress_bar 를 숨기고 버튼을 활성화해야 한다."""
    main_app = pytest.importorskip('main_app', reason="PyQt5 필요")
    mw = MagicMock()
    with patch('main_app.QMessageBox'):
        main_app.MainWindow._on_error(mw, "테스트 오류")
    mw.progress_bar.setVisible.assert_called_once_with(False)
    mw._set_action_buttons_enabled.assert_called_once_with(True)


def test_run_interaction_standalone_passes_cb_to_load_data(monkeypatch):
    """run_interaction(cb=..., df_prepared=None) 시 _load_data 에 cb 전달."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_interaction(cb=cb, df_prepared=None)
    except Exception:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        f"run_interaction fallback: cb 미전달. received={load_cb_received}"


def test_run_subgroup_standalone_passes_cb_to_load_data(monkeypatch):
    """run_subgroup(cb=..., df_prepared=None) 시 _load_data 에 cb 전달."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_subgroup(cb=cb, df_prepared=None)
    except Exception:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        f"run_subgroup fallback: cb 미전달. received={load_cb_received}"
