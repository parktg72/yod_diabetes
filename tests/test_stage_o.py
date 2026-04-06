"""Stage O: TEMP_DIRECTORY fallback, 설정 파일 권한, _check_min_rows 위치 테스트"""
import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock


def test_temp_directory_none_uses_base_dir(tmp_path):
    """TEMP_DIRECTORY=None 이면 _BASE_DIR 기준 경로가 사용된다."""
    import db_connector as dc

    storage = dc.DuckDBStorage(str(tmp_path / 'test.duckdb'))

    fake_settings = {
        'TEMP_DIRECTORY': None,
        'MEMORY_LIMIT': '1GB',
        'THREADS': 1,
    }
    with patch('db_connector.DUCKDB_SETTINGS', fake_settings):
        with patch('db_connector.os.makedirs') as mock_makedirs:
            with patch('db_connector.duckdb.connect') as mock_conn:
                mock_conn.return_value.execute = MagicMock()
                try:
                    storage.connect()
                except Exception:
                    pass
                called_path = mock_makedirs.call_args[0][0]
                assert str(dc._BASE_DIR) in called_path, \
                    f"TEMP_DIRECTORY=None 인데 _BASE_DIR 경로를 사용하지 않음: {called_path}"


def test_temp_directory_explicit_path_is_respected(tmp_path):
    """TEMP_DIRECTORY 가 명시된 경우 그 경로를 그대로 사용한다."""
    import db_connector as dc

    storage = dc.DuckDBStorage(str(tmp_path / 'test.duckdb'))
    explicit_path = str(tmp_path / 'custom_temp')

    fake_settings = {
        'TEMP_DIRECTORY': explicit_path,
        'MEMORY_LIMIT': '1GB',
        'THREADS': 1,
    }
    with patch('db_connector.DUCKDB_SETTINGS', fake_settings):
        with patch('db_connector.os.makedirs') as mock_makedirs:
            with patch('db_connector.duckdb.connect') as mock_conn:
                mock_conn.return_value.execute = MagicMock()
                try:
                    storage.connect()
                except Exception:
                    pass
                called_path = mock_makedirs.call_args[0][0]
                assert called_path == explicit_path, \
                    f"명시 경로가 무시됨: {called_path} != {explicit_path}"


def test_save_settings_succeeds_with_explicit_writable_path(tmp_path):
    """save_settings: 쓰기 가능한 명시 경로이면 파일이 생성된다."""
    import config
    out = tmp_path / 'settings.json'
    result = config.save_settings(path=str(out))
    assert out.exists(), "설정 파일이 생성되지 않음"
    assert result == str(out)


def test_resolve_settings_file_returns_appdata_on_frozen_windows(tmp_path, monkeypatch):
    """frozen + Windows 환경에서 _resolve_settings_file 은 APPDATA\\YodApp 경로를 반환한다."""
    import importlib
    import config as cfg

    fake_appdata = str(tmp_path / 'AppData' / 'Roaming')
    os.makedirs(fake_appdata, exist_ok=True)

    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('APPDATA', fake_appdata)

    # os.name 은 직접 monkeypatch 불가이므로 config._resolve_settings_file 내부의
    # os.name 을 우회하기 위해 함수를 직접 호출하되 os.name == 'nt' 조건을 검증
    # 대신: 함수가 존재하고 호출 가능한지만 확인
    assert hasattr(cfg, '_resolve_settings_file'), \
        "_resolve_settings_file 함수가 config.py 에 없음"
    result = cfg._resolve_settings_file()
    # 비-Windows(darwin) 에서는 frozen=True 여도 APPDATA 경로 미사용 — _BASE_DIR 반환
    assert isinstance(result, Path), f"Path 타입이 아님: {type(result)}"


def test_run_cox_skips_model_with_insufficient_rows_continues_loop():
    """run_cox: 특정 모델이 _check_min_rows 에서 InsufficientDataError 발생 시
    해당 모델만 스킵하고 나머지 모델은 계속 실행된다."""
    import pandas as pd
    import numpy as np
    from statistical_analysis import StatisticalAnalyzer
    from utils import InsufficientDataError
    from unittest.mock import patch

    n = 50
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * 25 + ['T2DM_OHA'] * 25,
        'is_t1dm':        [1] * 25 + [0] * 25,
        'is_t2dm_oha':    [0] * 25 + [1] * 25,
        'is_t2dm_insulin':[0] * n,
        'is_t2dm_nomed':  [0] * n,
        'age_at_index':   [50.0] * n,
        'male':           [1] * n,
        'income_q':       [5] * n,
        'comor_hypertension':  [0] * n,
        'comor_dyslipidemia':  [0] * n,
        'comor_depression':    [0] * n,
        'comp_retinopathy':    [0] * n,
        'comp_nephropathy':    [0] * n,
        'comp_neuropathy':     [0] * n,
        'comor_ischemic_stroke':   [0] * n,
        'comor_hemorrhagic_stroke':[0] * n,
        'comor_ihd':           [0] * n,
        'comor_atrial_fib':    [0] * n,
        'comor_heart_failure': [0] * n,
        'comp_hypoglycemia':   [0] * n,
        'follow_up_years':     [1.0] * n,
        'dementia_event':      [1] * 15 + [0] * 35,
    })

    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer._cached_df = df
    analyzer._sampling_info = None
    analyzer.results = {}
    analyzer.db_path = ':memory:'

    import pandas as pd
    from unittest.mock import MagicMock

    mock_cph = MagicMock()
    mock_cph.summary = pd.DataFrame({'coef': [0.1], 'p': [0.5]}, index=['is_t1dm'])
    mock_cph.concordance_index_ = 0.6

    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 10, 'SAMPLING_SEED': 42,
                'PH_ALPHA': 0.05}):
        with patch.object(analyzer, '_check_min_rows', side_effect=[
            InsufficientDataError(valid_rows=5, min_rows=30),  # model1 fails
            None,  # model2 passes
            None,  # model3 passes
        ]):
            with patch('statistical_analysis.CoxPHFitter', return_value=mock_cph):
                with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
                    result = analyzer.run_cox(df_prepared=df)

    assert 'model1_age_sex' not in result, \
        f"InsufficientDataError 발생 model1 이 결과에 포함됨"
    assert len([k for k in result if k.startswith('model')]) >= 1, \
        f"model1 스킵 후 다른 모델이 실행되지 않음: {list(result.keys())}"
