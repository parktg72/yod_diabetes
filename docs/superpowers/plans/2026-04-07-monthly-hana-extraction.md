# Monthly HANA Extraction (T20/T30/T40/T60) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** T20/T30/T40/T60 테이블을 월별로 분할 추출(MDCARE_STRT_YYYYMM 기준)하여 Parquet 파일로 캐시한 뒤 DuckDB로 병합함으로써 HANA DB 부하를 줄인다.

**Architecture:** `HANAConnector.load_table_to_duckdb()` 내부에서 T20/T30/T40/T60 감지 시 새로운 `MonthlyHanaExtractor` 클래스에 투명 위임한다. 추출기는 144개월(2013-01~2024-12)을 순회하며 `MDCARE_STRT_YYYYMM = 'YYYYMM'` WHERE 절로 월별 청크 추출 → Parquet 원자적 저장 → DuckDB 병합을 수행한다. 기존 호출부(tabs.py 등)는 수정 없이 동일한 인터페이스를 사용한다.

**Tech Stack:** Python 3.12, pandas (to_parquet), DuckDB (read_parquet, union_by_name), pytest

---

## 파일 변경 맵

| 파일 | 변경 유형 | 내용 |
|------|-----------|------|
| `config.py` | Modify | `DUCKDB_SETTINGS`에 `'HANA_CACHE_DIR': None` 추가 |
| `db_connector.py` | Modify | `_MONTHLY_TABLES`, `_get_hana_cache_dir()`, `MonthlyHanaExtractor` 클래스 추가; `HANAConnector.load_table_to_duckdb()` 상단에 라우팅 로직 추가 |
| `tests/test_db_connector.py` | Modify | `TestMonthlyHanaExtractor` 클래스 추가 (월 범위, Parquet 저장, 진행 emit, 라우팅 테스트) |

---

## Task 1: config.py — HANA_CACHE_DIR 추가

**Files:**
- Modify: `config.py:212-216`

- [ ] **Step 1: `config.py` 수정**

현재 (`config.py:212-216`):
```python
DUCKDB_SETTINGS = {
    'MEMORY_LIMIT': '4GB',
    'THREADS': 4,
    'TEMP_DIRECTORY': None,  # None → db_connector.py 가 _BASE_DIR 기준으로 해결
}
```

변경 후:
```python
DUCKDB_SETTINGS = {
    'MEMORY_LIMIT': '4GB',
    'THREADS': 4,
    'TEMP_DIRECTORY': None,  # None → db_connector.py 가 _BASE_DIR 기준으로 해결
    'HANA_CACHE_DIR': None,  # None → _BASE_DIR / 'hana_cache'
}
```

- [ ] **Step 2: 문법 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m py_compile config.py && echo "OK"
```

기대: `OK`

- [ ] **Step 3: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add config.py
git commit -m "feat: DUCKDB_SETTINGS에 HANA_CACHE_DIR 설정 추가 (월별 추출 준비)"
```

---

## Task 2: `_month_range()` 헬퍼 + 상수 구현 (TDD)

**Files:**
- Modify: `db_connector.py` (상단 상수 + `HANAConnector` 클래스 이전에 `MonthlyHanaExtractor` 클래스 골격 추가)
- Test: `tests/test_db_connector.py`

### 배경

`_MONTHLY_TABLES`, `_get_hana_cache_dir()` 헬퍼, `MonthlyHanaExtractor.__init__`, `MonthlyHanaExtractor._month_range()`를 구현한다. `_month_range()`는 `config.STUDY_SETTINGS['STUDY_START_YEAR']`~`STUDY_END_YEAR` 범위의 'YYYYMM' 문자열 목록을 반환한다.

- [ ] **Step 1: 실패하는 테스트 추가 (`tests/test_db_connector.py` 끝에 추가)**

```python
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
```

- [ ] **Step 2: 테스트 실행 — FAIL 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_db_connector.py::TestMonthlyHanaExtractor -v 2>&1 | tail -10
```

기대: 3개 FAIL (`MonthlyHanaExtractor` 미정의)

- [ ] **Step 3: `db_connector.py` 수정 — 상수 + 클래스 골격 추가**

`db_connector.py` 내 `_VALID_TABLE_RE` 정의(line 24) 바로 아래에 추가:

```python
_MONTHLY_TABLES = frozenset({'T20', 'T30', 'T40', 'T60'})
_MONTHLY_FILTER_COL = 'MDCARE_STRT_YYYYMM'
```

`SASFileLoader` 클래스(line 672) 바로 앞에 `MonthlyHanaExtractor` 클래스 추가 (현재 db_connector.py에서 HANAConnector 클래스는 line 291에 있고, SASFileLoader는 line 672에 있음 — 즉 HANAConnector 정의 직후, SASFileLoader 직전에 삽입):

```python
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
        months = []
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                months.append(f'{year:04d}{month:02d}')
        return months

    def extract_all_months(self, table_name, duckdb_table, progress_callback=None):
        """구현 예정 (Task 3에서 추가)."""
        raise NotImplementedError
