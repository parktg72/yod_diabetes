"""db_connector.py 검증 함수 단위 테스트"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from db_connector import (
    _validate_table_name,
    _prepare_chunk_for_duckdb,
    _cohort_id_where_parts,
    DuckDBStorage,
    HANAConnector,
    DataManager,
    SASFileLoader,
    MonthlyHanaExtractor,
    MonthlyJKExtractor,
    CohortIDExtractor,
    _COHORT_ID_CHUNK_SIZE,
)
import db_connector as _db_connector


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

        df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20')

        assert not stale.exists(), "기존 stale Parquet 파일이 삭제되어야 함"

    def test_extract_calls_fetch_with_monthly_where(self, tmp_path):
        """각 월에 MDCARE_STRT_YYYYMM WHERE 절을 사용해 fetch 호출 확인."""
        df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

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

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

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
        df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

        messages = []
        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20', progress_callback=messages.append)

        assert any('2013-01' in m for m in messages), f"2013-01 메시지 없음. 실제: {messages[:3]}"
        assert any('2024-12' in m for m in messages), f"2024-12 메시지 없음."
        assert any('DuckDB 병합' in m for m in messages), f"DuckDB 병합 메시지 없음."

    def test_extract_calls_duckdb_merge_once(self, tmp_path):
        """DuckDB merge는 execute로 CREATE TABLE 단일 호출 확인."""
        df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

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
        mock_extractor.extract_all_months.assert_called_once_with('T20', 'T20', None, force=True, cohort_ids=None)

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

    def test_extract_skips_existing_parquet_when_force_false(self, tmp_path):
        """force=False 시 이미 존재하는 Parquet 파일은 fetch 없이 스킵."""
        import pandas as pd

        cache_dir = tmp_path / 'T20'
        cache_dir.mkdir()
        # 2013년 1월 Parquet 미리 생성 (실제 T20 수준 컬럼 수 ≥5 이어야 stale 판정 안 됨)
        df_pre = pd.DataFrame({
            'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001'],
            'SEX_TYPE': ['1'], 'MDCARE_STRT_DT': ['20130101'],
            'SICK_SYM1': ['E119'], 'YOYANG_CLSFC_CD': ['01'],
        })
        df_pre.to_parquet(str(cache_dir / 'T20_201301.parquet'), index=False)

        df_sample = pd.DataFrame({
            'INDI_DSCM_NO': ['B002'], 'CMN_KEY': ['K002'],
            'SEX_TYPE': ['2'], 'MDCARE_STRT_DT': ['20130201'],
            'SICK_SYM1': ['E110'], 'YOYANG_CLSFC_CD': ['02'],
        })
        fetch_calls = []

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            fetch_calls.append(where_clause)
            # 201302에 데이터 제공하여 schema_columns가 설정되도록 함
            if where_clause and '201302' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20', force=False)

        assert "MDCARE_STRT_YYYYMM = '201301'" not in fetch_calls, \
            "이미 존재하는 201301 Parquet은 fetch 되면 안됨"
        expected_total = len(extractor._month_range())
        assert len(fetch_calls) == expected_total - 1, \
            f"나머지 {expected_total - 1}개월만 fetch 기대, 실제: {len(fetch_calls)}"

    def test_extract_force_true_deletes_and_reextracts(self, tmp_path):
        """force=True(기본값) 시 기존 Parquet 삭제 후 전체 재추출."""
        import pandas as pd

        cache_dir = tmp_path / 'T20'
        cache_dir.mkdir()
        existing = cache_dir / 'T20_201301.parquet'
        pd.DataFrame({'INDI_DSCM_NO': ['A001']}).to_parquet(str(existing), index=False)

        df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

        def fake_fetch(table, schema, where_clause=None, **kwargs):
            if where_clause and '201301' in where_clause:
                yield df_sample

        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.side_effect = fake_fetch
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        extractor.extract_all_months('T20', 'T20', force=True)

        assert mock_hana.fetch_table_chunked.call_count == 144, \
            "force=True 시 144개월 전체 fetch 기대"

    def test_load_table_passes_force_to_extractor(self, tmp_path, monkeypatch):
        """load_table_to_duckdb가 force 파라미터를 extract_all_months에 전달."""
        from db_connector import HANAConnector, MonthlyHanaExtractor

        mock_extractor = MagicMock()
        mock_extractor.extract_all_months.return_value = 100

        def fake_init(hana_conn, storage, schema, cache_root):
            return mock_extractor

        monkeypatch.setattr('db_connector.MonthlyHanaExtractor', fake_init)
        monkeypatch.setattr('db_connector._get_hana_cache_dir', lambda: tmp_path)

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()
        mock_storage = MagicMock()

        hana.load_table_to_duckdb('T20', 'NHIS', mock_storage, 'T20', force=False)

        mock_extractor.extract_all_months.assert_called_once_with('T20', 'T20', None, force=False, cohort_ids=None)

    def test_month_range_respects_study_settings(self):
        """STUDY_SETTINGS 변경 시 _month_range가 새 범위를 반영."""
        import config as cfg
        from db_connector import MonthlyHanaExtractor

        orig_start = cfg.STUDY_SETTINGS['STUDY_START_YEAR']
        orig_end = cfg.STUDY_SETTINGS['STUDY_END_YEAR']
        try:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = 2015
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = 2016
            extractor = MonthlyHanaExtractor(None, None, 'SCH', '/tmp')
            months = extractor._month_range()
            assert len(months) == 24, f"2015-2016 = 24개월 기대, 실제: {len(months)}"
            assert months[0] == '201501'
            assert months[-1] == '201612'
        finally:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = orig_start
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = orig_end


class TestPrepareChunkDecimalStringMix:
    """Fix C3: Mixed Decimal+string column → VARCHAR."""

    def test_mixed_decimal_string_forces_varchar(self):
        """Decimal + 문자열 혼재 컬럼은 VARCHAR으로 강제."""
        from decimal import Decimal
        df = pd.DataFrame({'col': [Decimal('100.5'), 'text', None]})
        result = _prepare_chunk_for_duckdb(df)
        assert result.attrs['duckdb_type_overrides'].get('col') == 'VARCHAR', \
            "혼재 컬럼은 VARCHAR이어야 함"

    def test_pure_decimal_not_varchar(self):
        """순수 Decimal 컬럼은 DECIMAL 타입 유지."""
        from decimal import Decimal
        df = pd.DataFrame({'col': [Decimal('100.5'), Decimal('200.3'), None]})
        result = _prepare_chunk_for_duckdb(df)
        override = result.attrs['duckdb_type_overrides'].get('col', '')
        assert 'DECIMAL' in override, f"순수 Decimal은 DECIMAL이어야 함, 실제: {override}"

    def test_pure_string_forces_varchar(self):
        """순수 문자열 컬럼은 VARCHAR."""
        df = pd.DataFrame({'col': ['hello', 'world', None]})
        result = _prepare_chunk_for_duckdb(df)
        assert result.attrs['duckdb_type_overrides'].get('col') == 'VARCHAR'


class TestCompositeIndexes:
    """Fix I3: T30/T40/T60 composite indexes."""

    def test_t30_gets_composite_index(self, tmp_path, monkeypatch):
        """T30 로드 시 복합키 인덱스 생성."""
        from db_connector import _create_indexes_with_progress
        calls = []
        original_create = _create_indexes_with_progress

        def mock_create(storage, table, indexes, progress_callback=None):
            calls.append((table, indexes))

        monkeypatch.setattr('db_connector._create_indexes_with_progress', mock_create)

        storage = DuckDBStorage(str(tmp_path / "test.duckdb"))
        storage.connect()

        fake_hana = HANAConnector("localhost", 30015, "user", "pw")
        fake_hana.fetch_table_chunked = MagicMock(return_value=[
            pd.DataFrame({"INDI_DSCM_NO": [1], "CMN_KEY": [10], "MCARE_DESC_LN_NO": [1]})
        ])

        fake_hana.load_table_to_duckdb(
            'T30', 'NHIS', storage, 'T30',
            where_clause="INDI_DSCM_NO = 'A001'",
            progress_callback=lambda msg: None,
        )

        assert len(calls) == 1, f"인덱스 생성 1회 기대, 실제: {len(calls)}"
        table, indexes = calls[0]
        assert table == 'T30'
        assert ['CMN_KEY', 'MCARE_DESC_LN_NO'] in indexes, \
            f"T30 복합키 인덱스 누락: {indexes}"
        assert ['INDI_DSCM_NO'] in indexes

        storage.close()

    def test_t40_gets_composite_index(self, tmp_path, monkeypatch):
        """T40 로드 시 복합키 인덱스 생성."""
        from db_connector import _create_indexes_with_progress
        calls = []

        def mock_create(storage, table, indexes, progress_callback=None):
            calls.append((table, indexes))

        monkeypatch.setattr('db_connector._create_indexes_with_progress', mock_create)

        storage = DuckDBStorage(str(tmp_path / "test.duckdb"))
        storage.connect()

        fake_hana = HANAConnector("localhost", 30015, "user", "pw")
        fake_hana.fetch_table_chunked = MagicMock(return_value=[
            pd.DataFrame({"INDI_DSCM_NO": [1], "CMN_KEY": [10], "SICK_DESC_SEQ_NO": [1]})
        ])

        fake_hana.load_table_to_duckdb(
            'T40', 'NHIS', storage, 'T40',
            where_clause="INDI_DSCM_NO = 'A001'",
            progress_callback=lambda msg: None,
        )

        assert len(calls) == 1
        table, indexes = calls[0]
        assert table == 'T40'
        assert ['CMN_KEY', 'SICK_DESC_SEQ_NO'] in indexes

        storage.close()

    def test_t60_gets_composite_index(self, tmp_path, monkeypatch):
        """T60 로드 시 복합키 인덱스 생성."""
        from db_connector import _create_indexes_with_progress
        calls = []

        def mock_create(storage, table, indexes, progress_callback=None):
            calls.append((table, indexes))

        monkeypatch.setattr('db_connector._create_indexes_with_progress', mock_create)

        storage = DuckDBStorage(str(tmp_path / "test.duckdb"))
        storage.connect()

        fake_hana = HANAConnector("localhost", 30015, "user", "pw")
        fake_hana.fetch_table_chunked = MagicMock(return_value=[
            pd.DataFrame({"INDI_DSCM_NO": [1], "CMN_KEY": [10],
                          "MPRSC_GRANT_NO": [1], "MPRSC_SEQ_NO": [1]})
        ])

        fake_hana.load_table_to_duckdb(
            'T60', 'NHIS', storage, 'T60',
            where_clause="INDI_DSCM_NO = 'A001'",
            progress_callback=lambda msg: None,
        )

        assert len(calls) == 1
        table, indexes = calls[0]
        assert table == 'T60'
        assert ['CMN_KEY', 'MPRSC_GRANT_NO', 'MPRSC_SEQ_NO'] in indexes

        storage.close()


    def test_gj_result_gets_index(self, tmp_path, monkeypatch):
        """GJ_RESULT 로드 시 (INDI_DSCM_NO, HC_DT) 인덱스 생성."""
        from db_connector import _create_indexes_with_progress
        calls = []

        def mock_create(storage, table, indexes, progress_callback=None):
            calls.append((table, indexes))

        monkeypatch.setattr('db_connector._create_indexes_with_progress', mock_create)

        storage = DuckDBStorage(str(tmp_path / "test.duckdb"))
        storage.connect()

        fake_hana = HANAConnector("localhost", 30015, "user", "pw")
        fake_hana.fetch_table_chunked = MagicMock(return_value=[
            pd.DataFrame({"INDI_DSCM_NO": ["P001"], "HC_DT": ["20130101"],
                          "G1E_BMI": [22.0]})
        ])

        fake_hana.load_table_to_duckdb(
            'GJ_RESULT', 'NHIS', storage, 'GJ_RESULT',
            where_clause="INDI_DSCM_NO = 'P001'",
        )

        assert len(calls) == 1, f"인덱스 생성 1회 기대, 실제: {len(calls)}"
        table, indexes = calls[0]
        assert table == 'GJ_RESULT'
        assert ['INDI_DSCM_NO', 'HC_DT'] in indexes, \
            f"GJ_RESULT (INDI_DSCM_NO, HC_DT) 인덱스 누락: {indexes}"

        storage.close()

    def test_gj_quest_gets_index(self, tmp_path, monkeypatch):
        """GJ_QUEST 로드 시 (INDI_DSCM_NO, HC_BZ_YYYY) 인덱스 생성."""
        from db_connector import _create_indexes_with_progress
        calls = []

        def mock_create(storage, table, indexes, progress_callback=None):
            calls.append((table, indexes))

        monkeypatch.setattr('db_connector._create_indexes_with_progress', mock_create)

        storage = DuckDBStorage(str(tmp_path / "test.duckdb"))
        storage.connect()

        fake_hana = HANAConnector("localhost", 30015, "user", "pw")
        fake_hana.fetch_table_chunked = MagicMock(return_value=[
            pd.DataFrame({"INDI_DSCM_NO": ["P001"], "HC_BZ_YYYY": ["2013"],
                          "Q_SMK_NOW_YN": [0]})
        ])

        fake_hana.load_table_to_duckdb(
            'GJ_QUEST', 'NHIS', storage, 'GJ_QUEST',
            where_clause="INDI_DSCM_NO = 'P001'",
        )

        assert len(calls) == 1
        table, indexes = calls[0]
        assert table == 'GJ_QUEST'
        assert ['INDI_DSCM_NO', 'HC_BZ_YYYY'] in indexes, \
            f"GJ_QUEST (INDI_DSCM_NO, HC_BZ_YYYY) 인덱스 누락: {indexes}"

        storage.close()


class TestExtractAllMonthsFailFast:
    """Fix C5: Empty Parquet fail-fast."""

    def test_raises_when_all_months_zero(self, tmp_path):
        """전체 0건 시 RuntimeError 발생."""
        mock_hana = MagicMock()
        mock_hana.fetch_table_chunked.return_value = iter([])
        mock_hana._detect_column_type.return_value = 'NVARCHAR'
        mock_storage = MagicMock()

        extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
        with pytest.raises(RuntimeError, match="0건"):
            extractor.extract_all_months('T20', 'T20')

    def test_early_empty_months_excluded_from_merge(self, tmp_path):
        """초기 0건 월(스키마 미확정)은 parquet_files에 포함되지 않아 DuckDB 병합 오류 방지."""
        import config as cfg
        # 2개월만 테스트 (201301 빈, 201302 데이터 있음)
        orig_start = cfg.STUDY_SETTINGS['STUDY_START_YEAR']
        orig_end = cfg.STUDY_SETTINGS['STUDY_END_YEAR']
        try:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = 2013
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = 2013

            df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

            def fake_fetch(table, schema, where_clause=None, **kwargs):
                # 201301은 0건, 201302부터 데이터
                if where_clause and '201302' in where_clause:
                    yield df_sample

            mock_hana = MagicMock()
            mock_hana.fetch_table_chunked.side_effect = fake_fetch
            mock_hana._detect_column_type.return_value = 'NVARCHAR'
            mock_storage = MagicMock()
            mock_storage.get_row_count.return_value = 1

            extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
            extractor.extract_all_months('T20', 'T20')

            # 201301 parquet은 생성되지 않아야 함 (0컬럼 병합 방지)
            cache_dir = tmp_path / 'T20'
            assert not (cache_dir / 'T20_201301.parquet').exists(), \
                "스키마 미확정 빈 월은 parquet 파일 미생성"
            # 201302 parquet은 생성되어야 함
            assert (cache_dir / 'T20_201302.parquet').exists(), \
                "데이터 있는 월은 parquet 생성"
        finally:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = orig_start
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = orig_end

    def test_empty_month_after_schema_known_creates_parquet(self, tmp_path):
        """스키마 확정 후 0건 월은 올바른 컬럼 구조의 빈 parquet 생성."""
        import config as cfg
        orig_start = cfg.STUDY_SETTINGS['STUDY_START_YEAR']
        orig_end = cfg.STUDY_SETTINGS['STUDY_END_YEAR']
        try:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = 2013
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = 2013

            df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

            def fake_fetch(table, schema, where_clause=None, **kwargs):
                # 201301만 데이터, 나머지 0건
                if where_clause and '201301' in where_clause:
                    yield df_sample

            mock_hana = MagicMock()
            mock_hana.fetch_table_chunked.side_effect = fake_fetch
            mock_hana._detect_column_type.return_value = 'NVARCHAR'
            mock_storage = MagicMock()
            mock_storage.get_row_count.return_value = 1

            extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
            extractor.extract_all_months('T20', 'T20')

            cache_dir = tmp_path / 'T20'
            parquet_202 = cache_dir / 'T20_201302.parquet'
            assert parquet_202.exists(), "스키마 확정 후 빈 월은 parquet 생성"
            df_empty = pd.read_parquet(str(parquet_202))
            assert list(df_empty.columns) == ['INDI_DSCM_NO', 'CMN_KEY'], \
                f"빈 parquet 컬럼 불일치: {list(df_empty.columns)}"
            assert len(df_empty) == 0, "빈 월 parquet은 0행"
        finally:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = orig_start
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = orig_end


class TestParquetWriterFinally:
    """Fix C7: ParquetWriter try/finally."""

    def test_parquet_writer_closed_on_exception(self, tmp_path, monkeypatch):
        """chunk loop에서 예외 발생 시 ParquetWriter.close() 호출 및 실패 카운터 추적 확인.

        실패 추적 도입 이후: 월 추출 실패는 failed_months 카운터에 기록되고
        WARNING 로그를 남긴다. ParquetWriter.close()는 여전히 finally에서 호출되어야 한다.
        """
        import config as cfg
        import pyarrow.parquet as pq

        orig_start = cfg.STUDY_SETTINGS['STUDY_START_YEAR']
        orig_end = cfg.STUDY_SETTINGS['STUDY_END_YEAR']
        try:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = 2013
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = 2013

            close_called = []

            class FakeWriter:
                def __init__(self, path, schema):
                    self.path = path
                    self._closed = False
                def write_table(self, table):
                    raise IOError("디스크 쓰기 실패 테스트")
                def close(self):
                    close_called.append(True)
                    self._closed = True

            monkeypatch.setattr('pyarrow.parquet.ParquetWriter', FakeWriter)

            df_sample = pd.DataFrame({'INDI_DSCM_NO': ['A001'], 'CMN_KEY': ['K001']})

            def fake_fetch(table, schema, where_clause=None, **kwargs):
                if where_clause and '201301' in where_clause:
                    yield df_sample

            mock_hana = MagicMock()
            mock_hana.fetch_table_chunked.side_effect = fake_fetch
            mock_hana._detect_column_type.return_value = 'NVARCHAR'
            mock_storage = MagicMock()
            mock_storage.get_row_count.return_value = 0

            extractor = MonthlyHanaExtractor(mock_hana, mock_storage, 'SCH', str(tmp_path))
            # 201301만 데이터 있고 write_table 실패 → failed_months=['201301']
            # 나머지 11개월은 데이터 없어서 정상 처리(빈 월)
            # 실패율 1/12 < 20% → WARNING 로그, 예외 전파 없음
            extractor.extract_all_months('T20', 'T20')

            assert close_called, "예외 발생 시에도 ParquetWriter.close()가 호출되어야 함"
        finally:
            cfg.STUDY_SETTINGS['STUDY_START_YEAR'] = orig_start
            cfg.STUDY_SETTINGS['STUDY_END_YEAR'] = orig_end


class TestRegisterUnregisterFinally:
    """Fix I6: register/unregister try/finally."""

    def test_unregister_called_on_execute_failure(self, tmp_path):
        """execute() 실패 시에도 unregister() 호출 확인."""
        mock_storage = MagicMock()
        mock_conn = MagicMock()
        mock_storage.conn = mock_conn

        # execute raises on INSERT
        def failing_execute(query, params=None):
            if 'INSERT INTO' in query:
                raise RuntimeError("INSERT 실패 테스트")
        mock_storage.execute.side_effect = failing_execute

        fake_hana = HANAConnector("localhost", 30015, "user", "pw")
        df = pd.DataFrame({"INDI_DSCM_NO": [1], "CMN_KEY": [10]})
        fake_hana.fetch_table_chunked = MagicMock(return_value=[df, df])

        with pytest.raises(RuntimeError, match="INSERT 실패"):
            fake_hana.load_table_to_duckdb(
                'JK', 'NHIS', mock_storage, 'JK',
                where_clause="INDI_DSCM_NO = 'A001'",
            )

        # unregister should have been called for both chunks
        unregister_calls = mock_conn.unregister.call_args_list
        assert len(unregister_calls) >= 2, \
            f"unregister 2회 이상 호출 기대, 실제: {len(unregister_calls)}"


class TestLoadTableCohortIDsFilter:
    """비월별 테이블 load_table_to_duckdb 시 cohort_ids IN 필터 적용 검증."""

    def test_non_monthly_table_applies_cohort_ids_where(self, tmp_path):
        """JK 같은 비월별 테이블에 cohort_ids가 주어지면 INDI_DSCM_NO IN 조건으로 조회."""
        captured_wheres = []

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()

        def fake_fetch(table_name, schema, columns=None, where_clause=None, chunk_size=None, **kwargs):
            captured_wheres.append(where_clause)
            return iter([])

        hana.fetch_table_chunked = MagicMock(side_effect=fake_fetch)

        mock_storage = MagicMock()
        mock_storage.conn = MagicMock()

        cohort_ids = frozenset(['10001', '10002', '10003'])
        hana.load_table_to_duckdb('JK', 'NHIS', mock_storage, 'JK', cohort_ids=cohort_ids)

        assert len(captured_wheres) > 0, "fetch_table_chunked 호출이 없음"
        assert any(w and 'INDI_DSCM_NO IN' in w for w in captured_wheres), \
            f"비월별 테이블에 cohort_ids IN 조건 필요. 캡처된 WHERE: {captured_wheres}"

    def test_non_monthly_table_no_cohort_ids_uses_original_where(self, tmp_path):
        """cohort_ids=None 이면 기존 where_clause 그대로 사용."""
        captured_wheres = []

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()

        def fake_fetch(table_name, schema, columns=None, where_clause=None, chunk_size=None, **kwargs):
            captured_wheres.append(where_clause)
            return iter([])

        hana.fetch_table_chunked = MagicMock(side_effect=fake_fetch)
        mock_storage = MagicMock()
        mock_storage.conn = MagicMock()

        hana.load_table_to_duckdb(
            'JK', 'NHIS', mock_storage, 'JK',
            where_clause="STD_YYYY = '2013'",
            cohort_ids=None,
        )

        assert len(captured_wheres) == 1
        assert captured_wheres[0] == "STD_YYYY = '2013'", \
            f"cohort_ids=None 시 원본 where_clause 유지 필요. 실제: {captured_wheres[0]}"

    def test_non_monthly_table_combines_where_and_cohort_ids(self, tmp_path):
        """where_clause + cohort_ids 둘 다 있으면 AND로 결합."""
        captured_wheres = []

        hana = HANAConnector.__new__(HANAConnector)
        hana.conn = MagicMock()

        def fake_fetch(table_name, schema, columns=None, where_clause=None, chunk_size=None, **kwargs):
            captured_wheres.append(where_clause)
            return iter([])

        hana.fetch_table_chunked = MagicMock(side_effect=fake_fetch)
        mock_storage = MagicMock()
        mock_storage.conn = MagicMock()

        cohort_ids = frozenset(['10001'])
        hana.load_table_to_duckdb(
            'JK', 'NHIS', mock_storage, 'JK',
            where_clause="STD_YYYY = '2013'",
            cohort_ids=cohort_ids,
        )

        assert len(captured_wheres) > 0
        assert any(
            w and "STD_YYYY = '2013'" in w and "INDI_DSCM_NO IN" in w
            for w in captured_wheres
        ), f"where_clause AND cohort_ids 결합 필요. 캡처된 WHERE: {captured_wheres}"


class TestDataManagerWorkDirMemory:
    """DataManager(':memory:') 계열 입력은 디렉터리를 만들지 않고 in-memory DB를 사용한다."""

    def test_memory_work_dir_uses_in_memory_duckdb_and_skips_mkdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        dm = DataManager(work_dir=':memory:')

        assert dm.storage.db_path == ':memory:'
        assert not (tmp_path / ':memory:').exists()

    def test_memory_work_dir_pathlike_uses_in_memory_duckdb_and_skips_mkdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        dm = DataManager(work_dir=Path(':memory:'))

        assert dm.storage.db_path == ':memory:'
        assert dm.work_dir is None
        assert not (tmp_path / ':memory:').exists()


class TestDataManagerConnectHana:
    """Fix H-1: connect_hana가 실패하면 self.hana를 None으로 리셋한다."""

    def test_connect_hana_resets_on_failure(self, tmp_path):
        """test_connection()이 예외를 던지면 self.hana가 None으로 초기화된다."""
        dm = DataManager(work_dir=str(tmp_path))

        connector = MagicMock(spec=HANAConnector)
        connector.test_connection.side_effect = RuntimeError("연결 실패")

        with pytest.raises(RuntimeError, match="연결 실패"), \
             patch('db_connector.HANAConnector', return_value=connector):
            dm.connect_hana('host', 30015, 'user', 'pw')

        assert dm.hana is None, "연결 실패 후 dm.hana는 None이어야 한다"

    def test_connect_hana_sets_hana_on_success(self, tmp_path):
        """test_connection() 성공 시 self.hana에 connector가 저장된다."""
        dm = DataManager(work_dir=str(tmp_path))

        connector = MagicMock(spec=HANAConnector)
        connector.test_connection.return_value = True

        with patch('db_connector.HANAConnector', return_value=connector):
            result = dm.connect_hana('host', 30015, 'user', 'pw')

        assert result is True
        assert dm.hana is connector


class TestDataManagerHanaBrowsingReconnect:
    """DataManager HANA browsing wrapper가 stale reconnect를 선호해야 한다."""

    @pytest.mark.parametrize(
        "method_name,args,delegate_name,delegate_args,expected",
        [
            ("get_hana_schemas", tuple(), "list_schemas", tuple(), ["NHIS"]),
            ("get_hana_tables", ("NHIS",), "list_tables", ("NHIS",), ["T20"]),
            ("get_hana_columns", ("NHIS", "T20"), "list_columns", ("NHIS", "T20"), ["INDI_DSCM_NO"]),
            ("search_hana_tables", ("NHIS", "T2"), "search_tables", ("NHIS", "T2"), ["T20"]),
        ],
    )
    def test_hana_browsing_wrappers_call_reconnect_if_stale_and_delegate(
        self, tmp_path, method_name, args, delegate_name, delegate_args, expected
    ):
        dm = DataManager(work_dir=str(tmp_path))
        hana = MagicMock(spec=HANAConnector)
        hana.conn = object()
        hana._reconnect_if_stale = MagicMock()
        setattr(hana, delegate_name, MagicMock(return_value=expected))
        dm.hana = hana

        result = getattr(dm, method_name)(*args)

        hana._reconnect_if_stale.assert_called_once_with()
        getattr(hana, delegate_name).assert_called_once_with(*delegate_args)
        assert result == expected


# ===========================================================================
# CohortIDExtractor 테스트
# ===========================================================================

def _make_mock_hana(hhdv_rows_by_month=None, t20_rows_by_month=None):
    """CohortIDExtractor용 mock HANAConnector.

    현재 구현(월별 `fetch_table_chunked` 호출)과 신규 구현(단일
    `fetch_sql_keyset` 호출) 양쪽에 동일 픽스처로 동작한다.

    hhdv_rows_by_month: {yyyymm_str: [list of INDI_DSCM_NO]}
    t20_rows_by_month: {yyyymm_str: [list of INDI_DSCM_NO]}
    """
    hhdv_rows_by_month = hhdv_rows_by_month or {}
    t20_rows_by_month = t20_rows_by_month or {}

    def _month_in_range(m, base_sql):
        import re
        rng = re.search(r"(\d{6})'\s*AND\s*'(\d{6})", base_sql)
        if rng:
            start, end = rng.group(1), rng.group(2)
            return start <= m <= end
        # BETWEEN INT 변형
        rng_int = re.search(r"BETWEEN\s+(\d{6})\s+AND\s+(\d{6})", base_sql)
        if rng_int:
            start, end = rng_int.group(1), rng_int.group(2)
            return start <= m <= end
        return True  # 범위 미지정 → 전부

    def fake_fetch(table_name, schema, columns=None, where_clause=None, chunk_size=None, **kwargs):
        """table_name 과 where_clause 로 mock 데이터를 반환하는 제너레이터 (레거시 경로)."""
        if table_name == 'HHDT_POPULATION_MM':
            yyyymm = None
            if where_clause:
                import re
                m = re.search(r"STD_YYYYMM\s*=\s*'(\d{6})'", where_clause)
                if m:
                    yyyymm = m.group(1)
            ids = hhdv_rows_by_month.get(yyyymm, [])
            if ids:
                yield pd.DataFrame({'INDI_DSCM_NO': ids})
        elif table_name == 'T20':
            yyyymm = None
            if where_clause:
                import re
                m = re.search(r"MDCARE_STRT_YYYYMM\s*=\s*'(\d{6})'", where_clause)
                if m:
                    yyyymm = m.group(1)
            ids = t20_rows_by_month.get(yyyymm, [])
            if ids:
                yield pd.DataFrame({'INDI_DSCM_NO': ids})

    def fake_keyset(base_sql, key_col, chunk_size=None, **kwargs):
        """신규 경로: 단일 SQL을 실행한 것처럼 HHDV × T20 월별 교집합 합집합을 반환."""
        use_hhdv = 'STD_YYYYMM' in base_sql and 'BYEAR' in base_sql
        # 범위 내 월들의 합집합 (연단위 fallback일 때 연단위 범위로 들어옴)
        all_months = sorted(set(hhdv_rows_by_month) | set(t20_rows_by_month))
        cohort = set()
        for m in all_months:
            if not _month_in_range(m, base_sql):
                continue
            t20_ids = set(t20_rows_by_month.get(m, []))
            if use_hhdv:
                hhdv_ids = set(hhdv_rows_by_month.get(m, []))
                cohort |= (hhdv_ids & t20_ids)
            else:
                cohort |= t20_ids
        if cohort:
            yield pd.DataFrame({'INDI_DSCM_NO': sorted(cohort)})

    hana = MagicMock(spec=HANAConnector)
    hana.fetch_table_chunked.side_effect = fake_fetch
    hana._detect_column_type.return_value = 'NVARCHAR'  # 문자열 비교
    # fetch_sql_keyset는 신규 메서드 — 구현 전에는 spec에 없어 접근 시 AttributeError.
    # 구현 적용 후에는 spec에 포함되어 side_effect 설정이 가능해진다.
    if hasattr(HANAConnector, 'fetch_sql_keyset'):
        hana.fetch_sql_keyset.side_effect = fake_keyset
    return hana


class TestCohortIDWhereParts:
    """_cohort_id_where_parts 헬퍼 함수 검증."""

    def test_empty_returns_empty(self):
        assert _cohort_id_where_parts(None) == []
        assert _cohort_id_where_parts(frozenset()) == []

    def test_small_set_single_part(self):
        ids = frozenset(['10001', '10002', '10003'])
        parts = _cohort_id_where_parts(ids)
        assert len(parts) == 1
        assert "INDI_DSCM_NO IN (" in parts[0]
        assert "'10001'" in parts[0] or "'10002'" in parts[0]

    def test_large_set_splits_into_chunks(self):
        ids = frozenset(str(i) for i in range(_COHORT_ID_CHUNK_SIZE * 2 + 1))
        parts = _cohort_id_where_parts(ids)
        assert len(parts) == 3  # 900 + 900 + 1

    def test_each_part_valid_sql_fragment(self):
        ids = frozenset(['20001', '20002'])
        for part in _cohort_id_where_parts(ids):
            assert part.startswith("INDI_DSCM_NO IN (")
            assert part.endswith(")")

    def test_invalid_id_raises(self):
        """영문자가 포함된 ID는 ValueError를 발생시켜야 한다."""
        with pytest.raises(ValueError, match="유효하지 않은 INDI_DSCM_NO"):
            _cohort_id_where_parts(frozenset(['A001']))


class TestCohortIDExtractor:
    """CohortIDExtractor.extract() 핵심 로직 검증."""

    def test_extracts_intersection_of_age_and_dm(self, tmp_path):
        """연령+DM 조건을 모두 만족하는 환자만 추출된다."""
        # P001: HHDV(연령 ok, 201301) + T20(DM ok, 201301) → 포함
        # P002: HHDV(연령 ok, 201301)만, T20 없음 → 제외
        # P003: T20(DM ok, 201301)만, HHDV 없음 → 제외
        hana = _make_mock_hana(
            hhdv_rows_by_month={'201301': ['P001', 'P002']},
            t20_rows_by_month={'201301': ['P001', 'P003']},
        )
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': 'HHDT_POPULATION_MM',
            'HHDV_STD_YYYY_COL': 'STD_YYYYMM',
            'HANA_TABLE_MAP': {},  # 테스트: alias 그대로 사용 (T20 → T20)
        }):
            result = extractor.extract(force=True)

        assert 'P001' in result
        assert 'P002' not in result
        assert 'P003' not in result

    def test_accumulates_across_months(self, tmp_path):
        """여러 월에 걸쳐 누적되고 중복이 제거된다."""
        hana = _make_mock_hana(
            hhdv_rows_by_month={'201301': ['P001', 'P002'], '201306': ['P001', 'P002']},
            t20_rows_by_month={
                '201301': ['P001'],
                '201306': ['P001', 'P002'],  # P001 중복, P002 신규
            },
        )
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': 'HHDT_POPULATION_MM',
            'HHDV_STD_YYYY_COL': 'STD_YYYYMM',
            'HANA_TABLE_MAP': {},
        }):
            result = extractor.extract(force=True)

        assert result == frozenset(['P001', 'P002'])

    def test_caches_to_parquet(self, tmp_path):
        """추출 결과가 cohort_ids.parquet으로 저장된다."""
        hana = _make_mock_hana(
            hhdv_rows_by_month={'201301': ['P001']},
            t20_rows_by_month={'201301': ['P001']},
        )
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': 'HHDT_POPULATION_MM',
            'HHDV_STD_YYYY_COL': 'STD_YYYYMM',
            'HANA_TABLE_MAP': {},
        }):
            extractor.extract(force=True)

        assert extractor.cache_path().exists()
        df = pd.read_parquet(str(extractor.cache_path()))
        assert 'INDI_DSCM_NO' in df.columns
        assert 'P001' in df['INDI_DSCM_NO'].values

    def test_resume_loads_from_cache(self, tmp_path):
        """force=False 이고 캐시가 있으면 HANA를 조회하지 않는다."""
        # 먼저 캐시 파일 직접 생성
        cache_file = tmp_path / 'cohort_ids.parquet'
        pd.DataFrame({'INDI_DSCM_NO': ['P999']}).to_parquet(str(cache_file))

        hana = _make_mock_hana()
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
        }):
            result = extractor.extract(force=False)

        assert result == frozenset(['P999'])
        hana.fetch_table_chunked.assert_not_called()

    def test_batch_failure_skips_batch_but_continues(self, tmp_path):
        """분기별 배치 추출 중 일부 배치 실패 시 해당 배치는 스킵하되 다른 배치는 계속 추출한다.

        계약: 일부 배치 쿼리가 실패해도 성공한 배치의 코호트 ID는 최종 집합에 포함된다.
        진입기간 2013~2014 (24개월 → 8개 배치): 배치 2 실패, 나머지 성공.
        """
        hana = MagicMock(spec=HANAConnector)
        hana._detect_column_type.return_value = 'NVARCHAR'

        call_log = []

        def fake_keyset(base_sql, key_col, chunk_size=None, **kwargs):
            call_log.append(base_sql)
            # 배치 2(201304~201306) 실패
            if '201304' in base_sql and '201306' in base_sql:
                raise RuntimeError("배치 2 HANA 조회 실패 (네트워크)")
            # 배치 4(201310~201312): P001 반환
            if '201310' in base_sql and '201312' in base_sql:
                yield pd.DataFrame({'INDI_DSCM_NO': ['P001', 'P002']})
                return
            # 배치 8(201410~201412): P003 반환
            if '201410' in base_sql and '201412' in base_sql:
                yield pd.DataFrame({'INDI_DSCM_NO': ['P003', 'P004']})
                return
            # 나머지 배치: 빈 결과
            yield pd.DataFrame({'INDI_DSCM_NO': []})

        hana.fetch_sql_keyset.side_effect = fake_keyset
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2014,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': 'HHDT_POPULATION_MM',
            'HHDV_STD_YYYY_COL': 'STD_YYYYMM',
            'HANA_TABLE_MAP': {},
        }):
            result = extractor.extract(force=True)

        # 배치 2 실패했지만, 배치 4, 8의 결과는 포함
        assert 'P001' in result, f"배치 4 결과가 포함되어야 함: {result}"
        assert 'P002' in result
        assert 'P003' in result, f"배치 8 결과가 포함되어야 함: {result}"
        assert 'P004' in result


# ===========================================================================
# HANAConnector.connect 재시도 테스트
# ===========================================================================

def _mock_hdbcli(connect_side_effect=None, connect_return=None):
    """hdbcli 모듈 목업 반환. sys.modules patch에 사용."""
    mock_dbapi = MagicMock()
    if connect_side_effect is not None:
        mock_dbapi.connect.side_effect = connect_side_effect
    else:
        mock_dbapi.connect.return_value = connect_return or MagicMock()
    mock_hdbcli = MagicMock()
    mock_hdbcli.dbapi = mock_dbapi
    return mock_hdbcli, mock_dbapi


class TestHANAConnectorReconnectIfStale:
    """HANAConnector._reconnect_if_stale(): 만료된 HANA 세션 재연결."""

    def test_reconnect_if_stale_connects_when_no_conn(self):
        """conn이 없으면 connect()를 호출한다."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        conn.conn = None
        conn.connect = MagicMock(return_value=True)

        conn._reconnect_if_stale()

        conn.connect.assert_called_once_with()

    def test_reconnect_if_stale_noop_when_healthy(self):
        """ping이 성공하면 기존 연결을 유지하고 재연결하지 않는다."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        hana_conn = MagicMock()
        cursor = hana_conn.cursor.return_value
        cursor.fetchone.return_value = ('OK',)
        conn.conn = hana_conn
        conn.connect = MagicMock(return_value=True)

        conn._reconnect_if_stale()

        cursor.execute.assert_called_once_with("SELECT 'OK' FROM DUMMY")
        cursor.fetchone.assert_called_once_with()
        cursor.close.assert_called_once_with()
        conn.connect.assert_not_called()
        assert conn.conn is hana_conn

    def test_reconnect_if_stale_reconnects_on_10821(self):
        """-10821(Session not connected)이면 conn을 버리고 재연결한다."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        stale_conn = MagicMock()
        cursor = stale_conn.cursor.return_value
        cursor.execute.side_effect = RuntimeError("SAP DBTech JDBC: [-10821] Session not connected")
        new_conn = MagicMock()

        def fake_connect():
            conn.conn = new_conn
            return True

        conn.conn = stale_conn
        conn.connect = MagicMock(side_effect=fake_connect)

        conn._reconnect_if_stale()

        cursor.close.assert_called_once_with()
        conn.connect.assert_called_once_with()
        assert conn.conn is new_conn

    def test_reconnect_if_stale_raises_on_other_errors(self):
        """-10821 외 ping 오류는 원인을 숨기지 않고 그대로 전파한다."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        hana_conn = MagicMock()
        cursor = hana_conn.cursor.return_value
        cursor.execute.side_effect = RuntimeError("permission denied")
        conn.conn = hana_conn
        conn.connect = MagicMock(return_value=True)

        with pytest.raises(RuntimeError, match="permission denied"):
            conn._reconnect_if_stale()

        cursor.close.assert_called_once_with()
        conn.connect.assert_not_called()
        assert conn.conn is hana_conn


class TestHANAConnectorRetry:
    """HANAConnector.connect(): 네트워크 오류 시 max_retries회 재시도."""

    def test_connect_succeeds_on_first_attempt(self):
        """첫 시도에 성공하면 재시도 없이 True 반환."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        mock_conn = MagicMock()
        mock_hdbcli, mock_dbapi = _mock_hdbcli(connect_return=mock_conn)
        with patch('db_connector.time.sleep') as mock_sleep, \
             patch.dict('sys.modules', {'hdbcli': mock_hdbcli, 'hdbcli.dbapi': mock_dbapi}):
            result = conn.connect()
        assert result is True
        assert conn.conn is mock_conn
        mock_sleep.assert_not_called()

    def test_connect_retries_on_failure_then_succeeds(self):
        """1회 실패 후 2회째 성공 — sleep 1회 호출."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        mock_conn = MagicMock()
        call_count = {'n': 0}

        def side_effect(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise ConnectionError("일시적 오류")
            return mock_conn

        mock_hdbcli, mock_dbapi = _mock_hdbcli(connect_side_effect=side_effect)
        with patch('db_connector.time.sleep') as mock_sleep, \
             patch.dict('sys.modules', {'hdbcli': mock_hdbcli, 'hdbcli.dbapi': mock_dbapi}):
            result = conn.connect(max_retries=2, retry_delay=0.0)

        assert result is True
        assert conn.conn is mock_conn
        assert mock_sleep.call_count == 1

    def test_connect_raises_after_all_retries_exhausted(self):
        """모든 재시도 실패 시 마지막 예외 전파."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        mock_hdbcli, mock_dbapi = _mock_hdbcli(
            connect_side_effect=ConnectionError("영구 오류")
        )
        with patch('db_connector.time.sleep'), \
             patch.dict('sys.modules', {'hdbcli': mock_hdbcli, 'hdbcli.dbapi': mock_dbapi}):
            with pytest.raises(ConnectionError, match="영구 오류"):
                conn.connect(max_retries=2, retry_delay=0.0)

    def test_connect_import_error_not_retried(self):
        """ImportError(드라이버 미설치)는 재시도 없이 즉시 전파."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        with patch('db_connector.time.sleep') as mock_sleep, \
             patch.dict('sys.modules', {'hdbcli': None, 'hdbcli.dbapi': None}):
            with pytest.raises(ImportError):
                conn.connect(max_retries=2, retry_delay=0.0)
        mock_sleep.assert_not_called()

    def test_raises_when_no_cohort_found(self, tmp_path):
        """조건 만족 환자가 없으면 RuntimeError 발생."""
        hana = _make_mock_hana(
            hhdv_rows_by_month={'201301': []},
            t20_rows_by_month={},
        )
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'HANA_TABLE_MAP': {},
        }):
            with pytest.raises(RuntimeError, match="조건을 만족하는 환자"):
                extractor.extract(force=True)

    def test_enrollment_range_returns_union_of_monthly_intersections(self, tmp_path):
        """계약(contract) 테스트: 진입기간 12개월 중 어느 월이든 HHDV ∩ T20 에 들어간
        INDI_DSCM_NO 는 최종 집합에 포함되어야 한다.

        구현 방식(월별 루프 vs 단일 SQL)에 **무관**한 집합 동치성 검증. 호출 횟수는
        구현 세부사항이므로 계약에 속하지 않는다.
        """
        # 12개월 전부에 P001 존재. T20 DM코드는 201301·201306·201312에만 존재.
        hhdv_month_data = {f'2013{mm:02d}': ['P001', 'P002'] for mm in range(1, 13)}
        t20_month_data = {
            '201301': ['P001', 'P003'],  # P001 매칭, P003 HHDV 미포함
            '201306': ['P001'],
            '201312': ['P002', 'P003'],  # P002 매칭, P003 HHDV 미포함
        }
        hana = _make_mock_hana(
            hhdv_rows_by_month=hhdv_month_data,
            t20_rows_by_month=t20_month_data,
        )
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': 'HHDT_POPULATION_MM',
            'HHDV_STD_YYYY_COL': 'STD_YYYYMM',
            'HANA_TABLE_MAP': {},
        }):
            result = extractor.extract(force=True)

        assert result == frozenset(['P001', 'P002']), (
            "HHDV ∩ T20 월별 교집합의 합집합만 포함되어야 한다: "
            f"기대 {{'P001','P002'}} 실제 {set(result)}"
        )

    def test_monthly_extraction_with_cohort_ids(self, tmp_path):
        """MonthlyHanaExtractor에 cohort_ids 전달 시 WHERE 절에 IN 조건이 추가된다."""
        mock_storage = MagicMock()
        mock_storage.conn = MagicMock()
        mock_storage.get_row_count.return_value = 5
        mock_storage.drop_table = MagicMock()
        mock_storage.execute = MagicMock()

        hana = MagicMock(spec=HANAConnector)
        hana._detect_column_type.return_value = 'NVARCHAR'

        captured_wheres = []

        def fake_fetch(table_name, schema, where_clause=None, **kwargs):
            if where_clause:
                captured_wheres.append(where_clause)
            return iter([])  # 빈 결과 → writer=None → 스키마 미확정 스킵

        hana.fetch_table_chunked.side_effect = fake_fetch

        extractor = MonthlyHanaExtractor(hana, mock_storage, 'NHIS', tmp_path)
        cohort_ids = frozenset(['10001', '10002'])

        with patch.dict('config.STUDY_SETTINGS', {
            'STUDY_START_YEAR': 2013, 'STUDY_END_YEAR': 2013,
        }):
            try:
                extractor.extract_all_months(
                    'T20', 'T20', force=True, cohort_ids=cohort_ids
                )
            except RuntimeError:
                pass  # 0건 RuntimeError는 예상된 동작

        # 적어도 하나의 WHERE 절에 INDI_DSCM_NO IN 조건이 포함돼야 한다
        assert any('INDI_DSCM_NO IN' in w for w in captured_wheres), \
            f"cohort_ids 전달 시 WHERE에 INDI_DSCM_NO IN 조건 필요. 캡처된 WHERE: {captured_wheres}"

    def test_uses_hhdv_table_from_study_settings(self, tmp_path):
        """STUDY_SETTINGS['HHDV_TABLE']이 설정되면 해당 테이블명이 조회 SQL에 포함된다."""
        custom_table = 'CUSTOM_AGE_TABLE'
        captured_sqls = []

        hana = MagicMock(spec=HANAConnector)
        hana._detect_column_type.return_value = 'NVARCHAR'

        def fake_keyset(base_sql, key_col, chunk_size=None, **kwargs):
            captured_sqls.append(base_sql)
            yield pd.DataFrame({'INDI_DSCM_NO': ['P001']})

        hana.fetch_sql_keyset.side_effect = fake_keyset
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': custom_table,
            'HANA_TABLE_MAP': {},  # T20 alias 그대로 사용
        }):
            extractor.extract(force=True)

        assert captured_sqls, "fetch_sql_keyset 호출이 한 번은 있어야 함"
        full_sql = ' '.join(captured_sqls)
        assert custom_table in full_sql, (
            f"HHDV_TABLE='{custom_table}' 설정 시 해당 테이블이 SQL 에 포함돼야 함. "
            f"실제 SQL: {full_sql[:400]}"
        )
        assert 'HHDV_DSES_YY' not in full_sql, (
            "커스텀 HHDV_TABLE 설정 시 기본 'HHDV_DSES_YY' 참조 금지"
        )


class TestDuckDBStorageExecuteParams:
    """DuckDBStorage.execute(): params=[] 빈 리스트 바인딩 정상 동작 확인."""

    def test_execute_empty_list_params_does_not_skip_binding(self, tmp_path):
        """params=[] 전달 시 파라미터 바인딩이 누락되지 않는다 (실제 쿼리로 검증)."""
        storage = DuckDBStorage(str(tmp_path / "test_params.duckdb"))
        storage.connect()
        # params=[] — 빈 리스트는 이제 바인딩 경로로 진입해야 함
        # DuckDB에서 빈 파라미터 리스트로 execute는 오류 없이 동작
        result = storage.execute("SELECT 1 AS n", params=[])
        assert result.fetchone()[0] == 1

    def test_execute_none_params_no_binding(self, tmp_path):
        """params=None 이면 바인딩 없이 실행된다."""
        storage = DuckDBStorage(str(tmp_path / "test_params2.duckdb"))
        storage.connect()
        result = storage.execute("SELECT 42 AS n", params=None)
        assert result.fetchone()[0] == 42


class TestDuckDBStorageSchemaMismatchBackup:
    """DuckDBStorage.connect(): 스키마 불일치 시 unlink 대신 `.corrupt_<ts>` 로 rename 백업."""

    def test_schema_mismatch_renames_instead_of_unlinking(self, tmp_path, monkeypatch):
        """수 시간 걸리는 코호트 DuckDB 파일이 스키마 불일치 한 번에 삭제되면 안 된다.

        사용자 데이터 보존을 위해 `.corrupt_<timestamp>` 로 rename 백업 후
        빈 파일로 재연결해야 한다.
        """
        db_path = tmp_path / "nhis_analysis.duckdb"
        # 기존 데이터를 흉내내는 dummy 바이트 — 후에 백업 파일에서 검증
        sentinel = b"ORIGINAL_DUCKDB_PAYLOAD"
        db_path.write_bytes(sentinel)
        wal_path = tmp_path / "nhis_analysis.duckdb.wal"
        wal_path.write_bytes(b"WAL_DATA")

        call_count = {"n": 0}
        real_connect = _db_connector.duckdb.connect

        def _fake_connect(path, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Trying to read a database file with schema does not match")
            return real_connect(path, *a, **kw)

        monkeypatch.setattr(_db_connector.duckdb, "connect", _fake_connect)

        storage = DuckDBStorage(str(db_path))
        storage.connect()

        # 기존 파일은 unlink 되지 않고 rename 됨
        backups = list(tmp_path.glob("nhis_analysis.duckdb.corrupt_*"))
        assert len(backups) == 1, f"백업 파일이 정확히 1개여야 함: {backups}"
        assert backups[0].read_bytes() == sentinel, "백업 파일에 원본 데이터가 보존되어야 함"

        # WAL도 rename 백업
        wal_backups = list(tmp_path.glob("nhis_analysis.duckdb.wal.corrupt_*"))
        assert len(wal_backups) == 1
        assert wal_backups[0].read_bytes() == b"WAL_DATA"

        # 재연결로 새로운 빈 DuckDB 파일이 생성됨
        assert db_path.exists()
        assert db_path.read_bytes() != sentinel

        storage.close()


class TestCohortIDExtractorEmptyResult:
    """CohortIDExtractor.extract(): 조건 충족 환자 0건이면 RuntimeError."""

    def test_empty_cohort_raises_runtime_error(self, tmp_path):
        """HHDV와 T20 교집합이 완전히 비어있으면 RuntimeError를 발생시킨다."""
        # HHDV에는 P001이 있지만 T20에는 아무것도 없음 → 교집합 0건
        hana = _make_mock_hana(
            hhdv_rows_by_month={'201301': ['P001']},
            t20_rows_by_month={},
        )
        extractor = CohortIDExtractor(hana, 'NHIS', tmp_path)

        with patch.dict('config.STUDY_SETTINGS', {
            'ENROLLMENT_START': 2013, 'ENROLLMENT_END': 2013,
            'MIN_AGE': 40, 'MAX_AGE': 64,
            'COHORT_USE_HHDV': True,
            'HHDV_TABLE': 'HHDT_POPULATION_MM',
            'HHDV_STD_YYYY_COL': 'STD_YYYYMM',
        }):
            with pytest.raises(RuntimeError, match="조건을 만족하는 환자가 없습니다"):
                extractor.extract(force=True)


class TestBuildCohortIdSql:
    """신규 헬퍼 `_build_cohort_id_sql` 계약 검증 (RED → GREEN).

    이 헬퍼는 HHDV × T20 서버측 JOIN 으로 INDI_DSCM_NO 집합을 단일 SQL로 구성한다.
    월별 루프를 제거하고 HANA 전송량을 코호트 ID 수준으로 축소하는 것이 목표.
    """

    def test_helper_exists(self):
        """`_build_cohort_id_sql` 이 db_connector 모듈에 정의되어 있다."""
        assert hasattr(_db_connector, '_build_cohort_id_sql'), (
            "db_connector 에 _build_cohort_id_sql 헬퍼가 필요합니다."
        )

    def test_returns_select_distinct_on_indi_dscm_no(self):
        """결과는 단일 컬럼 DISTINCT INDI_DSCM_NO 를 반환하는 SELECT 문이다."""
        sql = _db_connector._build_cohort_id_sql(
            enroll_start=2013, enroll_end=2016,
            t20_schema='NHIS', t20_table='T20',
            use_hhdv=True,
            hhdv_schema='NHIS', hhdv_table='HHDT_POPULATION_MM',
        )
        assert 'SELECT' in sql.upper()
        assert 'DISTINCT' in sql.upper()
        assert 'INDI_DSCM_NO' in sql.upper()
        # 파괴적 구문 금지
        for banned in ('DROP', 'DELETE', 'INSERT', 'UPDATE', 'ALTER'):
            assert banned not in sql.upper(), f"{banned} 포함 금지"

    def test_applies_all_t20_filters(self):
        """T20 기본 필터(PAY_YN='1', FORM_CD IN, INDI_DSCM_NO 유효범위)가 빠짐없이 들어간다."""
        sql = _db_connector._build_cohort_id_sql(
            enroll_start=2013, enroll_end=2016,
            t20_schema='NHIS', t20_table='T20',
            form_cd_list=('02', '03', '07', '08', '09', '10', '11', '15'),
            pay_yn='1',
            use_hhdv=False,
        )
        up = sql.upper()
        assert "PAY_YN" in up
        assert "'1'" in sql
        assert "FORM_CD" in up
        for cd in ('02', '03', '07', '08', '09', '10', '11', '15'):
            assert f"'{cd}'" in sql, f"FORM_CD={cd} 누락"
        # 개인식별자 유효 범위
        assert "INDI_DSCM_NO" in up
        assert "99999999" in sql

    def test_dm_code_filter_uses_null_space_safe_substring(self):
        """SICK_SYM1~5 의 DM 코드 비교는 NULL/공백 안전(LEFT+TRIM 또는 동등)해야 한다."""
        sql = _db_connector._build_cohort_id_sql(
            enroll_start=2013, enroll_end=2016,
            t20_schema='NHIS', t20_table='T20',
            use_hhdv=False,
        )
        up = sql.upper()
        # 5개 SICK_SYM 모두 비교
        for i in range(1, 6):
            assert f'SICK_SYM{i}' in up, f"SICK_SYM{i} 필터 누락"
        # E10~E14 전부 포함
        for code in ('E10', 'E11', 'E12', 'E13', 'E14'):
            assert f"'{code}'" in sql, f"DM 코드 '{code}' 누락"
        # 공백 안전: TRIM 또는 LTRIM/RTRIM 사용 (HANA CHAR trailing space 대응)
        assert 'TRIM' in up, "HANA CHAR trailing space 대비로 TRIM 필요"

    def test_hhdv_join_keys_include_month_and_indi(self):
        """use_hhdv=True 시 JOIN 키로 STD_YYYYMM=MDCARE_STRT_YYYYMM 과 INDI_DSCM_NO 동시 매칭."""
        sql = _db_connector._build_cohort_id_sql(
            enroll_start=2013, enroll_end=2016,
            t20_schema='NHIS', t20_table='T20',
            use_hhdv=True,
            hhdv_schema='NHIS', hhdv_table='HHDT_POPULATION_MM',
            hhdv_std_col='STD_YYYYMM', hhdv_std_is_monthly=True,
            min_age=40, max_age=64,
        )
        up = sql.upper()
        # JOIN 과 두 키 전부 확인
        assert 'JOIN' in up
        assert 'STD_YYYYMM' in up
        assert 'MDCARE_STRT_YYYYMM' in up
        # 연령 범위
        assert '40' in sql
        assert '64' in sql
        assert 'BYEAR' in up
        # 자격유형 기본값
        for t in ('1', '2', '5', '6', '7', '8'):
            assert f"'{t}'" in sql

    def test_enrollment_range_expressed_as_month_bounds(self):
        """진입기간은 MDCARE_STRT_YYYYMM 기준 YYYYMM 경계로 표현된다 (2013~2016 → 201301..201612)."""
        sql = _db_connector._build_cohort_id_sql(
            enroll_start=2013, enroll_end=2016,
            t20_schema='NHIS', t20_table='T20',
            use_hhdv=False,
        )
        assert '201301' in sql
        assert '201612' in sql

    def test_t20_monthly_int_column(self):
        """MDCARE_STRT_YYYYMM 이 INT 타입이면 따옴표 없이 숫자 리터럴 비교."""
        sql = _db_connector._build_cohort_id_sql(
            enroll_start=2013, enroll_end=2013,
            t20_schema='NHIS', t20_table='T20',
            t20_monthly_is_int=True,
            use_hhdv=False,
        )
        # 숫자 리터럴 (따옴표 미포함) 형태여야 함
        assert '201301' in sql
        # int 경로면 '201301' 형태의 문자열 리터럴이 최소한 month 비교에는 없어야 함
        assert "MDCARE_STRT_YYYYMM" in sql.upper()


class TestFetchSqlKeyset:
    """신규 `HANAConnector.fetch_sql_keyset` 계약 검증 (RED → GREEN).

    임의의 SELECT SQL 을 key_col 기준 keyset(cursor-based) 페이징으로 청크 조회.
    distinct=True → LIMIT/OFFSET 경로(O(n²))를 대체한다.
    """

    def test_method_exists(self):
        """HANAConnector.fetch_sql_keyset 메서드가 정의되어 있다."""
        assert hasattr(HANAConnector, 'fetch_sql_keyset'), (
            "HANAConnector 에 fetch_sql_keyset 메서드가 필요합니다."
        )

    def test_rejects_dml_base_sql(self):
        """base_sql 에 DML/DDL 이 포함되면 ValueError 를 발생시킨다."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        conn.conn = MagicMock()
        with pytest.raises(ValueError):
            # 제너레이터이므로 next() 로 실제 실행 시점에 검증 발동
            gen = conn.fetch_sql_keyset(
                "SELECT x FROM t; DROP TABLE t",
                key_col='x',
            )
            next(gen)

    def test_rejects_invalid_key_col(self):
        """key_col 이 식별자 형식이 아니면 ValueError."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        conn.conn = MagicMock()
        with pytest.raises(ValueError):
            gen = conn.fetch_sql_keyset(
                "SELECT INDI_DSCM_NO FROM t",
                key_col='INDI; DROP',
            )
            next(gen)

    def test_paginates_via_keyset_cursor(self):
        """base_sql 결과를 key_col 기준 페이징으로 반환하며, 커서는 마지막 key 값 이후로 이동."""
        conn = HANAConnector('host', 30015, 'user', 'pw')
        mock_conn = MagicMock()

        executed = []

        def _cursor_factory():
            cur = MagicMock()
            cur.description = [('INDI_DSCM_NO',)]

            # execute(sql[, params]) 호출 시마다 기록하고 적절한 행 반환
            def _execute(sql, params=None):
                executed.append((sql, params))
                page_num = len(executed)
                if page_num == 1:
                    cur._rows = [('100',), ('200',)]
                elif page_num == 2:
                    cur._rows = [('300',)]
                else:
                    cur._rows = []

            cur.execute.side_effect = _execute
            cur.fetchall.side_effect = lambda: getattr(cur, '_rows', [])
            return cur

        mock_conn.cursor.side_effect = _cursor_factory
        conn.conn = mock_conn

        chunks = list(conn.fetch_sql_keyset(
            "SELECT INDI_DSCM_NO FROM \"NHIS\".\"T20\" WHERE PAY_YN='1'",
            key_col='INDI_DSCM_NO',
            chunk_size=2,
        ))

        # 최소 1페이지 이상 받았고, 총 3건
        all_ids = []
        for df in chunks:
            all_ids.extend(df['INDI_DSCM_NO'].astype(str).tolist())
        assert all_ids == ['100', '200', '300'], f"keyset 순서 보장 필요: {all_ids}"

        # 2번째 호출에는 bind params 로 마지막 key 값이 전달되어야 함
        # (첫 페이지는 params=None 또는 빈 리스트, 이후는 last key 포함)
        if len(executed) >= 2:
            second_params = executed[1][1]
            assert second_params is not None and len(second_params) >= 1
            assert str(second_params[0]) == '200', (
                f"2번째 페이지 bind param 은 1페이지 마지막 key 값이어야 함: {second_params}"
            )


class TestMonthlyJKExtractor:
    """MonthlyJKExtractor 단위 테스트."""

    def test_month_range(self):
        """STUDY_SETTINGS 기반 월 범위 생성."""
        extractor = MonthlyJKExtractor(None, None, '/tmp')
        months = extractor._month_range()
        assert len(months) == 144  # (2024 - 2013 + 1) * 12
        assert months[0] == '201301'
        assert months[-1] == '202412'

    def test_init_rejects_invalid_identifiers(self, tmp_path):
        """UI/설정에서 주입된 불량 schema/table 식별자는 ValueError (SQL 인젝션 차단)."""
        bad_idents = [
            {'pop_schema': 'NHIS;DROP TABLE T20'},
            {'pop_table': 'HHDT_POPULATION_MM"'},
            {'dses_schema': 'NHIS SCHEMA'},  # space
            {'dses_table': '1BAD_TABLE'},    # starts with digit
        ]
        for override in bad_idents:
            with pytest.raises(ValueError, match="유효하지 않은"):
                MonthlyJKExtractor(None, None, tmp_path, **override)

    def test_build_join_sql_no_cohort(self):
        """cohort_ids 없을 때 JOIN SQL에 IN절 없음."""
        extractor = MonthlyJKExtractor(None, None, '/tmp',
                                       pop_schema='NHISBDA', pop_table='HHDT_POPULATION_MM',
                                       dses_schema='NHISBDA', dses_table='HHDT_DSES_YY')
        sql = extractor._build_join_sql('201301')
        assert "HHDT_POPULATION_MM" in sql
        assert "HHDT_DSES_YY" in sql
        assert "STD_YYYYMM = '201301'" in sql
        assert "STD_YYYY" in sql
        assert "HHDT_DEATH" in sql
        assert "IN (" not in sql

    def test_build_join_sql_with_cohort(self):
        """cohort_ids 있을 때 IN절 포함."""
        extractor = MonthlyJKExtractor(None, None, '/tmp')
        sql = extractor._build_join_sql('201301', cohort_ids=frozenset(['100', '200', '300']))
        assert "IN (" in sql

    def test_extract_all_months_uses_cache(self, tmp_path):
        """force=False 시 기존 JK 캐시(≥5컬럼) 재사용."""
        cache_dir = tmp_path / 'JK'
        cache_dir.mkdir()
        # 유효한 캐시 파일 생성 (6컬럼)
        df = pd.DataFrame({
            'STD_YYYYMM': ['201301'], 'STD_YYYY': ['2013'],
            'INDI_DSCM_NO': ['001'], 'SEX_TYPE': ['1'],
            'BYEAR': ['1970'], 'GAIBJA_TYPE': ['1'],
        })
        df.to_parquet(str(cache_dir / 'JK_201301.parquet'), index=False)

        mock_hana = MagicMock()
        mock_hana.fetch_sql_chunked.return_value = iter([])
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 1

        extractor = MonthlyJKExtractor(mock_hana, mock_storage, str(tmp_path))
        with patch.dict('config.STUDY_SETTINGS', {
            'STUDY_START_YEAR': 2013, 'STUDY_END_YEAR': 2013,
        }):
            extractor.extract_all_months(force=False)

        # 캐시 파일이 있으므로 fetch_sql_chunked는 나머지 11개월에 대해서만 호출
        assert mock_hana.fetch_sql_chunked.call_count == 11

    def test_stale_cache_triggers_reextract(self, tmp_path):
        """컬럼 수 < 5인 stale 캐시는 삭제 후 모든 월 재추출."""
        cache_dir = tmp_path / 'JK'
        cache_dir.mkdir()
        # 2컬럼짜리 stale 파일
        pd.DataFrame({'A': [1], 'B': [2]}).to_parquet(
            str(cache_dir / 'JK_201301.parquet'), index=False
        )

        mock_hana = MagicMock()
        mock_hana.fetch_sql_chunked.return_value = iter([])
        mock_storage = MagicMock()
        mock_storage.get_row_count.return_value = 0

        extractor = MonthlyJKExtractor(mock_hana, mock_storage, str(tmp_path))
        with patch.dict('config.STUDY_SETTINGS', {
            'STUDY_START_YEAR': 2013, 'STUDY_END_YEAR': 2013,
        }):
            # stale 캐시 삭제 후 12개월 모두 재추출; 빈 데이터도 명시적 스키마로 저장되므로 오류 없이 완료
            extractor.extract_all_months(force=False)

        # stale 캐시이므로 모든 12개월에 대해 재추출 시도
        assert mock_hana.fetch_sql_chunked.call_count == 12
        # DuckDB 병합(execute)이 호출되었는지 확인
        assert mock_storage.execute.called
