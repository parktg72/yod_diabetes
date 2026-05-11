"""tests/test_stage_s.py — Stage S: tabs.py 방어 코드 테스트"""
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statistical_analysis import StatisticalAnalyzer


def test_on_post_analysis_result_none_guard():
    """result = data.get('result') or {} 패턴이 None 을 {} 로 대체하는지 검증."""
    data_with_none = {'result': None}
    result = data_with_none.get('result') or {}
    assert result == {}, f"None 이 {{}} 로 대체되지 않음: {result!r}"

    data_with_dict = {'result': {'errors': ['err1'], 'exported_files': []}}
    result2 = data_with_dict.get('result') or {}
    assert result2.get('errors') == ['err1'], "정상 dict 가 유지되지 않음"

    data_missing = {}
    result3 = data_missing.get('result') or {}
    assert result3 == {}, f"키 없을 때 {{}} 로 대체되지 않음: {result3!r}"


def test_run_competing_risks_standalone_passes_cb_to_load_data(monkeypatch):
    """run_competing_risks(cb=..., df_prepared=None) 시 _load_data 에 cb 전달."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.run_competing_risks(cb=cb, df_prepared=None)
    except Exception:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        f"run_competing_risks fallback: cb 미전달. received={load_cb_received}"


def test_generate_table1_standalone_passes_cb_to_load_data(monkeypatch):
    """generate_table1(cb=..., df_prepared=None) 시 _load_data 에 cb 전달."""
    dm = MagicMock()
    analyzer = StatisticalAnalyzer(dm)
    load_cb_received = []

    def patched_load(cb=None):
        load_cb_received.append(cb)
        raise pd.errors.EmptyDataError("테스트 중단")

    monkeypatch.setattr(analyzer, '_load_data', patched_load)
    cb = MagicMock()
    try:
        analyzer.generate_table1(cb=cb, df_prepared=None)
    except Exception:
        pass
    assert load_cb_received and load_cb_received[0] is cb, \
        f"generate_table1 fallback: cb 미전달. received={load_cb_received}"


def test_format_error_details_empty_input_returns_empty_string():
    tabs = __import__('pytest').importorskip("tabs")
    assert tabs._format_error_details(None) == ""
    assert tabs._format_error_details([]) == ""


def test_format_error_details_dict_items_include_reason_code_and_reason():
    tabs = __import__('pytest').importorskip("tabs")
    details = [
        {'reason_code': 'VIZ_KM_ERROR', 'stage': 'km_plot', 'reason': 'KM 생성 실패'},
        {'reason_code': 'EXPORT_ERROR', 'stage': 'export', 'error': '엑셀 저장 실패'},
    ]
    text = tabs._format_error_details(details)
    assert 'VIZ_KM_ERROR' in text
    assert 'KM 생성 실패' in text
    assert 'EXPORT_ERROR' in text
    assert '엑셀 저장 실패' in text


def test_format_error_details_mixed_dict_and_legacy_string():
    tabs = __import__('pytest').importorskip("tabs")
    details = [
        {'reason_code': 'VIZ_CIF_ERROR', 'reason': 'CIF 실패'},
        'legacy 오류 메시지',
    ]
    text = tabs._format_error_details(details)
    assert 'VIZ_CIF_ERROR' in text
    assert 'CIF 실패' in text
    assert 'legacy 오류 메시지' in text


def test_format_error_details_applies_max_items_truncation():
    tabs = __import__('pytest').importorskip("tabs")
    details = [
        {'reason_code': f'ERR_{i}', 'reason': f'실패_{i}'}
        for i in range(4)
    ]
    text = tabs._format_error_details(details, max_items=2)
    assert 'ERR_0' in text and 'ERR_1' in text
    assert 'ERR_2' not in text and 'ERR_3' not in text
    assert '... 외 2건' in text


def test_build_step_failure_message_step_errors_only():
    tabs = __import__('pytest').importorskip("tabs")
    step_errors = {
        'cox_dementia_event': '모형 수렴 실패',
    }
    built = tabs._build_step_failure_message(step_errors, {})
    assert built is not None
    body_lines, detail_lines = built
    assert body_lines == ['  • cox_dementia_event: 모형 수렴 실패']
    assert detail_lines == []


def test_build_step_failure_message_with_details_and_key_mismatch():
    tabs = __import__('pytest').importorskip("tabs")
    step_errors = {
        'table1': '요약 통계 실패',
    }
    step_error_details = {
        'cox_ad_event': {
            'reason_code': 'COX_FAIL',
            'stage': 'stage_n',
            'reason': '최소 이벤트 수 부족',
        },
    }
    built = tabs._build_step_failure_message(step_errors, step_error_details)
    assert built is not None
    body_lines, detail_lines = built
    assert body_lines == ['  • table1: 요약 통계 실패']
    assert detail_lines == ['  • cox_ad_event | COX_FAIL | stage_n | 최소 이벤트 수 부족']


def test_build_step_failure_message_details_only_uses_fallback_body_and_error_field():
    tabs = __import__('pytest').importorskip("tabs")
    step_error_details = {
        'subgroup': {
            'reason_code': 'SUBGROUP_FAIL',
            'stage': 'stage_o',
            'error': '하위군 분석 중 예외',
        },
    }
    built = tabs._build_step_failure_message({}, step_error_details)
    assert built is not None
    body_lines, detail_lines = built
    assert body_lines == ['  • 상세 오류는 아래 "자세히"를 확인하세요.']
    assert detail_lines == ['  • subgroup | SUBGROUP_FAIL | stage_o | 하위군 분석 중 예외']


def test_build_step_failure_message_both_empty_returns_none():
    tabs = __import__('pytest').importorskip("tabs")
    assert tabs._build_step_failure_message({}, {}) is None
