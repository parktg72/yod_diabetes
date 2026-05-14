"""
db_connector.py - 데이터베이스 연결 및 디스크 기반 데이터 처리
SAP HANA DB 스키마/테이블 검색 + SAS 파일 + DuckDB 로컬 저장소
건강검진 연도별 분리(2018+) / 통합(2002-2017) 처리
"""

import os
import gc
import re
import time
import logging
import numbers
from decimal import Decimal
import duckdb
import pandas as pd
import sys
from pathlib import Path

_BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

from config import DUCKDB_SETTINGS, EXAM_STRUCTURE
from memory_manager import mem_manager, chunk_controller

_VALID_TABLE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MONTHLY_TABLES = frozenset({'T20', 'T30', 'T40', 'T60'})  # 내부 별칭 기준
_MONTHLY_FILTER_COL = 'MDCARE_STRT_YYYYMM'


def _resolve_hana_table(alias: str) -> str:
    """내부 별칭(T20 등)을 실제 HANA 테이블명으로 변환.

    config.py HANA_TABLE_MAP에 매핑이 없으면 alias를 그대로 반환.
    """
    from config import STUDY_SETTINGS
    table_map = STUDY_SETTINGS.get('HANA_TABLE_MAP') or {}
    return table_map.get(alias, table_map.get(alias.upper(), alias))
_READ_ONLY_FORBIDDEN = re.compile(
    r'\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|EXEC|EXECUTE|GRANT|REVOKE|TRUNCATE)\b',
    re.IGNORECASE
)


def _validate_table_name(name):
    """테이블명 유효성 검증 (SQL 인젝션 방지)"""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"유효하지 않은 테이블명: {name!r}")
    return name

logger = logging.getLogger(__name__)

DUCKDB_WIDE_INTEGER_DECIMAL = 'DECIMAL(38,0)'
DUCKDB_WIDE_DECIMAL_PRECISION = 38


def _quote_identifier(name):
    """DuckDB identifier를 안전하게 이스케이프한다."""
    return f'"{str(name).replace(chr(34), chr(34) * 2)}"'


def _emit_progress(progress_callback, message, total=None, item_name=None):
    """진행 메시지를 WorkerThread(string)와 레거시(total, name) 콜백 모두에 전달."""
    if not progress_callback:
        return
    try:
        progress_callback(message)
    except TypeError:
        if total is None or item_name is None:
            raise
        progress_callback(total, item_name)


def _emit_chunk_progress(progress_callback, table_name, total):
    _emit_progress(progress_callback, f"{table_name}: {total:,}건 적재 완료", total, table_name)


def _create_indexes_with_progress(duckdb_storage, table_name, indexes, progress_callback=None):
    if not indexes:
        return
    for columns in indexes:
        cols_label = ', '.join(columns)
        _emit_progress(
            progress_callback,
            f"{table_name}: 인덱스 생성 중... ({cols_label})"
        )
        duckdb_storage.create_index(table_name, columns)
    _emit_progress(progress_callback, f"{table_name}: 인덱스 생성 완료")


def _build_chunk_select_sql(chunk_df, temp_table_name):
    """등록된 임시 청크에서 타입 override를 반영한 SELECT SQL 생성."""
    type_overrides = chunk_df.attrs.get('duckdb_type_overrides', {})
    select_parts = []

    for col in chunk_df.columns:
        quoted = _quote_identifier(col)
        override_type = type_overrides.get(col)
        if override_type:
            select_parts.append(f'CAST({quoted} AS {override_type}) AS {quoted}')
        else:
            select_parts.append(quoted)

    return f"SELECT {', '.join(select_parts)} FROM {temp_table_name}"


