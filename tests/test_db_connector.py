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
        "T20", "NHISBASE", storage, "T20",
        where_clause="STD_YYYY = '2020'",
        progress_callback=messages.append,
    )

    assert count == 1
    assert messages[0] == "T20: 1건 적재 완료"
    assert any("T20: 인덱스 생성 중..." in msg for msg in messages)
    assert messages[-1] == "T20: 인덱스 생성 완료"

    storage.close()


class TestMonthlyHanaExtractor:
    def test_month_range_length(self):
        """STUDY_START_YEAR~STUDY_END_YEAR 범위의 월 수 = (years) * 12."""
        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(None, None, 'SCH', '/tmp')
        months = extractor._month_range()
        assert len(months) == 144  # (2024 - 2013 + 1) * 12

    def test_month_range_first_last(self):
        """첫 달 = STUDY_START_YEAR-01, 마지막 달 = STUDY_END_YEAR-12."""
        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(None, None, 'SCH', '/tmp')
        months = extractor._month_range()
        assert months[0] == '201301'
        assert months[-1] == '202412'

    def test_month_range_year_boundary(self):
        """연도 경계: 12월 다음이 이듬해 1월."""
        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(None, None, 'SCH', '/tmp')
        months = extractor._month_range()
        assert months[11] == '201312'
        assert months[12] == '201401'

    def test_extract_deletes_existing_cache(self, tmp_path):
        """시작 시 기존 Parquet 파일 삭제 확인."""
        import pandas as pd
        cache_dir = tmp_path / 'T20'
        cache_dir.mkdir()
        stale = cache_dir / 'T20_201212.parquet'
        # 0행 Parquet 생성
        pd.DataFrame().to_parquet(str(stale))

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.return_value = iter([])
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 0

        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20')

        assert not stale.exists(), "기존 stale Parquet 파일이 삭제되어야 함"

    def test_extract_calls_fetch_with_monthly_where(self, tmp_path):
        """각 월에 MDCARE_STRT_YYYYMM WHERE 절을 사용해 fetch 호출 확인."""
        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.return_value = iter([])
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 0

        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20')

        call_args_list = mock_hana.fetch_table_chunked.call_args_list
        assert len(call_args_list) == 144, f"144회 호출 기대, 실제: {len(call_args_list)}"
        # 첫 번째 호출: 2013년 1월
        first_kwargs = call_args_list[0].kwargs
        assert first_kwargs.get('where_clause') == "MDCARE_STRT_YYYYMM = '201301'", \
            f"첫 WHERE 절 오류: {first_kwargs}"
        # 마지막 호출: 2024년 12월
        last_kwargs = call_args_list[-1].kwargs
        assert last_kwargs.get('where_clause') == "MDCARE_STRT_YYYYMM = '202412'", \
            f"마지막 WHERE 절 오류: {last_kwargs}"

    def test_extract_creates_parquet_per_month(self, tmp_path):
        """144개 Parquet 파일 생성 확인 (행 있는 달 + 빈 달 모두)."""
        import pandas as pd

        df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

        def fake_fetch(table, schema, where_clause=None):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20')

        parquet_files = list((tmp_path / 'T20').glob('T20_*.parquet'))
        assert len(parquet_files) == 144, f"144개 Parquet 기대, 실제: {len(parquet_files)}"
        assert (tmp_path / 'T20' / 'T20_201301.parquet').exists()
        assert (tmp_path / 'T20' / 'T20_202412.parquet').exists()
        # .tmp 파일이 남아있으면 안됨 (원자적 rename 확인)
        tmp_files = list((tmp_path / 'T20').glob('*.tmp.parquet'))
        assert not tmp_files, f".tmp 잔류 파일: {tmp_files}"

    def test_extract_emits_progress_per_month(self, tmp_path):
        """각 월 및 DuckDB 병합 진행 메시지 emit 확인."""
        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.return_value = iter([])
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 0

        messages = []
        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20', progress_callback=messages.append)

        assert any('2013-01' in m for m in messages), f"2013-01 메시지 없음. 실제: {messages[:3]}"
        assert any('2024-12' in m for m in messages), f"2024-12 메시지 없음."
        assert any('DuckDB 병합' in m for m in messages), f"DuckDB 병합 메시지 없음."

    def test_extract_calls_duckdb_merge_once(self, tmp_path):
        """DuckDB merge는 execute로 CREATE TABLE 단일 호출 확인."""
        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.return_value = iter([])
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 0

        from db_connector import MonthlyHanaExtractor
        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20')

        # execute 호출 중 CREATE TABLE ... read_parquet 포함 확인
        execute_calls = [str(c) for c in mock_storage.execute.call_args_list]
        create_calls = [c for c in execute_calls if 'CREATE TABLE' in c and 'read_parquet' in c]
        assert len(create_calls) == 1, f"CREATE TABLE read_parquet 1회 기대. 실제: {create_calls}"

    def test_load_table_routes_t20_to_extractor(self, tmp_path, monkeypatch):
        """T20 where_clause=None 시 MonthlyHanaExtractor.extract_all_months 호출."""
        from db_connector import HANAConnector, MonthlyHanaExtractor

        mock_extractor = MagicMock()
        mock_extractor.extract_all_months.return_value = 5000

        def fake_init(hana_conn, storage, schema, cache_root):
            return mock_extractor

        monkeypatch.setattr('db_connector.MonthlyHanaExtractor', fake_init)
        monkeypatch.setattr('db_connector._get_hana_cache_dir', lambda: tmp_path)

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()
        mock_storage = MagicMock()

        result = hana.load_table_to_duckdb('T20', 'NHIS', mock_storage, 'T20')

        assert result == 5000
        mock_extractor.extract_all_months.assert_called_once_with('T20', 'T20', None)

    def test_load_table_skips_routing_when_where_clause(self, tmp_path, monkeypatch):
        """where_clause 있으면 MonthlyHanaExtractor 생성 안 함."""
        from db_connector import HANAConnector

        created = []

        def fake_init(*args, **kwargs):
            created.append(True)
            return MagicMock()

        monkeypatch.setattr('db_connector.MonthlyHanaExtractor', fake_init)
        monkeypatch.setattr('db_connector._get_hana_cache_dir', lambda: tmp_path)

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()
        mock_storage = MagicMock()
        mock_storage.drop_table.return_value = None
        mock_storage.conn = MagicMock()

        # fetch_table_chunked가 빈 이터레이터 반환 → 기존 경로 실행
        hana.fetch_table_chunked = MagicMock(return_value=iter([]))

        hana.load_table_to_duckdb(
            'T20', 'NHIS', mock_storage, 'T20',
            where_clause="INDI_DSCM_NO = 'A001'"
        )

        assert not created, "where_clause 있을 때 MonthlyHanaExtractor 생성 금지"

    def test_load_table_skips_routing_for_non_monthly_table(self, tmp_path, monkeypatch):
        """T20/T30/T40/T60 이외 테이블은 라우팅 안 함."""
        from db_connector import HANAConnector

        created = []

        def fake_init(*args, **kwargs):
            created.append(True)
            return MagicMock()

        monkeypatch.setattr('db_connector.MonthlyHanaExtractor', fake_init)
        monkeypatch.setattr('db_connector._get_hana_cache_dir', lambda: tmp_path)

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()
        mock_storage = MagicMock()
        mock_storage.drop_table.return_value = None
        mock_storage.conn = MagicMock()

        hana.fetch_table_chunked = MagicMock(return_value=iter([]))

        hana.load_table_to_duckdb('JK', 'NHIS', mock_storage, 'JK')

        assert not created, "JK 테이블은 MonthlyHanaExtractor 생성 금지"
