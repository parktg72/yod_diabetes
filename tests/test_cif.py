"""statistical_analysis._compute_cif 단위 테스트"""

import pytest
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

lifelines = pytest.importorskip("lifelines", reason="lifelines 미설치")
from statistical_analysis import StatisticalAnalyzer


class TestComputeCIF:
    def test_no_events_all_censored(self):
        """모든 관측이 검열된 경우 CIF는 비어야 함"""
        times = np.array([1.0, 2.0, 3.0, 4.0])
        event_type = np.array([0, 0, 0, 0])
        ut, cif1, cif2 = StatisticalAnalyzer._compute_cif(times, event_type)
        assert len(ut) == 0
        assert len(cif1) == 0
        assert len(cif2) == 0

    def test_single_event_type1(self):
        """관심사건 1건만 발생"""
        times = np.array([1.0, 2.0, 3.0])
        event_type = np.array([0, 1, 0])  # t=2에서 이벤트
        ut, cif1, cif2 = StatisticalAnalyzer._compute_cif(times, event_type)
        assert len(ut) == 1
        assert ut[0] == 2.0
        # 3명 중 1명 이벤트 → CIF ≈ 1/3 (at-risk 시점에서)
        assert 0 < cif1[0] <= 1.0
        assert cif2[0] == 0.0

    def test_competing_event(self):
        """경쟁위험만 발생"""
        times = np.array([1.0, 2.0, 3.0])
        event_type = np.array([0, 2, 0])
        ut, cif1, cif2 = StatisticalAnalyzer._compute_cif(times, event_type)
        assert cif1[-1] == 0.0
        assert cif2[-1] > 0.0

    def test_both_event_types(self):
        """관심사건 + 경쟁위험 모두 발생"""
        times = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        event_type = np.array([0, 1, 2, 1, 0])
        ut, cif1, cif2 = StatisticalAnalyzer._compute_cif(times, event_type)
        # CIF는 단조증가
        assert all(cif1[i] <= cif1[i + 1] for i in range(len(cif1) - 1))
        assert all(cif2[i] <= cif2[i + 1] for i in range(len(cif2) - 1))
        # CIF1 + CIF2 <= 1
        assert all(c1 + c2 <= 1.0 + 1e-10 for c1, c2 in zip(cif1, cif2))

    def test_all_event_type1(self):
        """모든 관측이 관심사건"""
        times = np.array([1.0, 2.0, 3.0])
        event_type = np.array([1, 1, 1])
        ut, cif1, cif2 = StatisticalAnalyzer._compute_cif(times, event_type)
        assert cif1[-1] == pytest.approx(1.0)
        assert cif2[-1] == 0.0

    def test_monotonic_cif(self):
        """CIF는 항상 단조증가"""
        rng = np.random.RandomState(42)
        times = rng.exponential(2.0, size=100)
        event_type = rng.choice([0, 1, 2], size=100, p=[0.5, 0.3, 0.2])
        ut, cif1, cif2 = StatisticalAnalyzer._compute_cif(times, event_type)
        if len(cif1) > 1:
            assert all(cif1[i] <= cif1[i + 1] + 1e-10 for i in range(len(cif1) - 1))
        if len(cif2) > 1:
            assert all(cif2[i] <= cif2[i + 1] + 1e-10 for i in range(len(cif2) - 1))
