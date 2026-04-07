"""db_connector.py 검증 함수 단위 테스트"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_connector import (
    _validate_table_name,
    DuckDBStorage,
    HANAConnector,
    SASFileLoader,
)


class TestValidateTableName:
    def test_valid_simple(self):
        assert _validate_table_name('T40') == 'T40'

    def test_valid_underscore(self):
        assert _validate_table_name('GJ_RESULT_2018') == 'GJ_RESULT_2018'

    def test_valid_lowercase(self):
        assert _validate_table_name('analysis_data') == 'analysis_data'

    def test_invalid_sql_injection(self):
        with pytest.raises(ValueError, match="유효하지 않은 테이블명"):
            _validate_table_name("T40; DROP TABLE")

    def test_invalid_space(self):
        with pytest.raises(ValueError, match="유효하지 않은 테이블명"):
            _validate_table_name("T40 T20")

    def test_invalid_dash(self):
        with pytest.raises(ValueError, match="유효하지 않은 테이블명"):
            _validate_table_name("my-table")

    def test_invalid_starts_with_number(self):
        with pytest.raises(ValueError, match="유효하지 않은 테이블명"):
            _validate_table_name("123table")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="유효하지 않은 테이블명"):
            _validate_table_name("")

    def test_invalid_dot(self):
        with pytest.raises(ValueError, match="유효하지 않은 테이블명"):
            _validate_table_name("schema.table")


class TestValidateWhereClause:
    def test_valid_simple(self):
        result = HANAConnector._validate_where_clause("STD_YYYY = '2020'")
        assert result == "STD_YYYY = '2020'"

    def test_valid_none(self):
        assert HANAConnector._validate_where_clause(None) is None

    def test_valid_empty(self):
        assert HANAConnector._validate_where_clause('') == ''

    def test_forbidden_drop(self):
        with pytest.raises(ValueError, match="허용되지 않는 SQL 구문"):
            HANAConnector._validate_where_clause("1=1; DROP TABLE T40")

    def test_forbidden_delete(self):
        with pytest.raises(ValueError, match="허용되지 않는 SQL 구문"):
            HANAConnector._validate_where_clause("DELETE FROM T40")

    def test_forbidden_semicolon(self):
        with pytest.raises(ValueError, match="세미콜론"):
            HANAConnector._validate_where_clause("STD_YYYY = '2020'; SELECT 1")

    def test_forbidden_insert(self):
        with pytest.raises(ValueError, match="허용되지 않는 SQL 구문"):
            HANAConnector._validate_where_clause("INSERT INTO T40 VALUES (1)")

    def test_forbidden_update(self):
        with pytest.raises(ValueError, match="허용되지 않는 SQL 구문"):
            HANAConnector._validate_where_clause("UPDATE T40 SET x=1")


def test_csv_loader_emits_string_progress_and_index_messages(tmp_path):
    csv_path = tmp_path / "t20.csv"
    csv_path.write_text("INDI_DSCM_NO\n1\n2\n", encoding="utf-8")

    storage = DuckDBStorage(str(tmp_path / "test.duckdb"))
    storage.connect()

    messages = []
    loader = SASFileLoader()
    count = loader.load_csv_to_duckdb(
        csv_path, storage, "T20", progress_callback=messages.append
    )

    assert count == 2
    assert any(msg == "T20: 2건 적재 완료" for msg in messages)
    assert any("T20: 인덱스 생성 중..." in msg for msg in messages)
    assert messages[-1] == "T20: 인덱스 생성 완료"

    storage.close()


def test_hana_loader_progress_callback_accepts_workerthread_style_string(monkeypatch, tmp_path):
    storage = DuckDBStorage(str(tmp_path / "test_hana.duckdb"))
    storage.connect()

    fake_hana = HANAConnector("localhost", 30015, "user", "pw")
    fake_hana.fetch_table_chunked = MagicMock(return_value=[
        pd.DataFrame({"INDI_DSCM_NO": [1], "CMN_KEY": [10]})
    ])

    messages = []

    count = fake_hana.load_table_to_duckdb(
        "T20", "NHISBASE", storage, "T20", progress_callback=messages.append
    )

    assert count == 1
    assert messages[0] == "T20: 1건 적재 완료"
    assert any("T20: 인덱스 생성 중..." in msg for msg in messages)
    assert messages[-1] == "T20: 인덱스 생성 완료"

    storage.close()
