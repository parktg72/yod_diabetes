"""visualization.py 단위 테스트 — plot_km / plot_cif 빈 데이터 가드 + 컬럼/키 검증"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from visualization import Visualizer


class TestPlotKmEmptyGuard:
    """plot_km: 모든 그룹이 최소 크기 미달일 때 None 반환."""

    def test_returns_none_when_all_groups_too_small(self, tmp_path):
        """모든 exposure_group이 10건 미만이면 빈 파일 생성 없이 None 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        df = pd.DataFrame({
            'exposure_group': ['NON_DM'] * 5,   # < 10 → skip
            'follow_up_years': [1.0] * 5,
            'dementia_event': [0] * 5,
        })
        result = viz.plot_km(df)
        assert result is None, f"빈 데이터에서 None 반환 기대, 실제: {result}"
        # 빈 파일이 생성되지 않아야 한다
        assert not (tmp_path / 'km.png').exists(), "빈 그래프 파일 생성 금지"

    def test_returns_none_when_dataframe_empty(self, tmp_path):
        """빈 DataFrame → None 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        df = pd.DataFrame(columns=['exposure_group', 'follow_up_years', 'dementia_event'])
        result = viz.plot_km(df)
        assert result is None

    def test_returns_path_when_sufficient_data(self, tmp_path):
        """충분한 데이터(≥10건, 이벤트 있음)가 있으면 파일 경로 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        n = 30
        df = pd.DataFrame({
            'exposure_group': ['NON_DM'] * n,
            'follow_up_years': [float(i + 1) for i in range(n)],
            'dementia_event': [1 if i < 10 else 0 for i in range(n)],
        })
        result = viz.plot_km(df)
        assert result is not None, "충분한 데이터에서 경로 반환 기대"
        assert Path(result).exists(), f"파일이 실제로 생성돼야 함: {result}"


class TestPlotCifEmptyGuard:
    """plot_cif: 빈 cif_data → None 반환."""

    def test_returns_none_when_empty_dict(self, tmp_path):
        """cif_data={}이면 None 반환, 파일 미생성."""
        viz = Visualizer(output_dir=str(tmp_path))
        result = viz.plot_cif({})
        assert result is None, f"빈 dict에서 None 반환 기대, 실제: {result}"
        assert not (tmp_path / 'cif.png').exists(), "빈 그래프 파일 생성 금지"

    def test_returns_none_when_none(self, tmp_path):
        """cif_data=None이면 None 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        result = viz.plot_cif(None)
        assert result is None

    def test_returns_path_when_data_present(self, tmp_path):
        """유효한 cif_data가 있으면 파일 경로 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        times = [0.0, 1.0, 2.0, 3.0]
        cif_data = {
            'NON_DM': {
                'times': times,
                'cif_event': [0.0, 0.01, 0.02, 0.03],
                'cif_competing': [0.0, 0.05, 0.10, 0.15],
            }
        }
        result = viz.plot_cif(cif_data)
        assert result is not None, "유효한 cif_data에서 경로 반환 기대"
        assert Path(result).exists(), f"파일이 실제로 생성돼야 함: {result}"


class TestPlotKmColumnGuard:
    """plot_km: 필수 컬럼 없을 때 None 반환."""

    def test_missing_outcome_column_returns_none(self, tmp_path):
        """outcome 컬럼 없으면 None 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        df = pd.DataFrame({'exposure_group': ['NON_DM'] * 30,
                           'follow_up_years': [1.0] * 30})
        result = viz.plot_km(df, outcome='dementia_event')
        assert result is None

    def test_missing_follow_up_column_returns_none(self, tmp_path):
        """follow_up_years 컬럼 없으면 None 반환."""
        viz = Visualizer(output_dir=str(tmp_path))
        df = pd.DataFrame({'exposure_group': ['NON_DM'] * 30,
                           'dementia_event': [0] * 30})
        result = viz.plot_km(df, outcome='dementia_event')
        assert result is None


class TestPlotCifKeyGuard:
    """plot_cif: 그룹 데이터 키 불완전 시 해당 그룹 생략."""

    def test_incomplete_group_data_still_renders(self, tmp_path):
        """times/cif_event 없는 그룹은 생략하고 나머지 그룹으로 파일 생성."""
        viz = Visualizer(output_dir=str(tmp_path))
        cif_data = {
            'NON_DM': {
                'times': [0.0, 1.0],
                'cif_event': [0.0, 0.01],
                'cif_competing': [0.0, 0.05],
            },
            'T1DM': {'bad_key': 'missing times/cif_event'},  # 불완전 데이터
        }
        result = viz.plot_cif(cif_data)
        assert result is not None, "완전한 그룹이 있으면 파일 생성 기대"
        assert Path(result).exists()