def _decimal_value_to_text(value):
    """Decimal 계열 값을 DuckDB CAST용 문자열로 정규화한다."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, numbers.Integral):
        return str(value)
    if isinstance(value, numbers.Real):
        return format(Decimal(str(value)), 'f')
    return str(value)


def _prepare_chunk_for_duckdb(chunk_df):
    """DuckDB 등록 전 청크 dtype을 안정화한다.

    Python Decimal object가 첫 청크에서 좁은 DECIMAL(p,s)로 추론되면
    이후 청크의 더 큰 값이 INSERT 시 범위 초과를 일으킬 수 있다.
    적재 전 Decimal 컬럼을 문자열로 정규화한 뒤 명시적으로
    넉넉한 DECIMAL 타입으로 CAST해 청크 간 스키마를 고정한다.

    주의: HANA 컬럼은 Decimal + int 혼재(mixed-type)로 반환될 수 있다.
    이전에는 all() 검사로 혼재 컬럼을 무시해 DECIMAL(4,0) 오추론이 발생했다.
    any()로 변경하여 Decimal 값이 하나라도 있으면 넉넉한 타입을 강제한다.
    """
    type_overrides = {}

    for col in chunk_df.select_dtypes(include=['category']).columns:
        chunk_df[col] = chunk_df[col].astype('object')

    for col in chunk_df.columns:
        series = chunk_df[col]
        is_object_dtype = series.dtype == 'object'
        is_string_extension_dtype = (
            pd.api.types.is_string_dtype(series.dtype) and not is_object_dtype
        )

        if is_string_extension_dtype:
            # pandas nullable string/string[pyarrow] 컬럼도 DuckDB 추론 전에 동일 처리
            chunk_df[col] = series.astype('object')
            series = chunk_df[col]
            is_object_dtype = True

        if not is_object_dtype:
            continue

        non_null = series[series.notna()]
        if non_null.empty:
            # 첫 청크 전체 NULL이면 DuckDB가 INTEGER로 추론할 수 있음 → VARCHAR 강제
            type_overrides[col] = 'VARCHAR'
            continue

        decimal_mask = non_null.map(lambda value: isinstance(value, Decimal))
        if not decimal_mask.any():
            # Decimal 없는 object 컬럼: str 값이 하나라도 있으면 VARCHAR 강제
            # (첫 청크에 숫자 문자열만 있어 INT32로 추론 후 이후 청크에 한글 삽입 시 오류 방지)
            if non_null.map(lambda v: isinstance(v, str)).any():
                type_overrides[col] = 'VARCHAR'
            continue

        # Mixed Decimal + string → VARCHAR 강제 (INSERT 타입 불일치 방지)
        has_string = non_null.map(lambda v: isinstance(v, str)).any()
        if has_string:
            type_overrides[col] = 'VARCHAR'
            continue

        # Decimal 값만으로 scale을 계산 (int/None 혼재 시 Decimal 부분만 사용)
        decimal_values = non_null[decimal_mask]

        chunk_df[col] = series.map(_decimal_value_to_text)

        if decimal_values.map(lambda value: value == value.to_integral_value()).all():
            type_overrides[col] = DUCKDB_WIDE_INTEGER_DECIMAL
        else:
            scale = decimal_values.map(
                lambda value: max(0, -value.as_tuple().exponent)
            ).max()
            type_overrides[col] = f'DECIMAL({DUCKDB_WIDE_DECIMAL_PRECISION},{scale})'

    chunk_df.attrs['duckdb_type_overrides'] = type_overrides
    return chunk_df


def _widen_decimal_columns(storage, table_name):
    """CREATE TABLE 직후 좁은 DECIMAL 컬럼을 안전한 너비로 확장한다.

    첫 청크의 값이 작아 DuckDB가 DECIMAL(p,s)를 좁게 추론하더라도
    이후 청크의 큰 값이 INSERT 실패하지 않도록 DECIMAL(38,s)로 확장한다.
    _prepare_chunk_for_duckdb의 CAST override가 실패한 경우의 안전망이다.
    """
    _validate_table_name(table_name)
    try:
        schema_df = storage.execute_df(
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_name = ?",
            [table_name],
        )
    except Exception as exc:
        logger.warning(f"_widen_decimal_columns: information_schema 조회 실패 — {exc}")
        return

    for _, row in schema_df.iterrows():
        data_type_upper = str(row['data_type']).upper()
        if not (data_type_upper.startswith('DECIMAL') or data_type_upper.startswith('NUMERIC')):
            continue
        try:
            prec = int(row['numeric_precision']) if row['numeric_precision'] is not None else DUCKDB_WIDE_DECIMAL_PRECISION
            scale = int(row['numeric_scale']) if row['numeric_scale'] is not None else 0
        except (TypeError, ValueError):
            continue
        if scale < 0 or scale > 38:
            logger.warning(f"비정상적인 scale 값({scale}) 무시: {table_name}.{row['column_name']}")
            continue
        if prec >= DUCKDB_WIDE_DECIMAL_PRECISION:
            continue  # 이미 충분히 넓음
        safe_prec = DUCKDB_WIDE_DECIMAL_PRECISION
        quoted_col = _quote_identifier(row['column_name'])
        _validate_table_name(table_name)
        try:
            storage.execute(
                f'ALTER TABLE "{table_name}" ALTER {quoted_col} '
                f'TYPE DECIMAL({safe_prec},{scale})'
            )
            logger.debug(
                f"DECIMAL 컬럼 확장: {table_name}.{row['column_name']} "
                f"DECIMAL({prec},{scale}) → DECIMAL({safe_prec},{scale})"
            )
        except Exception as e:
            logger.warning(
                f"DECIMAL 컬럼 자동 확장 실패 ({table_name}.{row['column_name']}): {e}"
            )


class DuckDBStorage:
    """DuckDB 기반 디스크 저장소"""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = str(_BASE_DIR / 'nhis_analysis.duckdb')
        self.db_path = db_path
        self.conn = None

    def connect(self):
        _raw_temp = DUCKDB_SETTINGS.get('TEMP_DIRECTORY')
        temp_dir = str(_BASE_DIR / 'temp_duckdb') if not _raw_temp else _raw_temp
        os.makedirs(temp_dir, exist_ok=True)
        try:
            self.conn = duckdb.connect(self.db_path)
        except Exception as e:
            err_str = str(e).lower()
            if 'locked' in err_str or 'already opened' in err_str or 'lock' in err_str:
                raise RuntimeError(
                    f"DuckDB 파일이 잠겨 있습니다: {self.db_path}\n"
                    "원인: 이전 세션이 비정상 종료되어 파일 잠금이 남아 있거나, "
                    "다른 프로세스(Python/DBeaver 등)가 같은 파일을 열고 있습니다.\n"
                    "해결 방법:\n"
                    "  1. 앱을 완전히 종료한 뒤 다시 실행하세요.\n"
                    "  2. 작업 관리자에서 python.exe 프로세스가 남아 있으면 종료하세요.\n"
                    f"  3. 그래도 안 되면 '{self.db_path}' 파일을 삭제 후 다시 추출하세요."
                ) from e
            if 'schema does not match' in err_str or 'schema used to create' in err_str:
                # 스키마 불일치: 기존 DuckDB/WAL 파일을 절대 삭제하지 않고
                # `.corrupt_<timestamp>` 로 rename 백업 후 빈 파일로 재연결.
                # 코호트 추출은 수 시간 걸리는 작업이므로 사용자 데이터 보존이 최우선.
                import time as _time
                ts = _time.strftime('%Y%m%d_%H%M%S')
                db_file = Path(self.db_path)
                wal_file = Path(str(self.db_path) + '.wal')
                backup_db = db_file.with_name(f"{db_file.name}.corrupt_{ts}")
                backup_wal = wal_file.with_name(f"{wal_file.name}.corrupt_{ts}")
                try:
                    if db_file.exists():
                        db_file.rename(backup_db)
                    if wal_file.exists():
                        wal_file.rename(backup_wal)
                except Exception as rn_e:
                    raise RuntimeError(
                        f"DuckDB 스키마 불일치 감지 — 기존 파일 백업 실패: {self.db_path}\n"
                        f"원인: {rn_e}\n"
                        "해결 방법: 파일 권한 확인 후 수동으로 백업/이동하세요."
                    ) from rn_e
                logger.warning(
                    "DuckDB 스키마 불일치 감지 — 기존 파일을 %s 로 백업하고 빈 파일로 재연결합니다. "
                    "이전 데이터가 필요하면 백업 파일에서 복구하세요.",
                    backup_db,
                )
                try:
                    self.conn = duckdb.connect(self.db_path)
                except Exception as retry_e:
                    raise RuntimeError(
                        f"DuckDB 파일 백업 후 재연결 실패: {self.db_path}\n"
                        f"백업: {backup_db}\n"
                        f"원인: {retry_e}"
                    ) from retry_e
            else:
                raise
        mem_limit = DUCKDB_SETTINGS['MEMORY_LIMIT']
        threads = DUCKDB_SETTINGS['THREADS']
        if not re.match(r'^\d+(\.\d+)?(GB|MB|KB|B)$', str(mem_limit), re.IGNORECASE):
            raise ValueError(f"유효하지 않은 MEMORY_LIMIT: {mem_limit!r}")
        if not isinstance(threads, int) or threads < 1:
            raise ValueError(f"유효하지 않은 THREADS: {threads!r}")
        self.conn.execute(f"SET memory_limit='{mem_limit}'")
        self.conn.execute(f"SET threads TO {threads}")
        self.conn.execute(f"SET temp_directory='{temp_dir}'")
        logger.info(f"DuckDB 연결됨: {self.db_path}")
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def execute(self, query, params=None):
        if not self.conn:
            self.connect()
        return self.conn.execute(query, params) if params is not None else self.conn.execute(query)

    def execute_df(self, query, params=None):
        result = self.execute(query, params)
        return result.fetchdf()

    def table_exists(self, table_name):
        result = self.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name]
        )
        return result.fetchone()[0] > 0

    def get_row_count(self, table_name):
        _validate_table_name(table_name)
        if self.table_exists(table_name):
            return self.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}").fetchone()[0]
        return 0

    def drop_table(self, table_name):
        _validate_table_name(table_name)
        self.execute(f'DROP TABLE IF EXISTS "{table_name}"')

    def create_index(self, table_name, columns, index_name=None):
        _validate_table_name(table_name)
        for col in columns:
            _validate_table_name(col)
        if not index_name:
            index_name = f"idx_{table_name}_{'_'.join(columns)}"
        _validate_table_name(index_name)
        try:
            self.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({', '.join(columns)})")
        except Exception as e:
            logger.warning(f"인덱스 생성 실패 ({index_name}): {e}")


class HANAConnector:
    """SAP HANA DB 연결 + 스키마/테이블 검색"""

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = int(port)
        self.user = user
        # 보안: mutable bytearray로 저장하여 메모리에서 확실히 소거 가능
        self._password_buf = bytearray(password.encode('utf-8')) if password else bytearray()
        self.conn = None

    @property
    def _password(self):
        """패스워드 문자열 반환 (연결 시에만 사용)"""
        return self._password_buf.decode('utf-8') if self._password_buf else ''

    def _clear_password(self):
        """메모리에서 패스워드 바이트를 확실히 소거"""
        for i in range(len(self._password_buf)):
            self._password_buf[i] = 0
        self._password_buf = bytearray()

    def connect(self, max_retries: int = 2, retry_delay: float = 2.0):
        """HANA DB 연결. 네트워크 오류 시 max_retries회 재시도."""
        try:
            from hdbcli import dbapi
        except ImportError:
            raise ImportError(
                "SAP HANA 드라이버가 설치되지 않았습니다.\n"
                "명령 프롬프트에서 다음을 실행하세요:\n"
                "  venv\\Scripts\\activate\n"
                "  pip install -r requirements-hana.txt"
            )
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                self.conn = dbapi.connect(
                    address=self.host, port=self.port,
                    user=self.user, password=self._password,
                )
                logger.info(f"HANA DB 연결 성공: {self.host}:{self.port}")
                return True
            except Exception as e:
                last_exc = e
                if attempt < max_retries:
                    logger.warning(
                        f"HANA DB 연결 실패 (시도 {attempt + 1}/{max_retries + 1}), "
                        f"{retry_delay:.0f}초 후 재시도: {e}"
                    )
                    time.sleep(retry_delay)
                else:
                    logger.error(f"HANA DB 연결 최종 실패: {e}")
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("HANA DB 연결 실패: 재시도 횟수 설정을 확인하세요 (max_retries >= 0).")

    def _reconnect_if_stale(self):
        """HANA 세션이 없거나 만료되었으면 연결을 복구한다."""
        if not self.conn:
            self.connect()
            return

        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 'OK' FROM DUMMY")
            cursor.fetchone()
        except Exception as e:
            err = str(e)
            if '-10821' in err or 'session not connected' in err.lower():
                logger.warning("HANA 세션 만료 감지 (-10821), 재연결 시도")
                self.conn = None
                self.connect()
                return
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    logger.debug("HANA ping cursor close failed", exc_info=True)

    def test_connection(self):
        try:
            self.connect()
            cursor = self.conn.cursor()
            cursor.execute("SELECT 'OK' FROM DUMMY")
            result = cursor.fetchone()
            cursor.close()
            return result[0] == 'OK'
        except Exception as e:
            logger.error(f"연결 테스트 실패: {e}")
            raise
        finally:
            self.close()

    def close(self):
        """연결 객체만 닫음 — 패스워드는 유지하여 재연결 가능"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def destroy(self):
        """완전 종료: 연결 닫고 메모리에서 패스워드 바이트 소거"""
        self.close()
        self._clear_password()

    def _detect_column_type(self, schema_name, table_name, column_name):
        """HANA 컬럼 타입 조회. 실패 시 None 반환."""
        if not self.conn:
            return None
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT DATA_TYPE_NAME FROM SYS.TABLE_COLUMNS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?
            """, (schema_name, table_name, column_name))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None
        finally:
            cursor.close()

    # -------------------------------------------------------
    # 스키마/테이블/컬럼 검색 기능
    # -------------------------------------------------------
    def list_schemas(self):
        """사용 가능한 스키마 목록 반환"""
        if not self.conn:
            self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT SCHEMA_NAME
            FROM SYS.SCHEMAS
            WHERE HAS_PRIVILEGES = 'TRUE'
            ORDER BY SCHEMA_NAME
        """)
        schemas = [row[0] for row in cursor.fetchall()]
        cursor.close()
        logger.info(f"HANA 스키마 {len(schemas)}개 검색됨")
        return schemas

    def list_tables(self, schema_name):
        """특정 스키마의 테이블 목록 반환"""
        if not self.conn:
            self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT TABLE_NAME, TABLE_TYPE
            FROM SYS.TABLES
            WHERE SCHEMA_NAME = ?
            ORDER BY TABLE_NAME
        """, (schema_name,))
        tables = [{'name': row[0], 'type': row[1]} for row in cursor.fetchall()]
        cursor.close()

        # VIEW도 포함
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT VIEW_NAME, 'VIEW' AS TABLE_TYPE
            FROM SYS.VIEWS
            WHERE SCHEMA_NAME = ?
            ORDER BY VIEW_NAME
        """, (schema_name,))
        views = [{'name': row[0], 'type': row[1]} for row in cursor.fetchall()]
        cursor.close()

        all_objects = tables + views
        logger.info(f"HANA {schema_name}: 테이블 {len(tables)}개 + 뷰 {len(views)}개")
        return all_objects

    def list_columns(self, schema_name, table_name):
        """테이블의 컬럼 목록 반환"""
        if not self.conn:
            self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, IS_NULLABLE, COMMENTS
            FROM SYS.TABLE_COLUMNS
            WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?
            ORDER BY POSITION
        """, (schema_name, table_name))
        columns = [
            {'name': row[0], 'type': row[1], 'length': row[2],
             'nullable': row[3], 'comment': row[4]}
            for row in cursor.fetchall()
        ]
        cursor.close()

        if not columns:
            # VIEW인 경우
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, IS_NULLABLE, COMMENTS
                FROM SYS.VIEW_COLUMNS
                WHERE SCHEMA_NAME = ? AND VIEW_NAME = ?
                ORDER BY POSITION
            """, (schema_name, table_name))
            columns = [
                {'name': row[0], 'type': row[1], 'length': row[2],
                 'nullable': row[3], 'comment': row[4]}
                for row in cursor.fetchall()
            ]
            cursor.close()

        return columns

    def search_tables(self, schema_name, keyword):
        """테이블명에서 키워드 검색"""
        if not self.conn:
            self.connect()
        cursor = self.conn.cursor()
        keyword_upper = keyword.upper()
        cursor.execute("""
            SELECT TABLE_NAME FROM SYS.TABLES
            WHERE SCHEMA_NAME = ? AND UPPER(TABLE_NAME) LIKE ?
            UNION ALL
            SELECT VIEW_NAME FROM SYS.VIEWS
            WHERE SCHEMA_NAME = ? AND UPPER(VIEW_NAME) LIKE ?
            ORDER BY 1
        """, (schema_name, f'%{keyword_upper}%', schema_name, f'%{keyword_upper}%'))
        results = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return results

    def get_table_row_count(self, schema_name, table_name):
        """HANA 테이블 행 수 조회"""
        if not self.conn:
            self.connect()
        cursor = self.conn.cursor()
        try:
            cursor.execute(f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"')
            count = cursor.fetchone()[0]
            cursor.close()
            return count
        except Exception as e:
            logger.warning(f"HANA 행수 조회 실패 ({schema_name}.{table_name}): {e}")
            cursor.close()
            return -1

    def _get_order_key(self, schema_name, table_name):
        """LIMIT/OFFSET 페이징을 위한 정렬 키 결정.

        1순위: PRIMARY KEY 컬럼
        2순위: 테이블 첫 번째 컬럼 (최소한 세션 내 결정적 순서 보장)
        """
        order_key, _, _ = self._get_order_info(schema_name, table_name)
        return order_key

    def _get_order_info(self, schema_name, table_name):
        """정렬 키 및 keyset 페이징 가능 여부 반환.

        Returns:
            (order_key_str, is_single_pk, pk_col_name)
            - order_key_str: ORDER BY 절에 사용할 문자열
            - is_single_pk: 단일 PK 컬럼인 경우 True (keyset 페이징 사용 가능)
            - pk_col_name: 단일 PK일 때 컬럼 이름(따옴표 없음), 아니면 None
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT COLUMN_NAME FROM SYS.CONSTRAINTS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? AND IS_PRIMARY_KEY = 'TRUE'
                ORDER BY POSITION
            """, (schema_name, table_name))
            pk_cols = [row[0] for row in cursor.fetchall()]
            if pk_cols:
                order_key = ', '.join(f'"{c}"' for c in pk_cols)
                if len(pk_cols) == 1:
                    return order_key, True, pk_cols[0]
                return order_key, False, None
        except Exception:
            pass
        finally:
            cursor.close()

        # PK가 없으면 첫 번째 컬럼 사용
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?
                ORDER BY POSITION LIMIT 1
            """, (schema_name, table_name))
            row = cursor.fetchone()
            if row:
                return f'"{row[0]}"', False, None
        except Exception:
            pass
        finally:
            cursor.close()

        return '1', False, None  # 최종 fallback

    # -------------------------------------------------------
    # 데이터 적재
    # -------------------------------------------------------
    @staticmethod
    def _validate_where_clause(clause):
        """WHERE 절 기본 검증 — 위험한 SQL 구문 차단"""
        if not clause:
            return clause
        forbidden = re.compile(
            r'\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|EXEC|EXECUTE|GRANT|REVOKE|TRUNCATE)\b',
            re.IGNORECASE
        )
        if forbidden.search(clause):
            raise ValueError(f"허용되지 않는 SQL 구문 포함: {clause[:100]}")
        # 세미콜론으로 다중 구문 방지
        if ';' in clause:
            raise ValueError("WHERE 절에 세미콜론 사용 불가")
        return clause

    def fetch_sql_keyset(self, base_sql, key_col, chunk_size=None):
        """임의의 SELECT SQL을 key_col 기준 keyset(cursor-based) 페이징으로 청크 조회.

        LIMIT/OFFSET 기반 `fetch_sql_chunked` 의 O(n²) 스캔 문제를 피하기 위한
        최적 경로. base_sql을 서브쿼리로 감싸 `WHERE key_col > ?` 조건을 추가해
        커서를 이동시킨다. DISTINCT/GROUP BY가 포함된 SQL에서도 안전하게 동작한다.

        Args:
            base_sql: ORDER BY / LIMIT / OFFSET 미포함 완전한 SELECT 문.
                      SELECT 결과 컬럼 목록에 key_col 이 포함되어야 한다.
            key_col:  정렬·커서 이동 기준 컬럼명(식별자, 따옴표 없이).
                      반드시 단일 컬럼이어야 하며, 값은 정렬 가능해야 한다.
                      **UNIQUE 필수** — `WHERE key_col > ?` 커서는 중복 키의
                      두 번째 이후 행을 스킵한다. 호출자는 PK 컬럼(예: INDI_DSCM_NO)
                      을 지정하거나 base_sql에 DISTINCT/GROUP BY 로 유일성을 보장해야 한다.
            chunk_size: 청크당 최대 행 수 (None이면 기본값).

        Yields: pandas.DataFrame
        """
        # DML/DDL 차단 — base_sql은 SELECT 전용이어야 함
        if _READ_ONLY_FORBIDDEN.search(base_sql):
            raise ValueError("fetch_sql_keyset: base_sql에 허용되지 않는 DML/DDL 구문 포함")
        # 세미콜론으로 다중 구문 방지
        if ';' in base_sql:
            raise ValueError("fetch_sql_keyset: base_sql에 세미콜론 사용 불가")
        # key_col 식별자 검증 (따옴표 없는 단일 식별자만 허용)
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key_col):
            raise ValueError(f"fetch_sql_keyset: 유효하지 않은 key_col: {key_col!r}")
        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        if not self.conn:
            self.connect()

        quoted_key = f'"{key_col}"'
        total_rows = 0
        last_value = None
        col_names = None

        while True:
            if last_value is None:
                paged = (
                    f'SELECT * FROM ({base_sql}) _k '
                    f'ORDER BY {quoted_key} '
                    f'LIMIT {chunk_size}'
                )
                bind_params = None
            else:
                paged = (
                    f'SELECT * FROM ({base_sql}) _k '
                    f'WHERE {quoted_key} > ? '
                    f'ORDER BY {quoted_key} '
                    f'LIMIT {chunk_size}'
                )
                bind_params = [last_value]

            # 청크 단위 재시도 (최대 3회, 지수 백오프)
            rows = None
            for attempt in range(3):
                cursor = self.conn.cursor()
                try:
                    if bind_params is None:
                        cursor.execute(paged)
                    else:
                        cursor.execute(paged, bind_params)
                    if col_names is None:
                        col_names = [d[0] for d in cursor.description]
                    rows = cursor.fetchall()
                    cursor.close()
                    break
                except Exception as e:
                    cursor.close()
                    if attempt < 2:
                        logger.warning(
                            f"HANA keyset 청크 조회 실패 (시도 {attempt + 1}/3): {e}"
                        )
                        time.sleep(2 ** attempt)
                        try:
                            self.connect()
                        except Exception:
                            pass
                    else:
                        raise

            if not rows:
                break

            chunk_df = pd.DataFrame(rows, columns=col_names)
            fetched = len(chunk_df)
            total_rows += fetched

            # key_col 컬럼의 마지막 값을 다음 페이지 커서로 사용
            last_value = chunk_df[key_col].iloc[-1]

            yield chunk_df

            if fetched < chunk_size:
                break

        logger.info(f"HANA fetch_sql_keyset: {total_rows:,}건 로드 (key_col={key_col})")

    def fetch_sql_chunked(self, base_sql, order_col, chunk_size=None):
        """임의의 SELECT SQL을 LIMIT/OFFSET으로 청크 조회.

        base_sql: ORDER BY / LIMIT / OFFSET 미포함 완전한 SELECT 문
                  (JOIN, WHERE 등 모두 포함)
        order_col: 페이징 안정성을 위한 ORDER BY 컬럼 (따옴표 포함 가능, e.g. '"INDI_DSCM_NO"')
        chunk_size: 청크당 최대 행 수 (None이면 기본값)

        Yields: pandas.DataFrame
        """
        # DML/DDL 차단 — base_sql은 SELECT 전용이어야 함
        if _READ_ONLY_FORBIDDEN.search(base_sql):
            raise ValueError("fetch_sql_chunked: base_sql에 허용되지 않는 DML/DDL 구문 포함")
        # order_col 식별자 검증 (선택적 따옴표 포함)
        if not re.match(r'^"?[A-Za-z_][A-Za-z0-9_]*"?$', order_col):
            raise ValueError(f"fetch_sql_chunked: 유효하지 않은 order_col: {order_col!r}")
        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        if not self.conn:
            self.connect()
        offset = 0
        col_names = None
        while True:
            paged = f"{base_sql} ORDER BY {order_col} LIMIT {chunk_size} OFFSET {offset}"
            cursor = self.conn.cursor()
            try:
                cursor.execute(paged)
                if col_names is None:
                    col_names = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if not rows:
                break
            yield pd.DataFrame(rows, columns=col_names)
            if len(rows) < chunk_size:
                break
            offset += chunk_size

    def fetch_table_chunked(self, table_name, schema_name, columns=None,
                            where_clause=None, chunk_size=None, distinct=False):
        """서버 측 페이징으로 대용량 테이블 분할 조회.

        단일 PK + distinct=False 조건에서는 keyset(cursor-based) 페이징을 사용하여
        OFFSET 증가에 따른 O(n²) 스캔 문제와 청크 경계에서의 행 중복/누락을 방지한다.
        그 외에는 기존 LIMIT/OFFSET 페이징으로 fallback한다.

        Args:
            distinct: True 이면 SELECT DISTINCT를 사용해 HANA 측에서 중복 제거.
                      Python set()으로도 중복 제거되지만 distinct=True 시 전송량 절감.
                      DISTINCT 사용 시 ORDER BY는 첫 번째 선택 컬럼으로 고정.
                      DISTINCT + keyset 조합은 복잡하므로 LIMIT/OFFSET 방식 유지.
        """
        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        self._reconnect_if_stale()
        col_str = ', '.join(f'"{c}"' for c in columns) if columns else '*'
        select_prefix = 'SELECT DISTINCT' if distinct else 'SELECT'
        from_clause = f'"{schema_name}"."{table_name}"' if schema_name else f'"{table_name}"'

        if where_clause:
            self._validate_where_clause(where_clause)

        # 컬럼 이름을 먼저 가져오기 (0건만 조회)
        where_part_meta = f' WHERE {where_clause}' if where_clause else ''
        meta_query = f'{select_prefix} {col_str} FROM {from_clause}{where_part_meta} LIMIT 0'
        cursor = self.conn.cursor()
        cursor.execute(meta_query)
        col_names = [desc[0] for desc in cursor.description]
        cursor.close()

        # DISTINCT 시: ORDER BY는 SELECT 목록 첫 컬럼으로 고정 (HANA 제약)
        # 비DISTINCT 시: PK → 첫 컬럼 fallback
        if distinct:
            first_col = (columns[0] if columns else col_names[0])
            order_key = f'"{first_col}"'
            use_keyset = False
            pk_col_name = None
        else:
            order_key, use_keyset, pk_col_name = self._get_order_info(schema_name, table_name)

        total_rows = 0

        if use_keyset:
            # keyset(cursor-based) 페이징: WHERE pk > last_value ORDER BY pk LIMIT N
            last_value = None
            while True:
                if last_value is None:
                    # 첫 번째 페이지: keyset 조건 없음
                    where_part = f' WHERE {where_clause}' if where_clause else ''
                    paged_query = (
                        f'{select_prefix} {col_str} FROM {from_clause}{where_part}'
                        f' ORDER BY {order_key} LIMIT {chunk_size}'
                    )
                    bind_params = []
                else:
                    # 이후 페이지: last_value 이후부터 조회
                    keyset_cond = f'{order_key} > ?'
                    if where_clause:
                        where_part = f' WHERE ({where_clause}) AND {keyset_cond}'
                    else:
                        where_part = f' WHERE {keyset_cond}'
                    paged_query = (
                        f'{select_prefix} {col_str} FROM {from_clause}{where_part}'
                        f' ORDER BY {order_key} LIMIT {chunk_size}'
                    )
                    bind_params = [last_value]

                # 청크 단위 재시도 (최대 3회, 지수 백오프)
                rows = None
                for attempt in range(3):
                    cursor = self.conn.cursor()
                    try:
                        if bind_params:
                            cursor.execute(paged_query, bind_params)
                        else:
                            cursor.execute(paged_query)
                        rows = cursor.fetchall()
                        cursor.close()
                        break  # 성공
                    except Exception as e:
                        cursor.close()
                        if attempt < 2:
                            logger.warning(
                                f"HANA 청크 조회 실패 (시도 {attempt + 1}/3): {e}"
                            )
                            time.sleep(2 ** attempt)
                            try:
                                self.connect()
                            except Exception:
                                pass
                        else:
                            raise  # 마지막 시도 실패 — 예외 전파

                if not rows:
                    break

                chunk_df = pd.DataFrame(rows, columns=col_names)
                fetched = len(chunk_df)
                total_rows += fetched

                # order_key 컬럼의 마지막 값을 다음 keyset 커서로 사용
                last_value = chunk_df[pk_col_name].iloc[-1]

                yield chunk_df

                # 마지막 페이지면 종료
                if fetched < chunk_size:
                    break

            logger.info(f"HANA {schema_name}.{table_name}: {total_rows:,}건 로드 (keyset 페이징)")

        else:
            # 기존 LIMIT/OFFSET 페이징 (복합 PK / PK 없음 / distinct=True)
            offset = 0
            while True:
                where_part = f' WHERE {where_clause}' if where_clause else ''
                paged_query = (
                    f'{select_prefix} {col_str} FROM {from_clause}{where_part}'
                    f' ORDER BY {order_key} LIMIT {chunk_size} OFFSET {offset}'
                )

                # 청크 단위 재시도 (최대 3회, 지수 백오프)
                rows = None
                for attempt in range(3):
                    cursor = self.conn.cursor()
                    try:
                        cursor.execute(paged_query)
                        rows = cursor.fetchall()
                        cursor.close()
                        break  # 성공
                    except Exception as e:
                        cursor.close()
                        if attempt < 2:
                            logger.warning(
                                f"HANA 청크 조회 실패 (시도 {attempt + 1}/3): {e}"
                            )
                            time.sleep(2 ** attempt)
                            try:
                                self.connect()
                            except Exception:
                                pass
                        else:
                            raise  # 마지막 시도 실패 — 예외 전파

                if not rows:
                    break

                chunk_df = pd.DataFrame(rows, columns=col_names)
                fetched = len(chunk_df)
                total_rows += fetched
                offset += fetched
                yield chunk_df

                # 마지막 페이지면 종료
                if fetched < chunk_size:
                    break

            logger.info(f"HANA {schema_name}.{table_name}: {total_rows:,}건 로드 (LIMIT/OFFSET 페이징)")

    def load_table_to_duckdb(self, hana_table, hana_schema, duckdb_storage,
                              duckdb_table, columns=None, where_clause=None,
                              chunk_size=None, progress_callback=None, force=True,
                              cohort_ids=None):
        # duckdb_table(내부 별칭) 기준으로 라우팅: 실제 HANA 테이블명과 무관하게 월별 추출 결정
        if duckdb_table.upper() in _MONTHLY_TABLES and where_clause is None:
            if columns is not None:
                logger.warning(
                    "load_table_to_duckdb: %s 월별 추출 경로에서 columns 인수 무시됨 "
                    "(MonthlyHanaExtractor는 전체 컬럼 추출)",
                    duckdb_table,
                )
            if chunk_size is not None:
                logger.warning(
                    "load_table_to_duckdb: %s 월별 추출 경로에서 chunk_size 인수 무시됨",
                    duckdb_table,
                )
            extractor = MonthlyHanaExtractor(
                self, duckdb_storage, hana_schema, _get_hana_cache_dir()
            )
            return extractor.extract_all_months(hana_table, duckdb_table, progress_callback,
                                                force=force, cohort_ids=cohort_ids)

        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        first_chunk = True
        total = 0

        # cohort_ids 적용: 900개 단위 IN절로 분할 후 각 파트별 개별 조회
        id_parts = _cohort_id_where_parts(cohort_ids)
        fetch_parts = id_parts if id_parts else [None]

        for id_part in fetch_parts:
            if id_part is not None:
                combined_where = (
                    f"({where_clause}) AND {id_part}" if where_clause else id_part
                )
            else:
                combined_where = where_clause

            for chunk_df in self.fetch_table_chunked(
                hana_table, hana_schema, columns, combined_where, chunk_size
            ):
                # DuckDB 적재 시 optimize_dtypes 사용 금지:
                # 청크별 min/max가 달라 첫 청크 기준 스키마와 이후 청크 값이 불일치
                # (예: 첫 청크 max=999999 → DECIMAL(6,0), 이후 값 1031900 → 범위 초과)
                # Integral Decimal은 적재 전에 넉넉한 DECIMAL(38,0)으로 고정한다.
                chunk_df = _prepare_chunk_for_duckdb(chunk_df)
                chunk_sql = _build_chunk_select_sql(chunk_df, '_temp_chunk')

                if first_chunk:
                    _validate_table_name(duckdb_table)
                    quoted_table = _quote_identifier(duckdb_table)
                    duckdb_storage.drop_table(duckdb_table)
                    duckdb_storage.conn.register('_temp_chunk', chunk_df)
                    try:
                        duckdb_storage.execute(f"CREATE TABLE {quoted_table} AS {chunk_sql}")
                    finally:
                        duckdb_storage.conn.unregister('_temp_chunk')
                    # 첫 청크 값이 작아 좁은 DECIMAL로 추론된 경우 DECIMAL(38,s)로 확장
                    _widen_decimal_columns(duckdb_storage, duckdb_table)
                    first_chunk = False
                else:
                    duckdb_storage.conn.register('_temp_chunk', chunk_df)
                    try:
                        duckdb_storage.execute(f"INSERT INTO {quoted_table} {chunk_sql}")
                    finally:
                        duckdb_storage.conn.unregister('_temp_chunk')

                total += len(chunk_df)
                # chunk_df 즉시 삭제 → Pandas 메모리 적층 방지
                del chunk_df
                gc.collect()

                # 메모리 상태 체크 → 위험 시 chunk 자동 축소
                chunk_controller.auto_adjust()

                if progress_callback:
                    _emit_chunk_progress(progress_callback, hana_table, total)

        table_up = duckdb_table.upper()
        if table_up == 'T20':
            _create_indexes_with_progress(duckdb_storage, duckdb_table,
                [['INDI_DSCM_NO'], ['CMN_KEY']], progress_callback=progress_callback)
        elif table_up == 'T30':
            _create_indexes_with_progress(duckdb_storage, duckdb_table,
                [['CMN_KEY', 'MCARE_DESC_LN_NO'], ['INDI_DSCM_NO']], progress_callback=progress_callback)
        elif table_up == 'T40':
            _create_indexes_with_progress(duckdb_storage, duckdb_table,
                [['CMN_KEY', 'SICK_DESC_SEQ_NO'], ['INDI_DSCM_NO']], progress_callback=progress_callback)
        elif table_up == 'T60':
            _create_indexes_with_progress(duckdb_storage, duckdb_table,
                [['CMN_KEY', 'MPRSC_GRANT_NO', 'MPRSC_SEQ_NO'], ['INDI_DSCM_NO']], progress_callback=progress_callback)
        elif table_up == 'JK':
            _create_indexes_with_progress(
                duckdb_storage, duckdb_table,
                [['INDI_DSCM_NO', 'STD_YYYY']],
                progress_callback=progress_callback
            )
        elif table_up == 'DEATH':
            _create_indexes_with_progress(
                duckdb_storage, duckdb_table,
                [['INDI_DSCM_NO']],
                progress_callback=progress_callback
            )
        elif table_up == 'GJ_RESULT':
            _create_indexes_with_progress(
                duckdb_storage, duckdb_table,
                [['INDI_DSCM_NO', 'HC_DT']],
                progress_callback=progress_callback
            )
        elif table_up == 'GJ_QUEST':
            _create_indexes_with_progress(
                duckdb_storage, duckdb_table,
                [['INDI_DSCM_NO', 'HC_BZ_YYYY']],
                progress_callback=progress_callback
            )

        logger.info(f"DuckDB 적재: {duckdb_table} ({total:,}건)")
        return total


def _get_hana_cache_dir():
    """HANA 월별 캐시 디렉토리 경로 반환.

    DUCKDB_SETTINGS['HANA_CACHE_DIR']가 None이면 _BASE_DIR / 'hana_cache' 사용.
    TEMP_DIRECTORY 처리 방식과 동일.
    """
    raw = DUCKDB_SETTINGS.get('HANA_CACHE_DIR')
    return Path(raw) if raw else _BASE_DIR / 'hana_cache'


class MonthlyHanaExtractor:
    """T20/T30/T40/T60 월별 분할 추출 → Parquet 저장 → DuckDB 병합.

    Args:
        hana_connector: HANAConnector 인스턴스 (fetch_table_chunked 사용)
        duckdb_storage: DuckDBStorage 인스턴스
        hana_schema: HANA 스키마 이름 (예: 'NHIS')
        cache_root: Parquet 캐시 루트 디렉토리 (예: Path('/app/hana_cache'))
    """

    def __init__(self, hana_connector, duckdb_storage, hana_schema, cache_root):
        self.hana = hana_connector
        self.storage = duckdb_storage
        self.schema = hana_schema
        self.cache_root = Path(cache_root)

    def _month_range(self):
        """STUDY_START_YEAR ~ STUDY_END_YEAR 범위의 YYYYMM 문자열 목록 반환."""
        from config import STUDY_SETTINGS
        start_year = int(STUDY_SETTINGS.get('STUDY_START_YEAR', 2013))
        end_year = int(STUDY_SETTINGS.get('STUDY_END_YEAR', 2024))
        if start_year > end_year:
            raise ValueError(
                f"STUDY_START_YEAR({start_year}) > STUDY_END_YEAR({end_year}): "
                "config.py 설정을 확인하세요."
            )
        months = []
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                months.append(f'{year:04d}{month:02d}')
        return months

    def extract_all_months(self, table_name, duckdb_table, progress_callback=None,
                           force=True, cohort_ids=None):
        """모든 월 추출 → Parquet 저장 → DuckDB 병합.

        Args:
            force: True(기본) → 기존 Parquet 전체 삭제 후 재추출.
                   False → 이미 존재하는 월 Parquet 파일 재사용(resume 모드).
            cohort_ids: frozenset[str] 또는 None.
                        지정 시 INDI_DSCM_NO를 해당 집합으로 한정하여 추출
                        (CohortIDExtractor로 선추출된 대상자만 적재).
        """
        table_upper = table_name.upper()
        cache_dir = self.cache_root / table_upper
        cache_dir.mkdir(parents=True, exist_ok=True)

        if force:
            # 전체 재추출: 기존 Parquet 삭제
            for f in cache_dir.glob(f'{table_upper}_*.parquet'):
                f.unlink()
        else:
            # resume 모드: 중단된 쓰기가 남긴 stale .tmp.parquet 정리
            for f in cache_dir.glob(f'{table_upper}_*.tmp.parquet'):
                f.unlink()

        months = self._month_range()
        total = len(months)
        parquet_files = []
        schema_columns = None

        import pyarrow as pa
        import pyarrow.parquet as pq

        # Fix C1: MDCARE_STRT_YYYYMM 컬럼 타입 감지 → INTEGER면 숫자 비교
        col_type = self.hana._detect_column_type(self.schema, table_name, _MONTHLY_FILTER_COL)
        use_int_where = col_type is not None and 'INT' in col_type.upper()
        if col_type is None:
            logger.warning(
                "%s: %s 컬럼 타입 감지 실패 — 문자열 비교로 폴백 "
                "(HANA 연결 전 호출 시 정상)",
                table_name, _MONTHLY_FILTER_COL
            )

        failed_months = []

        # resume 모드: 기존 캐시 스키마 유효성 검사 (컬럼 수 < 5 이면 stale로 판단)
        if not force:
            existing_cache = sorted(cache_dir.glob(f'{table_upper}_*.parquet'))
            if existing_cache:
                try:
                    first_schema = pq.read_schema(existing_cache[0])
                    if len(first_schema.names) < 5:
                        logger.warning(
                            "%s 캐시 스키마 불량 (%d개 컬럼) — stale 캐시 전체 삭제 후 재추출합니다.",
                            table_upper, len(first_schema.names)
                        )
                        for f in existing_cache:
                            f.unlink()
                except Exception as schema_e:
                    logger.warning(
                        "%s 캐시 스키마 읽기 실패 (%s) — stale 캐시 전체 삭제 후 재추출합니다.",
                        table_upper, schema_e
                    )
                    for f in cache_dir.glob(f'{table_upper}_*.parquet'):
                        f.unlink()

        for idx, yyyymm in enumerate(months, 1):
            parquet_path = cache_dir / f'{table_upper}_{yyyymm}.parquet'
            tmp_path = cache_dir / f'{table_upper}_{yyyymm}.tmp.parquet'

            # resume 모드: 유효한 Parquet 파일이 이미 존재하면 스킵
            if not force and parquet_path.exists() and parquet_path.stat().st_size > 0:
                parquet_files.append(parquet_path)
                _emit_progress(
                    progress_callback,
                    f"{table_upper} {yyyymm[:4]}-{yyyymm[4:]} 캐시 사용 ({idx}/{total})"
                )
                continue

            # Fix C1: 컬럼 타입에 따라 숫자/문자열 비교 분기
            if use_int_where:
                month_where = f"{_MONTHLY_FILTER_COL} = {int(yyyymm)}"
            else:
                month_where = f"{_MONTHLY_FILTER_COL} = '{yyyymm}'"

            _emit_progress(
                progress_callback,
                f"{table_upper} {yyyymm[:4]}-{yyyymm[4:]} 추출 중 ({idx}/{total})"
            )

            # cohort_ids가 있으면 INDI_DSCM_NO IN(...) 조건을 900개 단위 청크로 추가
            id_parts = _cohort_id_where_parts(cohort_ids)

            # 월별 청크를 PyArrow ParquetWriter로 스트리밍 저장 (메모리 효율)
            # Fix C7: try/finally로 ParquetWriter 안전 닫기
            writer = None
            month_rows = 0
            try:
                try:
                    fetch_parts = id_parts if id_parts else [None]
                    for id_part in fetch_parts:
                        where_clause = (
                            f"{month_where} AND {id_part}" if id_part else month_where
                        )
                        for chunk_df in self.hana.fetch_table_chunked(
                            table_name, self.schema,
                            where_clause=where_clause
                        ):
                            chunk_df = _prepare_chunk_for_duckdb(chunk_df)
                            arrow_table = pa.Table.from_pandas(chunk_df, preserve_index=False)
                            if writer is None:
                                writer = pq.ParquetWriter(str(tmp_path), arrow_table.schema)
                                if schema_columns is None:
                                    schema_columns = list(chunk_df.columns)
                            writer.write_table(arrow_table)
                            month_rows += len(chunk_df)
                            del chunk_df, arrow_table
                            gc.collect()
                except Exception as month_exc:
                    logger.warning(
                        "%s %s 월 추출 실패: %s", table_upper, yyyymm, month_exc
                    )
                    failed_months.append(yyyymm)
                    continue
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception as e:
                        logger.error("ParquetWriter close 실패 (%s %s): %s", table_upper, yyyymm, e)
                        tmp_path.unlink(missing_ok=True)

            if writer is None:
                if schema_columns is None:
                    # 스키마 미확정 상태의 0건 월: 파일 미생성, 병합 대상 제외
                    # (0컬럼 Parquet을 병합하면 DuckDB union_by_name 오류 발생)
                    logger.debug(
                        "%s %s: 스키마 미확정 빈 월 — 병합 대상 제외",
                        table_upper, yyyymm
                    )
                    continue
                # 스키마 확정 후 0건 월: 올바른 컬럼 구조의 빈 Parquet 저장
                empty_df = pd.DataFrame(columns=schema_columns)
                empty_df.to_parquet(str(tmp_path), index=False)

            tmp_path.replace(parquet_path)
            parquet_files.append(parquet_path)
            gc.collect()

        if failed_months:
            fail_rate = len(failed_months) / total
            if fail_rate >= 0.2:  # 20% 이상 실패 시 ERROR
                logger.error(
                    "extract_all_months: %d/%d 월 추출 실패 (실패율 %.0f%%) — %s",
                    len(failed_months), total, fail_rate * 100, failed_months
                )
            else:
                logger.warning(
                    "extract_all_months: %d/%d 월 추출 실패 — %s",
                    len(failed_months), total, failed_months
                )

        # Fix C5: 전체 0건 시 RuntimeError 발생 (빈 테이블 적재 방지)
        if schema_columns is None:
            raise RuntimeError(
                f"{table_upper}: 전체 {total}개월 데이터가 0건입니다. "
                f"MDCARE_STRT_YYYYMM 컬럼 타입 또는 HANA 접근 권한을 확인하세요. "
                f"(컬럼 타입 감지 결과: {col_type!r})"
            )

        # Parquet → DuckDB 병합 (단일 CREATE TABLE, union_by_name으로 컬럼 드리프트 대응)
        _emit_progress(progress_callback, f"{table_upper} DuckDB 병합 중...")
        self.storage.drop_table(duckdb_table)
        files_sql = '[' + ', '.join(f"'{p.as_posix()}'" for p in parquet_files) + ']'
        try:
            self.storage.execute(
                f"CREATE TABLE {duckdb_table} AS "
                f"SELECT * FROM read_parquet({files_sql}, union_by_name=true)"
            )
        except Exception as exc:
            logger.error(
                "월별 추출 DuckDB 병합 실패: %s — Parquet 파일은 %s 에 보존됨. "
                "재실행하면 복구됩니다.",
                exc, cache_dir,
            )
            raise

        total_rows = self.storage.get_row_count(duckdb_table)

        # Fix I3: 테이블별 복합 인덱스 생성
        table_up = duckdb_table.upper()
        if table_up == 'T20':
            _create_indexes_with_progress(self.storage, duckdb_table,
                [['INDI_DSCM_NO'], ['CMN_KEY']], progress_callback=progress_callback)
        elif table_up == 'T30':
            _create_indexes_with_progress(self.storage, duckdb_table,
                [['CMN_KEY', 'MCARE_DESC_LN_NO'], ['INDI_DSCM_NO']], progress_callback=progress_callback)
        elif table_up == 'T40':
            _create_indexes_with_progress(self.storage, duckdb_table,
                [['CMN_KEY', 'SICK_DESC_SEQ_NO'], ['INDI_DSCM_NO']], progress_callback=progress_callback)
        elif table_up == 'T60':
            _create_indexes_with_progress(self.storage, duckdb_table,
                [['CMN_KEY', 'MPRSC_GRANT_NO', 'MPRSC_SEQ_NO'], ['INDI_DSCM_NO']], progress_callback=progress_callback)

        logger.info(f"월별 추출 완료: {duckdb_table} ({total_rows:,}건, {total}개월)")
        return total_rows


# ---------------------------------------------------------------------------
# 코호트 ID 필터 헬퍼
# ---------------------------------------------------------------------------
_DM_CODES = ('E10', 'E11', 'E12', 'E13', 'E14')
_SICK_SYM_COLS = ('SICK_SYM1', 'SICK_SYM2', 'SICK_SYM3', 'SICK_SYM4', 'SICK_SYM5')
_COHORT_ID_CHUNK_SIZE = 900  # HANA IN 절 안전 상한


def _cohort_id_where_parts(cohort_ids):
    """cohort_ids를 _COHORT_ID_CHUNK_SIZE 단위로 나눈 IN-절 문자열 목록 반환.

    각 원소는 단독으로 AND 조건에 추가 가능한 문자열.
    cohort_ids가 None이거나 비어 있으면 빈 리스트 반환.
    """
    if not cohort_ids:
        return []
    ids = sorted(cohort_ids)
    parts = []
    for i in range(0, len(ids), _COHORT_ID_CHUNK_SIZE):
        chunk = ids[i:i + _COHORT_ID_CHUNK_SIZE]
        # 환자 식별자는 반드시 숫자여야 함 — SQL 인젝션 및 데이터 무결성 방지
        validated = []
        for _id in chunk:
            id_str = str(_id)
            if not re.match(r'^\d+$', id_str):
                raise ValueError(f"유효하지 않은 INDI_DSCM_NO: {_id!r}")
            validated.append(id_str)
        quoted = ', '.join(f"'{_id}'" for _id in validated)
        parts.append(f"INDI_DSCM_NO IN ({quoted})")
    return parts


def _build_cohort_id_sql(
    enroll_start,
    enroll_end,
    *,
    t20_schema,
    t20_table,
    t20_monthly_col='MDCARE_STRT_YYYYMM',
    t20_monthly_is_int=False,
    form_cd_list=('02', '03', '07', '08', '09', '10', '11', '15'),
    pay_yn='1',
    use_hhdv=True,
    hhdv_schema=None,
    hhdv_table=None,
    hhdv_std_col='STD_YYYYMM',
    hhdv_std_is_monthly=True,
    hhdv_byear_col='BYEAR',
    hhdv_gaibja_types=('1', '2', '5', '6', '7', '8'),
    min_age=40,
    max_age=64,
):
    """CohortIDExtractor용 단일 HANA SQL 빌더.

    반환 SQL은 T20(DM 상병) × HHDV(연령·자격) 서버측 JOIN으로 진입기간 전체의
    코호트 INDI_DSCM_NO 집합(DISTINCT)을 한 번에 산출한다. fetch_sql_keyset 과
    조합되면 월별 LIMIT/OFFSET 스캔을 키셋 기반 단일 쿼리로 대체한다.

    Args:
        enroll_start / enroll_end: 진입기간 연도(포함). YYYY(4자리) 또는 YYYYMM(6자리) 형식.
            YYYY → YYYYMM01/12로 자동 변환. YYYYMM → 직접 사용.
        t20_*: T20 스키마·테이블명과 MDCARE_STRT_YYYYMM 컬럼 타입(INT/VARCHAR).
        form_cd_list / pay_yn: T20 청구 필터.
        use_hhdv: True 면 HHDV 와 JOIN 하여 연령·자격 필터 적용.
        hhdv_*: HHDV 스키마·테이블·컬럼명. hhdv_std_is_monthly=True 는 STD_YYYYMM(6자리),
                False 는 STD_YYYY(4자리).
        min_age / max_age: 연령 하한·상한(포함).

    Returns:
        str: ORDER BY / LIMIT 미포함 SELECT DISTINCT INDI_DSCM_NO SQL.

    Notes:
        - SICK_SYM1~5 의 DM 코드(E10~E14) 비교는 NULL/공백 안전한
          ``LEFT(TRIM(col), 3) IN (...)`` 형태로 구성된다.
        - 식별자는 모두 ``"``로 quote하여 예약어 충돌을 방지한다.
    """
    # enroll_start/enroll_end: YYYY(4자리) 또는 YYYYMM(6자리) 형식 지원
    enroll_start_str = str(enroll_start).strip()
    enroll_end_str = str(enroll_end).strip()
    if len(enroll_start_str) == 6:  # YYYYMM 직접 입력
        month_lo = enroll_start_str
        month_hi = enroll_end_str
    elif len(enroll_start_str) == 4:  # YYYY → YYYYMM01/12 변환
        month_lo = f"{enroll_start_str}01"
        month_hi = f"{enroll_end_str}12"
    else:
        raise ValueError(
            f"enroll_start/enroll_end는 YYYY(4자리) 또는 YYYYMM(6자리) 형식이어야 합니다: "
            f"start={enroll_start_str}, end={enroll_end_str}"
        )
    if int(month_lo) > int(month_hi):
        raise ValueError(
            f"month_lo({month_lo}) > month_hi({month_hi})"
        )
    # 식별자 검증 (_VALID_TABLE_RE와 동일 규칙)
    for ident in (t20_schema, t20_table, t20_monthly_col, hhdv_byear_col,
                  hhdv_std_col):
        if ident and not _VALID_TABLE_RE.match(str(ident)):
            raise ValueError(f"유효하지 않은 식별자: {ident!r}")
    if use_hhdv:
        for ident in (hhdv_schema, hhdv_table):
            if ident and not _VALID_TABLE_RE.match(str(ident)):
                raise ValueError(f"유효하지 않은 HHDV 식별자: {ident!r}")

    # T20 월 경계 — 타입에 따라 리터럴 형태 결정
    if t20_monthly_is_int:
        month_clause = (
            f't."{t20_monthly_col}" BETWEEN {int(month_lo)} AND {int(month_hi)}'
        )
    else:
        month_clause = (
            f't."{t20_monthly_col}" BETWEEN \'{month_lo}\' AND \'{month_hi}\''
        )

    form_cd_sql = ', '.join(f"'{c}'" for c in form_cd_list)
    dm_codes_sql = ', '.join(f"'{c}'" for c in _DM_CODES)
    sick_conditions = ' OR '.join(
        f'LEFT(TRIM(t."{col}"), 3) IN ({dm_codes_sql})'
        for col in _SICK_SYM_COLS
    )

    t20_where = (
        f'{month_clause} '
        f"AND t.\"PAY_YN\" = '{pay_yn}' "
        f'AND t."FORM_CD" IN ({form_cd_sql}) '
        f'AND t."INDI_DSCM_NO" <> 0 '
        f'AND t."INDI_DSCM_NO" IS NOT NULL '
        f'AND t."INDI_DSCM_NO" <= 99999999 '
        f'AND ({sick_conditions})'
    )

    if not use_hhdv:
        return (
            f'SELECT DISTINCT t."INDI_DSCM_NO" AS "INDI_DSCM_NO" '
            f'FROM "{t20_schema}"."{t20_table}" t '
            f'WHERE {t20_where}'
        )

    # HHDV JOIN 구성 ----------------------------------------------------
    gaibja_sql = ', '.join(f"'{t}'" for t in hhdv_gaibja_types)

    # 월 경계 조건 — HHDV 측도 진입기간으로 압축(성능 힌트)
    if hhdv_std_is_monthly:
        hhdv_month_clause = (
            f'a."{hhdv_std_col}" BETWEEN \'{month_lo}\' AND \'{month_hi}\''
        )
        age_year_expr = f'CAST(SUBSTR(a."{hhdv_std_col}", 1, 4) AS INT)'
        join_on_month = (
            f'a."{hhdv_std_col}" = t."{t20_monthly_col}"'
        )
    else:
        year_lo = enroll_start_str[:4]  # YYYYMM/YYYY → 첫 4자리 = 연도
        year_hi = enroll_end_str[:4]
        hhdv_month_clause = (
            f'a."{hhdv_std_col}" BETWEEN \'{year_lo}\' AND \'{year_hi}\''
        )
        age_year_expr = f'CAST(a."{hhdv_std_col}" AS INT)'
        # 연단위 HHDV 와 월단위 T20 JOIN — 연도 substring 매칭
        if t20_monthly_is_int:
            join_on_month = (
                f'a."{hhdv_std_col}" = '
                f'SUBSTR(LPAD(CAST(t."{t20_monthly_col}" AS VARCHAR), 6, \'0\'), 1, 4)'
            )
        else:
            join_on_month = (
                f'a."{hhdv_std_col}" = SUBSTR(t."{t20_monthly_col}", 1, 4)'
            )

    age_range_expr = (
        f'({age_year_expr} - CAST(a."{hhdv_byear_col}" AS INT)) '
        f'BETWEEN {int(min_age)} AND {int(max_age)}'
    )

    return (
        f'SELECT DISTINCT t."INDI_DSCM_NO" AS "INDI_DSCM_NO" '
        f'FROM "{t20_schema}"."{t20_table}" t '
        f'INNER JOIN "{hhdv_schema}"."{hhdv_table}" a '
        f'ON {join_on_month} '
        f'AND a."INDI_DSCM_NO" = t."INDI_DSCM_NO" '
        f'WHERE {t20_where} '
        f'AND {hhdv_month_clause} '
        f'AND {age_range_expr} '
        f'AND a."GAIBJA_TYPE" IN ({gaibja_sql}) '
        f'AND a."SEX_TYPE" IN (\'1\', \'2\') '
        f'AND a."INDI_DSCM_NO" <> 0 '
        f'AND a."INDI_DSCM_NO" IS NOT NULL '
        f'AND a."INDI_DSCM_NO" <= 99999999'
    )


class CohortIDExtractor:
    """진입기간 내 연령+DM 코드 조건 충족 INDI_DSCM_NO를 월별로 추출해 DISTINCT 집합 반환.

    흐름:
      ① 진입기간(ENROLLMENT_START~END) 모든 YYYYMM 순회
      ② 각 월: HHDT_POPULATION_MM(연령조건) ∩ T20(E10~E14 상병조건) → 교집합을 set에 누적
      ③ 전체 누적 set → cohort_ids.parquet 캐시 (resume 지원)

    중복 처리:
      - HANA 조회 시 SELECT DISTINCT 적용 (전송량 절감)
      - Python set() 구조로 월 간·청크 간 INDI_DSCM_NO 중복 자동 제거
      - 캐시 저장: sorted(set) → parquet INDI_DSCM_NO 컬럼 DISTINCT 보장

    Args:
        hana_connector: HANAConnector 인스턴스
        hana_schema: HANA 스키마 (예: 'NHIS')
        cache_root: Parquet 캐시 루트 디렉토리
    """

    def __init__(self, hana_connector, hana_schema, cache_root):
        self.hana = hana_connector
        self.schema = hana_schema
        self.cache_root = Path(cache_root)

    def _enrollment_month_range(self):
        """ENROLLMENT_START ~ ENROLLMENT_END 범위의 YYYYMM 문자열 목록 반환."""
        from config import STUDY_SETTINGS
        start_year = int(STUDY_SETTINGS.get('ENROLLMENT_START', 2013))
        end_year = int(STUDY_SETTINGS.get('ENROLLMENT_END', 2016))
        if start_year > end_year:
            raise ValueError(
                f"ENROLLMENT_START({start_year}) > ENROLLMENT_END({end_year}): "
                "config.py 설정을 확인하세요."
            )
        months = []
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                months.append(f'{year:04d}{month:02d}')
        return months

    def cache_path(self):
        return self.cache_root / 'cohort_ids.parquet'

    def _run_single_sql(self, enroll_start, enroll_end, sql_kwargs,
                        progress_callback=None, label=None):
        """단일 SQL 실행 후 INDI_DSCM_NO set 반환 (keyset 페이징)."""
        base_sql = _build_cohort_id_sql(
            enroll_start=enroll_start,
            enroll_end=enroll_end,
            **sql_kwargs,
        )
        ids: set = set()
        _emit_progress(
            progress_callback,
            f"코호트 ID 추출 중 ({label or f'{enroll_start}~{enroll_end}'})..."
        )
        for chunk_df in self.hana.fetch_sql_keyset(
            base_sql, key_col='INDI_DSCM_NO',
        ):
            ids.update(chunk_df['INDI_DSCM_NO'].astype(str).tolist())
            del chunk_df
            gc.collect()
        return ids

    def extract(self, force=True, progress_callback=None):
        """코호트 INDI_DSCM_NO를 분기별(3개월) 배치로 추출해 frozenset 반환.

        흐름:
          ① resume 모드(force=False) + 캐시 존재 → parquet 로드
          ② 월별 목록 생성 → 3개월씩 배치로 분할
          ③ 각 배치별 HANA SQL 실행 (YYYYMM 범위)
          ④ 개별 배치 실패 → 스킵(경고) + 계속 추출
          ⑤ 연속 3회 배치 실패 → RuntimeError (조기 중단)
          ⑥ 결과를 cohort_ids.parquet 으로 저장

        Args:
            force: True → 기존 캐시 무시하고 재추출.
                   False → 캐시 존재 시 로드만 수행(resume).
        Returns:
            frozenset[str]: 조건 충족 INDI_DSCM_NO 집합
        """
        self.cache_root.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_path()

        # resume 모드: 캐시 파일이 있으면 로드
        if not force and cache_file.exists() and cache_file.stat().st_size > 0:
            _emit_progress(progress_callback, "코호트 ID 캐시 로드 중...")
            df_cache = pd.read_parquet(str(cache_file))
            ids = frozenset(df_cache['INDI_DSCM_NO'].astype(str).tolist())
            _emit_progress(progress_callback, f"코호트 ID 캐시 로드 완료: {len(ids):,}명")
            logger.info("CohortIDExtractor: 캐시 로드 %d명", len(ids))
            return ids

        from config import STUDY_SETTINGS
        use_hhdv = bool(STUDY_SETTINGS.get('COHORT_USE_HHDV', False))
        t20_schema = STUDY_SETTINGS.get('T20_SCHEMA') or self.schema
        form_cd_list = STUDY_SETTINGS.get(
            'T20_FORM_CD', ('02', '03', '07', '08', '09', '10', '11', '15')
        )
        pay_yn = STUDY_SETTINGS.get('T20_PAY_YN', '1')

        enroll_start = int(STUDY_SETTINGS.get('ENROLLMENT_START', 2013))
        enroll_end = int(STUDY_SETTINGS.get('ENROLLMENT_END', 2016))
        if enroll_start > enroll_end:
            raise ValueError(
                f"ENROLLMENT_START({enroll_start}) > ENROLLMENT_END({enroll_end}): "
                "config.py 설정을 확인하세요."
            )

        # T20 실제 HANA 테이블명 + 월 컬럼 타입 감지
        t20_hana_table = _resolve_hana_table('T20')
        t20_col_type = self.hana._detect_column_type(
            t20_schema, t20_hana_table, _MONTHLY_FILTER_COL
        )
        t20_int_where = (
            t20_col_type is not None and 'INT' in t20_col_type.upper()
        )

        # SQL 빌더 공통 kwargs
        sql_kwargs = dict(
            t20_schema=t20_schema,
            t20_table=t20_hana_table,
            t20_monthly_col=_MONTHLY_FILTER_COL,
            t20_monthly_is_int=t20_int_where,
            form_cd_list=tuple(form_cd_list),
            pay_yn=pay_yn,
            use_hhdv=use_hhdv,
        )

        if use_hhdv:
            hhdv_alias = STUDY_SETTINGS.get('HHDV_TABLE', 'HHDT_POPULATION_MM')
            hhdv_table = _resolve_hana_table(hhdv_alias)
            std_yyyy_col = STUDY_SETTINGS.get('HHDV_STD_YYYY_COL', 'STD_YYYYMM')
            hhdv_std_is_monthly = std_yyyy_col.upper().endswith('MM')
            byear_col = STUDY_SETTINGS.get('HHDV_BYEAR_COL', 'BYEAR')
            hhdv_schema = STUDY_SETTINGS.get('HHDV_SCHEMA') or self.schema
            gaibja_types = tuple(STUDY_SETTINGS.get(
                'HHDV_GAIBJA_TYPES', ('1', '2', '5', '6', '7', '8'),
            ))
            min_age = int(STUDY_SETTINGS.get('MIN_AGE', 40))
            max_age = int(STUDY_SETTINGS.get('MAX_AGE', 64))
            sql_kwargs.update(
                hhdv_schema=hhdv_schema,
                hhdv_table=hhdv_table,
                hhdv_std_col=std_yyyy_col,
                hhdv_std_is_monthly=hhdv_std_is_monthly,
                hhdv_byear_col=byear_col,
                hhdv_gaibja_types=gaibja_types,
                min_age=min_age,
                max_age=max_age,
            )
        else:
            _emit_progress(
                progress_callback,
                "[코호트ID] HHDV 단계 스킵 — NHISBASE.TBGJME20 단독 추출"
            )
            logger.info("CohortIDExtractor: COHORT_USE_HHDV=False, T20 단독 모드")

        # ── ① 월별 목록 생성 → 3개월 배치 분할 ────────────────────────────
        months = self._enrollment_month_range()
        _emit_progress(
            progress_callback,
            f"[코호트ID] 진입기간 {months[0][:4]}-{months[0][4:]}~{months[-1][:4]}-{months[-1][4:]} ({len(months)}개월) 분기별 추출 시작"
        )
        logger.info("CohortIDExtractor: 월별 배치 추출 시작, 총 %d개월", len(months))

        cohort_set: set = set()
        failed_batches = []
        consecutive_failures = 0
        _MAX_CONSECUTIVE_FAILURES = 3

        # ── ② 3개월씩 배치 처리 ───────────────────────────────────────
        BATCH_MONTHS = 3
        for batch_idx in range(0, len(months), BATCH_MONTHS):
            batch_months = months[batch_idx:batch_idx + BATCH_MONTHS]
            batch_start = batch_months[0]  # YYYYMM
            batch_end = batch_months[-1]    # YYYYMM
            batch_label = f"{batch_start[:4]}-{batch_start[4:]}~{batch_end[:4]}-{batch_end[4:]}"

            try:
                batch_ids = self._run_single_sql(
                    batch_start, batch_end, sql_kwargs,
                    progress_callback=progress_callback,
                    label=batch_label,
                )
                cohort_set.update(batch_ids)
                consecutive_failures = 0  # 성공 시 카운터 리셋
                _emit_progress(
                    progress_callback,
                    f"[코호트ID] {batch_label} 완료 — 누적 {len(cohort_set):,}명"
                )
            except Exception as batch_exc:
                consecutive_failures += 1
                failed_batches.append((batch_label, str(batch_exc)))
                logger.warning(
                    "CohortIDExtractor: 배치 추출 실패 [%s] (%s/%d) — %s",
                    batch_label, consecutive_failures, _MAX_CONSECUTIVE_FAILURES, batch_exc,
                )
                _emit_progress(
                    progress_callback,
                    f"[코호트ID] {batch_label} 추출 실패 ({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}) — 계속 진행"
                )

                # 연속 3회 실패 → 조기 중단
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "CohortIDExtractor: 연속 %d회 배치 실패, 조기 중단",
                        _MAX_CONSECUTIVE_FAILURES,
                    )
                    raise RuntimeError(
                        f"CohortIDExtractor: 연속 {_MAX_CONSECUTIVE_FAILURES}개 배치 추출 실패, 조기 중단\n"
                        f"마지막 실패: {batch_label} — {batch_exc}\n"
                        f"HANA 연결 상태 및 권한을 확인하세요."
                    )

        # 배치 실패율 판정
        if failed_batches:
            total_batches = (len(months) + BATCH_MONTHS - 1) // BATCH_MONTHS
            failure_rate = len(failed_batches) / total_batches * 100
            failure_summary = '\n'.join(
                f"  • {label}: {err[:100]}" for label, err in failed_batches[:5]
            )
            if failure_rate >= 20:
                logger.error(
                    "CohortIDExtractor: 배치 실패율 %.1f%% (경고: 코호트 데이터 손실 위험)\n%s",
                    failure_rate, failure_summary
                )
                _emit_progress(
                    progress_callback,
                    f"[경고] 배치 실패율 {failure_rate:.1f}% — 결과 검증 필수"
                )
            else:
                logger.warning(
                    "CohortIDExtractor: 배치 실패율 %.1f%% (부분 손실)\n%s",
                    failure_rate, failure_summary
                )
                _emit_progress(
                    progress_callback,
                    f"[경고] 배치 실패율 {failure_rate:.1f}% — 일부 기간 데이터 미포함"
                )

        if not cohort_set:
            mode = "HHDV+T20" if use_hhdv else "T20(NHISBASE.TBGJME20) 단독"
            raise RuntimeError(
                f"CohortIDExtractor: 조건을 만족하는 환자가 없습니다. [{mode} 모드]\n"
                f"진입기간({enroll_start}~{enroll_end}), "
                f"T20 스키마({t20_schema}), 테이블({t20_hana_table}) 접근 권한을 확인하세요."
            )

        # 캐시 저장
        _emit_progress(progress_callback, f"코호트 ID 저장 중 ({len(cohort_set):,}명)...")
        pd.DataFrame({'INDI_DSCM_NO': sorted(cohort_set)}).to_parquet(
            str(cache_file), index=False
        )
        result = frozenset(cohort_set)
        _emit_progress(progress_callback, f"코호트 ID 추출 완료: {len(result):,}명")
        logger.info("CohortIDExtractor: 완료 %d명 → %s", len(result), cache_file)
        return result


class SASFileLoader:
    """SAS/CSV 파일 로더 (메모리 안전)"""

    def __init__(self):
        self.chunk_size = chunk_controller.get_chunk('sas')

    def load_sas_to_duckdb(self, sas_path, duckdb_storage, table_name,
                           columns=None, progress_callback=None):
        _validate_table_name(table_name)
        import pyreadstat
        sas_path = Path(sas_path)
        if not sas_path.exists():
            raise FileNotFoundError(f"SAS 파일 없음: {sas_path}")

        duckdb_storage.drop_table(table_name)
        first_chunk = True
        total = 0
        current_chunk = chunk_controller.get_chunk('sas')

        reader = pyreadstat.read_file_in_chunks(
            pyreadstat.read_sas7bdat, str(sas_path),
            chunksize=current_chunk, usecols=columns,
        )

        for chunk_df, meta in reader:
            # Integral Decimal은 적재 전에 넉넉한 DECIMAL(38,0)으로 고정한다.
            chunk_df = _prepare_chunk_for_duckdb(chunk_df)
            chunk_sql = _build_chunk_select_sql(chunk_df, '_temp_sas')

            if first_chunk:
                duckdb_storage.conn.register('_temp_sas', chunk_df)
                try:
                    duckdb_storage.execute(f"CREATE TABLE {table_name} AS {chunk_sql}")
                finally:
                    duckdb_storage.conn.unregister('_temp_sas')
                # 첫 청크 값이 작아 좁은 DECIMAL로 추론된 경우 DECIMAL(38,s)로 확장
                _widen_decimal_columns(duckdb_storage, table_name)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_sas', chunk_df)
                try:
                    duckdb_storage.execute(f"INSERT INTO {table_name} {chunk_sql}")
                finally:
                    duckdb_storage.conn.unregister('_temp_sas')

            total += len(chunk_df)
            del chunk_df
            gc.collect()

            # 메모리 자동 체크 & chunk 조절
            chunk_controller.auto_adjust()

            if progress_callback:
                _emit_chunk_progress(progress_callback, table_name, total)

        if table_name.upper() in _MONTHLY_TABLES:
            _create_indexes_with_progress(
                duckdb_storage, table_name,
                [['INDI_DSCM_NO']],
                progress_callback=progress_callback
            )
        elif table_name.upper() == 'JK':
            _create_indexes_with_progress(
                duckdb_storage, table_name,
                [['INDI_DSCM_NO', 'STD_YYYY']],
                progress_callback=progress_callback
            )

        logger.info(f"SAS → DuckDB: {table_name} ({total:,}건)")
        return total

    def load_csv_chunked_to_duckdb(self, csv_path, duckdb_storage, table_name,
                                    delimiter=',', is_append=False, progress_callback=None):
        """메모리 안전 CSV 로드 — pandas 청크 리더 사용 (대용량 5GB+ 파일 대응)"""
        _validate_table_name(table_name)
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV 파일 없음: {csv_path}")
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ValueError(f"delimiter는 단일 문자여야 합니다: {delimiter!r}")

        if not is_append:
            duckdb_storage.drop_table(table_name)

        first_chunk = not is_append and not duckdb_storage.table_exists(table_name)
        total = 0
        current_chunk = chunk_controller.get_chunk('csv')

        for chunk_df in pd.read_csv(str(csv_path), delimiter=delimiter,
                                     chunksize=current_chunk, low_memory=False,
                                     dtype=str):  # dtype=str prevents mixed type issues
            chunk_df = _prepare_chunk_for_duckdb(chunk_df)
            chunk_sql = _build_chunk_select_sql(chunk_df, '_temp_csv')

            if first_chunk:
                duckdb_storage.conn.register('_temp_csv', chunk_df)
                try:
                    duckdb_storage.execute(f"CREATE TABLE {table_name} AS {chunk_sql}")
                finally:
                    duckdb_storage.conn.unregister('_temp_csv')
                _widen_decimal_columns(duckdb_storage, table_name)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_csv', chunk_df)
                try:
                    duckdb_storage.execute(f"INSERT INTO {table_name} {chunk_sql}")
                finally:
                    duckdb_storage.conn.unregister('_temp_csv')

            total += len(chunk_df)
            del chunk_df
            gc.collect()
            chunk_controller.auto_adjust()

            if progress_callback:
                _emit_chunk_progress(progress_callback, table_name, total)

        return total

    def load_csv_to_duckdb(self, csv_path, duckdb_storage, table_name,
                           delimiter=',', progress_callback=None):
        """CSV → DuckDB (메모리 안전 청크 로드)"""
        count = self.load_csv_chunked_to_duckdb(
            csv_path, duckdb_storage, table_name,
            delimiter=delimiter, is_append=False,
            progress_callback=progress_callback
        )
        # Create indexes
        if table_name.upper() in _MONTHLY_TABLES:
            _create_indexes_with_progress(
                duckdb_storage, table_name,
                [['INDI_DSCM_NO']],
                progress_callback=progress_callback
            )
        elif table_name.upper() == 'JK':
            _create_indexes_with_progress(
                duckdb_storage, table_name,
                [['INDI_DSCM_NO', 'STD_YYYY']],
                progress_callback=progress_callback
            )
        logger.info(f"CSV → DuckDB: {table_name} ({count:,}건)")
        return count

    def _load_sas_append(self, sas_path, duckdb_storage, table_name,
                         columns=None, is_append=False, progress_callback=None):
        """SAS 파일 1개를 DuckDB에 로드 (append 모드 지원)"""
        import pyreadstat
        sas_path = Path(sas_path)
        if not sas_path.exists():
            raise FileNotFoundError(f"SAS 파일 없음: {sas_path}")

        if not is_append:
            duckdb_storage.drop_table(table_name)

        first_chunk = not is_append and not duckdb_storage.table_exists(table_name)
        total = 0
        current_chunk = chunk_controller.get_chunk('sas')

        reader = pyreadstat.read_file_in_chunks(
            pyreadstat.read_sas7bdat, str(sas_path),
            chunksize=current_chunk, usecols=columns,
        )

        for chunk_df, meta in reader:
            chunk_df = _prepare_chunk_for_duckdb(chunk_df)
            chunk_sql = _build_chunk_select_sql(chunk_df, '_temp_sas')

            if first_chunk:
                duckdb_storage.conn.register('_temp_sas', chunk_df)
                try:
                    duckdb_storage.execute(f"CREATE TABLE {table_name} AS {chunk_sql}")
                finally:
                    duckdb_storage.conn.unregister('_temp_sas')
                # 첫 청크 값이 작아 좁은 DECIMAL로 추론된 경우 DECIMAL(38,s)로 확장
                _widen_decimal_columns(duckdb_storage, table_name)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_sas', chunk_df)
                try:
                    duckdb_storage.execute(f"INSERT INTO {table_name} {chunk_sql}")
                finally:
                    duckdb_storage.conn.unregister('_temp_sas')

            total += len(chunk_df)
            del chunk_df
            gc.collect()
            chunk_controller.auto_adjust()

            if progress_callback:
                _emit_chunk_progress(progress_callback, table_name, total)

        return total

    def load_multi_files_to_duckdb(self, file_paths, file_type, duckdb_storage, table_name,
                                    delimiter=',', columns=None, progress_callback=None):
        """다중 분할 파일을 하나의 DuckDB 테이블로 병합 로드

        Args:
            file_paths: list of file paths (CSV or SAS)
            file_type: 'csv' or 'sas'
            duckdb_storage: DuckDBStorage instance
            table_name: target DuckDB table name
            delimiter: CSV delimiter (ignored for SAS)
            columns: column filter (SAS only)
            progress_callback: fn(total_rows, current_file_name)
        Returns:
            total row count
        """
        _validate_table_name(table_name)
        if not file_paths:
            raise ValueError("로드할 파일이 없습니다")

        # Sort files by name for deterministic order
        file_paths = sorted(file_paths)

        grand_total = 0
        duckdb_storage.drop_table(table_name)

        for i, fpath in enumerate(file_paths):
            fpath = Path(fpath)
            fname = fpath.name
            is_append = (i > 0)  # first file creates table, rest append

            if progress_callback:
                progress_callback(f"[{i+1}/{len(file_paths)}] {fname} 로드 중...")

            logger.info(f"분할 파일 로드 [{i+1}/{len(file_paths)}]: {fname}")

            if file_type == 'csv':
                count = self.load_csv_chunked_to_duckdb(
                    fpath, duckdb_storage, table_name,
                    delimiter=delimiter, is_append=is_append,
                    progress_callback=progress_callback
                )
            elif file_type == 'sas':
                # For SAS, reuse existing chunked loader but with append mode
                count = self._load_sas_append(
                    fpath, duckdb_storage, table_name,
                    columns=columns, is_append=is_append,
                    progress_callback=progress_callback
                )
            else:
                raise ValueError(f"지원하지 않는 파일 유형: {file_type}")

            grand_total += count
            logger.info(f"  → {fname}: {count:,}건 (누적: {grand_total:,}건)")
            _emit_progress(progress_callback, f"{table_name}: {fname} 완료 ({grand_total:,}건 누적)")

            # Inter-file memory cleanup
            mem_manager.force_cleanup()

        # Create indexes after all files loaded
        if table_name.upper() in _MONTHLY_TABLES:
            _create_indexes_with_progress(
                duckdb_storage, table_name,
                [['INDI_DSCM_NO'], ['CMN_KEY']],
                progress_callback=progress_callback
            )
        elif table_name.upper() == 'JK':
            _create_indexes_with_progress(
                duckdb_storage, table_name,
                [['INDI_DSCM_NO', 'STD_YYYY']],
                progress_callback=progress_callback
            )

        logger.info(f"다중 파일 병합 완료: {table_name} ({grand_total:,}건, {len(file_paths)}개 파일)")
        return grand_total


class MonthlyJKExtractor:
    """JK 자격DB 월별 분할 추출 — HHDT_POPULATION_MM + HHDT_DSES_YY BFC 패턴 조인.

    BFC_MONTHLY SAS 매크로와 동일한 로직:
      - HHDT_POPULATION_MM : 월별 자격 (STD_YYYYMM, SEX_TYPE, BYEAR, GAIBJA_TYPE, RVSN_ADDR_CD)
      - HHDT_DSES_YY       : 연별 소득/재산 (FOREIGNER_Y, SES05, CALC_CTRB_VTILE_FD, SURV_YR)
    결과 JK 테이블 보장 컬럼:
      STD_YYYYMM, STD_YYYY(파생), INDI_DSCM_NO, SEX_TYPE, BYEAR, GAIBJA_TYPE,
      RVSN_ADDR_CD, FOREIGNER_Y, SES05, CALC_CTRB_VTILE_FD, SURV_YR
    cohort_builder 호환: STD_YYYY(4자리), SURV_YR, HHDT_DEATH(NULL)

    Args:
        hana_connector: HANAConnector 인스턴스
        duckdb_storage: DuckDBStorage 인스턴스
        cache_root: Parquet 캐시 루트 (예: Path('/app/hana_cache'))
        pop_schema: HHDT_POPULATION_MM 스키마
        pop_table: 월별 자격 테이블명 (기본 'HHDT_POPULATION_MM')
        dses_schema: HHDT_DSES_YY 스키마
        dses_table: 연별 소득/재산 테이블명 (기본 'HHDT_DSES_YY')
    """

    def __init__(self, hana_connector, duckdb_storage, cache_root,
                 pop_schema='NHISBDA', pop_table='HHDT_POPULATION_MM',
                 dses_schema='NHISBDA', dses_table='HHDT_DSES_YY'):
        # 식별자 검증 — UI/설정에서 주입되는 schema/table 이름으로 인한 SQL 인젝션 차단.
        # _build_join_sql 이 f-string 으로 직접 삽입하므로 여기서 정규식으로 보증.
        for _label, _ident in (
            ('pop_schema', pop_schema), ('pop_table', pop_table),
            ('dses_schema', dses_schema), ('dses_table', dses_table),
        ):
            if not _VALID_TABLE_RE.match(str(_ident)):
                raise ValueError(
                    f"MonthlyJKExtractor: 유효하지 않은 {_label} 식별자: {_ident!r}"
                )
        self.hana = hana_connector
        self.storage = duckdb_storage
        self.cache_root = Path(cache_root)
        self.pop_schema = pop_schema
        self.pop_table = pop_table
        self.dses_schema = dses_schema
        self.dses_table = dses_table

    def _month_range(self):
        """STUDY_SETTINGS 기반 YYYYMM 목록 반환."""
        from config import STUDY_SETTINGS
        sy = int(STUDY_SETTINGS['STUDY_START_YEAR'])
        ey = int(STUDY_SETTINGS['STUDY_END_YEAR'])
        months = []
        for y in range(sy, ey + 1):
            for m in range(1, 13):
                months.append(f"{y}{m:02d}")
        return months

    def _build_join_sql(self, yyyymm, cohort_ids=None):
        """월별 JK JOIN SQL 생성 (ORDER BY / LIMIT / OFFSET 미포함)."""
        if not re.match(r'^\d{6}$', str(yyyymm)):
            raise ValueError(f"_build_join_sql: 유효하지 않은 yyyymm: {yyyymm!r}")
        pop = f'"{self.pop_schema}"."{self.pop_table}"'
        dses = f'"{self.dses_schema}"."{self.dses_table}"'
        base_cond = (
            f"B.STD_YYYYMM = '{yyyymm}' "
            f"AND B.INDI_DSCM_NO <> 0 "
            f"AND B.INDI_DSCM_NO IS NOT NULL "
            f"AND B.INDI_DSCM_NO < 90000000"
        )
        id_parts = _cohort_id_where_parts(cohort_ids)
        if id_parts:
            cohort_cond = ' AND '.join(f"B.{p}" for p in id_parts)
            where_clause = f"{base_cond} AND ({cohort_cond})"
        else:
            where_clause = base_cond

        sql = f"""
