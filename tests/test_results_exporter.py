"""results_exporter.py 단위 테스트 — export_all 통합 + interaction 내보내기"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from results_exporter import ResultsExporter


def _make_summary_df():
    """Cox/interaction summary를 모사하는 최소 DataFrame."""
    return pd.DataFrame({
        'coef': [0.5, -0.3],
        'exp(coef)': [1.65, 0.74],
        'p': [0.03, 0.12],
    }, index=['var1', 'var2'])


class TestExportInteractionResults:
    """interaction 결과 내보내기 검증."""

    def test_export_interaction_creates_file(self, tmp_path):
        """정상 interaction 결과 → xlsx 파일 생성."""
        exp = ResultsExporter(str(tmp_path))
        interaction = {'summary': _make_summary_df()}
        path = exp.export_interaction_results(interaction)
        assert path is not None
        assert Path(path).exists(), f"interaction 파일 미생성: {path}"

    def test_export_interaction_skipped_returns_none(self, tmp_path):
        """skipped=True → None 반환, 파일 미생성."""
        exp = ResultsExporter(str(tmp_path))
        interaction = {'skipped': True, 'reason': '데이터 부족'}
        path = exp.export_interaction_results(interaction)
        assert path is None
        assert not (tmp_path / 'interaction.xlsx').exists()

    def test_export_interaction_no_summary_returns_none(self, tmp_path):
        """summary 없는 dict → None 반환."""
        exp = ResultsExporter(str(tmp_path))
        path = exp.export_interaction_results({})
        assert path is None

    def test_export_interaction_none_returns_none(self, tmp_path):
        """None 입력 → None 반환."""
        exp = ResultsExporter(str(tmp_path))
        path = exp.export_interaction_results(None)
        assert path is None


class TestExportAll:
    """export_all 통합 검증."""

    def test_export_all_includes_interaction(self, tmp_path):
        """results에 interaction 있으면 export_all이 내보내기 목록에 포함."""
        exp = ResultsExporter(str(tmp_path))
        results = {
            'interaction': {'summary': _make_summary_df()},
        }
        exported = exp.export_all(results)
        assert any('interaction' in str(p) for p in exported if p), \
            f"interaction 파일이 exported 목록에 없음: {exported}"

    def test_export_all_skipped_interaction_not_in_list(self, tmp_path):
        """skipped interaction → export_all 결과 목록에 미포함."""
        exp = ResultsExporter(str(tmp_path))
        results = {
            'interaction': {'skipped': True, 'reason': '컬럼 없음'},
        }
        exported = exp.export_all(results)
        assert not any('interaction' in str(p) for p in exported if p), \
            f"skipped interaction이 exported 목록에 포함됨: {exported}"

    def test_export_all_returns_only_nonnone_paths(self, tmp_path):
        """export_all 반환 목록에 None이 포함되지 않는다."""
        exp = ResultsExporter(str(tmp_path))
        results = {
            'psm': {'skipped': True, 'reason': 'pooled_sd=0'},
            'interaction': {'skipped': True, 'reason': '컬럼 없음'},
        }
        exported = exp.export_all(results)
        assert all(p is not None for p in exported), \
            f"export_all 결과에 None 포함: {exported}"

    def test_export_all_table1_and_interaction(self, tmp_path):
        """table1 + interaction 동시 처리 — 둘 다 파일 생성."""
        exp = ResultsExporter(str(tmp_path))
        table1_df = pd.DataFrame({'col': ['A', 'B'], 'val': [1, 2]})
        results = {
            'table1': table1_df,
            'interaction': {'summary': _make_summary_df()},
        }
        exported = exp.export_all(results)
        assert len(exported) == 2, f"table1 + interaction = 2개 파일 기대: {exported}"
        assert any('table1' in str(p) for p in exported)
        assert any('interaction' in str(p) for p in exported)


class TestExportSensitivityResults:
    """sensitivity 결과 내보내기 검증."""

    def _make_sensitivity(self):
        return {
            'dementia_with_drug': {'n': 1234, 'desc': '치매진단 + 항치매약'},
            'fine_gray': {'implemented': True, 'desc': 'Fine-Gray 설명'},
        }

    def test_export_sensitivity_creates_file(self, tmp_path):
        """정상 sensitivity 결과 → xlsx 파일 생성."""
        exp = ResultsExporter(str(tmp_path))
        path = exp.export_sensitivity_results(self._make_sensitivity())
        assert path is not None
        assert Path(path).exists(), f"sensitivity 파일 미생성: {path}"

    def test_export_sensitivity_none_returns_none(self, tmp_path):
        """None → None 반환, 파일 미생성."""
        exp = ResultsExporter(str(tmp_path))
        assert exp.export_sensitivity_results(None) is None
        assert not (tmp_path / 'sensitivity.xlsx').exists()

    def test_export_sensitivity_empty_dict_returns_none(self, tmp_path):
        """빈 dict → None 반환."""
        exp = ResultsExporter(str(tmp_path))
        assert exp.export_sensitivity_results({}) is None

    def test_export_all_includes_sensitivity(self, tmp_path):
        """export_all이 sensitivity 키를 처리하고 exported 목록에 포함."""
        exp = ResultsExporter(str(tmp_path))
        results = {'sensitivity': self._make_sensitivity()}
        exported = exp.export_all(results)
        assert any('sensitivity' in str(p) for p in exported if p), \
            f"sensitivity 파일이 exported 목록에 없음: {exported}"

    def test_export_all_missing_sensitivity_not_exported(self, tmp_path):
        """sensitivity 키 없으면 sensitivity 파일 미생성."""
        exp = ResultsExporter(str(tmp_path))
        exported = exp.export_all({'interaction': {'skipped': True, 'reason': 'x'}})
        assert not any('sensitivity' in str(p) for p in exported if p)


class TestExportAllTable1Guard:
    """export_all: table1 None 반환 시 exported 목록에 포함되지 않아야 한다."""

    def test_table1_none_not_in_exported(self, tmp_path):
        """export_table1이 None 반환(빈 df) 시 exported 목록에 추가되지 않는다."""
        exp = ResultsExporter(str(tmp_path))
        results = {'table1': None}
        exported = exp.export_all(results)
        assert all(p is not None for p in exported), \
            f"None이 exported에 포함됨: {exported}"
        assert len(exported) == 0, f"table1=None이면 exported 빈 목록이어야 함: {exported}"

    def test_table1_empty_df_not_in_exported(self, tmp_path):
        """export_table1이 빈 DataFrame 시 exported 목록에 추가되지 않는다."""
        exp = ResultsExporter(str(tmp_path))
        results = {'table1': pd.DataFrame()}
        exported = exp.export_all(results)
        assert len(exported) == 0, f"빈 df table1은 exported에 없어야 함: {exported}"

    def test_table1_valid_df_in_exported(self, tmp_path):
        """정상 DataFrame이면 exported에 포함된다."""
        exp = ResultsExporter(str(tmp_path))
        results = {'table1': pd.DataFrame({'col': ['A'], 'val': [1]})}
        exported = exp.export_all(results)
        assert len(exported) == 1
        assert 'table1' in exported[0]
