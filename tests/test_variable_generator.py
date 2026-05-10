"""variable_generator.py 단위 테스트

DuckDB in-memory DB로 합성 데이터를 구성하여
VariableGenerator의 각 메서드를 검증한다.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# MockDataManager: DuckDB in-memory로 VariableGenerator가 기대하는 인터페이스 제공
# ---------------------------------------------------------------------------
class MockDataManager:
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
    """MockDataManager + 합성 테이블 생성"""
    mgr = MockDataManager()

    # -- analysis_data: 3명의 환자 --
    # P001: 1970년생, index 2015, T1DM, first_dm 20100301
    # P002: 1965년생, index 2016, T2DM_OHA, first_dm 20020615
    # P003: 1980년생, index 2017, NON_DM, first_dm NULL
    mgr.execute("""
        CREATE TABLE analysis_data (
            INDI_DSCM_NO VARCHAR,
            SEX_TYPE VARCHAR,
            BYEAR VARCHAR,
            exposure_group VARCHAR,
            index_date VARCHAR,
            first_dm_date VARCHAR,
            censor_date VARCHAR,
            dementia_date VARCHAR,
            ad_date VARCHAR,
            vad_date VARCHAR,
            death_date VARCHAR,
            age65_date VARCHAR,
            dementia_event INTEGER,
            ad_event INTEGER,
            vad_event INTEGER,
            death_event INTEGER,
            competing_death_event INTEGER,
            follow_up_days INTEGER,
            follow_up_years DOUBLE
        )
    """)
    mgr.execute("""
        INSERT INTO analysis_data VALUES
        ('P001','1','1970','T1DM','20150101','20100301','20191231',NULL,NULL,NULL,NULL,'20350101',0,0,0,0,0,1826,5.0),
        ('P002','2','1965','T2DM_OHA','20160601','20020615','20191231','20180301','20180301',NULL,NULL,'20300101',1,1,0,0,0,1309,3.58),
        ('P003','1','1980','NON_DM','20170101',NULL,'20191231',NULL,NULL,NULL,NULL,'20450101',0,0,0,0,0,1095,3.0)
    """)

    # -- JK: 자격 테이블 --
    mgr.execute("""
        CREATE TABLE JK (
            INDI_DSCM_NO VARCHAR,
            STD_YYYY VARCHAR,
            SES05 INTEGER,
            GAIBJA_TYPE VARCHAR,
            RVSN_ADDR_CD VARCHAR,
            SURV_YR INTEGER,
            HHDT_DEATH VARCHAR
        )
    """)
    mgr.execute("""
        INSERT INTO JK VALUES
        ('P001','2015',3,'1','11',2019,NULL),
        ('P002','2016',1,'2','26',2019,NULL),
        ('P003','2017',5,'1','41',2019,NULL)
    """)

    # -- T40: 진단 테이블 --
    # P001: 고혈압(I10), DM_WITH_COMP(E103 retinopathy)
    # P002: 이상지질혈증(E780), 우울증(F32), MI(I21)
    # P003: 없음
    mgr.execute("""
        CREATE TABLE T40 (
            INDI_DSCM_NO VARCHAR,
            MCEX_SICK_SYM VARCHAR,
            MDCARE_STRT_DT VARCHAR
        )
    """)
    mgr.execute("""
        INSERT INTO T40 VALUES
        ('P001','I101','20140501'),
        ('P001','E1031','20140801'),
        ('P002','E7801','20151001'),
        ('P002','F321','20150601'),
        ('P002','I211','20160101')
    """)

    # -- GJ_RESULT: 건강검진 결과 --
    # P001: BMI 22.5 (NW), SBP 130
    # P002: BMI 27.0 (OB), SBP 145
    # P003: 검진 없음
    mgr.execute("""
        CREATE TABLE GJ_RESULT (
            INDI_DSCM_NO VARCHAR,
            HC_DT VARCHAR,
            G1E_BMI DOUBLE,
            G1E_BP_SYS DOUBLE,
            G1E_BP_DIA DOUBLE,
            G1E_FBS DOUBLE,
            G1E_TOT_CHOL DOUBLE,
            G1E_TG DOUBLE,
            G1E_HDL DOUBLE,
            G1E_LDL_CALC DOUBLE,
            G1E_CRTN DOUBLE,
            G1E_GFR DOUBLE,
            G1E_HGB DOUBLE,
            G1E_SGOT DOUBLE,
            G1E_SGPT DOUBLE,
            G1E_GGT DOUBLE
        )
    """)
    mgr.execute("""
        INSERT INTO GJ_RESULT VALUES
        ('P001','20140601',22.5,130,80,95,200,150,55,120,0.9,90,14.5,25,20,30),
        ('P002','20160101',27.0,145,92,110,240,200,40,160,1.1,75,13.0,35,40,55)
    """)

    # -- GJ_QUEST: 건강 문진 --
    # P001: 현재 흡연, 주 2회 음주 (Mild)
    # P002: 비흡연, 주 5회 음주 (Heavy)
    mgr.execute("""
        CREATE TABLE GJ_QUEST (
            INDI_DSCM_NO VARCHAR,
            HC_BZ_YYYY VARCHAR,
            Q_SMK_YN INTEGER,
            Q_SMK_NOW_YN INTEGER,
            Q_DRK_PER INTEGER
        )
    """)
    mgr.execute("""
        INSERT INTO GJ_QUEST VALUES
        ('P001','2014',0,1,2),
        ('P002','2015',0,0,5)
    """)

    yield mgr
    mgr.close()


@pytest.fixture
def vg(dm):
    """VariableGenerator 인스턴스 — mem_manager를 모킹하여 GC 무효화"""
    with patch('variable_generator.mem_manager') as mock_mem:
        mock_mem.cleanup_after_step = MagicMock()
        from variable_generator import VariableGenerator
        gen = VariableGenerator(dm)
        gen._mock_mem = mock_mem  # generate_all 테스트용 참조
        yield gen


# ---------------------------------------------------------------------------
# 1. Demographics
# ---------------------------------------------------------------------------
class TestGenerateDemographics:
    def test_age_at_index(self, dm, vg):
        vg.generate_demographics()
        df = dm.query("SELECT * FROM demo_vars ORDER BY INDI_DSCM_NO")

        assert len(df) == 3
        # P001: 2015 - 1970 = 45
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'age_at_index'].iloc[0] == 45
        # P002: 2016 - 1965 = 51
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'age_at_index'].iloc[0] == 51
        # P003: 2017 - 1980 = 37
        assert df.loc[df['INDI_DSCM_NO'] == 'P003', 'age_at_index'].iloc[0] == 37

    def test_age_group(self, dm, vg):
        vg.generate_demographics()
        df = dm.query("SELECT * FROM demo_vars ORDER BY INDI_DSCM_NO")

        # P001(45), P002(51) → '40-54';  P003(37) → '55-64' (else branch)
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'age_group'].iloc[0] == '40-54'
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'age_group'].iloc[0] == '40-54'
        assert df.loc[df['INDI_DSCM_NO'] == 'P003', 'age_group'].iloc[0] == '55-64'

    def test_income_quintile(self, dm, vg):
        vg.generate_demographics()
        df = dm.query("SELECT * FROM demo_vars ORDER BY INDI_DSCM_NO")

        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'income_quintile'].iloc[0] == 3
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'income_quintile'].iloc[0] == 1
        assert df.loc[df['INDI_DSCM_NO'] == 'P003', 'income_quintile'].iloc[0] == 5

    def test_insurance_and_region(self, dm, vg):
        vg.generate_demographics()
        df = dm.query("SELECT * FROM demo_vars ORDER BY INDI_DSCM_NO")

        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'insurance_type'].iloc[0] == '1'
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'region_code'].iloc[0] == '26'

    def test_jk_duplicate_rows_deduplicated(self, dm, vg):
        """JK에 동일 연도 중복 레코드가 있어도 demo_vars 행이 1건만 생성된다."""
        dm.execute("""
            INSERT INTO JK VALUES ('P001','2015',4,'2','22',2019,NULL)
        """)
        vg.generate_demographics()
        df = dm.query("SELECT * FROM demo_vars WHERE INDI_DSCM_NO='P001'")
        assert len(df) == 1, f"JK 중복 레코드 시 1행이어야 함, 실제: {len(df)}"


# ---------------------------------------------------------------------------
# 2. Health Behaviors
# ---------------------------------------------------------------------------
class TestGenerateHealthBehaviors:
    def test_bmi_categories(self, dm, vg):
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM health_exam_final ORDER BY INDI_DSCM_NO")

        # P001: BMI 22.5 → NW (18.5 <= 22.5 < 23)
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'bmi_cat'].iloc[0] == 'NW'
        # P002: BMI 27.0 → OB (>= 25)
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'bmi_cat'].iloc[0] == 'OB'

    def test_bmi_values(self, dm, vg):
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM health_exam_final ORDER BY INDI_DSCM_NO")

        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'bmi'].iloc[0] == pytest.approx(22.5)
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'sbp'].iloc[0] == pytest.approx(130)

    def test_smoking_status(self, dm, vg):
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM quest_final ORDER BY INDI_DSCM_NO")

        # P001: Q_SMK_NOW_YN=1 → Current
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'smoking_status'].iloc[0] == 'Current'
        # P002: Q_SMK_NOW_YN=0, Q_SMK_YN=0 → Never
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'smoking_status'].iloc[0] == 'Never'

    def test_drinking_status(self, dm, vg):
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM quest_final ORDER BY INDI_DSCM_NO")

        # P001: Q_DRK_PER=2 → Mild (<=2)
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'drinking_status'].iloc[0] == 'Mild'
        # P002: Q_DRK_PER=5 → Heavy (>4)
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'drinking_status'].iloc[0] == 'Heavy'

    def test_no_exam_patient_excluded(self, dm, vg):
        """P003은 GJ_RESULT에 없으므로 health_exam_final에 포함되지 않음"""
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM health_exam_final")
        assert 'P003' not in df['INDI_DSCM_NO'].values

    def test_no_quest_patient_excluded(self, dm, vg):
        """P003은 GJ_QUEST에 없으므로 quest_final에 포함되지 않음"""
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM quest_final")
        assert 'P003' not in df['INDI_DSCM_NO'].values

    def test_missing_gj_tables(self, dm, vg):
        """GJ_RESULT/GJ_QUEST 테이블이 없을 때 빈 테이블 생성"""
        dm.execute("DROP TABLE GJ_RESULT")
        dm.execute("DROP TABLE GJ_QUEST")

        vg.generate_health_behaviors()

        he = dm.query("SELECT COUNT(*) AS n FROM health_exam_final")
        assert he.iloc[0, 0] == 0
        qf = dm.query("SELECT COUNT(*) AS n FROM quest_final")
        assert qf.iloc[0, 0] == 0


# ---------------------------------------------------------------------------
# 3. Comorbidities
# ---------------------------------------------------------------------------
class TestGenerateComorbidities:
    def test_hypertension_flag(self, dm, vg):
        vg._create_t40_filtered()
        vg.generate_comorbidities()

        df = dm.query("SELECT * FROM comorbidity_vars ORDER BY INDI_DSCM_NO")
        # P001: I101 matches I10 → hypertension=1
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'comor_hypertension'].iloc[0] == 1
        # P002: no hypertension code
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'comor_hypertension'].iloc[0] == 0

    def test_dyslipidemia_flag(self, dm, vg):
        vg._create_t40_filtered()
        vg.generate_comorbidities()

        df = dm.query("SELECT * FROM comorbidity_vars ORDER BY INDI_DSCM_NO")
        # P002: E7801 matches E780 → dyslipidemia=1
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'comor_dyslipidemia'].iloc[0] == 1
        # P001: no dyslipidemia code
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'comor_dyslipidemia'].iloc[0] == 0

    def test_depression_flag(self, dm, vg):
        vg._create_t40_filtered()
        vg.generate_comorbidities()

        df = dm.query("SELECT * FROM comorbidity_vars ORDER BY INDI_DSCM_NO")
        # P002: F321 matches F32 → depression=1
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'comor_depression'].iloc[0] == 1
        # P003: no T40 records
        assert df.loc[df['INDI_DSCM_NO'] == 'P003', 'comor_depression'].iloc[0] == 0

    def test_no_diagnosis_patient_all_zero(self, dm, vg):
        """P003: T40 레코드 없음 → 모든 comorbidity 0"""
        vg._create_t40_filtered()
        vg.generate_comorbidities()

        df = dm.query("SELECT * FROM comorbidity_vars WHERE INDI_DSCM_NO='P003'")
        comor_cols = [c for c in df.columns if c.startswith('comor_')]
        for col in comor_cols:
            assert df[col].iloc[0] == 0, f"P003 {col} should be 0"


# ---------------------------------------------------------------------------
# 3b. LOOKBACK_YEARS 필터 + 흡연력 Former 케이스
# ---------------------------------------------------------------------------
class TestLookbackYearsFilter:
    def test_out_of_window_diagnosis_excluded(self, dm, vg):
        """LOOKBACK_YEARS=1: index_date 1년 초과 이전 진단은 _t40_pre_index에서 제외"""
        # P001 index=20150101 → window: 20140101~20150101
        # 20130601 record is outside the window (> 1 year before index)
        dm.execute("INSERT INTO T40 VALUES ('P001','I999','20130601')")
        vg._create_t40_filtered()
        result = dm.query(
            "SELECT COUNT(*) AS n FROM _t40_pre_index "
            "WHERE INDI_DSCM_NO='P001' AND MCEX_SICK_SYM='I999'"
        )
        assert result.iloc[0]['n'] == 0, "out-of-window record should not appear in _t40_pre_index"

    def test_in_window_diagnosis_included(self, dm, vg):
        """LOOKBACK_YEARS=1: window 내 진단은 _t40_pre_index에 포함"""
        # P001 I101 on 20140501 is within window (20140101~20150101)
        vg._create_t40_filtered()
        result = dm.query(
            "SELECT COUNT(*) AS n FROM _t40_pre_index "
            "WHERE INDI_DSCM_NO='P001' AND MCEX_SICK_SYM='I101'"
        )
        assert result.iloc[0]['n'] == 1, "in-window record should appear in _t40_pre_index"


class TestSmokingFormerCase:
    def test_former_smoker_case(self, dm, vg):
        """Q_SMK_NOW_YN=0, Q_SMK_YN=1 → 'Former' (과거 흡연자)"""
        dm.execute(
            "INSERT INTO GJ_QUEST VALUES ('P003','2016',1,0,0)"
        )
        vg.generate_health_behaviors()
        df = dm.query("SELECT * FROM quest_final WHERE INDI_DSCM_NO='P003'")
        assert df.iloc[0]['smoking_status'] == 'Former', \
            f"expected 'Former', got {df.iloc[0]['smoking_status']!r}"


# ---------------------------------------------------------------------------
# 4. DM Complications
# ---------------------------------------------------------------------------
class TestGenerateDmComplications:
    def test_retinopathy_flag(self, dm, vg):
        vg._create_t40_filtered()
        vg.generate_dm_complications()

        df = dm.query("SELECT * FROM complication_vars ORDER BY INDI_DSCM_NO")
        # P001: E1031 matches E103 → retinopathy=1
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'comp_retinopathy'].iloc[0] == 1
        # P002: no retinopathy code
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'comp_retinopathy'].iloc[0] == 0

    def test_no_complications_all_zero(self, dm, vg):
        """P003: 합병증 없음"""
        vg._create_t40_filtered()
        vg.generate_dm_complications()

        df = dm.query("SELECT * FROM complication_vars WHERE INDI_DSCM_NO='P003'")
        comp_cols = [c for c in df.columns if c.startswith('comp_')]
        for col in comp_cols:
            assert df[col].iloc[0] == 0, f"P003 {col} should be 0"

    def test_all_complication_columns_exist(self, dm, vg):
        """DM_COMPLICATION_CODES의 모든 키에 대응하는 컬럼 존재"""
        from config import DM_COMPLICATION_CODES
        vg._create_t40_filtered()
        vg.generate_dm_complications()

        df = dm.query("SELECT * FROM complication_vars LIMIT 1")
        for key in DM_COMPLICATION_CODES:
            assert f'comp_{key.lower()}' in df.columns, f"Missing comp_{key.lower()}"


# ---------------------------------------------------------------------------
# 5. CCI (Charlson Comorbidity Index)
# ---------------------------------------------------------------------------
class TestGenerateCCI:
    def test_cci_score_p001(self, dm, vg):
        """P001: E1031 → DM_WITH_COMP(w=2). I101 is hypertension — not in CCI."""
        vg._create_t40_filtered()
        vg.generate_cci()

        df = dm.query("SELECT * FROM cci_vars WHERE INDI_DSCM_NO='P001'")
        # E1031 matches E103 in DM_WITH_COMP (weight=2)
        assert df['cci_score'].iloc[0] == 2

    def test_cci_score_p002(self, dm, vg):
        """P002: I211 → MI(w=1), F321 → DEMENTIA_CCI? No (F32 not in DEMENTIA_CCI).
        E7801 → not in CCI. So only MI=1."""
        vg._create_t40_filtered()
        vg.generate_cci()

        df = dm.query("SELECT * FROM cci_vars WHERE INDI_DSCM_NO='P002'")
        # I211 matches I21 in MI (weight=1)
        assert df['cci_score'].iloc[0] == 1

    def test_cci_score_p003(self, dm, vg):
        """P003: T40 레코드 없음 → CCI=0"""
        vg._create_t40_filtered()
        vg.generate_cci()

        df = dm.query("SELECT * FROM cci_vars WHERE INDI_DSCM_NO='P003'")
        assert df['cci_score'].iloc[0] == 0

    def test_cci_category(self, dm, vg):
        vg._create_t40_filtered()
        vg.generate_cci()

        df = dm.query("SELECT * FROM cci_vars ORDER BY INDI_DSCM_NO")
        # P001: score=2 → '1-2'
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'cci_category'].iloc[0] == '1-2'
        # P002: score=1 → '1-2'
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'cci_category'].iloc[0] == '1-2'
        # P003: score=0 → '0'
        assert df.loc[df['INDI_DSCM_NO'] == 'P003', 'cci_category'].iloc[0] == '0'


# ---------------------------------------------------------------------------
# 6. DM Duration
# ---------------------------------------------------------------------------
class TestGenerateDmDuration:
    def test_duration_years(self, dm, vg):
        vg.generate_dm_duration()
        df = dm.query("SELECT * FROM dm_duration_vars ORDER BY INDI_DSCM_NO")

        # P001: index=20150101, first_dm=20100301 → ~4.84 years
        dur_p001 = df.loc[df['INDI_DSCM_NO'] == 'P001', 'dm_duration_years'].iloc[0]
        assert 4.5 < dur_p001 < 5.0  # approximately 4.84

        # P002: index=20160601, first_dm=20020615 → ~13.96 years
        dur_p002 = df.loc[df['INDI_DSCM_NO'] == 'P002', 'dm_duration_years'].iloc[0]
        assert 13.5 < dur_p002 < 14.5

    def test_duration_null_for_non_dm(self, dm, vg):
        """P003: first_dm_date=NULL → dm_duration_years=NULL (pandas NaN)"""
        vg.generate_dm_duration()
        df = dm.query("SELECT * FROM dm_duration_vars WHERE INDI_DSCM_NO='P003'")
        assert pd.isna(df['dm_duration_years'].iloc[0])

    def test_duration_categories(self, dm, vg):
        vg.generate_dm_duration()
        df = dm.query("SELECT * FROM dm_duration_vars ORDER BY INDI_DSCM_NO")

        # P001: ~4.84yr → '<5yr'
        assert df.loc[df['INDI_DSCM_NO'] == 'P001', 'dm_duration_cat'].iloc[0] == '<5yr'
        # P002: ~14yr → '>=10yr'
        assert df.loc[df['INDI_DSCM_NO'] == 'P002', 'dm_duration_cat'].iloc[0] == '>=10yr'
        # P003: NULL → 'No DM'
        assert df.loc[df['INDI_DSCM_NO'] == 'P003', 'dm_duration_cat'].iloc[0] == 'No DM'


# ---------------------------------------------------------------------------
# 7. Merge All Variables
# ---------------------------------------------------------------------------
class TestMergeAllVariables:
    def test_final_analysis_row_count(self, dm, vg):
        """merge 후 final_analysis 행 수 = analysis_data 행 수"""
        vg.generate_demographics()
        vg.generate_health_behaviors()
        vg._create_t40_filtered()
        vg.generate_comorbidities()
        vg.generate_dm_complications()
        vg.generate_dm_duration()
        vg.generate_cci()
        vg._drop_t40_filtered()

        n = vg.merge_all_variables()
        assert n == 3

    def test_final_analysis_has_all_columns(self, dm, vg):
        """final_analysis에 모든 변수 그룹 컬럼 존재"""
        vg.generate_demographics()
        vg.generate_health_behaviors()
        vg._create_t40_filtered()
        vg.generate_comorbidities()
        vg.generate_dm_complications()
        vg.generate_dm_duration()
        vg.generate_cci()
        vg._drop_t40_filtered()
        vg.merge_all_variables()

        df = dm.query("SELECT * FROM final_analysis LIMIT 1")
        expected_cols = [
            # demographics
            'age_at_index', 'age_group', 'income_quintile', 'insurance_type', 'region_code',
            # health exam
            'bmi', 'sbp', 'dbp', 'bmi_cat',
            # questionnaire
            'smoking_status', 'drinking_status',
            # comorbidities
            'comor_hypertension', 'comor_dyslipidemia',
            # complications
            'comp_retinopathy', 'comp_nephropathy', 'comp_neuropathy',
            'comp_foot', 'comp_hypoglycemia',
            # duration
            'dm_duration_years', 'dm_duration_cat',
            # CCI
            'cci_score', 'cci_category',
        ]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_final_analysis_values_propagated(self, dm, vg):
        """merge 후 개별 변수 값이 올바르게 전파되었는지 확인"""
        vg.generate_demographics()
        vg.generate_health_behaviors()
        vg._create_t40_filtered()
        vg.generate_comorbidities()
        vg.generate_dm_complications()
        vg.generate_dm_duration()
        vg.generate_cci()
        vg._drop_t40_filtered()
        vg.merge_all_variables()

        df = dm.query("SELECT * FROM final_analysis WHERE INDI_DSCM_NO='P001'")
        assert df['age_at_index'].iloc[0] == 45
        assert df['bmi_cat'].iloc[0] == 'NW'
        assert df['comor_hypertension'].iloc[0] == 1
        assert df['comp_retinopathy'].iloc[0] == 1
        assert df['cci_score'].iloc[0] == 2

    def test_left_join_preserves_null(self, dm, vg):
        """P003: 검진/문진 없음 → LEFT JOIN으로 NULL 유지"""
        vg.generate_demographics()
        vg.generate_health_behaviors()
        vg._create_t40_filtered()
        vg.generate_comorbidities()
        vg.generate_dm_complications()
        vg.generate_dm_duration()
        vg.generate_cci()
        vg._drop_t40_filtered()
        vg.merge_all_variables()

        df = dm.query("SELECT * FROM final_analysis WHERE INDI_DSCM_NO='P003'")
        assert pd.isna(df['bmi'].iloc[0])
        assert pd.isna(df['smoking_status'].iloc[0])


# ---------------------------------------------------------------------------
# 8. End-to-End: generate_all
# ---------------------------------------------------------------------------
class TestMissingDataStrategy:
    def _prepare_for_merge(self, vg):
        vg.generate_demographics()
        vg.generate_health_behaviors()
        vg._create_t40_filtered()
        vg.generate_comorbidities()
        vg.generate_dm_complications()
        vg.generate_dm_duration()
        vg.generate_cci()
        vg._drop_t40_filtered()

    def test_merge_all_variables_without_med_switch_creates_null_med_switch_date(self, dm, vg):
        self._prepare_for_merge(vg)
        assert not dm._table_exists('med_switch')

        vg.merge_all_variables()

        cols = dm.query("SELECT * FROM final_analysis LIMIT 0").columns
        assert 'med_switch_date' in cols
        null_count = dm.query("SELECT COUNT(*) FROM final_analysis WHERE med_switch_date IS NULL").iloc[0, 0]
        assert null_count == 3

    def test_multiple_imputation_fallback_does_not_mutate_global_setting(self, dm, vg, monkeypatch):
        self._prepare_for_merge(vg)
        vg.merge_all_variables()

        from variable_generator import STUDY_SETTINGS
        original = STUDY_SETTINGS.get('MISSING_DATA_STRATEGY', 'complete_case')
        monkeypatch.setitem(STUDY_SETTINGS, 'MISSING_DATA_STRATEGY', 'multiple_imputation')

        vg.apply_missing_data_strategy()

        assert STUDY_SETTINGS['MISSING_DATA_STRATEGY'] == 'multiple_imputation'
        # cleanup for test isolation
        monkeypatch.setitem(STUDY_SETTINGS, 'MISSING_DATA_STRATEGY', original)


class TestGenerateAll:
    def test_generate_all_returns_count(self, dm, vg):
        """generate_all → final_analysis 생성 후 행 수 반환 (complete-case: 3→2명)"""
        n = vg.generate_all()
        # Phase 1: complete_case 분석으로 결측값 있는 1명 제외 (3→2명)
        assert n == 2

    def test_generate_all_creates_final_table(self, dm, vg):
        """generate_all 후 final_analysis 테이블 존재"""
        vg.generate_all()
        assert dm._table_exists('final_analysis')

    def test_generate_all_callback_called(self, dm, vg):
        """콜백 함수가 호출되는지 확인"""
        cb = MagicMock()
        vg.generate_all(cb=cb)
        # 9 generate steps + 2 missing_data steps + 완료 = at least 9
        assert cb.call_count >= 9

    def test_generate_all_cleanup_called(self, dm, vg):
        """mem_manager.cleanup_after_step이 각 단계마다 호출되는지 확인"""
        vg.generate_all()
        cleanup = vg._mock_mem.cleanup_after_step
        called_steps = [call.args[0] for call in cleanup.call_args_list]
        for step in ['demographics', 'health_behaviors', 'comorbidities',
                      'complications', 'duration', 'cci', 'merge',
                      'missing_data_assessment', 'missing_data_strategy']:
            assert step in called_steps, f"cleanup_after_step('{step}') not called"

    def test_generate_all_t40_filtered_cleaned(self, dm, vg):
        """generate_all 후 _t40_pre_index 임시 테이블이 삭제되었는지 확인"""
        vg.generate_all()
        assert not dm._table_exists('_t40_pre_index')

    def test_generate_all_idempotent(self, dm, vg):
        """두 번 실행해도 동일한 결과 (2명 완전사례분석)"""
        n1 = vg.generate_all()
        n2 = vg.generate_all()
        assert n1 == n2 == 2
