"""variable_generator.py 동적 SQL 식별자 안전성 회귀 테스트."""

from pathlib import Path
from unittest.mock import MagicMock
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import variable_generator
from variable_generator import VariableGenerator


class FakeStorage:
    def __init__(self, row_count=0):
        self._row_count = row_count

    def get_row_count(self, table_name):
        return self._row_count


class FakeDataManager:
    def __init__(self):
        self.storage = FakeStorage()
        self.execute = MagicMock()


def test_generate_comorbidities_rejects_unsafe_config_key_before_sql_execute(monkeypatch):
    """config dict key가 unsafe alias가 되면 SQL 실행 전에 명시적으로 거부한다."""
    monkeypatch.setattr(
        variable_generator,
        "COMORBIDITY_CODES",
        {"hypertension; DROP TABLE final_analysis": ["I10"]},
    )
    dm = FakeDataManager()

    with pytest.raises(ValueError, match="유효하지 않은 SQL 식별자"):
        VariableGenerator(dm).generate_comorbidities()

    dm.execute.assert_not_called()


def test_generate_dm_complications_rejects_unsafe_config_key_before_sql_execute(monkeypatch):
    """당뇨 합병증 config key도 unsafe alias면 SQL 실행 전에 거부한다."""
    monkeypatch.setattr(
        variable_generator,
        "DM_COMPLICATION_CODES",
        {"foot; DROP TABLE final_analysis": ["E11"]},
    )
    dm = FakeDataManager()

    with pytest.raises(ValueError, match="유효하지 않은 SQL 식별자"):
        VariableGenerator(dm).generate_dm_complications()

    dm.execute.assert_not_called()


def test_generate_cci_rejects_unsafe_config_key_before_sql_execute(monkeypatch):
    """CCI config key도 unsafe alias면 detail/score SQL 실행 전에 거부한다."""
    monkeypatch.setattr(
        variable_generator,
        "CCI_CODES",
        {"mi; DROP TABLE final_analysis": (["I21"], 1)},
    )
    dm = FakeDataManager()

    with pytest.raises(ValueError, match="유효하지 않은 SQL 식별자"):
        VariableGenerator(dm).generate_cci()

    dm.execute.assert_not_called()


def test_complete_case_strategy_rejects_unsafe_critical_var_before_sql_execute():
    """complete-case 핵심 변수명이 unsafe하면 WHERE SQL 생성/실행 전에 거부한다."""
    dm = FakeDataManager()
    generator = VariableGenerator(dm)
    generator.complete_case_critical_vars = ["bmi", "income_quintile; DROP TABLE final_analysis"]

    with pytest.raises(ValueError, match="유효하지 않은 SQL 식별자"):
        generator._apply_complete_case_strategy()

    dm.execute.assert_not_called()
