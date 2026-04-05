"""
tests/test_stage_j.py - Stage J: CIF per-group 이벤트 가드 + MIN_SUBGROUP_EVENTS 테스트
"""

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch
from statistical_analysis import StatisticalAnalyzer, SamplingInfo


def _make_analyzer_with_df(df):
    analyzer = StatisticalAnalyzer.__new__(StatisticalAnalyzer)
    analyzer.results = {}
    analyzer._cached_df = df
    analyzer._sampling_info = SamplingInfo(applied=False, total_rows=len(df), sampled_rows=len(df))
    return analyzer


def test_cif_skips_group_with_zero_events():
    """CIF per-group 루프가 이벤트 0건 그룹을 skip 해야 한다.

    T1DM: 15행, 0 이벤트 → MIN_SUBGROUP_EVENTS=3 → CIF skip 되어야 함
    T2DM_OHA: 25행, 5 이벤트 → CIF 포함되어야 함
    Stage J 이전에는 행 수만 확인해 T1DM 이 포함됐으나, 이벤트 수 가드 추가(Stage J)로 수정됨.
    """
    n = 40
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * 15 + [1] * 5 + [0] * 20,  # T1DM=0건, T2DM_OHA=5건
        'competing_death_event': [0] * n,
        'is_t1dm': [1] * 15 + [0] * 25,
        'is_t2dm_oha': [0] * 15 + [1] * 25,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer_with_df(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 3, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result = analyzer.run_competing_risks(df_prepared=df)
    cif = result.get('dementia_event', {}).get('cif_by_group', {})
    assert 'T1DM' not in cif, \
        f"이벤트 0건 T1DM 이 CIF 에 포함됨 — 이벤트 수 가드 미적용: {list(cif.keys())}"
    assert 'T2DM_OHA' in cif, \
        f"이벤트 5건 T2DM_OHA 가 CIF 에서 누락됨: {list(cif.keys())}"


def test_cif_respects_min_subgroup_events_threshold():
    """MIN_SUBGROUP_EVENTS 를 임계값 위아래로 패치해 CIF 포함/skip 전환을 검증한다.

    T2DM_OHA: 25행, 4 이벤트
    MIN_SUBGROUP_EVENTS=3 → 포함 (4 >= 3)
    MIN_SUBGROUP_EVENTS=5 → skip  (4 < 5)
    """
    n = 40
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * 15 + [1] * 4 + [0] * 21,  # T1DM=0건, T2DM_OHA=4건
        'competing_death_event': [0] * n,
        'is_t1dm': [1] * 15 + [0] * 25,
        'is_t2dm_oha': [0] * 15 + [1] * 25,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer_with_df(df)

    # MIN_SUBGROUP_EVENTS=3 → T2DM_OHA (4건) 포함
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 3, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result_runs = analyzer.run_competing_risks(df_prepared=df)

    # MIN_SUBGROUP_EVENTS=5 → T2DM_OHA (4건) skip
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 5, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result_skips = analyzer.run_competing_risks(df_prepared=df)

    cif_runs = result_runs.get('dementia_event', {}).get('cif_by_group', {})
    cif_skips = result_skips.get('dementia_event', {}).get('cif_by_group', {})
    assert 'T2DM_OHA' in cif_runs, \
        f"MIN_SUBGROUP_EVENTS=3 인데 이벤트 4건 T2DM_OHA 가 CIF 에서 누락됨: {list(cif_runs.keys())}"
    assert 'T2DM_OHA' not in cif_skips, \
        f"MIN_SUBGROUP_EVENTS=5 인데 이벤트 4건 T2DM_OHA 가 CIF 에 포함됨: {list(cif_skips.keys())}"


def test_cif_non_dm_skips_group_with_zero_events():
    """NON_DM CIF 블록이 이벤트 0건일 때 skip 해야 한다.

    NON_DM: 15행, 0 이벤트 → MIN_SUBGROUP_EVENTS=3 → CIF skip 되어야 함
    T2DM_OHA: 35행, 5 이벤트 → CIF 포함되어야 함
    Stage J 에서 추가된 NON_DM 이벤트 수 가드의 회귀 방지 테스트.
    """
    n = 50
    # 행 0-14: is_t1dm=0, is_t2dm_oha=0, ... → NON_DM (15행, 0 이벤트)
    # 행 15-49: is_t2dm_oha=1 → T2DM_OHA (35행, 5 이벤트)
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'dementia_event': [0] * 15 + [1] * 5 + [0] * 30,  # NON_DM=0건, T2DM_OHA=5건
        'competing_death_event': [0] * n,
        'is_t1dm': [0] * n,
        'is_t2dm_oha': [0] * 15 + [1] * 35,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer_with_df(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 3, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result = analyzer.run_competing_risks(df_prepared=df)
    cif = result.get('dementia_event', {}).get('cif_by_group', {})
    assert 'NON_DM' not in cif, \
        f"이벤트 0건 NON_DM 이 CIF 에 포함됨 — NON_DM 이벤트 수 가드 미적용: {list(cif.keys())}"
    assert 'T2DM_OHA' in cif, \
        f"이벤트 5건 T2DM_OHA 가 CIF 에서 누락됨: {list(cif.keys())}"


def test_cif_non_dm_respects_min_subgroup_events_threshold():
    """NON_DM CIF 블록이 MIN_SUBGROUP_EVENTS 임계값을 정확히 적용한다.

    NON_DM: 15행, 4 이벤트
    MIN_SUBGROUP_EVENTS=3 → 포함 (4 >= 3)
    MIN_SUBGROUP_EVENTS=5 → skip  (4 < 5)
    """
    n = 50
    # 행 0-14: NON_DM (15행) — 이 중 4건 이벤트
    # 행 15-49: T2DM_OHA (35행, 5 이벤트)
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'dementia_event': [1] * 4 + [0] * 11 + [1] * 5 + [0] * 30,  # NON_DM=4건, T2DM_OHA=5건
        'competing_death_event': [0] * n,
        'is_t1dm': [0] * n,
        'is_t2dm_oha': [0] * 15 + [1] * 35,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer_with_df(df)

    # MIN_SUBGROUP_EVENTS=3 → NON_DM (4건) 포함
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 3, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result_runs = analyzer.run_competing_risks(df_prepared=df)

    # MIN_SUBGROUP_EVENTS=5 → NON_DM (4건) skip
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 5, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result_skips = analyzer.run_competing_risks(df_prepared=df)

    cif_runs = result_runs.get('dementia_event', {}).get('cif_by_group', {})
    cif_skips = result_skips.get('dementia_event', {}).get('cif_by_group', {})
    assert 'NON_DM' in cif_runs, \
        f"MIN_SUBGROUP_EVENTS=3 인데 이벤트 4건 NON_DM 이 CIF 에서 누락됨: {list(cif_runs.keys())}"
    assert 'NON_DM' not in cif_skips, \
        f"MIN_SUBGROUP_EVENTS=5 인데 이벤트 4건 NON_DM 이 CIF 에 포함됨: {list(cif_skips.keys())}"


def test_cif_ad_event_skips_group_with_insufficient_events():
    """ad_event 경로에서도 CIF per-group 이벤트 가드가 동일하게 적용된다.

    T1DM: 15행, 0 AD 이벤트 → MIN_SUBGROUP_EVENTS=3 → CIF skip 되어야 함
    T2DM_OHA: 25행, 5 AD 이벤트 → CIF 포함되어야 함
    dementia_event 는 모두 0 → other_dementia 경쟁위험 없음 (순수 이벤트 가드 테스트).
    """
    n = 40
    # 행 0-14: T1DM (15행, AD 이벤트 0건)
    # 행 15-39: T2DM_OHA (25행, AD 이벤트 5건)
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'ad_event': [0] * 15 + [1] * 5 + [0] * 20,  # T1DM=0건, T2DM_OHA=5건
        'dementia_event': [0] * n,                   # non-AD 치매 없음 → other_dementia=0
        'competing_death_event': [0] * n,
        'is_t1dm': [1] * 15 + [0] * 25,
        'is_t2dm_oha': [0] * 15 + [1] * 25,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer_with_df(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 3, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result = analyzer.run_competing_risks(df_prepared=df)
    cif = result.get('ad_event', {}).get('cif_by_group', {})
    assert 'T1DM' not in cif, \
        f"AD 이벤트 0건 T1DM 이 ad_event CIF 에 포함됨 — 이벤트 가드 미적용: {list(cif.keys())}"
    assert 'T2DM_OHA' in cif, \
        f"AD 이벤트 5건 T2DM_OHA 가 ad_event CIF 에서 누락됨: {list(cif.keys())}"


def test_cif_ad_event_other_dementia_classified_as_competing_risk():
    """ad_event 경로에서 non-AD 치매(dementia=1, ad=0)가 경쟁위험(event_type=2)으로 분류된다.

    T2DM_OHA: 25행
      - 5건: ad_event=1 (관심사건, event_type=1)
      - 3건: dementia_event=1 AND ad_event=0 (other_dementia 경쟁위험, event_type=2)
      - 나머지: 검열 (event_type=0)
    경쟁위험 분류가 올바르면 CIF 결과에 'cif_competing' 값이 양수여야 한다.
    """
    n = 40
    # 행 0-14: T1DM (15행, 이벤트 없음 — MIN_SUBGROUP_EVENTS=3 이므로 skip)
    # 행 15-39: T2DM_OHA (25행)
    #   행 15-19: ad_event=1 (5건)
    #   행 20-22: dementia_event=1, ad_event=0 (other_dementia 3건)
    #   행 23-39: 이벤트 없음
    ad_events   = [0] * 15 + [1] * 5 + [0] * 20
    dem_events  = [0] * 15 + [0] * 5 + [1] * 3 + [0] * 17  # non-AD 치매 3건
    df = pd.DataFrame({
        'follow_up_years': [1.0] * n,
        'ad_event':         ad_events,
        'dementia_event':   dem_events,
        'competing_death_event': [0] * n,
        'is_t1dm': [1] * 15 + [0] * 25,
        'is_t2dm_oha': [0] * 15 + [1] * 25,
        'is_t2dm_insulin': [0] * n,
        'is_t2dm_nomed': [0] * n,
        'age_at_index': [60.0] * n,
        'male': [1] * n,
    })
    analyzer = _make_analyzer_with_df(df)
    with patch('statistical_analysis.STUDY_SETTINGS',
               {'MIN_VALID_ROWS': 10, 'MIN_EVENTS': 3, 'MIN_SUBGROUP_EVENTS': 3, 'SAMPLING_SEED': 42}):
        with patch('gpu_accelerator.is_gpu_enabled', return_value=False):
            result = analyzer.run_competing_risks(df_prepared=df)
    ad_result = result.get('ad_event', {})
    cif = ad_result.get('cif_by_group', {})
    assert 'T2DM_OHA' in cif, \
        f"T2DM_OHA 가 ad_event CIF 에서 누락됨: {list(cif.keys())}"
    # n_competing 는 전체 코호트의 경쟁위험 건수 (other_dementia 3건)
    n_competing = result.get('ad_event', {}).get('n_competing')
    assert n_competing == 3, \
        f"other_dementia 경쟁위험 분류 건수가 3 이어야 함: {n_competing}"
    # other_dementia 경쟁위험이 분류됐으면 cif_competing 에 양수 값이 있어야 함
    cif_competing = cif['T2DM_OHA'].get('cif_competing', [])
    assert any(v > 0 for v in cif_competing), \
        (f"ad_event CIF T2DM_OHA 의 cif_competing 이 모두 0 — "
         f"other_dementia 경쟁위험 미분류 의심: {cif_competing}")
