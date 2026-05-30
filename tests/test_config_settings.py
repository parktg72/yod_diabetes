import json
from copy import deepcopy

import pytest

import config


def _snapshot_settings():
    return {name: deepcopy(settings) for name, settings in config._SAVEABLE_SETTINGS.items()}


def _restore_settings(snapshot):
    for name, values in snapshot.items():
        target = config._SAVEABLE_SETTINGS[name]
        target.clear()
        target.update(values)


def test_load_settings_rejects_invalid_study_settings_and_rolls_back(tmp_path):
    """JSON 설정 로드 후 전체 STUDY_SETTINGS 검증에 실패하면 기존 설정을 보존해야 한다."""
    snapshot = _snapshot_settings()
    original_min_age = config.STUDY_SETTINGS['MIN_AGE']
    original_max_age = config.STUDY_SETTINGS['MAX_AGE']
    settings_path = tmp_path / 'bad_settings.json'
    settings_path.write_text(json.dumps({
        'STUDY_SETTINGS': {
            'MIN_AGE': original_max_age,
            'MAX_AGE': original_min_age,
        }
    }), encoding='utf-8')

    try:
        with pytest.raises(ValueError, match='MIN_AGE'):
            config.load_settings(settings_path)

        assert config.STUDY_SETTINGS['MIN_AGE'] == original_min_age
        assert config.STUDY_SETTINGS['MAX_AGE'] == original_max_age
    finally:
        _restore_settings(snapshot)


def test_load_settings_rejects_invalid_missing_data_strategy_and_rolls_back(tmp_path):
    """MISSING_DATA_STRATEGY 같은 저장 가능 연구 설정도 로드 직후 검증해야 한다."""
    snapshot = _snapshot_settings()
    original = config.STUDY_SETTINGS['MISSING_DATA_STRATEGY']
    settings_path = tmp_path / 'bad_missing_strategy.json'
    settings_path.write_text(json.dumps({
        'STUDY_SETTINGS': {
            'MISSING_DATA_STRATEGY': 'drop_everything',
        }
    }), encoding='utf-8')

    try:
        with pytest.raises(ValueError, match='MISSING_DATA_STRATEGY'):
            config.load_settings(settings_path)

        assert config.STUDY_SETTINGS['MISSING_DATA_STRATEGY'] == original
    finally:
        _restore_settings(snapshot)
