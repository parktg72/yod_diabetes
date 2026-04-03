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
from pathlib import Path

from config import DUCKDB_SETTINGS, EXAM_STRUCTURE
from memory_manager import mem_manager, chunk_controller

_VALID_TABLE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
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
    except Exception:
        # DuckDB 버전에 따라 파라미터 바인딩 미지원 가능 — 직접 쿼리로 fallback
        schema_df = storage.execute_df(
            f"SELECT column_name, data_type, numeric_precision, numeric_scale "
            f"FROM information_schema.columns WHERE table_name = '{table_name}'"
        )

    for _, row in schema_df.iterrows():
        if str(row['data_type']).upper() not in ('DECIMAL', 'NUMERIC'):
            continue
        try:
            prec = int(row['numeric_precision']) if row['numeric_precision'] is not None else DUCKDB_WIDE_DECIMAL_PRECISION
            scale = int(row['numeric_scale']) if row['numeric_scale'] is not None else 0
        except (TypeError, ValueError):
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

    def __init__(self, db_path='./nhis_analysis.duckdb'):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        temp_dir = DUCKDB_SETTINGS.get('TEMP_DIRECTORY', './temp_duckdb')
        os.makedirs(temp_dir, exist_ok=True)
        self.conn = duckdb.connect(self.db_path)
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
        return self.conn.execute(query, params) if params else self.conn.execute(query)

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
            return self.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        return 0

    def drop_table(self, table_name):
        _validate_table_name(table_name)
        self.execute(f"DROP TABLE IF EXISTS {table_name}")

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

    def connect(self):
        try:
            from hdbcli import dbapi
            self.conn = dbapi.connect(
                address=self.host, port=self.port,
                user=self.user, password=self._password,
            )
            logger.info(f"HANA DB 연결 성공: {self.host}:{self.port}")
            return True
        except ImportError:
            raise ImportError("hdbcli 패키지 필요: pip install hdbcli")
        except Exception as e:
            logger.error(f"HANA DB 연결 실패: {e}")
            raise

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
            return False
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
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT COLUMN_NAME FROM SYS.CONSTRAINTS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? AND IS_PRIMARY_KEY = 'TRUE'
                ORDER BY POSITION
            """, (schema_name, table_name))
            pk_cols = [row[0] for row in cursor.fetchall()]
            if pk_cols:
                return ', '.join(f'"{c}"' for c in pk_cols)
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
                return f'"{row[0]}"'
        except Exception:
            pass
        finally:
            cursor.close()

        return '1'  # 최종 fallback

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

    def fetch_table_chunked(self, table_name, schema_name, columns=None,
                            where_clause=None, chunk_size=None):
        """서버 측 LIMIT/OFFSET 페이징으로 대용량 테이블 분할 조회.

        HANA의 search result size limit 초과를 방지하기 위해
        한 번의 대형 SELECT 대신 LIMIT/OFFSET으로 분할 실행한다.
        """
        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        if not self.conn:
            self.connect()
        col_str = ', '.join(f'"{c}"' for c in columns) if columns else '*'
        from_clause = f'"{schema_name}"."{table_name}"' if schema_name else f'"{table_name}"'

        where_part = ''
        if where_clause:
            self._validate_where_clause(where_clause)
            where_part = f' WHERE {where_clause}'

        # 컬럼 이름을 먼저 가져오기 (0건만 조회)
        meta_query = f'SELECT {col_str} FROM {from_clause}{where_part} LIMIT 0'
        cursor = self.conn.cursor()
        cursor.execute(meta_query)
        col_names = [desc[0] for desc in cursor.description]
        cursor.close()

        # 결정적 페이징을 위한 정렬 키 (PK → 첫 컬럼 fallback)
        order_key = self._get_order_key(schema_name, table_name)

        # 서버 측 LIMIT/OFFSET 페이징
        total_rows = 0
        offset = 0

        while True:
            paged_query = (
                f'SELECT {col_str} FROM {from_clause}{where_part}'
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
                              chunk_size=None, progress_callback=None):
        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        first_chunk = True
        total = 0

        for chunk_df in self.fetch_table_chunked(
            hana_table, hana_schema, columns, where_clause, chunk_size
        ):
            # DuckDB 적재 시 optimize_dtypes 사용 금지:
            # 청크별 min/max가 달라 첫 청크 기준 스키마와 이후 청크 값이 불일치
            # (예: 첫 청크 max=999999 → DECIMAL(6,0), 이후 값 1031900 → 범위 초과)
            # Integral Decimal은 적재 전에 넉넉한 DECIMAL(38,0)으로 고정한다.
            chunk_df = _prepare_chunk_for_duckdb(chunk_df)
            chunk_sql = _build_chunk_select_sql(chunk_df, '_temp_chunk')

            if first_chunk:
                duckdb_storage.drop_table(duckdb_table)
                duckdb_storage.conn.register('_temp_chunk', chunk_df)
                duckdb_storage.execute(f"CREATE TABLE {duckdb_table} AS {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_chunk')
                # 첫 청크 값이 작아 좁은 DECIMAL로 추론된 경우 DECIMAL(38,s)로 확장
                _widen_decimal_columns(duckdb_storage, duckdb_table)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_chunk', chunk_df)
                duckdb_storage.execute(f"INSERT INTO {duckdb_table} {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_chunk')

            total += len(chunk_df)
            # chunk_df 즉시 삭제 → Pandas 메모리 적층 방지
            del chunk_df
            gc.collect()

            # 메모리 상태 체크 → 위험 시 chunk 자동 축소
            chunk_controller.auto_adjust()

            if progress_callback:
                progress_callback(total, hana_table)

        if duckdb_table.upper() in ['T20', 'T30', 'T40', 'T60']:
            duckdb_storage.create_index(duckdb_table, ['INDI_DSCM_NO'])
            duckdb_storage.create_index(duckdb_table, ['CMN_KEY'])
        elif duckdb_table.upper() == 'JK':
            duckdb_storage.create_index(duckdb_table, ['INDI_DSCM_NO', 'STD_YYYY'])
        elif duckdb_table.upper() == 'DEATH':
            duckdb_storage.create_index(duckdb_table, ['INDI_DSCM_NO'])

        logger.info(f"DuckDB 적재: {duckdb_table} ({total:,}건)")
        return total


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
                duckdb_storage.execute(f"CREATE TABLE {table_name} AS {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_sas')
                # 첫 청크 값이 작아 좁은 DECIMAL로 추론된 경우 DECIMAL(38,s)로 확장
                _widen_decimal_columns(duckdb_storage, table_name)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_sas', chunk_df)
                duckdb_storage.execute(f"INSERT INTO {table_name} {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_sas')

            total += len(chunk_df)
            del chunk_df
            gc.collect()

            # 메모리 자동 체크 & chunk 조절
            chunk_controller.auto_adjust()

            if progress_callback:
                progress_callback(total, table_name)

        if table_name.upper() in ['T20', 'T30', 'T40', 'T60']:
            duckdb_storage.create_index(table_name, ['INDI_DSCM_NO'])
        elif table_name.upper() == 'JK':
            duckdb_storage.create_index(table_name, ['INDI_DSCM_NO', 'STD_YYYY'])

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
                duckdb_storage.execute(f"CREATE TABLE {table_name} AS {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_csv')
                _widen_decimal_columns(duckdb_storage, table_name)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_csv', chunk_df)
                duckdb_storage.execute(f"INSERT INTO {table_name} {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_csv')

            total += len(chunk_df)
            del chunk_df
            gc.collect()
            chunk_controller.auto_adjust()

            if progress_callback:
                progress_callback(total, table_name)

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
        if table_name.upper() in ['T20', 'T30', 'T40', 'T60']:
            duckdb_storage.create_index(table_name, ['INDI_DSCM_NO'])
        elif table_name.upper() == 'JK':
            duckdb_storage.create_index(table_name, ['INDI_DSCM_NO', 'STD_YYYY'])
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
                duckdb_storage.execute(f"CREATE TABLE {table_name} AS {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_sas')
                # 첫 청크 값이 작아 좁은 DECIMAL로 추론된 경우 DECIMAL(38,s)로 확장
                _widen_decimal_columns(duckdb_storage, table_name)
                first_chunk = False
            else:
                duckdb_storage.conn.register('_temp_sas', chunk_df)
                duckdb_storage.execute(f"INSERT INTO {table_name} {chunk_sql}")
                duckdb_storage.conn.unregister('_temp_sas')

            total += len(chunk_df)
            del chunk_df
            gc.collect()
            chunk_controller.auto_adjust()

            if progress_callback:
                progress_callback(total, table_name)

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

            # Inter-file memory cleanup
            mem_manager.force_cleanup()

        # Create indexes after all files loaded
        if table_name.upper() in ['T20', 'T30', 'T40', 'T60']:
            duckdb_storage.create_index(table_name, ['INDI_DSCM_NO'])
            duckdb_storage.create_index(table_name, ['CMN_KEY'])
        elif table_name.upper() == 'JK':
            duckdb_storage.create_index(table_name, ['INDI_DSCM_NO', 'STD_YYYY'])

        logger.info(f"다중 파일 병합 완료: {table_name} ({grand_total:,}건, {len(file_paths)}개 파일)")
        return grand_total


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
            self.storage.execute(f"DROP TABLE IF EXISTS {tname}")
        self.loaded_tables.clear()
        self.exam_merger = ExamDataMerger(self.storage)
        logger.info("DuckDB 저장소 초기화 완료 (모든 테이블 삭제)")
        return True

    def connect_hana(self, host, port, user, password):
        self.hana = HANAConnector(host, port, user, password)
        return self.hana.test_connection()

    def get_hana_schemas(self):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        return self.hana.list_schemas()

    def get_hana_tables(self, schema_name):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        return self.hana.list_tables(schema_name)

    def get_hana_columns(self, schema_name, table_name):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        return self.hana.list_columns(schema_name, table_name)

    def search_hana_tables(self, schema_name, keyword):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        return self.hana.search_tables(schema_name, keyword)

    def load_from_hana(self, table_name, hana_schema, hana_table=None,
                       columns=None, where_clause=None, progress_callback=None):
        if not self.hana:
            raise RuntimeError("HANA 미연결")
        if not self.hana.conn:
            self.hana.connect()
        if hana_table is None:
            hana_table = table_name
        count = self.hana.load_table_to_duckdb(
            hana_table, hana_schema, self.storage,
            table_name, columns, where_clause,
            progress_callback=progress_callback
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

    def close(self):
        self.storage.close()
        if self.hana:
            self.hana.destroy()  # 앱 종료 시 패스워드까지 완전 소거