```

- [ ] **Step 4: 테스트 PASS 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_db_connector.py::TestMonthlyHanaExtractor -v 2>&1 | tail -10
```

기대: 3개 PASSED

- [ ] **Step 5: 전체 회귀 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: no new failures

- [ ] **Step 6: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add db_connector.py tests/test_db_connector.py
git commit -m "feat: MonthlyHanaExtractor 골격 + _month_range() 구현 (T2)"
```

---

## Task 3: `MonthlyHanaExtractor.extract_all_months()` 구현 (TDD)

**Files:**
- Modify: `db_connector.py` — `extract_all_months()` 본체 구현
- Test: `tests/test_db_connector.py` — `TestMonthlyHanaExtractor` 클래스에 테스트 추가

### 배경

`extract_all_months(table_name, duckdb_table, progress_callback=None)` 구현:
1. `{cache_root}/{TABLE}/` 디렉토리 생성
2. 기존 `{TABLE}_*.parquet` 전체 삭제 (전체 재추출)
3. 144개월 순회: WHERE 절로 청크 추출 → `{TABLE}_{YYYYMM}.tmp.parquet` 쓰기 → `{TABLE}_{YYYYMM}.parquet`으로 rename (원자성)
4. `DuckDB: CREATE TABLE AS SELECT * FROM read_parquet([...], union_by_name=true)`
5. 인덱스 생성 (`INDI_DSCM_NO`, `CMN_KEY`)
6. 총 행 수 반환

- [ ] **Step 1: 실패하는 테스트 추가 (`TestMonthlyHanaExtractor` 클래스에 추가)**

```python
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
```

- [ ] **Step 2: 테스트 실행 — FAIL 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_db_connector.py::TestMonthlyHanaExtractor -v 2>&1 | tail -15
```

기대: Task 2에서 추가한 3개 PASSED + 신규 5개 FAIL (`NotImplementedError`)

- [ ] **Step 3: `extract_all_months()` 본체 구현**

`db_connector.py`의 `MonthlyHanaExtractor.extract_all_months()` 에서 `raise NotImplementedError`를 아래로 교체:

```python
    def extract_all_months(self, table_name, duckdb_table, progress_callback=None):
        """모든 월 추출 → Parquet 저장 → DuckDB 병합."""
        table_upper = table_name.upper()
        cache_dir = self.cache_root / table_upper
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 전체 재추출: 기존 Parquet 삭제
        for f in cache_dir.glob(f'{table_upper}_*.parquet'):
            f.unlink()

        months = self._month_range()
        total = len(months)
        parquet_files = []

        for idx, yyyymm in enumerate(months, 1):
            parquet_path = cache_dir / f'{table_upper}_{yyyymm}.parquet'
            tmp_path = cache_dir / f'{table_upper}_{yyyymm}.tmp.parquet'
            where_clause = f"{_MONTHLY_FILTER_COL} = '{yyyymm}'"

            _emit_progress(
                progress_callback,
                f"{table_upper} {yyyymm[:4]}-{yyyymm[4:]} 추출 중 ({idx}/{total})"
            )

            frames = []
            for chunk_df in self.hana.fetch_table_chunked(
                table_name, self.schema,
                where_clause=where_clause
            ):
                chunk_df = _prepare_chunk_for_duckdb(chunk_df)
                frames.append(chunk_df)
                gc.collect()

            if frames:
                pd.concat(frames, ignore_index=True).to_parquet(str(tmp_path), index=False)
            else:
                pd.DataFrame().to_parquet(str(tmp_path), index=False)

            tmp_path.rename(parquet_path)
            parquet_files.append(parquet_path)
            del frames
            gc.collect()

        # Parquet → DuckDB 병합 (단일 CREATE TABLE, union_by_name으로 컬럼 드리프트 대응)
        _emit_progress(progress_callback, f"{table_upper} DuckDB 병합 중...")
        self.storage.drop_table(duckdb_table)
        files_sql = '[' + ', '.join(f"'{p}'" for p in parquet_files) + ']'
        self.storage.execute(
            f"CREATE TABLE {duckdb_table} AS "
            f"SELECT * FROM read_parquet({files_sql}, union_by_name=true)"
        )

        total_rows = self.storage.get_row_count(duckdb_table)
        _create_indexes_with_progress(
            self.storage, duckdb_table,
            [['INDI_DSCM_NO'], ['CMN_KEY']],
            progress_callback=progress_callback
        )

        logger.info(f"월별 추출 완료: {duckdb_table} ({total_rows:,}건, {total}개월)")
        return total_rows
```

- [ ] **Step 4: 테스트 PASS 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_db_connector.py::TestMonthlyHanaExtractor -v 2>&1 | tail -15
```

기대: 8개 PASSED

- [ ] **Step 5: 전체 회귀 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: no new failures

- [ ] **Step 6: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add db_connector.py tests/test_db_connector.py
git commit -m "feat: MonthlyHanaExtractor.extract_all_months() 구현 — 월별 Parquet 추출+DuckDB 병합 (T3)"
```

