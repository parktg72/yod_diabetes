"""
tests/test_stage_b.py - Stage B 핵심 수정 테스트

Task 1: setup_logging Windows 경로
Task 2: CohortStepError 예외 체인
Task 3: _load_data() 0건 EmptyDataError
Task 4: ORDER BY RANDOM() → setseed
Task 5: export_all/export sampling_info 전달
Task 6: 샘플링 다이얼로그 사전 확인
Task 7: _on_error 트레이스백 미표시
"""

import sys
import logging
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Task 1: setup_logging Windows 경로
# ---------------------------------------------------------------------------

def test_setup_logging_windows_uses_localappdata(tmp_path, monkeypatch):
    """Windows에서 log_dir 생략 시 %LOCALAPPDATA% 하위 경로를 사용한다."""
    monkeypatch.setattr(sys, 'platform', 'win32')
    fake_local = tmp_path / "AppData" / "Local"
    fake_local.mkdir(parents=True)
    monkeypatch.setenv('LOCALAPPDATA', str(fake_local))

    import utils
    importlib.reload(utils)

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    utils.setup_logging()

    expected_dir = fake_local / "NHIS_YOD_DM_Analyzer" / "logs"
    assert expected_dir.exists(), f"로그 디렉토리 미생성: {expected_dir}"
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers, "FileHandler가 추가되지 않았습니다"
    assert "NHIS_YOD_DM_Analyzer" in file_handlers[0].baseFilename


def test_setup_logging_non_windows_uses_dot(tmp_path, monkeypatch):
    """비-Windows에서 log_dir 생략 시 현재 디렉토리(.)를 사용한다."""
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.chdir(tmp_path)

    import utils
    importlib.reload(utils)

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    utils.setup_logging()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers
    assert str(tmp_path) in file_handlers[0].baseFilename


# ---------------------------------------------------------------------------
# Task 2: CohortStepError 예외 체인
# ---------------------------------------------------------------------------

def test_run_step_exception_chain_preserved():
    """raise CohortStepError from e — __cause__ 가 원본 예외를 가리킨다."""
    import duckdb
    from cohort_builder import CohortBuilder
    from utils import CohortStepError

    dm = MagicMock()
    original_error = duckdb.Error("original db error")
    dm.execute.side_effect = original_error
    dm.storage = MagicMock()

    cb = CohortBuilder(dm)

    with patch('cohort_builder.time.sleep'):
        with pytest.raises(CohortStepError) as exc_info:
            cb._run_step(1, "테스트", "SELECT 1", "t")

    assert exc_info.value.__cause__ is original_error, \
        "__cause__ 가 원본 duckdb.Error 를 가리켜야 합니다"


# ---------------------------------------------------------------------------
# Task 3: _load_data() 0건 EmptyDataError
# ---------------------------------------------------------------------------

def test_load_data_zero_valid_rows_raises_empty_data_error():
    """follow_up_days > 0 인 행이 0건이면 EmptyDataError 를 발생시킨다."""
    from statistical_analysis import StatisticalAnalyzer

    dm = MagicMock()
    dm.storage.get_row_count.return_value = 1000
    dm.query.return_value = pd.DataFrame({'exposure_group': [], 'cnt': []})

    analyzer = StatisticalAnalyzer(dm)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 500
        with pytest.raises(pd.errors.EmptyDataError):
            analyzer._load_data()


# ---------------------------------------------------------------------------
# Task 4: ORDER BY RANDOM() → setseed
# ---------------------------------------------------------------------------

def test_sampling_info_has_seed_field():
    """SamplingInfo 는 seed 필드를 가진다."""
    from statistical_analysis import SamplingInfo
    info = SamplingInfo(applied=True, total_rows=1000, sampled_rows=500, seed=42)
    assert info.seed == 42


def test_sampling_info_label_includes_seed():
    """label 에 seed 값이 포함된다."""
    from statistical_analysis import SamplingInfo
    info = SamplingInfo(applied=True, total_rows=1000, sampled_rows=500, seed=42)
    assert "seed=42" in info.label


