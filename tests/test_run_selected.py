"""statistical_analysis.run_selected() — _safe_run 부분 실패 격리 테스트"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_analyzer(tmp_path):
    """최소한의 StatisticalAnalyzer 목업."""
    from statistical_analysis import StatisticalAnalyzer
    dm = MagicMock()
    # _load_data / _prepare 목업
    raw_df = pd.DataFrame({
        'exposure_group': ['NON_DM'] * 20,
        'follow_up_years': [1.0] * 20,
        'dementia_event': [0] * 20,
        'ad_event': [0] * 20,
        'vad_event': [0] * 20,
        'follow_up_days': [365] * 20,
        'INDI_DSCM_NO': [f'P{i:04d}' for i in range(20)],
    })
    dm.storage.get_work_dir.return_value = str(tmp_path)
    dm.storage.table_exists.return_value = True
    dm.query.return_value = raw_df
    return StatisticalAnalyzer(dm), raw_df


class TestSafeRunPartialFailure:
    """run_selected(): 한 단계 실패해도 나머지 결과 보존."""

    def test_psm_failure_does_not_block_subgroup(self, tmp_path):
        """PSM이 RuntimeError를 던져도 subgroup 결과가 results에 남는다."""
        analyzer, df = _make_analyzer(tmp_path)

        # 모든 분석을 noop으로 패치
        noop = MagicMock(return_value=None)

        sentinel = {'subgroup_called': False}

        def fake_subgroup(cb=None, df_prepared=None):
            sentinel['subgroup_called'] = True
            analyzer.results['subgroup'] = {'dummy': True}

        with patch.object(analyzer, '_load_data', return_value=(df, MagicMock(applied=False, label=''))), \
             patch.object(analyzer, '_prepare', return_value=df), \
             patch.object(analyzer, 'generate_table1', noop), \
             patch.object(analyzer, 'run_cox', noop), \
             patch.object(analyzer, 'run_psm', side_effect=RuntimeError("PSM 실패")), \
             patch.object(analyzer, 'run_interaction', noop), \
             patch.object(analyzer, 'run_subgroup', fake_subgroup), \
             patch.object(analyzer, 'run_competing_risks', noop), \
             patch.object(analyzer, 'run_sensitivity', noop), \
             patch('statistical_analysis.mem_manager'):
            results = analyzer.run_selected(
                run_cox=False, run_psm=True, run_interaction=False,
                run_subgroup=True, run_competing_risks=False, run_sensitivity=False,
            )

        assert 'step_errors' in results, "PSM 실패 시 step_errors 있어야 함"
        assert 'psm' in results['step_errors']
        assert sentinel['subgroup_called'], "PSM 실패 후에도 subgroup은 실행돼야 함"
        assert results.get('subgroup') == {'dummy': True}

    def test_step_errors_empty_when_all_succeed(self, tmp_path):
        """모든 단계 성공 시 step_errors 없음 (또는 빈 dict)."""
        analyzer, df = _make_analyzer(tmp_path)
        noop = MagicMock(return_value=None)

        with patch.object(analyzer, '_load_data', return_value=(df, MagicMock(applied=False, label=''))), \
             patch.object(analyzer, '_prepare', return_value=df), \
             patch.object(analyzer, 'generate_table1', noop), \
             patch.object(analyzer, 'run_cox', noop), \
             patch.object(analyzer, 'run_psm', noop), \
             patch.object(analyzer, 'run_interaction', noop), \
             patch.object(analyzer, 'run_subgroup', noop), \
             patch.object(analyzer, 'run_competing_risks', noop), \
             patch.object(analyzer, 'run_sensitivity', noop), \
             patch('statistical_analysis.mem_manager'):
            results = analyzer.run_selected()

        assert not results.get('step_errors'), \
            f"모든 단계 성공 시 step_errors 없어야 함: {results.get('step_errors')}"

    def test_multiple_steps_fail_all_recorded(self, tmp_path):
        """여러 단계 실패 시 step_errors에 모두 기록."""
        analyzer, df = _make_analyzer(tmp_path)
        noop = MagicMock(return_value=None)

        with patch.object(analyzer, '_load_data', return_value=(df, MagicMock(applied=False, label=''))), \
             patch.object(analyzer, '_prepare', return_value=df), \
             patch.object(analyzer, 'generate_table1', noop), \
             patch.object(analyzer, 'run_cox', side_effect=RuntimeError("Cox 실패")), \
             patch.object(analyzer, 'run_psm', side_effect=ValueError("PSM 실패")), \
             patch.object(analyzer, 'run_interaction', noop), \
             patch.object(analyzer, 'run_subgroup', noop), \
             patch.object(analyzer, 'run_competing_risks', noop), \
             patch.object(analyzer, 'run_sensitivity', noop), \
             patch('statistical_analysis.mem_manager'):
            results = analyzer.run_selected(run_cox=True, run_psm=True)

        errs = results.get('step_errors', {})
        # Cox는 outcome 3개 → 각각 기록
        assert any('cox_' in k for k in errs), f"Cox 오류 기록 기대: {errs}"
        assert 'psm' in errs, f"PSM 오류 기록 기대: {errs}"

    def test_step_error_details_keeps_legacy_step_errors(self, tmp_path):
        """R2-3a: step_errors(문자열 dict)는 유지하고 구조화 상세를 별도 저장한다."""
        analyzer, df = _make_analyzer(tmp_path)
        noop = MagicMock(return_value=None)

        with patch.object(analyzer, '_load_data', return_value=(df, MagicMock(applied=False, label=''))), \
             patch.object(analyzer, '_prepare', return_value=df), \
             patch.object(analyzer, 'generate_table1', noop), \
             patch.object(analyzer, 'run_cox', noop), \
             patch.object(analyzer, 'run_psm', side_effect=RuntimeError("PSM 실패")), \
             patch.object(analyzer, 'run_interaction', noop), \
             patch.object(analyzer, 'run_subgroup', noop), \
             patch.object(analyzer, 'run_competing_risks', noop), \
             patch.object(analyzer, 'run_sensitivity', noop), \
             patch('statistical_analysis.mem_manager'):
            results = analyzer.run_selected(run_cox=False, run_psm=True)

        assert results['step_errors']['psm'] == 'PSM 실패'
        detail = results['step_error_details']['psm']
        assert detail['reason_code'] == 'STEP_SKIPPED'
        assert detail['stage'] == 'psm'
        assert detail['exception_type'] == 'RuntimeError'