SELECT
    B.STD_YYYYMM,
    SUBSTR(CAST(B.STD_YYYYMM AS VARCHAR), 1, 4)           AS STD_YYYY,
    CAST(B.INDI_DSCM_NO AS VARCHAR(34))                    AS INDI_DSCM_NO,
    B.SEX_TYPE,
    B.BYEAR,
    B.GAIBJA_TYPE,
    SUBSTR(COALESCE(B.RVSN_ADDR_CD, ''), 1, 5)             AS RVSN_ADDR_CD,
    C.FOREIGNER_Y,
    C.SES05,
    CAST(C.CALC_CTRB_VTILE_FD AS VARCHAR(10))              AS CALC_CTRB_VTILE_FD,
    CAST(C.SURV_YR AS INTEGER)                             AS SURV_YR,
    NULL                                                   AS HHDT_DEATH
FROM {pop} B
INNER JOIN {dses} C
    ON  B.INDI_DSCM_NO = C.INDI_DSCM_NO
    AND C.STD_YYYY = SUBSTR(CAST(B.STD_YYYYMM AS VARCHAR), 1, 4)
WHERE {where_clause}
""".strip()
        return sql

    def extract_all_months(self, force=False, cohort_ids=None, progress_callback=None):
        """전 기간 월별 JK 추출 → Parquet 캐시 → DuckDB JK 테이블 병합.

        Args:
            force: True이면 기존 캐시 무시 후 재추출
            cohort_ids: frozenset[str] 또는 None (None이면 전체 추출)
            progress_callback: 진행 상태 콜백
        Returns:
            총 로드 행 수 (int)
        """
        import pyarrow.parquet as pq

        import pyarrow as pa
        import pyarrow.parquet as pq

        # 빈 월 parquet용 명시적 스키마 — union_by_name 타입 추론 오류 방지
        _JK_PA_SCHEMA = pa.schema([
            pa.field('STD_YYYYMM', pa.string()),
            pa.field('STD_YYYY', pa.string()),
            pa.field('INDI_DSCM_NO', pa.string()),
            pa.field('SEX_TYPE', pa.string()),
            pa.field('BYEAR', pa.string()),
            pa.field('GAIBJA_TYPE', pa.string()),
            pa.field('RVSN_ADDR_CD', pa.string()),
            pa.field('FOREIGNER_Y', pa.string()),
            pa.field('SES05', pa.string()),
            pa.field('CALC_CTRB_VTILE_FD', pa.string()),
            pa.field('SURV_YR', pa.int64()),
            pa.field('HHDT_DEATH', pa.string()),
        ])

        cache_dir = self.cache_root / 'JK'
        cache_dir.mkdir(parents=True, exist_ok=True)

        if force:
            for f in cache_dir.glob('JK_*.parquet'):
                f.unlink()
        else:
            for f in cache_dir.glob('JK_*.tmp.parquet'):
                f.unlink()

        # resume 모드: 기존 캐시 스키마 유효성 검사 (컬럼 수 < 5 이면 stale)
        if not force:
            existing = sorted(cache_dir.glob('JK_*.parquet'))
            if existing:
                try:
                    first_schema = pq.read_schema(existing[0])
                    if len(first_schema.names) < 5:
                        logger.warning(
                            "JK 캐시 스키마 불량 (%d개 컬럼) — stale 캐시 전체 삭제 후 재추출합니다.",
                            len(first_schema.names)
                        )
                        for f in existing:
                            f.unlink()
                except Exception as schema_e:
                    logger.warning("JK 캐시 스키마 읽기 실패 (%s) — 전체 재추출합니다.", schema_e)
                    for f in cache_dir.glob('JK_*.parquet'):
                        f.unlink()

        months = self._month_range()
        total = len(months)
        parquet_files = []
        failed_months = []
        consecutive_failures = 0
        _MAX_CONSECUTIVE_FAILURES = 3

        for idx, yyyymm in enumerate(months, 1):
            parquet_path = cache_dir / f'JK_{yyyymm}.parquet'
            tmp_path = cache_dir / f'JK_{yyyymm}.tmp.parquet'

            if not force and parquet_path.exists() and parquet_path.stat().st_size > 0:
                parquet_files.append(parquet_path)
                consecutive_failures = 0
                _emit_progress(
                    progress_callback,
                    f"JK {yyyymm[:4]}-{yyyymm[4:]} 캐시 사용 ({idx}/{total})"
                )
                continue

            _emit_progress(
                progress_callback,
                f"JK {yyyymm[:4]}-{yyyymm[4:]} 추출 중 ({idx}/{total})"
            )

            try:
                base_sql = self._build_join_sql(yyyymm, cohort_ids=cohort_ids)
                chunks = []
                for chunk_df in self.hana.fetch_sql_chunked(base_sql, '"INDI_DSCM_NO"'):
                    chunk_df = _prepare_chunk_for_duckdb(chunk_df)
                    chunks.append(chunk_df)

                if chunks:
                    combined = pd.concat(chunks, ignore_index=True)
                    table = pa.Table.from_pandas(combined, preserve_index=False)
                    pq.write_table(table, str(tmp_path))
                    tmp_path.rename(parquet_path)
                    parquet_files.append(parquet_path)
                    consecutive_failures = 0
                    logger.debug("JK %s: %d행 저장", yyyymm, len(combined))
                else:
                    # 해당 월 데이터 없음 → 명시적 스키마 빈 parquet 저장 (resume 스킵용)
                    empty_table = pa.table(
                        {f.name: pa.array([], type=f.type) for f in _JK_PA_SCHEMA},
                        schema=_JK_PA_SCHEMA,
                    )
                    pq.write_table(empty_table, str(parquet_path))
                    parquet_files.append(parquet_path)
                    consecutive_failures = 0
                    logger.debug("JK %s: 데이터 없음 (빈 parquet)", yyyymm)

            except Exception as e:
                logger.warning("JK %s 추출 실패: %s — 건너뜁니다.", yyyymm, e)
                failed_months.append(yyyymm)
                consecutive_failures += 1
                if tmp_path.exists():
                    tmp_path.unlink()
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"JK 추출: {_MAX_CONSECUTIVE_FAILURES}개월 연속 실패 — HANA 연결 또는 테이블 설정을 확인하세요. "
                        f"마지막 오류: {e}"
                    ) from e

        if failed_months:
            logger.warning("JK 추출 실패 월 (%d개): %s", len(failed_months), failed_months)

        if not parquet_files:
            raise RuntimeError("JK: 유효한 Parquet 파일이 없습니다. HANA 연결 및 테이블 설정을 확인하세요.")

        # DuckDB JK 테이블 병합
        _emit_progress(progress_callback, "JK DuckDB 병합 중...")
        self.storage.drop_table('JK')
        pq_list = [str(p) for p in sorted(parquet_files)]
        self.storage.execute(
            f"CREATE TABLE JK AS SELECT * FROM read_parquet({pq_list!r}, union_by_name=true)"
        )
        total_rows = self.storage.get_row_count('JK')

        _create_indexes_with_progress(
            self.storage, 'JK',
            [['INDI_DSCM_NO', 'STD_YYYY'], ['STD_YYYYMM']],
            progress_callback=progress_callback
        )

        logger.info("JK 월별 추출 완료: %d행, 실패 월 %d개", total_rows, len(failed_months))
        _emit_progress(progress_callback, f"JK 완료: {total_rows:,}행")
        return total_rows


class ExamDataMerger:
    """건강검진 데이터 연도별 병합 처리
    2002~2017: 검진+문진 통합 → GJ_LEGACY
    2018~: 각 연도별 검진결과 → GJ_RESULT_YYYY, 문진 → GJ_QUEST_YYYY
    최종: GJ_RESULT (통합), GJ_QUEST (통합)
    """

    def __init__(self, duckdb_storage):
        self.storage = duckdb_storage
        self.es = EXAM_STRUCTURE

    def merge_exam_results(self, progress_callback=None):
        """모든 연도의 검진결과를 GJ_RESULT로 통합"""
        if progress_callback:
            progress_callback("검진결과 테이블 통합 중...")

        self.storage.drop_table('GJ_RESULT')
        common_cols = self.es['RESULT_COMMON_COLS']
        first = True

        # 2002-2017 통합 테이블 처리
        split_start, split_end = self.es['SPLIT_RANGE']
        col_types = self._get_column_type_map(
            'GJ_RESULT', split_start, split_end, common_cols, default_type='DOUBLE'
        )

        if self.storage.table_exists('GJ_LEGACY'):
            legacy_map = self.es['LEGACY_KEY_MAP']
            legacy_existing = self._get_table_columns('GJ_LEGACY')
            select_parts = []
            for col in common_cols:
                if col in legacy_map:
                    mapped = legacy_map[col]
                    if mapped in legacy_existing:
                        select_parts.append(f'"{mapped}" AS "{col}"')
                    else:
                        null_type = col_types.get(col, 'DOUBLE')
                        select_parts.append(f'CAST(NULL AS {null_type}) AS "{col}"')
                elif col in legacy_existing:
                    select_parts.append(f'"{col}"')
                else:
                    null_type = col_types.get(col, 'DOUBLE')
                    select_parts.append(f'CAST(NULL AS {null_type}) AS "{col}"')

            self.storage.execute(f"""
                CREATE TABLE GJ_RESULT AS
                SELECT {', '.join(select_parts)} FROM GJ_LEGACY
            """)
            first = False
            cnt = self.storage.get_row_count('GJ_RESULT')
            logger.info(f"GJ_LEGACY → GJ_RESULT: {cnt:,}건")

        # 2018+ 연도별 검진결과 테이블 처리
        for year in range(split_start, split_end + 1):
            tname = f'GJ_RESULT_{year}'
            if not self.storage.table_exists(tname):
                continue

            # 해당 테이블에 실제 존재하는 컬럼만 선택
            existing_cols = self._get_table_columns(tname)
            select_cols = [c for c in common_cols if c in existing_cols]

            if not select_cols:
                continue

            # 누락 컬럼은 타입 명시 NULL로 채움
            # (NULL 리터럴은 DuckDB에서 INTEGER로 추론되어 이후 DOUBLE INSERT 시 ConversionException 발생)
            select_parts = []
            for col in common_cols:
                if col in existing_cols:
                    select_parts.append(f'"{col}"')
                else:
                    null_type = col_types.get(col, 'DOUBLE')
                    select_parts.append(f'CAST(NULL AS {null_type}) AS "{col}"')
            select_str = ', '.join(select_parts)

            if first:
                self.storage.execute(f"CREATE TABLE GJ_RESULT AS SELECT {select_str} FROM {tname}")
                first = False
            else:
                self.storage.execute(f"INSERT INTO GJ_RESULT SELECT {select_str} FROM {tname}")

            cnt_year = self.storage.get_row_count(tname)
            logger.info(f"{tname} → GJ_RESULT 추가: {cnt_year:,}건")

        if not first:
            self.storage.create_index('GJ_RESULT', ['INDI_DSCM_NO'])
            total = self.storage.get_row_count('GJ_RESULT')
            logger.info(f"GJ_RESULT 통합 완료: {total:,}건")
            return total
        return 0

    def merge_exam_questionnaires(self, progress_callback=None):
        """모든 연도의 문진을 GJ_QUEST로 통합"""
        if progress_callback:
            progress_callback("검진 문진 테이블 통합 중...")

        self.storage.drop_table('GJ_QUEST')
        common_cols = self.es['QUEST_COMMON_COLS']
        first = True

        # 2002-2017 통합 테이블에서 문진 변수 추출
        # QUEST_COMMON_COLS 전체를 기준으로 생성하여 downstream 쿼리(Q_SMK_NOW_YN 등)의
        # 컬럼 누락 오류를 방지. LEGACY_QUEST_MAP에 매핑된 컬럼은 레거시명으로 대체,
        # 매핑 없는 컬럼은 타입 명시 NULL로 채워 스키마를 완전히 일치시킴.
        split_start_q, split_end_q = self.es['SPLIT_RANGE']
        quest_col_types = self._get_column_type_map(
            'GJ_QUEST', split_start_q, split_end_q, common_cols, default_type='VARCHAR'
        )

        if self.storage.table_exists('GJ_LEGACY'):
            legacy_quest_map = self.es['LEGACY_QUEST_MAP']
            existing_cols = self._get_table_columns('GJ_LEGACY')

            select_parts = []
            for col in common_cols:
                if col == 'INDI_DSCM_NO':
                    select_parts.append('INDI_DSCM_NO')
                elif col == 'HC_BZ_YYYY':
                    # 레거시 테이블에 EXMD_BZ_YYYY가 없을 수 있으므로 존재 확인
                    if 'EXMD_BZ_YYYY' in existing_cols:
                        select_parts.append('EXMD_BZ_YYYY AS HC_BZ_YYYY')
                    else:
                        null_type = quest_col_types.get('HC_BZ_YYYY', 'VARCHAR')
                        select_parts.append(f'CAST(NULL AS {null_type}) AS HC_BZ_YYYY')
                elif col in legacy_quest_map:
                    legacy_col = legacy_quest_map[col]
                    if legacy_col in existing_cols:
                        select_parts.append(f'"{legacy_col}" AS "{col}"')
                    else:
                        null_type = quest_col_types.get(col, 'VARCHAR')
                        select_parts.append(f'CAST(NULL AS {null_type}) AS "{col}"')
                else:
                    # LEGACY_QUEST_MAP에 없는 컬럼(Q_SMK_NOW_YN, Q_DRK_FRQ 등) → 타입 명시 NULL
                    null_type = quest_col_types.get(col, 'VARCHAR')
                    select_parts.append(f'CAST(NULL AS {null_type}) AS "{col}"')

            try:
                self.storage.execute(f"""
                    CREATE TABLE GJ_QUEST AS
                    SELECT {', '.join(select_parts)} FROM GJ_LEGACY
                """)
                first = False
            except Exception as e:
                logger.warning(f"GJ_LEGACY 문진 추출 오류: {e}")

        # 2018+ 연도별 문진 테이블
        for year in range(split_start_q, split_end_q + 1):
            tname = f'GJ_QUEST_{year}'
            if not self.storage.table_exists(tname):
                continue

            existing_cols = self._get_table_columns(tname)
            select_parts = []
            for col in common_cols:
                if col in existing_cols:
                    select_parts.append(f'"{col}"')
                else:
                    null_type = quest_col_types.get(col, 'VARCHAR')
                    select_parts.append(f'CAST(NULL AS {null_type}) AS "{col}"')
            select_str = ', '.join(select_parts)

            if first:
                self.storage.execute(f"CREATE TABLE GJ_QUEST AS SELECT {select_str} FROM {tname}")
                first = False
            else:
                self.storage.execute(f"INSERT INTO GJ_QUEST SELECT {select_str} FROM {tname}")

        if not first:
            self.storage.create_index('GJ_QUEST', ['INDI_DSCM_NO'])
            total = self.storage.get_row_count('GJ_QUEST')
            logger.info(f"GJ_QUEST 통합 완료: {total:,}건")
            return total
        return 0

    def merge_all(self, progress_callback=None):
        """검진결과 + 문진 모두 통합"""
        n_result = self.merge_exam_results(progress_callback)
        n_quest = self.merge_exam_questionnaires(progress_callback)
        return n_result, n_quest

    def _get_table_columns(self, table_name):
        """DuckDB 테이블의 컬럼명 목록 반환"""
        try:
            _validate_table_name(table_name)
            result = self.storage.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table_name]
            )
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.warning(f"컬럼 조회 실패 ({table_name}): {e}")
            return []

    def _get_column_type_map(self, table_prefix, split_start, split_end, common_cols,
                             default_type='DOUBLE'):
        """2018+ 소스 테이블에서 공통 컬럼의 실제 DuckDB 타입을 조회한다.

        NULL 리터럴을 CAST 없이 사용하면 DuckDB가 INTEGER로 추론하여
        이후 청크 INSERT 시 ConversionException이 발생한다.
        실제 분할 연도 테이블에 존재하는 컬럼은 관측된 DuckDB 타입을 우선 사용하고,
        config.py의 명시 타입은 실테이블에 없는 컬럼의 fallback으로만 사용한다.
        """
        explicit_type_map = {}
        if table_prefix == 'GJ_RESULT':
            explicit_type_map = self.es.get('RESULT_COMMON_COL_TYPES', {})
        elif table_prefix == 'GJ_QUEST':
            explicit_type_map = self.es.get('QUEST_COMMON_COL_TYPES', {})

        col_types = {}

        for year in range(split_start, split_end + 1):
            tname = f'{table_prefix}_{year}'
            if not self.storage.table_exists(tname):
                continue
            try:
                try:
                    schema_df = self.storage.execute_df(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = ?",
                        [tname],
                    )
                except Exception:
                    # DuckDB 버전에 따라 파라미터 바인딩 미지원 가능 — 직접 쿼리로 fallback
                    schema_df = self.storage.execute_df(
                        f"SELECT column_name, data_type FROM information_schema.columns "
                        f"WHERE table_name = '{tname}'"
                    )
                for _, row in schema_df.iterrows():
                    if row['column_name'] in common_cols:
                        col_types[row['column_name']] = row['data_type']
            except Exception as e:
                logger.warning(
                    f"컬럼 타입 조회 실패 ({tname}): {e}; "
                    "명시 타입 선언을 fallback으로 사용합니다."
                )
            if len(col_types) == len(common_cols):
                break  # 모든 공통 컬럼 타입을 확보하면 중단

        # 실테이블에 없는 컬럼만 명시 타입, 그다음 기본 타입으로 채움
        for col in common_cols:
            if col not in col_types and col in explicit_type_map:
                col_types[col] = explicit_type_map[col]

        for col in common_cols:
            col_types.setdefault(col, default_type)
        return col_types


class DataManager:
    """통합 데이터 관리자"""

    def __init__(self, work_dir='./work'):
        if str(work_dir) == ':memory:':
            self.work_dir = None
            self.duckdb_path = ':memory:'
        else:
            self.work_dir = Path(work_dir)
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self.duckdb_path = str(self.work_dir / 'nhis_analysis.duckdb')
        self.storage = DuckDBStorage(self.duckdb_path)
        self.hana = None
        self.sas_loader = SASFileLoader()
        self.exam_merger = None
        self.loaded_tables = {}

    def init_storage(self):
        self.storage.connect()
        self.exam_merger = ExamDataMerger(self.storage)
        return True

    def reset_storage(self):
        """기존 DuckDB 테이블 전체 삭제 후 재초기화 (stale data 방지)"""
        self.storage.connect()
        tables = self.storage.execute_df(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        )
        for tname in tables['table_name']:
            _validate_table_name(tname)
            self.storage.execute(f"DROP TABLE IF EXISTS {_quote_identifier(tname)}")
        self.loaded_tables.clear()
        self.exam_merger = ExamDataMerger(self.storage)
        logger.info("DuckDB 저장소 초기화 완료 (모든 테이블 삭제)")
        return True

    def connect_hana(self, host, port, user, password):
        self.hana = HANAConnector(host, port, user, password)
        try:
            return self.hana.test_connection()
        except Exception:
            self.hana = None
            raise

    def get_hana_schemas(self):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        self.hana._reconnect_if_stale()
        return self.hana.list_schemas()

    def get_hana_tables(self, schema_name):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        self.hana._reconnect_if_stale()
        return self.hana.list_tables(schema_name)

    def get_hana_columns(self, schema_name, table_name):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        self.hana._reconnect_if_stale()
        return self.hana.list_columns(schema_name, table_name)

    def search_hana_tables(self, schema_name, keyword):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        self.hana._reconnect_if_stale()
        return self.hana.search_tables(schema_name, keyword)

    def extract_cohort_ids(self, hana_schema, force=True, progress_callback=None):
        """진입기간 내 연령+DM 코드 조건 충족 INDI_DSCM_NO를 월별 추출해 frozenset 반환.

        HHDT_POPULATION_MM(연령) ∩ T20(E10~E14 상병)을 진입기간 월별로 순회하며 누적.
        결과는 cohort_ids.parquet으로 캐시되어 resume 모드에서 재사용된다.
        """
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        extractor = CohortIDExtractor(self.hana, hana_schema, _get_hana_cache_dir())
        return extractor.extract(force=force, progress_callback=progress_callback)

    def load_from_hana(self, table_name, hana_schema, hana_table=None,
                       columns=None, where_clause=None, progress_callback=None,
                       force=True, cohort_ids=None):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        if hana_table is None:
            # UI에서 실제 테이블명을 입력하지 않으면 내부 별칭 → 실제 HANA 테이블명 자동 변환
            hana_table = _resolve_hana_table(table_name)
        count = self.hana.load_table_to_duckdb(
            hana_table, hana_schema, self.storage,
            table_name, columns, where_clause,
            progress_callback=progress_callback, force=force,
            cohort_ids=cohort_ids,
        )
        self.loaded_tables[table_name] = count
        return count

    def load_from_sas(self, table_name, sas_path, columns=None, progress_callback=None):
        count = self.sas_loader.load_sas_to_duckdb(
            sas_path, self.storage, table_name, columns, progress_callback
        )
        self.loaded_tables[table_name] = count
        return count

    def load_from_csv(self, table_name, csv_path, delimiter=',', progress_callback=None):
        count = self.sas_loader.load_csv_to_duckdb(
            csv_path, self.storage, table_name, delimiter, progress_callback
        )
        self.loaded_tables[table_name] = count
        return count

    def load_from_files_multi(self, table_name, file_paths, file_type='csv',
                               delimiter=',', columns=None, progress_callback=None):
        """다중 분할 파일 병합 로드"""
        count = self.sas_loader.load_multi_files_to_duckdb(
            file_paths, file_type, self.storage, table_name,
            delimiter=delimiter, columns=columns,
            progress_callback=progress_callback
        )
        self.loaded_tables[table_name] = count
        return count

    def merge_exam_data(self, progress_callback=None):
        """검진 데이터 연도별 병합 실행"""
        if not self.exam_merger:
            self.exam_merger = ExamDataMerger(self.storage)
        return self.exam_merger.merge_all(progress_callback)

    def get_table_info(self):
        info = {}
        try:
            tables = self.storage.execute_df(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            )
            for tname in tables['table_name']:
                count = self.storage.get_row_count(tname)
                info[tname] = {'rows': count}
        except Exception as e:
            logger.warning(f"테이블 정보 조회 실패: {e}")
        return info

    def query(self, sql):
        """읽기 전용 쿼리 — DDL/DML 차단"""
        if _READ_ONLY_FORBIDDEN.search(sql):
            raise ValueError(f"읽기 전용 쿼리에서 DDL/DML 사용 불가: {sql[:100]}")
        return self.storage.execute_df(sql)

    def query_safe(self, sql, max_rows=None):
        """메모리 안전 읽기 전용 쿼리 — DDL/DML 차단 + max_rows 초과 시 자동 LIMIT"""
        if _READ_ONLY_FORBIDDEN.search(sql):
            raise ValueError(f"읽기 전용 쿼리에서 DDL/DML 사용 불가: {sql[:100]}")
        import re
        if max_rows is None:
            max_rows = mem_manager.get_safe_analysis_rows()
        # 최외곽 SELECT에 LIMIT/SAMPLE이 있는지 확인 (서브쿼리 내부는 무시)
        # 괄호 깊이가 0인 위치의 LIMIT 또는 USING SAMPLE 키워드만 감지
        depth = 0
        has_outer_limit = False
        tokens = re.split(r'(\(|\))', sql.upper())
        outer_sql = ''
        for tok in tokens:
            if tok == '(':
                depth += 1
            elif tok == ')':
                depth -= 1
            elif depth == 0:
                outer_sql += tok
        if re.search(r'\bLIMIT\b', outer_sql) or re.search(r'\bUSING\s+SAMPLE\b', outer_sql):
            has_outer_limit = True
        if has_outer_limit:
            return self.storage.execute_df(sql)
        return self.storage.execute_df(f"{sql} LIMIT {max_rows}")

    def execute(self, sql):
        self.storage.execute(sql)

    @staticmethod
    def get_hana_cache_dir() -> Path:
        """HANA 월별 Parquet 캐시 디렉토리 경로 반환 (UI용 공개 API)."""
        return _get_hana_cache_dir()

    def close(self):
        self.storage.close()
        if self.hana:
            self.hana.destroy()  # 앱 종료 시 패스워드까지 완전 소거
