"""test_cohort_builder.py - CohortBuilder 단위 테스트

In-memory DuckDB 기반 MockDataManager로 각 코호트 구축 단계를 검증한다.
합성 데이터(5명)로 DM 분류, 치매 제외, 결과변수 산출 등 핵심 로직을 테스트.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import duckdb
import pandas as pd
from unittest.mock import MagicMock, patch

from cohort_builder import CohortBuilder


def _create_table_from_df(conn, table_name, df):
    """DuckDB의 replacement scan으로 DataFrame을 테이블로 생성한다."""
    conn.register('_tmp_df', df)
    conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM _tmp_df")
    conn.unregister('_tmp_df')


# ---------------------------------------------------------------------------
# MockDataManager: in-memory DuckDB 래퍼
# ---------------------------------------------------------------------------
class MockDataManager:
    """CohortBuilder가 요구하는 DataManager 인터페이스를 in-memory DuckDB로 구현."""

    def __init__(self):
        self.storage = MagicMock()
        self.conn = duckdb.connect(':memory:')
        self.storage.conn = self.conn
        self.storage.get_row_count = self._get_row_count
        self.storage.table_exists = self._table_exists

    def execute(self, sql):
        self.conn.execute(sql)

    def query(self, sql):
        return self.conn.execute(sql).fetchdf()

    def _get_row_count(self, table):
        try:
            return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return 0

    def _table_exists(self, table):
        try:
            self.conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
            return True
        except Exception:
            return False

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def dm():
    """5명의 합성 NHIS 데이터가 채워진 MockDataManager를 반환한다.

    환자 설계:
      P0001 - T1DM (E10), 외래 3회, first_year=2013, BYEAR=1965
      P0002 - T2DM (E11), 외래 2회, first_year=2014, BYEAR=1965
      P0003 - T2DM (E11), 외래 2회, first_year=2013, BYEAR=1965
      P0004 - NON_DM, first_year=2013, BYEAR=1965
      P0005 - NON_DM, first_year=2013, BYEAR=1965, 사망(HHDT_DEATH=20221231)
    모든 환자: BYEAR=1965 -> 2013년 기준 48세 (40-64 범위 충족)
    """
    m = MockDataManager()

    # ------------------------------------------------------------------
    # JK (자격 DB) - 5명, 2012-2024 자격 보유
    # ------------------------------------------------------------------
    jk_data = []
    for pid in range(1, 6):
        for year in range(2012, 2025):
            jk_data.append({
                'INDI_DSCM_NO': f'P{pid:04d}',
                'STD_YYYY': str(year),
                'SEX_TYPE': '1' if pid <= 3 else '2',
                'BYEAR': '1965',
                'FOREIGNER_Y': None,
                'SURV_YR': str(year),
                'GAIBJA_TYPE': '1',
                'SES05': '5',
                'RVSN_ADDR_CD': '11',
                'HHDT_DEATH': '20221231' if pid == 5 else None,
            })
    _create_table_from_df(m.conn, 'JK', pd.DataFrame(jk_data))

    # ------------------------------------------------------------------
    # T40 (상병내역) - DM 청구
    # ------------------------------------------------------------------
    t40_data = [
        # P0001: T1DM (E10) - 외래 3회 (2013~2014), first_year=2013이므로 모두 포함
        {'INDI_DSCM_NO': 'P0001', 'MCEX_SICK_SYM': 'E100', 'MDCARE_STRT_DT': '20130301', 'CMN_KEY': 'C001'},
        {'INDI_DSCM_NO': 'P0001', 'MCEX_SICK_SYM': 'E101', 'MDCARE_STRT_DT': '20130601', 'CMN_KEY': 'C002'},
        {'INDI_DSCM_NO': 'P0001', 'MCEX_SICK_SYM': 'E109', 'MDCARE_STRT_DT': '20140101', 'CMN_KEY': 'C003'},
        # P0002: T2DM (E11) - 외래 2회 (2014), first_year=2014이므로 모두 포함
        {'INDI_DSCM_NO': 'P0002', 'MCEX_SICK_SYM': 'E110', 'MDCARE_STRT_DT': '20140101', 'CMN_KEY': 'C004'},
        {'INDI_DSCM_NO': 'P0002', 'MCEX_SICK_SYM': 'E119', 'MDCARE_STRT_DT': '20140601', 'CMN_KEY': 'C005'},
        # P0003: T2DM (E11) - 외래 2회 (2015), first_year=2013이므로 >= 2013 조건 충족
        {'INDI_DSCM_NO': 'P0003', 'MCEX_SICK_SYM': 'E119', 'MDCARE_STRT_DT': '20150301', 'CMN_KEY': 'C006'},
        {'INDI_DSCM_NO': 'P0003', 'MCEX_SICK_SYM': 'E119', 'MDCARE_STRT_DT': '20150901', 'CMN_KEY': 'C007'},
    ]
    _create_table_from_df(m.conn, 'T40', pd.DataFrame(t40_data))

    # ------------------------------------------------------------------
    # T20 (진료명세서) - 모두 외래 (FORM_CD != '02')
    # ------------------------------------------------------------------
    t20_data = [
        {'INDI_DSCM_NO': 'P0001', 'SICK_SYM1': 'E100', 'MDCARE_STRT_DT': '20130301', 'CMN_KEY': 'C001', 'FORM_CD': '01'},
        {'INDI_DSCM_NO': 'P0002', 'SICK_SYM1': 'E110', 'MDCARE_STRT_DT': '20140101', 'CMN_KEY': 'C004', 'FORM_CD': '01'},
        {'INDI_DSCM_NO': 'P0003', 'SICK_SYM1': 'E119', 'MDCARE_STRT_DT': '20150301', 'CMN_KEY': 'C006', 'FORM_CD': '01'},
        {'INDI_DSCM_NO': 'P0003', 'SICK_SYM1': 'E119', 'MDCARE_STRT_DT': '20150901', 'CMN_KEY': 'C007', 'FORM_CD': '01'},
    ]
    _create_table_from_df(m.conn, 'T20', pd.DataFrame(t20_data))

    # ------------------------------------------------------------------
    # T30 (진료내역) - 빈 테이블 (약물 없음)
    # DDL로 직접 생성: 빈 DataFrame은 DuckDB가 타입을 INTEGER로 추론하여 SUBSTR 오류 발생
    # ------------------------------------------------------------------
    m.conn.execute("""
        CREATE TABLE T30 (
            INDI_DSCM_NO VARCHAR,
            MDCARE_STRT_DT VARCHAR,
            CMN_KEY VARCHAR,
            WK_COMPN_CD VARCHAR,
            RVSN_WK_COMPN_CD VARCHAR,
            EFMDC_CLSF_NO VARCHAR,
            TOT_MCNT VARCHAR
        )
    """)

    # ------------------------------------------------------------------
    # T60 (처방전내역) - 빈 테이블
    # ------------------------------------------------------------------
    m.conn.execute("""
        CREATE TABLE T60 (
            INDI_DSCM_NO VARCHAR,
            MDCARE_STRT_DT VARCHAR,
            CMN_KEY VARCHAR,
            GNL_NM_CD VARCHAR,
            RVSN_WK_COMPN_CD VARCHAR,
            TOT_MCNT VARCHAR
        )
    """)

    yield m
    m.close()


def _run_steps_up_to(cb, step_num):
    """지정된 단계까지 순차 실행하는 헬퍼. mem_manager.cleanup_after_step을 mock한다."""
    if step_num >= 1:
        cb.step1_base_population()
    if step_num >= 2:
        cb.step2_dm_claims()
    if step_num >= 3:
        cb.step3_dm_medications()
    if step_num >= 4:
        cb.step4_classify_groups()
    if step_num >= 5:
        cb.step5_exclude_dementia()
    if step_num >= 6:
        cb.step6_outcomes()


@pytest.fixture
def builder(dm):
    """mem_manager를 mock하여 CohortBuilder를 생성한다."""
    with patch('cohort_builder.mem_manager'):
        cb = CohortBuilder(dm)
        yield cb


# ===========================================================================
# Step 1: 기본 대상 인구 정의
# ===========================================================================
class TestStep1BasePopulation:
    """step1_base_population: 40-64세, 진입기간 2013-2016, 진입 전 1년 자격유지."""

    def test_extracts_all_five_patients(self, builder, dm):
        """5명 모두 BYEAR=1965이고 2012년 자격 보유 -> 전원 통과."""
        n = builder.step1_base_population()
        assert n == 5

    def test_base_population_columns(self, builder, dm):
        """base_population 테이블에 필수 컬럼이 존재하는지 확인."""
        builder.step1_base_population()
        df = dm.query("SELECT * FROM base_population LIMIT 1")
        required = {'INDI_DSCM_NO', 'SEX_TYPE', 'BYEAR', 'first_year', 'last_year'}
        assert required.issubset(set(df.columns))

    def test_excludes_foreigner(self, dm):
        """FOREIGNER_Y='1'인 외국인은 제외된다."""
        # P0001을 외국인으로 변경
        dm.conn.execute("UPDATE JK SET FOREIGNER_Y = '1' WHERE INDI_DSCM_NO = 'P0001'")
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            n = cb.step1_base_population()
        assert n == 4

    def test_excludes_no_prior_year_qualification(self, dm):
        """진입 전 1년 자격(washout)이 없는 환자는 제외된다."""
        # P0001의 2012년 자격 삭제 -> first_year=2013에서 washout 불충족
        # 단, 2014-2016에도 자격 있으므로 first_year가 2014로 밀릴 수 있음
        # 2012, 2013년 자격 모두 삭제하면 first_year=2014인데 2013년 자격 없음 -> 제외
        dm.conn.execute("""
            DELETE FROM JK
            WHERE INDI_DSCM_NO = 'P0001'
              AND CAST(STD_YYYY AS INTEGER) <= 2013
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            n = cb.step1_base_population()
        # P0001: first_year=2014, 2013년 JK 없음 -> 제외
        assert n == 4

    def test_age_range_boundary(self, dm):
        """MAX_AGE(64)를 초과하는 환자는 제외된다."""
        # P0001의 BYEAR을 1948로 변경 -> 2013년 기준 65세 (초과)
        dm.conn.execute("UPDATE JK SET BYEAR = '1948' WHERE INDI_DSCM_NO = 'P0001'")
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            n = cb.step1_base_population()
        assert n == 4


