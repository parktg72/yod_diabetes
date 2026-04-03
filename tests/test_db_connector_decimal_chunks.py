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
