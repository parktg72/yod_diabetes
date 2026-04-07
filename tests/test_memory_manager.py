"""memory_manager.py 단위 테스트"""
from unittest.mock import patch
import pytest

from memory_manager import MemoryManager


class TestGetSafeAnalysisRows:
    """get_safe_analysis_rows() — 행당 4KB, 가용 메모리 20% 사용"""

    def _make_mgr(self):
        with patch("memory_manager.psutil"):
            return MemoryManager()

    def test_uses_4096_bytes_per_row(self):
        """행당 4096 바이트(4KB)로 계산해야 한다"""
        mgr = self._make_mgr()
        with patch.object(mgr, "get_memory_info", return_value={"available_gb": 1.0}):
            rows = mgr.get_safe_analysis_rows()
        # 1 GB * 0.2 / 4096 = 51200
        expected = int(1.0 * 0.2 * 1024 * 1024 * 1024 / 4096)
        assert rows == expected, f"expected {expected}, got {rows}"

    def test_uses_20_percent_of_available_memory(self):
        """가용 메모리의 20%를 사용해야 한다 (30%가 아님)"""
        mgr = self._make_mgr()
        with patch.object(mgr, "get_memory_info", return_value={"available_gb": 2.0}):
            rows = mgr.get_safe_analysis_rows()
        expected_20 = int(2.0 * 0.2 * 1024 * 1024 * 1024 / 4096)
        wrong_30 = int(2.0 * 0.3 * 1024 * 1024 * 1024 / 4096)
        assert rows == expected_20, f"expected 20% ({expected_20}), got {rows}"
        assert rows != wrong_30, "should not use 30% factor"

    def test_capped_by_max_df_rows(self):
        """MAX_DF_ROWS_IN_MEMORY 상한을 초과하지 않는다"""
        mgr = self._make_mgr()
        with patch.object(mgr, "get_memory_info", return_value={"available_gb": 1000.0}):
            rows = mgr.get_safe_analysis_rows()
        from config import MEMORY_SETTINGS
        max_rows = MEMORY_SETTINGS.get("MAX_DF_ROWS_IN_MEMORY", 500000)
        assert rows <= max_rows
