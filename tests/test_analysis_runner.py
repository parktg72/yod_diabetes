"""analysis_runner.py 단위 테스트 — run_post_analysis 오류 격리 및 반환값 검증"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis_runner import run_post_analysis


def _make_dm(rows=None):
    """KM 쿼리용 mock DataManager."""
    dm = MagicMock()
    if rows is None:
        rows = []
    dm.query.return_value = pd.DataFrame(
        rows,
        columns=['exposure_group', 'follow_up_years',
                 'dementia_event', 'ad_event', 'vad_event']
    ) if rows else pd.DataFrame(
        columns=['exposure_group', 'follow_up_years',
                 'dementia_event', 'ad_event', 'vad_event']
    )
    return dm


class TestRunPostAnalysisErrors:
    """오류 격리: 개별 시각화 실패가 전체 실행을 중단하지 않는다."""

    def test_km_error_is_collected_not_raised(self, tmp_path):
        """KM 쿼리 실패 시 errors 목록에 수집되고 나머지 단계 계속 진행."""
        dm = MagicMock()
        dm.query.side_effect = RuntimeError("DuckDB 쿼리 실패")

        result = run_post_analysis(dm, {}, tmp_path)

        assert len(result['errors']) >= 1
        assert any('KM' in e for e in result['errors']), \
            f"KM 오류가 errors에 포함돼야 함: {result['errors']}"

    def test_forest_error_is_collected_not_raised(self, tmp_path):
        """Forest plot 실패 시 errors 목록에 수집."""
        dm = _make_dm()
        analysis_results = {'subgroup': {'BAD': None}}  # None → plot_forest 예외

        with patch('analysis_runner.Visualizer') as MockViz:
            instance = MockViz.return_value
            instance.plot_km.return_value = None
            instance.plot_forest.side_effect = TypeError("subgroup 형식 오류")

            result = run_post_analysis(dm, analysis_results, tmp_path)

        assert any('Forest' in e for e in result['errors']), \
            f"Forest 오류가 errors에 포함돼야 함: {result['errors']}"

    def test_cif_error_is_collected_not_raised(self, tmp_path):
        """CIF plot 실패 시 errors 목록에 수집."""
        dm = _make_dm()
        analysis_results = {
            'competing_risks': {
                'dementia': {'cif_by_group': {'BROKEN': None}}
            }
        }

        with patch('analysis_runner.Visualizer') as MockViz:
            instance = MockViz.return_value
            instance.plot_km.return_value = None
            instance.plot_cif.side_effect = KeyError("times 키 없음")

            result = run_post_analysis(dm, analysis_results, tmp_path)

        assert any('CIF' in e for e in result['errors']), \
            f"CIF 오류가 errors에 포함돼야 함: {result['errors']}"

    def test_export_error_is_collected_not_raised(self, tmp_path):
        """결과 내보내기 실패 시 errors 목록에 수집."""
        dm = _make_dm()

        with patch('analysis_runner.Visualizer') as MockViz, \
             patch('analysis_runner.ResultsExporter') as MockExp:
            MockViz.return_value.plot_km.return_value = None
            MockExp.return_value.export_all.side_effect = OSError("디스크 쓰기 실패")

            result = run_post_analysis(dm, {}, tmp_path)

        assert any('내보내기' in e for e in result['errors']), \
            f"내보내기 오류가 errors에 포함돼야 함: {result['errors']}"


class TestRunPostAnalysisReturnShape:
    """반환값 구조 검증."""

    def test_returns_dict_with_errors_and_exported_files(self, tmp_path):
        """정상 실행 시 errors=[] + exported_files=list 반환."""
        dm = _make_dm()

        with patch('analysis_runner.Visualizer') as MockViz, \
             patch('analysis_runner.ResultsExporter') as MockExp:
            MockViz.return_value.plot_km.return_value = None
            MockExp.return_value.export_all.return_value = ['a.xlsx', 'b.xlsx']

            result = run_post_analysis(dm, {}, tmp_path)

        assert 'errors' in result
        assert 'exported_files' in result
        assert isinstance(result['errors'], list)
        assert isinstance(result['exported_files'], list)

    def test_exported_files_from_exporter(self, tmp_path):
        """exporter.export_all 반환값이 exported_files에 포함된다."""
        dm = _make_dm()
        expected = ['result1.xlsx', 'result2.xlsx']

        with patch('analysis_runner.Visualizer') as MockViz, \
             patch('analysis_runner.ResultsExporter') as MockExp:
            MockViz.return_value.plot_km.return_value = None
            MockExp.return_value.export_all.return_value = expected

            result = run_post_analysis(dm, {}, tmp_path)

        assert result['exported_files'] == expected

    def test_log_callback_called(self, tmp_path):
        """log 콜백이 주어지면 KM 오류 발생 시 호출된다."""
        dm = MagicMock()
        dm.query.side_effect = RuntimeError("쿼리 실패")
        log_calls = []

        run_post_analysis(dm, {}, tmp_path, log=log_calls.append)

        assert any('KM' in msg for msg in log_calls), \
            f"log 콜백에 KM 오류 메시지 기대: {log_calls}"
