"""Stage N: run_interaction 가드 + Cox 침묵 실패 + 임계값 설정화 테스트"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import pytest
from unittest.mock import patch, MagicMock
from statistical_analysis import StatisticalAnalyzer, SamplingInfo


def _make_analyzer(df):
    """테스트용 StatisticalAnalyzer — _cached_df 직접 주입."""
    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer._cached_df = df
    analyzer._sampling_info = SamplingInfo(applied=False, total_rows=len(df), sampled_rows=len(df))
    analyzer.results = {}
    analyzer.db_path = ':memory:'
    return analyzer


def test_run_interaction_returns_none_when_too_few_rows():
    """run_interaction: MIN_VALID_ROWS 미만이면 None 반환 (Cox 시도 안 함)."""
    n = 5  # MIN_VALID_ROWS=30 보다 훨씬 적음
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * n,
        'is_t1dm': [1] * n,
        'dm_duration_cat': ['<5yr'] * n,
        'age_at_index': [50.0] * n,
        'male': [1] * n,
        'income_q': [5] * n,
        'cci_score': [0] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * n,
    })
    analyzer = _make_analyzer(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 30, 'MIN_EVENTS': 10, 'SAMPLING_SEED': 42}):
        result = analyzer.run_interaction(df_prepared=df)
    assert result is None, \
        f"MIN_VALID_ROWS 미만 데이터에서 run_interaction 이 None 을 반환해야 함: {result}"


def test_run_interaction_returns_none_when_too_few_events():
    """run_interaction: MIN_EVENTS 미만이면 None 반환."""
    n = 50
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * n,
        'is_t1dm': [1] * n,
        'dm_duration_cat': ['<5yr'] * n,
        'age_at_index': [50.0] * n,
        'male': [1] * n,
        'income_q': [5] * n,
        'cci_score': [0] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * (n - 2) + [1, 1],  # 이벤트 2건 (MIN_EVENTS=10 미만)
    })
    analyzer = _make_analyzer(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 30, 'MIN_EVENTS': 10, 'SAMPLING_SEED': 42}):
        result = analyzer.run_interaction(df_prepared=df)
    assert result is None, \
        f"MIN_EVENTS 미만 이벤트에서 run_interaction 이 None 을 반환해야 함: {result}"

def test_run_cox_raises_when_all_models_fail():
    """run_cox: 모든 모델 피팅 실패 시 RuntimeError 발생 (침묵 실패 방지)."""
    import pytest as _pytest
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
    analyzer = _make_analyzer(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 10, 'SAMPLING_SEED': 42,
                'PH_ALPHA': 0.05}):
        with patch('statistical_analysis.CoxPHFitter') as mock_cox_cls:
            mock_cox_cls.return_value.fit.side_effect = ValueError("강제 실패")
            with _pytest.raises(RuntimeError, match="ALL_COX_MODELS_FAILED") as exc_info:
                analyzer.run_cox(df_prepared=df)

    exc = exc_info.value
    assert getattr(exc, 'reason_code', None) == 'ALL_COX_MODELS_FAILED'
    assert set(getattr(exc, 'failed_models', {}).keys()) == {
        'model1_age_sex',
        'model2_socio',
        'model3_full',
    }
    assert {
        failure.get('reason_code')
        for failure in exc.failed_models.values()
    } == {'COX_MODEL_FAILED'}


def test_psm_caliper_respects_study_settings():
    """PSM caliper 는 STUDY_SETTINGS['PSM_CALIPER'] 를 사용해야 한다."""
    from config import STUDY_SETTINGS
    assert 'PSM_CALIPER' in STUDY_SETTINGS, \
        "STUDY_SETTINGS 에 PSM_CALIPER 키가 없음"
    assert 'PSM_SMD_THRESHOLD' in STUDY_SETTINGS, \
        "STUDY_SETTINGS 에 PSM_SMD_THRESHOLD 키가 없음"
    assert 'PH_ALPHA' in STUDY_SETTINGS, \
        "STUDY_SETTINGS 에 PH_ALPHA 키가 없음"
    assert STUDY_SETTINGS['PH_ALPHA'] == 0.05
    assert STUDY_SETTINGS['PSM_CALIPER'] == 0.2
    assert STUDY_SETTINGS['PSM_SMD_THRESHOLD'] == 0.1


def _make_cox_df(n=50):
    """Cox 테스트용 최소 DataFrame 생성."""
    return pd.DataFrame({
        'exposure_group':      ['T1DM'] * (n // 2) + ['T2DM_OHA'] * (n // 2),
        'is_t1dm':             [1] * (n // 2) + [0] * (n // 2),
        'is_t2dm_oha':         [0] * (n // 2) + [1] * (n // 2),
        'is_t2dm_insulin':     [0] * n,
        'is_t2dm_nomed':       [0] * n,
        'age_at_index':        [50.0] * n,
        'male':                [1] * n,
        'income_q':            [5] * n,
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
        'dementia_event':      [1] * 15 + [0] * (n - 15),
    })


def test_run_cox_skips_model_when_exposure_ph_violated(caplog):
    """H-2: 노출변수가 PH 가정 위반 시 해당 모델만 스킵하고 경고 로그를 남긴다."""
    import logging
    df = _make_cox_df()
    analyzer = _make_analyzer(df)

    # 3개 모델 중 model1만 is_t1dm PH 위반, model2/model3은 정상
    ph_summary_violated = pd.DataFrame({'p': [0.01, 0.5]}, index=['is_t1dm', 'age_at_index'])
    ph_summary_ok = pd.DataFrame({'p': [0.9, 0.8]}, index=['is_t1dm', 'age_at_index'])
    ph_mock_violated = MagicMock()
    ph_mock_violated.summary = ph_summary_violated
    ph_mock_ok = MagicMock()
    ph_mock_ok.summary = ph_summary_ok

    call_count = {'n': 0}

    def ph_side_effect(cph, df_model, time_transform):
        call_count['n'] += 1
        return ph_mock_violated if call_count['n'] == 1 else ph_mock_ok

    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 5, 'SAMPLING_SEED': 42,
                'PH_ALPHA': 0.05}):
        with patch('statistical_analysis.CoxPHFitter') as mock_cls, \
             patch('statistical_analysis.proportional_hazard_test',
                   side_effect=ph_side_effect):
            mock_cls.return_value.fit.return_value = None
            mock_cls.return_value.summary = pd.DataFrame()
            mock_cls.return_value.concordance_index_ = 0.6
            with caplog.at_level(logging.WARNING, logger='statistical_analysis'):
                result = analyzer.run_cox(df_prepared=df)

    # model1은 PH 위반으로 스킵, model2+model3은 결과에 포함
    assert 'model1_age_sex' not in result, "PH 위반 모델은 결과에서 제외"
    assert 'model2_socio' in result, "정상 모델은 결과에 포함"
    assert 'model3_full' in result, "정상 모델은 결과에 포함"
    assert result['failed_models']['model1_age_sex']['reason_code'] == 'PH_VIOLATION'
    assert result['failed_models']['model1_age_sex']['violated_variables'] == ['is_t1dm']
    assert any('PH 가정 위반' in r.message for r in caplog.records), \
        "PH 위반 경고 로그 필요"


def test_run_cox_all_models_succeed_has_no_failed_models_key():
    """R2-2 회귀: 모든 Cox 모델 성공 시 failed_models 키를 만들지 않는다."""
    df = _make_cox_df()
    analyzer = _make_analyzer(df)

    ph_mock = MagicMock()
    ph_mock.summary = pd.DataFrame({'p': [0.9]}, index=['is_t1dm'])

    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 5, 'SAMPLING_SEED': 42,
                'PH_ALPHA': 0.05}):
        with patch('statistical_analysis.CoxPHFitter') as mock_cls, \
             patch('statistical_analysis.proportional_hazard_test',
                   return_value=ph_mock):
            mock_cls.return_value.fit.return_value = None
            mock_cls.return_value.summary = pd.DataFrame()
            mock_cls.return_value.concordance_index_ = 0.6

            result = analyzer.run_cox(df_prepared=df)

    assert 'model1_age_sex' in result
    assert 'model2_socio' in result
    assert 'model3_full' in result
    assert 'failed_models' not in result


def test_sampling_seed_out_of_range_raises():
    """M3: SAMPLING_SEED 0-99 범위 초과 시 ValueError 발생."""
    import pytest as _pytest
    from statistical_analysis import StatisticalAnalyzer

    mock_dm = MagicMock()
    mock_dm.storage.get_row_count.return_value = 1000  # total > max_rows → 샘플링 분기
    group_df = pd.DataFrame({'exposure_group': ['T1DM', 'NON_DM'], 'cnt': [300, 700]})
    mock_dm.query.return_value = group_df

    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer.dm = mock_dm
    analyzer._cached_df = None
    analyzer._sampling_info = None
    analyzer.results = {}

    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 5, 'SAMPLING_SEED': 100,
                'PH_ALPHA': 0.05}):
        with patch('statistical_analysis.mem_manager') as mock_mem:
            mock_mem.get_safe_analysis_rows.return_value = 100  # 강제로 total > max_rows
            with _pytest.raises(ValueError, match="SAMPLING_SEED"):
                analyzer._load_data()


def test_run_psm_pooled_sd_zero_returns_skipped_dict():
    """M-1: pooled_sd = 0 이면 run_psm이 skipped dict를 반환하고 self.results에 저장한다."""
    import numpy as np
    from statistical_analysis import StatisticalAnalyzer, SamplingInfo

    n = 4
    df = pd.DataFrame({
        'exposure_group': ['T1DM', 'T1DM', 'T2DM_OHA', 'T2DM_OHA'],
        'is_t1dm':         [1, 1, 0, 0],
        'is_t2dm_oha':     [0, 0, 1, 1],
        'is_t2dm_insulin': [0, 0, 0, 0],
        'is_t2dm_nomed':   [0, 0, 0, 0],
        'age_at_index':    [50.0, 51.0, 55.0, 56.0],
        'male':            [1, 1, 1, 1],
        'income_q':        [5, 5, 5, 5],
        'comor_hypertension':   [0, 0, 0, 0],
        'comor_dyslipidemia':   [0, 0, 0, 0],
        'dm_duration_years':    [3.0, 3.0, 3.0, 3.0],
        'follow_up_years':      [1.0, 1.0, 1.0, 1.0],
        'dementia_event':       [1, 0, 0, 0],
        'ad_event':             [0, 0, 0, 0],
        'vad_event':            [0, 0, 0, 0],
    })
    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer._cached_df = df
    analyzer._sampling_info = SamplingInfo(applied=False, total_rows=n, sampled_rows=n)
    analyzer.results = {}
    analyzer.db_path = ':memory:'

    mock_lr = MagicMock()
    mock_lr.fit = MagicMock()
    # 같은 PS 값 → var()=0 → pooled_sd=0
    mock_lr.predict_proba = MagicMock(
        return_value=np.array([[0.1, 0.9], [0.1, 0.9], [0.9, 0.1], [0.9, 0.1]])
    )

    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 1, 'MIN_EVENTS': 1, 'SAMPLING_SEED': 42,
                'PSM_RATIO': 1, 'PSM_CALIPER': 0.2, 'PSM_SMD_THRESHOLD': 0.1,
                'PH_ALPHA': 0.05}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False), \
             patch('gpu_accelerator.get_logistic_regression', return_value=mock_lr):
            result = analyzer.run_psm(df_prepared=df)

    assert result is not None, "run_psm 이 None 대신 skipped dict를 반환해야 한다"
    assert result.get('skipped') is True, f"skipped=True 기대, 실제: {result}"
    assert 'reason' in result, "reason 키 필요"
    assert result.get('reason_code') == 'INVALID_PSM_CALIPER'
    assert result.get('stage') == 'psm'
    assert analyzer.results.get('psm') == result, "self.results['psm']에 저장되어야 한다"


def test_skip_result_helper_adds_reason_code_stage_and_extra():
    """R2-1: skip dict 공통 스키마는 skipped/reason_code/reason/stage/extra를 보존한다."""
    analyzer = StatisticalAnalyzer(data_manager=None)

    result = analyzer._skip_result(
        'INSUFFICIENT_DATA',
        '데이터 부족',
        stage='unit',
        valid_rows=3,
    )

    assert result == {
        'skipped': True,
        'reason_code': 'INSUFFICIENT_DATA',
        'reason': '데이터 부족',
        'stage': 'unit',
        'valid_rows': 3,
    }


def test_run_interaction_saves_skipped_dict_when_no_duration_col():
    """M-2: dm_duration_cat 컬럼 없으면 skipped dict가 self.results['interaction']에 저장된다."""
    n = 5
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * n,
        'is_t1dm': [1] * n,
        # dm_duration_cat 컬럼 없음
        'age_at_index': [50.0] * n,
        'male': [1] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * n,
    })
    analyzer = _make_analyzer(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 1, 'MIN_EVENTS': 1, 'SAMPLING_SEED': 42}):
        result = analyzer.run_interaction(df_prepared=df)

    assert result is None
    stored = analyzer.results.get('interaction')
    assert stored is not None, "self.results['interaction']에 skipped dict 저장 필요"
    assert stored.get('skipped') is True
    assert 'reason' in stored
    assert stored.get('reason_code') == 'MISSING_REQUIRED_COLUMN'
    assert stored.get('stage') == 'interaction'


def test_run_interaction_saves_skipped_dict_when_insufficient_data():
    """M-2: 데이터 부족 시 skipped dict가 self.results['interaction']에 저장된다."""
    n = 5
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * n,
        'is_t1dm': [1] * n,
        'dm_duration_cat': ['<5yr'] * n,
        'age_at_index': [50.0] * n,
        'male': [1] * n,
        'income_q': [5] * n,
        'cci_score': [0] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * n,
    })
    analyzer = _make_analyzer(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 30, 'MIN_EVENTS': 10, 'SAMPLING_SEED': 42}):
        result = analyzer.run_interaction(df_prepared=df)

    assert result is None
    stored = analyzer.results.get('interaction')
    assert stored is not None, "self.results['interaction']에 skipped dict 저장 필요"
    assert stored.get('skipped') is True
    assert 'reason' in stored
    assert stored.get('reason_code') == 'INSUFFICIENT_DATA'
    assert stored.get('stage') == 'interaction'


def test_run_interaction_fit_failure_saves_analysis_error_reason_code():
    """R2-3a: interaction Cox 실패는 reason_code/exception_type을 남긴다."""
    n = 40
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * 20 + ['T2DM_OHA'] * 20,
        'is_t1dm': [1] * 20 + [0] * 20,
        'dm_duration_cat': ['5-10yr'] * 20 + ['>=10yr'] * 20,
        'age_at_index': [50.0] * n,
        'male': [1] * n,
        'income_q': [5] * n,
        'cci_score': [0] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [1] * 10 + [0] * 30,
    })
    analyzer = _make_analyzer(df)

    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 5, 'SAMPLING_SEED': 42}):
        with patch('statistical_analysis.CoxPHFitter') as mock_cls:
            mock_cls.return_value.fit.side_effect = RuntimeError("interaction fit failed")
            result = analyzer.run_interaction(df_prepared=df)

    assert result.get('skipped') is True
    assert result.get('reason_code') == 'ANALYSIS_ERROR'
    assert result.get('stage') == 'interaction'
    assert result.get('exception_type') == 'RuntimeError'
    assert 'interaction fit failed' in result.get('reason', '')


def test_run_competing_risks_missing_column_has_reason_code():
    """R2-1: competing_death_event 누락 skip dict에 reason_code/stage가 포함된다."""
    df = _make_cox_df()
    analyzer = _make_analyzer(df)

    with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
        result = analyzer.run_competing_risks(df_prepared=df)

    assert result.get('implemented') is False
    assert 'reason' in result
    assert result.get('reason_code') == 'MISSING_REQUIRED_COLUMN'
    assert result.get('stage') == 'competing_risks'


def test_run_cross_validation_missing_upstream_has_reason_code():
    """R2-1: competing_risks 선행 결과가 없으면 reason_code/stage를 저장한다."""
    analyzer = _make_analyzer(pd.DataFrame())

    result = analyzer.run_cross_validation(df_prepared=pd.DataFrame())

    assert result.get('skipped') is True
    assert 'reason' in result
    assert result.get('reason_code') == 'MISSING_UPSTREAM_RESULT'
    assert result.get('stage') == 'cross_validation'


def test_run_cross_validation_exception_has_reason_code():
    """R2-3a: cross-validation 내부 예외는 outcome 결과에 reason_code를 남긴다."""
    n = 35
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'dementia_event': [1] * 6 + [0] * (n - 6),
        'competing_death_event': [0] * n,
        'is_t1dm': [1] * 18 + [0] * 17,
        'is_t2dm_oha': [0] * 18 + [1] * 17,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [55.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer(df)
    analyzer.results['competing_risks'] = {
        'dementia_event': {'fine_gray_summary': pd.DataFrame()}
    }

    with patch('cross_validator.CrossValidator.export_csv_for_r',
               side_effect=RuntimeError("csv export failed")):
        result = analyzer.run_cross_validation(df_prepared=df)

    entry = result['dementia_event']
    assert entry['validation_status'] == 'ERROR'
    assert entry['reason_code'] == 'CROSS_VALIDATION_ERROR'
    assert entry['stage'] == 'cross_validation'
    assert entry['exception_type'] == 'RuntimeError'
    assert 'csv export failed' in entry['reason']


def test_run_sensitivity_unexpected_drug_query_error_has_reason_code():
    """R2-3a: sensitivity broad exception 결과에 reason_code를 남긴다."""
    df = _make_cox_df()
    analyzer = _make_analyzer(df)
    analyzer.dm = MagicMock()
    analyzer.dm.query.side_effect = RuntimeError("drug query failed")

    with patch('statistical_analysis.CoxPHFitter') as mock_cls:
        mock_cls.return_value.fit.return_value = None
        mock_cls.return_value.summary = pd.DataFrame()
        result = analyzer.run_sensitivity(df_prepared=df)

    entry = result['dementia_with_drug']
    assert entry['reason_code'] == 'SENSITIVITY_ERROR'
    assert entry['stage'] == 'sensitivity'
    assert entry['exception_type'] == 'RuntimeError'
    assert 'drug query failed' in entry['reason']


def test_run_sensitivity_followup_cutoff_outer_exception_has_reason_code():
    """R2-3a: follow-up cutoff outer 예외 경로도 구조화된 실패 정보를 남긴다."""
    df = _make_cox_df()
    analyzer = _make_analyzer(df)
    analyzer.dm = MagicMock()
    analyzer.dm.query.return_value = pd.DataFrame({'n': [0]})

    with patch('pandas.DataFrame.copy', side_effect=RuntimeError("copy failed")):
        result = analyzer.run_sensitivity(df_prepared=df)

    entry = result['followup_cutoff_1y']
    assert entry['error'] == 'copy failed'
    assert entry['reason_code'] == 'SENSITIVITY_ERROR'
    assert entry['stage'] == 'sensitivity'
    assert entry['exception_type'] == 'RuntimeError'
    assert 'copy failed' in entry['reason']


def test_run_sensitivity_cutoff_cox_failure_is_recorded_in_failed_models():
    """R2-3a: cutoff Cox 개별 노출 실패가 failed_models에 남는다."""
    df = _make_cox_df()
    analyzer = _make_analyzer(df)
    analyzer.dm = MagicMock()
    analyzer.dm.query.return_value = pd.DataFrame({'n': [0]})

    with patch('statistical_analysis.CoxPHFitter') as mock_cls:
        mock_cls.return_value.fit.side_effect = RuntimeError('cox fit failed')
        result = analyzer.run_sensitivity(df_prepared=df)

    cutoff_entry = result['followup_cutoff_1y']
    assert 'is_t1dm' in cutoff_entry['failed_models']
    failure = cutoff_entry['failed_models']['is_t1dm']
    assert failure['reason_code'] == 'COX_MODEL_FAILED'
    assert failure['stage'] == 'sensitivity_cutoff_cox'
    assert failure['exception_type'] == 'RuntimeError'
    assert 'cox fit failed' in failure['reason']


def test_load_settings_raises_on_invalid_sampling_seed(tmp_path):
    """M-3: load_settings 호출 시 SAMPLING_SEED 범위 초과는 ValueError 발생."""
    import json
    import pytest as _pytest
    from config import load_settings, STUDY_SETTINGS

    settings_file = tmp_path / 'test_settings.json'
    data = {'STUDY_SETTINGS': {'SAMPLING_SEED': 150}}
    settings_file.write_text(json.dumps(data), encoding='utf-8')

    with _pytest.raises(ValueError, match="SAMPLING_SEED"):
        load_settings(path=str(settings_file))


def test_load_settings_accepts_valid_sampling_seed(tmp_path):
    """M-3: 유효한 SAMPLING_SEED(0-99)는 정상 로드된다."""
    import json
    import pytest as _pytest
    from config import load_settings, STUDY_SETTINGS

    settings_file = tmp_path / 'test_settings.json'
    data = {'STUDY_SETTINGS': {'SAMPLING_SEED': 50}}
    settings_file.write_text(json.dumps(data), encoding='utf-8')

    result = load_settings(path=str(settings_file))
    assert result is True
    assert STUDY_SETTINGS['SAMPLING_SEED'] == 50


def test_run_psm_raises_on_post_index_covariate_guard_via_monkeypatch():
    """A: run_psm은 post-index 공변량 guard 위반 시 reason_code 포함 ValueError를 발생해야 한다."""
    n = 20
    df = pd.DataFrame({
        'exposure_group': ['T1DM'] * 10 + ['T2DM_OHA'] * 10,
        'is_t1dm': [1] * 10 + [0] * 10,
        'is_t2dm_oha': [0] * 10 + [1] * 10,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
        'index_year': [2015] * n,
        'income_q': [3.0] * n,
        'comor_hypertension': [0] * n,
        'comor_dyslipidemia': [0] * n,
        'dm_duration_years': [5.0] * n,
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * n,
        'ad_event': [0] * n,
        'vad_event': [0] * n,
    })
    analyzer = _make_analyzer(df)

    with patch.object(StatisticalAnalyzer, '_POST_INDEX_COVARIATES', {'age_at_index'}):
        with pytest.raises(ValueError, match='ITB_POST_INDEX_COVARIATE'):
            analyzer.run_psm(df_prepared=df)


def test_post_index_covariate_guard_allows_baseline_insulin_flag():
    """A: baseline_has_insulin은 기저선 변수이므로 ITB guard 대상이 아니다."""
    analyzer = StatisticalAnalyzer(data_manager=None)

    analyzer._assert_no_post_index_covariates(
        ['baseline_has_insulin', 'age_at_index', 'male'],
        context='unit',
    )


def test_post_index_covariate_guard_reports_actual_phase2_variables():
    """A: 실제 Phase 2 post-index 변수는 reason_code와 함께 차단된다."""
    analyzer = StatisticalAnalyzer(data_manager=None)

    with pytest.raises(ValueError, match='ITB_POST_INDEX_COVARIATE') as exc_info:
        analyzer._assert_no_post_index_covariates(
            ['age_at_index', 'had_insulin_switch', 'days_to_switch'],
            context='unit',
        )

    msg = str(exc_info.value)
    assert 'context=unit' in msg
    assert 'forbidden_covariates=days_to_switch,had_insulin_switch' in msg


def test_run_cox_raises_on_post_index_covariate_guard_via_monkeypatch():
    """A: run_cox는 post-index 공변량 guard 위반 시 reason_code 포함 ValueError를 발생해야 한다."""
    df = _make_cox_df()
    analyzer = _make_analyzer(df)

    with patch.object(StatisticalAnalyzer, '_POST_INDEX_COVARIATES', {'age_at_index'}):
        with pytest.raises(ValueError, match='ITB_POST_INDEX_COVARIATE'):
            analyzer.run_cox(df_prepared=df)