# ===========================================================================
# Step 2: 당뇨 진단 청구 식별
# ===========================================================================
class TestStep2DmClaims:
    """step2_dm_claims: T40+T20에서 DM 청구를 식별하고 prevalent bias를 방지한다."""

    def test_identifies_dm_claims(self, builder, dm):
        """P0001(T1DM), P0002(T2DM), P0003(T2DM)의 청구가 식별된다."""
        builder.step1_base_population()
        n = builder.step2_dm_claims()
        assert n > 0

        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM dm_claims ORDER BY INDI_DSCM_NO"
        )['INDI_DSCM_NO'].tolist()
        assert 'P0001' in patients
        assert 'P0002' in patients
        assert 'P0003' in patients
        # P0004, P0005는 DM 청구 없음
        assert 'P0004' not in patients
        assert 'P0005' not in patients

    def test_dm_type_classification(self, builder, dm):
        """T1DM/T2DM 유형이 올바르게 분류된다."""
        builder.step1_base_population()
        builder.step2_dm_claims()

        types = dm.query("""
            SELECT INDI_DSCM_NO, dm_type
            FROM dm_claims
            GROUP BY INDI_DSCM_NO, dm_type
            ORDER BY INDI_DSCM_NO
        """)
        p1_types = types[types['INDI_DSCM_NO'] == 'P0001']['dm_type'].tolist()
        p2_types = types[types['INDI_DSCM_NO'] == 'P0002']['dm_type'].tolist()
        assert 'T1DM' in p1_types
        assert 'T2DM' in p2_types

    def test_prevalent_dm_exclusion(self, dm):
        """진입연도 이전에 DM 청구가 있는 환자(prevalent DM)는 제외된다."""
        # P0002의 first_year=2013 (BYEAR=1965, 2013년 48세).
        # 2012년(진입 전)에 DM 청구를 추가하면 prevalent DM으로 제외된다.
        dm.conn.execute("""
            INSERT INTO T40 VALUES
            ('P0002', 'E110', '20120601', 'C_PREV')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()

        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM dm_claims"
        )['INDI_DSCM_NO'].tolist()
        # P0002는 prevalent DM으로 제외
        assert 'P0002' not in patients
        # P0001, P0003는 여전히 포함
        assert 'P0001' in patients
        assert 'P0003' in patients


# ===========================================================================
# Step 3: 당뇨 약물 처방 식별
# ===========================================================================
class TestStep3DmMedications:
    """step3_dm_medications: T30+T60에서 DM 약물을 식별한다."""

    def test_empty_medications(self, builder, dm):
        """약물 데이터가 비어있으면 0건을 반환한다."""
        _run_steps_up_to(builder, 2)
        n = builder.step3_dm_medications()
        assert n == 0

    def test_identifies_oha_medication(self, dm):
        """OHA 약물 처방이 올바르게 식별된다."""
        # T30에 metformin 처방 추가 (148801)
        dm.conn.execute("""
            INSERT INTO T30 VALUES
            ('P0002', '20140201', 'C004', '148801ABC', '', '', '30')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()
            n = cb.step3_dm_medications()
        assert n == 1
        med = dm.query("SELECT med_type FROM dm_medications")
        assert med.iloc[0]['med_type'] == 'OHA'

    def test_identifies_insulin_by_efmdc(self, dm):
        """EFMDC 분류코드로 인슐린이 식별된다."""
        dm.conn.execute("""
            INSERT INTO T30 VALUES
            ('P0001', '20130401', 'C001', '', '', '39620', '30')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()
            n = cb.step3_dm_medications()
        assert n == 1
        med = dm.query("SELECT med_type FROM dm_medications")
        assert med.iloc[0]['med_type'] == 'INSULIN'


# ===========================================================================
# Step 4: 노출군 분류
# ===========================================================================
class TestStep4ClassifyGroups:
    """step4_classify_groups: 외래2회+/입원1회+ 기준으로 T1DM/T2DM/NON_DM 분류."""

    def test_classification_basic(self, builder, dm):
        """P0001=T1DM, P0002/P0003=T2DM_NOMED, P0004/P0005=NON_DM."""
        _run_steps_up_to(builder, 3)
        result = builder.step4_classify_groups()

        groups = dm.query("""
            SELECT INDI_DSCM_NO, exposure_group
            FROM exposure_groups
            ORDER BY INDI_DSCM_NO
        """)
        group_map = dict(zip(groups['INDI_DSCM_NO'], groups['exposure_group']))

        assert group_map['P0001'] == 'T1DM'
        assert group_map['P0002'] == 'T2DM_NOMED'
        assert group_map['P0003'] == 'T2DM_NOMED'
        assert group_map['P0004'] == 'NON_DM'
        assert group_map['P0005'] == 'NON_DM'

    def test_index_date_dm_patients(self, builder, dm):
        """DM 환자의 index_date는 첫 DM 진단일이다."""
        _run_steps_up_to(builder, 3)
        builder.step4_classify_groups()

        dates = dm.query("""
            SELECT INDI_DSCM_NO, index_date
            FROM exposure_groups
            WHERE exposure_group != 'NON_DM'
            ORDER BY INDI_DSCM_NO
        """)
        date_map = dict(zip(dates['INDI_DSCM_NO'], dates['index_date']))
        assert date_map['P0001'] == '20130301'
        assert date_map['P0002'] == '20140101'
        assert date_map['P0003'] == '20150301'

    def test_index_date_non_dm(self, builder, dm):
        """NON_DM의 index_date는 first_year + '0101'이다."""
        _run_steps_up_to(builder, 3)
        builder.step4_classify_groups()

        dates = dm.query("""
            SELECT INDI_DSCM_NO, index_date
            FROM exposure_groups
            WHERE exposure_group = 'NON_DM'
            ORDER BY INDI_DSCM_NO
        """)
        date_map = dict(zip(dates['INDI_DSCM_NO'], dates['index_date']))
        assert date_map['P0004'] == '20130101'
        assert date_map['P0005'] == '20130101'

    def test_dual_dm_exclusion(self, dm):
        """T1DM + T2DM 동시 보유자는 제외된다."""
        # P0001에 T2DM 청구 추가 -> T1DM+T2DM 동시 보유
        dm.conn.execute("""
            INSERT INTO T40 VALUES
            ('P0001', 'E119', '20130801', 'C_DUAL1'),
            ('P0001', 'E119', '20131001', 'C_DUAL2')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()
            cb.step3_dm_medications()
            cb.step4_classify_groups()

        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM exposure_groups"
        )['INDI_DSCM_NO'].tolist()
        # P0001은 T1DM+T2DM 동시 보유로 제외
        assert 'P0001' not in patients
        # 나머지는 유지
        assert 'P0002' in patients
        assert 'P0003' in patients

    def test_result_dataframe_structure(self, builder, dm):
        """step4 반환값은 exposure_group별 카운트 DataFrame이다."""
        _run_steps_up_to(builder, 3)
        result = builder.step4_classify_groups()
        assert isinstance(result, pd.DataFrame)
        assert 'exposure_group' in result.columns
        assert 'n' in result.columns

    def test_t2dm_insulin_classification(self, dm):
        """인슐린 처방이 있는 T2DM 환자는 T2DM_INSULIN로 분류된다."""
        # P0002에 인슐린 처방 추가 (index_date 이후 1년 이내)
        dm.conn.execute("""
            INSERT INTO T30 VALUES
            ('P0002', '20140201', 'C004', '', '', '39620', '30')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()
            cb.step3_dm_medications()
            cb.step4_classify_groups()

        group = dm.query("""
            SELECT exposure_group FROM exposure_groups
            WHERE INDI_DSCM_NO = 'P0002'
        """).iloc[0]['exposure_group']
        assert group == 'T2DM_INSULIN'

    def test_t2dm_oha_classification(self, dm):
        """OHA 처방만 있는 T2DM 환자는 T2DM_OHA로 분류된다."""
        # P0002에 OHA 처방 추가 (metformin, index_date 이후 1년 이내)
        dm.conn.execute("""
            INSERT INTO T30 VALUES
            ('P0002', '20140201', 'C004', '148801ABC', '', '', '30')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()
            cb.step3_dm_medications()
            cb.step4_classify_groups()

        group = dm.query("""
            SELECT exposure_group FROM exposure_groups
            WHERE INDI_DSCM_NO = 'P0002'
        """).iloc[0]['exposure_group']
        assert group == 'T2DM_OHA'

    def test_insufficient_claims_excluded(self, dm):
        """외래 2회 미만 + 입원 0회인 환자는 DM으로 분류되지 않는다."""
        # P0002의 청구를 1건만 남김
        dm.conn.execute("DELETE FROM T40 WHERE CMN_KEY = 'C005'")
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.step1_base_population()
            cb.step2_dm_claims()
            cb.step3_dm_medications()
            cb.step4_classify_groups()

        group = dm.query("""
            SELECT exposure_group FROM exposure_groups
            WHERE INDI_DSCM_NO = 'P0002'
        """).iloc[0]['exposure_group']
        # 외래 1회만 -> DM 기준 미달 -> NON_DM
        assert group == 'NON_DM'


# ===========================================================================
# Step 5: 기존 치매 진단 + 항치매약 사용자 제외
# ===========================================================================
class TestStep5ExcludeDementia:
    """step5_exclude_dementia: index_date 이전 치매 진단/항치매약 사용자 제외."""

    def test_no_exclusion_in_clean_data(self, builder, dm):
        """치매 진단/약물이 없으면 아무도 제외되지 않는다."""
        _run_steps_up_to(builder, 4)
        n, excl = builder.step5_exclude_dementia()
        assert excl == 0
        assert n == 5  # 전원 유지

    def test_excludes_pre_existing_dementia_t40(self, dm):
        """index_date 이전 T40 치매 진단자가 제외된다."""
        # P0004에 index_date(20130101) 이전은 불가하므로 동일일 치매 진단 추가
        # index_date 이전/동일 = 제외 대상 (조건: <= index_date)
        dm.conn.execute("""
            INSERT INTO T40 VALUES
            ('P0004', 'F009', '20130101', 'C_DEM1')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 4)
            n, excl = cb.step5_exclude_dementia()
        assert excl >= 1
        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM study_cohort"
        )['INDI_DSCM_NO'].tolist()
        assert 'P0004' not in patients

    def test_excludes_pre_existing_dementia_t20(self, dm):
        """index_date 이전 T20 주상병 치매 진단자가 제외된다."""
        dm.conn.execute("""
            INSERT INTO T20 VALUES
            ('P0004', 'G300', '20130101', 'C_DEM2', '01')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 4)
            n, excl = cb.step5_exclude_dementia()
        assert excl >= 1
        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM study_cohort"
        )['INDI_DSCM_NO'].tolist()
        assert 'P0004' not in patients

    def test_excludes_anti_dementia_drug_t30(self, dm):
        """index_date 이전 항치매약(T30) 사용자가 제외된다."""
        # 도네페질(372701) 처방 추가
        dm.conn.execute("""
            INSERT INTO T30 VALUES
            ('P0004', '20130101', 'C_DRUG1', '372701ABC', '', '', '30')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 4)
            n, excl = cb.step5_exclude_dementia()
        assert excl >= 1
        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM study_cohort"
        )['INDI_DSCM_NO'].tolist()
        assert 'P0004' not in patients

    def test_post_index_dementia_not_excluded(self, dm):
        """index_date 이후 치매 진단은 제외 대상이 아니다 (결과변수)."""
        dm.conn.execute("""
            INSERT INTO T40 VALUES
            ('P0004', 'F009', '20150601', 'C_DEM_POST')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 4)
            n, excl = cb.step5_exclude_dementia()
        assert excl == 0
        patients = dm.query(
            "SELECT DISTINCT INDI_DSCM_NO FROM study_cohort"
        )['INDI_DSCM_NO'].tolist()
        assert 'P0004' in patients

    def test_study_cohort_preserves_columns(self, builder, dm):
        """study_cohort가 exposure_groups의 컬럼을 그대로 유지한다."""
        _run_steps_up_to(builder, 4)
        builder.step5_exclude_dementia()
        eg_cols = set(dm.query("SELECT * FROM exposure_groups LIMIT 0").columns)
        sc_cols = set(dm.query("SELECT * FROM study_cohort LIMIT 0").columns)
        assert eg_cols == sc_cols


# ===========================================================================
# Step 6: 결과변수 식별 + censoring
# ===========================================================================
class TestStep6Outcomes:
    """step6_outcomes: 치매 결과변수, 추적기간, censoring 로직 검증."""

    def test_follow_up_calculated(self, builder, dm):
        """follow_up_days와 follow_up_years가 산출된다."""
        _run_steps_up_to(builder, 5)
        builder.step6_outcomes()
        df = dm.query("SELECT follow_up_days, follow_up_years FROM analysis_data LIMIT 1")
        assert df.iloc[0]['follow_up_days'] is not None
        assert df.iloc[0]['follow_up_years'] is not None

    def test_no_dementia_events_in_clean_data(self, builder, dm):
        """치매 진단이 없으면 이벤트 = 0이다."""
        _run_steps_up_to(builder, 5)
        result = builder.step6_outcomes()
        # 모든 dementia_event 합계 = 0
        total_events = dm.query("SELECT SUM(dementia_event) AS s FROM analysis_data").iloc[0]['s']
        assert total_events == 0

    def test_dementia_event_detected(self, dm):
        """index_date 이후, 65세 이전 치매 진단이 이벤트로 기록된다."""
        # P0004에 index_date(20130101) 이후 치매 진단 추가 (2015년, 50세)
        dm.conn.execute("""
            INSERT INTO T40 VALUES
            ('P0004', 'F009', '20150601', 'C_OUT1')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 5)
            cb.step6_outcomes()

        row = dm.query("""
            SELECT dementia_event, dementia_date FROM analysis_data
            WHERE INDI_DSCM_NO = 'P0004'
        """)
        assert row.iloc[0]['dementia_event'] == 1
        assert row.iloc[0]['dementia_date'] == '20150601'

    def test_age65_censoring(self, dm):
        """65세 도달 시점에서 censoring된다 (BYEAR=1965 -> age65_date=20300101)."""
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 5)
            cb.step6_outcomes()

        row = dm.query("""
            SELECT age65_date FROM analysis_data WHERE INDI_DSCM_NO = 'P0001'
        """)
        # BYEAR=1965 + 65 = 2030 -> '20300101'
        assert row.iloc[0]['age65_date'] == '20300101'

    def test_death_censoring(self, dm):
        """사망자(P0005)의 censor_date가 사망일로 설정된다."""
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 5)
            cb.step6_outcomes()

        row = dm.query("""
            SELECT censor_date, death_event, death_date
            FROM analysis_data
            WHERE INDI_DSCM_NO = 'P0005'
        """)
        # P0005: HHDT_DEATH=20221231, index_date=20130101
        # censor_date = LEAST(study_end, age65, death_date) = LEAST(20241231, 20300101, 20221231) = 20221231
        assert row.iloc[0]['censor_date'] == '20221231'
        assert row.iloc[0]['death_event'] == 1

    def test_study_end_censoring(self, dm):
        """study_end(20241231)이 다른 censoring보다 먼저 도달하면 적용된다."""
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 5)
            cb.step6_outcomes()

        # P0001: age65=20300101, no death -> censor_date = study_end = 20241231
        row = dm.query("""
            SELECT censor_date FROM analysis_data WHERE INDI_DSCM_NO = 'P0001'
        """)
        assert row.iloc[0]['censor_date'] == '20241231'

    def test_follow_up_days_positive(self, builder, dm):
        """모든 환자의 follow_up_days > 0이어야 한다."""
        _run_steps_up_to(builder, 5)
        builder.step6_outcomes()
        bad = dm.query("SELECT COUNT(*) AS n FROM analysis_data WHERE follow_up_days <= 0")
        assert bad.iloc[0]['n'] == 0

    def test_ad_and_vad_outcomes_separate(self, dm):
        """AD와 VAD 결과변수가 독립적으로 기록된다."""
        # P0004에 AD(G30) 진단, P0005에 VAD(F01) 진단 추가
        dm.conn.execute("""
            INSERT INTO T40 VALUES
            ('P0004', 'G300', '20150601', 'C_AD1'),
            ('P0005', 'F019', '20150601', 'C_VAD1')
        """)
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            _run_steps_up_to(cb, 5)
            cb.step6_outcomes()

        p4 = dm.query("""
            SELECT ad_event, vad_event FROM analysis_data WHERE INDI_DSCM_NO = 'P0004'
        """)
        p5 = dm.query("""
            SELECT ad_event, vad_event FROM analysis_data WHERE INDI_DSCM_NO = 'P0005'
        """)
        assert p4.iloc[0]['ad_event'] == 1
        assert p4.iloc[0]['vad_event'] == 0
        assert p5.iloc[0]['ad_event'] == 0
        assert p5.iloc[0]['vad_event'] == 1

    def test_outcome_returns_dataframe(self, builder, dm):
        """step6 반환값은 exposure_group별 집계 DataFrame이다."""
        _run_steps_up_to(builder, 5)
        result = builder.step6_outcomes()
        assert isinstance(result, pd.DataFrame)
        assert 'exposure_group' in result.columns
        assert 'n' in result.columns
        assert 'dem' in result.columns


# ===========================================================================
# build_cohort: 전체 파이프라인 통합 테스트
# ===========================================================================
class TestBuildCohortFull:
    """build_cohort: 전체 파이프라인을 end-to-end로 실행한다."""

    def test_end_to_end(self, dm):
        """전체 6단계가 오류 없이 완료되고 analysis_data가 생성된다."""
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            results = cb.build_cohort()

        assert 'base_n' in results
        assert results['base_n'] == 5

        assert 'dm_claims' in results
        assert results['dm_claims'] > 0

        assert 'final_n' in results
        assert results['final_n'] == 5

        assert 'outcomes' in results
        assert isinstance(results['outcomes'], pd.DataFrame)

        # analysis_data 테이블 존재 확인
        assert dm._table_exists('analysis_data')
        n = dm._get_row_count('analysis_data')
        assert n == 5

    def test_end_to_end_with_callback(self, dm):
        """콜백 함수가 정상적으로 호출된다."""
        messages = []
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            results = cb.build_cohort(cb=lambda msg: messages.append(msg))

        # 각 단계 + 완료 메시지 = 최소 7개
        assert len(messages) >= 7
        assert '코호트 구축 완료!' in messages

    def test_all_groups_present(self, dm):
        """T1DM, T2DM_NOMED, NON_DM 그룹이 모두 존재한다."""
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.build_cohort()

        groups = dm.query("""
            SELECT DISTINCT exposure_group FROM analysis_data ORDER BY 1
        """)['exposure_group'].tolist()
        assert 'T1DM' in groups
        assert 'NON_DM' in groups
        # P0002, P0003는 약물 없으므로 T2DM_NOMED
        assert 'T2DM_NOMED' in groups

    def test_analysis_data_has_required_columns(self, dm):
        """analysis_data에 분석에 필요한 모든 컬럼이 존재한다."""
        with patch('cohort_builder.mem_manager'):
            cb = CohortBuilder(dm)
            cb.build_cohort()

        df = dm.query("SELECT * FROM analysis_data LIMIT 0")
        required = {
            'INDI_DSCM_NO', 'SEX_TYPE', 'BYEAR', 'exposure_group',
            'index_date', 'censor_date', 'age65_date',
            'dementia_event', 'ad_event', 'vad_event',
            'death_event', 'competing_death_event',
            'follow_up_days', 'follow_up_years',
        }
        assert required.issubset(set(df.columns))