def test_load_data_calls_setseed():
    """_load_data 가 샘플링 전에 DuckDB setseed 를 호출한다."""
    from statistical_analysis import StatisticalAnalyzer

    dm = MagicMock()
    dm.storage.get_row_count.return_value = 1000
    group_df = pd.DataFrame({'exposure_group': ['T2DM_OHA', 'NON_DM'], 'cnt': [300, 700]})
    sampled_df = pd.DataFrame({'col': range(500), 'follow_up_days': [1] * 500})
    call_log = []

    def fake_query(sql):
        if 'setseed' in sql.lower():
            call_log.append('setseed_query')
        return group_df if 'GROUP BY' in sql else sampled_df

    def fake_execute(sql):
        if 'setseed' in sql.lower():
            call_log.append('setseed_exec')

    dm.query.side_effect = fake_query
    dm.execute.side_effect = fake_execute

    analyzer = StatisticalAnalyzer(dm)

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 500
        mock_mm.optimize_dtypes.side_effect = lambda df: df
        analyzer._load_data()

    assert 'setseed_exec' in call_log or 'setseed_query' in call_log, \
        "setseed 가 호출되지 않았습니다"


# ---------------------------------------------------------------------------
# Task 5: export_all/export sampling_info 전달
# ---------------------------------------------------------------------------

def test_export_all_passes_sampling_info(tmp_path):
    """export_all 이 sampling_info 를 ResultsExporter.export_all 에 전달한다."""
    import openpyxl
    from statistical_analysis import SamplingInfo
    from results_exporter import ResultsExporter

    info = SamplingInfo(applied=True, total_rows=1000, sampled_rows=500, seed=42)
    table1_df = pd.DataFrame({'var': ['age'], 'mean': [55.0]})
    ar = {'table1': table1_df, 'sampling_info': info}

    exp = ResultsExporter(str(tmp_path))
    files = exp.export_all(ar, sampling_info=info)
    assert files, "내보낸 파일이 없습니다"

    wb = openpyxl.load_workbook(files[0])
    ws = wb.active
    cell_value = ws.cell(1, 1).value
    assert cell_value is not None and "샘플링" in str(cell_value), \
        f"Row 1 에 샘플링 헤더 없음: {cell_value!r}"


# ---------------------------------------------------------------------------
# Task 6: 샘플링 다이얼로그 사전 확인
# ---------------------------------------------------------------------------

def test_confirm_sampling_returns_false_when_dialog_cancelled():
    """_confirm_sampling_if_needed 가 False 를 반환하면 워커가 시작되지 않는다."""
    from tabs import AnalysisTab, AppContext

    ctx = AppContext()
    dm = MagicMock()
    dm.storage.get_row_count.return_value = 999_999
    ctx.dm = dm

    tab = AnalysisTab.__new__(AnalysisTab)
    tab.ctx = ctx

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 500
        with patch.object(tab, '_show_sampling_dialog', return_value=False) as mock_dlg:
            result = tab._confirm_sampling_if_needed()

    assert result is False
    mock_dlg.assert_called_once()


def test_confirm_sampling_returns_true_when_no_sampling_needed():
    """데이터가 한도 이내면 다이얼로그 없이 True 를 반환한다."""
    from tabs import AnalysisTab, AppContext

    ctx = AppContext()
    dm = MagicMock()
    dm.storage.get_row_count.return_value = 100
    ctx.dm = dm

    tab = AnalysisTab.__new__(AnalysisTab)
    tab.ctx = ctx

    with patch('statistical_analysis.mem_manager') as mock_mm:
        mock_mm.get_safe_analysis_rows.return_value = 500
        with patch.object(tab, '_show_sampling_dialog') as mock_dlg:
            result = tab._confirm_sampling_if_needed()

    assert result is True
    mock_dlg.assert_not_called()


# ---------------------------------------------------------------------------
# Task 7: _on_error 트레이스백 미표시
# ---------------------------------------------------------------------------

def test_on_error_dialog_shows_only_first_line(monkeypatch):
    """_on_error 다이얼로그에 트레이스백이 아닌 예외 메시지 첫 줄만 표시된다."""
    import PyQt5.QtWidgets as _qt
    from main_app import MainWindow

    mw = MainWindow.__new__(MainWindow)
    mw.progress_bar = MagicMock()
    mw.statusBar = MagicMock()
    mw.log_text = MagicMock()

    shown_texts = []

    monkeypatch.setattr(
        _qt.QMessageBox, 'critical',
        staticmethod(lambda parent, title, text: shown_texts.append(text))
    )
    monkeypatch.setattr(mw, '_set_action_buttons_enabled', MagicMock())

    full_msg = (
        "ValueError: invalid input\n"
        "Traceback (most recent call last):\n"
        "  File x.py line 1\n"
        "    raise ValueError('invalid input')"
    )
    mw._on_error(full_msg)

    assert shown_texts, "critical 이 호출되지 않았습니다"
    assert "Traceback" not in shown_texts[0], \
        f"트레이스백이 다이얼로그에 표시되었습니다: {shown_texts[0]!r}"
    assert "ValueError" in shown_texts[0]