---

## Task 4: `HANAConnector.load_table_to_duckdb()` 라우팅 수정 (TDD)

**Files:**
- Modify: `db_connector.py:607-609` (load_table_to_duckdb 상단에 라우팅 추가)
- Test: `tests/test_db_connector.py` (TestMonthlyHanaExtractor에 라우팅 테스트 추가)

### 배경

`load_table_to_duckdb()` 상단에 T20/T30/T40/T60 감지 로직을 추가한다. `where_clause is None`인 경우에만 월별 추출로 라우팅한다. `where_clause`가 있는 경우(테스트나 일회성 쿼리)는 기존 경로를 유지한다.

- [ ] **Step 1: 실패하는 테스트 추가 (`TestMonthlyHanaExtractor`에 추가)**

```python
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
```

- [ ] **Step 2: 테스트 실행 — FAIL 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_db_connector.py::TestMonthlyHanaExtractor::test_load_table_routes_t20_to_extractor tests/test_db_connector.py::TestMonthlyHanaExtractor::test_load_table_skips_routing_when_where_clause tests/test_db_connector.py::TestMonthlyHanaExtractor::test_load_table_skips_routing_for_non_monthly_table -v 2>&1 | tail -15
```

기대: 3개 FAIL

- [ ] **Step 3: `db_connector.py` 수정 — `load_table_to_duckdb` 상단에 라우팅 추가**

`db_connector.py` line 610 (현재: `if chunk_size is None:`) 바로 앞에 삽입:

```python
    def load_table_to_duckdb(self, hana_table, hana_schema, duckdb_storage,
                              duckdb_table, columns=None, where_clause=None,
                              chunk_size=None, progress_callback=None):
        # T20/T30/T40/T60: 월별 분할 추출 (where_clause 없는 경우에만)
        if duckdb_table.upper() in _MONTHLY_TABLES and where_clause is None:
            extractor = MonthlyHanaExtractor(
                self, duckdb_storage, hana_schema, _get_hana_cache_dir()
            )
            return extractor.extract_all_months(hana_table, duckdb_table, progress_callback)

        if chunk_size is None:
            chunk_size = chunk_controller.get_chunk('hana')
        # ... 이하 기존 코드 그대로
```

즉, 기존 `def load_table_to_duckdb` 헤더와 `if chunk_size is None:` 사이에 4행 삽입.

- [ ] **Step 4: 테스트 PASS 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/test_db_connector.py::TestMonthlyHanaExtractor -v 2>&1 | tail -20
```

기대: 11개 모두 PASSED

- [ ] **Step 5: 전체 회귀 + 문법 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m py_compile db_connector.py && echo "OK"
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: `OK`, no new failures

- [ ] **Step 6: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add db_connector.py tests/test_db_connector.py
git commit -m "feat: load_table_to_duckdb T20/T30/T40/T60 월별 추출 라우팅 추가 (T4)"
```

---

## 자체 점검

### 스펙 커버리지

| 요구사항 | Task | 상태 |
|---------|------|------|
| T20/T30/T40/T60 월별 추출 (MDCARE_STRT_YYYYMM) | T3, T4 | ✅ |
| 2013-01 ~ 2024-12 (144개월) 범위 | T2 | ✅ |
| Parquet 저장 `./hana_cache/{TABLE}/{TABLE}_{YYYYMM}.parquet` | T3 | ✅ |
| 항상 전체 재추출 (기존 캐시 삭제) | T3 | ✅ |
| 원자적 Parquet 쓰기 (.tmp → rename) | T3 | ✅ |
| 진행 콜백: 월별 + DuckDB 병합 메시지 | T3 | ✅ |
| DuckDB 병합: CREATE TABLE AS read_parquet (단일 쿼리) | T3 | ✅ |
| UNION_BY_NAME: 연도별 컬럼 드리프트 대응 | T3 | ✅ |
| 인덱스: INDI_DSCM_NO, CMN_KEY | T3 | ✅ |
| HANA_CACHE_DIR config 설정 | T1 | ✅ |
| 기존 호출부(tabs.py 등) 수정 없음 | T4 (라우팅 투명) | ✅ |
| where_clause 있으면 기존 경로 유지 | T4 | ✅ |
| STUDY_START_YEAR/STUDY_END_YEAR 직접 참조 | T2 | ✅ |

### 타입 일관성

- `MonthlyHanaExtractor.__init__` 파라미터 → Task 2에서 정의, Task 3·4에서 동일하게 사용
- `extract_all_months(table_name, duckdb_table, progress_callback=None)` → Task 3 정의, Task 4 테스트에서 `assert_called_once_with('T20', 'T20', None)` 일치
- `_get_hana_cache_dir()` → Task 2에서 정의, Task 4에서 monkeypatch 대상으로 동일 경로 참조
