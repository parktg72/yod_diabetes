"""db_connector.py 청크 적재 시 넓은 Decimal 스키마 고정 테스트."""

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_connector

from db_connector import (
    DUCKDB_WIDE_INTEGER_DECIMAL,
    DuckDBStorage,
    _build_chunk_select_sql,
    _prepare_chunk_for_duckdb,
    _widen_decimal_columns,
)


def test_prepare_chunk_for_duckdb_marks_integral_decimal_for_wide_decimal_cast():
    df = pd.DataFrame({
        'claim_id': [Decimal('999999'), Decimal('1031900'), None],
    })

    converted = _prepare_chunk_for_duckdb(df.copy())

    assert str(converted['claim_id'].dtype) == 'object'
    assert converted['claim_id'].iloc[0] == '999999'
    assert converted['claim_id'].iloc[1] == '1031900'
    assert pd.isna(converted['claim_id'].iloc[2])
    assert converted.attrs['duckdb_type_overrides'] == {
        'claim_id': DUCKDB_WIDE_INTEGER_DECIMAL,
    }


def test_prepare_chunk_for_duckdb_converts_mixed_decimal_objects_to_text():
    df = pd.DataFrame({
        'claim_id': [Decimal('12752'), 42, None],
    })

    converted = _prepare_chunk_for_duckdb(df.copy())

    assert list(converted['claim_id']) == ['12752', '42', None]
    assert converted.attrs['duckdb_type_overrides'] == {
        'claim_id': DUCKDB_WIDE_INTEGER_DECIMAL,
    }


def test_chunk_insert_does_not_freeze_decimal_6_0_schema(tmp_path):
    storage = DuckDBStorage(str(tmp_path / 'decimal_chunks.duckdb'))
    storage.connect()

    first_chunk = _prepare_chunk_for_duckdb(pd.DataFrame({
        'claim_id': [Decimal('999999')],
    }))
    storage.conn.register('_temp_chunk', first_chunk)
    storage.execute(f"CREATE TABLE claims AS {_build_chunk_select_sql(first_chunk, '_temp_chunk')}")
    storage.conn.unregister('_temp_chunk')

    second_chunk = _prepare_chunk_for_duckdb(pd.DataFrame({
        'claim_id': [Decimal('1031900')],
    }))
    storage.conn.register('_temp_chunk', second_chunk)
    storage.execute(f"INSERT INTO claims {_build_chunk_select_sql(second_chunk, '_temp_chunk')}")
    storage.conn.unregister('_temp_chunk')

    col_type = storage.execute("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_name = 'claims' AND column_name = 'claim_id'
    """).fetchone()[0]
    values = storage.execute("SELECT claim_id FROM claims ORDER BY claim_id").fetchall()

    assert col_type == DUCKDB_WIDE_INTEGER_DECIMAL
    assert values == [(Decimal('999999'),), (Decimal('1031900'),)]

    storage.close()


def test_widen_decimal_columns_expands_narrow_decimal_schema(tmp_path):
    storage = DuckDBStorage(str(tmp_path / 'decimal_widen.duckdb'))
    storage.connect()
    storage.execute("CREATE TABLE claims (claim_id DECIMAL(4,0))")

    _widen_decimal_columns(storage, 'claims')

    col_type = storage.execute("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_name = 'claims' AND column_name = 'claim_id'
    """).fetchone()[0]

    assert col_type == DUCKDB_WIDE_INTEGER_DECIMAL

    storage.close()


def test_csv_loader_invokes_decimal_widening_on_first_chunk(tmp_path, monkeypatch):
    csv_path = tmp_path / 'claims.csv'
    csv_path.write_text("claim_id\n12752\n", encoding='utf-8')

    storage = DuckDBStorage(str(tmp_path / 'csv_loader.duckdb'))
    storage.connect()

    calls = []

    def fake_widen(target_storage, table_name):
        calls.append((target_storage, table_name))

    monkeypatch.setattr(db_connector, '_widen_decimal_columns', fake_widen)

    loader = db_connector.SASFileLoader()
    loader.load_csv_chunked_to_duckdb(csv_path, storage, 'claims')

    assert calls == [(storage, 'claims')]

    storage.close()


def test_string_extension_columns_are_forced_to_varchar(tmp_path):
    storage = DuckDBStorage(str(tmp_path / 'string_columns.duckdb'))
    storage.connect()

    first_chunk = _prepare_chunk_for_duckdb(pd.DataFrame({
        'SPEC_ADD_DESC': pd.Series([None, None], dtype='string'),
        'DMD_TYPE': pd.Series(['101', '102'], dtype='string'),
        'SPEC_TP_CD': pd.Series(['1', '2'], dtype='string'),
        'EDC_ZN_CD': pd.Series(['11', '12'], dtype='string'),
        'EDC_ZN_IP': pd.Series(['21', '22'], dtype='string'),
    }))
    storage.conn.register('_temp_chunk', first_chunk)
    storage.execute(f"CREATE TABLE claims AS {_build_chunk_select_sql(first_chunk, '_temp_chunk')}")
    storage.conn.unregister('_temp_chunk')

    second_chunk = _prepare_chunk_for_duckdb(pd.DataFrame({
        'SPEC_ADD_DESC': pd.Series(['심부장기감염'], dtype='string'),
        'DMD_TYPE': pd.Series(['치주질환'], dtype='string'),
        'SPEC_TP_CD': pd.Series(['외래'], dtype='string'),
        'EDC_ZN_CD': pd.Series(['중환자실'], dtype='string'),
        'EDC_ZN_IP': pd.Series(['입원'], dtype='string'),
    }))
    storage.conn.register('_temp_chunk', second_chunk)
    storage.execute(f"INSERT INTO claims {_build_chunk_select_sql(second_chunk, '_temp_chunk')}")
    storage.conn.unregister('_temp_chunk')

    schema = dict(storage.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'claims'
    """).fetchall())

    assert schema == {
        'SPEC_ADD_DESC': 'VARCHAR',
        'DMD_TYPE': 'VARCHAR',
        'SPEC_TP_CD': 'VARCHAR',
        'EDC_ZN_CD': 'VARCHAR',
        'EDC_ZN_IP': 'VARCHAR',
    }
    assert storage.execute("""
        SELECT SPEC_ADD_DESC, DMD_TYPE, SPEC_TP_CD, EDC_ZN_CD, EDC_ZN_IP
        FROM claims
        ORDER BY SPEC_ADD_DESC NULLS FIRST
    """).fetchall() == [
        (None, '101', '1', '11', '21'),
        (None, '102', '2', '12', '22'),
        ('심부장기감염', '치주질환', '외래', '중환자실', '입원'),
    ]

    storage.close()
