"""
variable_generator.py - 공변량 생성 모듈
프로토콜 반영: 족부병변, 저혈당증, 불안장애, 갑상선기능저하증, 말초혈관질환
"""

import logging
from config import DM_COMPLICATION_CODES, COMORBIDITY_CODES, CCI_CODES, STUDY_SETTINGS
from memory_manager import mem_manager
from utils import icd_like

logger = logging.getLogger(__name__)


class VariableGenerator:
    def __init__(self, data_manager):
        self.dm = data_manager

    def generate_demographics(self, cb=None):
        if cb: cb("인구학적 변수 생성 중...")
        self.dm.execute("""
            CREATE OR REPLACE TABLE demo_vars AS
            SELECT ad.INDI_DSCM_NO,
                   CAST(SUBSTR(ad.index_date,1,4) AS INT) - CAST(ad.BYEAR AS INT) AS age_at_index,
                   CASE
                     WHEN CAST(SUBSTR(ad.index_date,1,4) AS INT)-CAST(ad.BYEAR AS INT) BETWEEN 40 AND 54 THEN '40-54'
                     ELSE '55-64'
                   END AS age_group,
                   jk.SES05 AS income_quintile,
                   jk.GAIBJA_TYPE AS insurance_type,
                   jk.RVSN_ADDR_CD AS region_code
            FROM analysis_data ad
            LEFT JOIN JK jk ON ad.INDI_DSCM_NO=jk.INDI_DSCM_NO
                AND jk.STD_YYYY = SUBSTR(ad.index_date,1,4)
        """)

    def generate_health_behaviors(self, cb=None):
        if cb: cb("건강행태 변수 생성 중...")
        has_result = self.dm.storage.table_exists('GJ_RESULT')
        has_quest = self.dm.storage.table_exists('GJ_QUEST')

        if has_result:
            # ★ ROW_NUMBER 중간 테이블 없이 직접 QUALIFY로 rn=1만 추출
            # DuckDB는 QUALIFY 지원 → 중간 테이블 메모리 절약
            self.dm.execute("""
                CREATE OR REPLACE TABLE health_exam_final AS
                SELECT ad.INDI_DSCM_NO,
                       gj.G1E_BMI AS bmi, gj.G1E_BP_SYS AS sbp, gj.G1E_BP_DIA AS dbp,
                       gj.G1E_FBS AS fbs, gj.G1E_TOT_CHOL AS total_chol,
                       gj.G1E_TG AS tg, gj.G1E_HDL AS hdl, gj.G1E_LDL_CALC AS ldl,
                       gj.G1E_CRTN AS creatinine, gj.G1E_GFR AS egfr,
                       gj.G1E_HGB AS hemoglobin,
                       gj.G1E_SGOT AS ast, gj.G1E_SGPT AS alt, gj.G1E_GGT AS ggt,
                       CASE WHEN gj.G1E_BMI<18.5 THEN 'UW' WHEN gj.G1E_BMI<23 THEN 'NW'
                            WHEN gj.G1E_BMI<25 THEN 'OW' ELSE 'OB' END AS bmi_cat
                FROM analysis_data ad
                INNER JOIN GJ_RESULT gj ON ad.INDI_DSCM_NO=gj.INDI_DSCM_NO
                -- HC_DT(YYYYMMDD)와 index_date(YYYYMMDD) 직접 비교: 동년 index 이전 검진도 포함
                WHERE gj.HC_DT <= ad.index_date
                QUALIFY ROW_NUMBER() OVER (PARTITION BY ad.INDI_DSCM_NO
                    ORDER BY gj.HC_DT DESC) = 1
            """)
        else:
            self.dm.execute("""
                CREATE OR REPLACE TABLE health_exam_final AS
                SELECT INDI_DSCM_NO, NULL::DOUBLE AS bmi, NULL::DOUBLE AS sbp, NULL::DOUBLE AS dbp,
                       NULL::DOUBLE AS fbs, NULL::DOUBLE AS total_chol, NULL::DOUBLE AS tg,
                       NULL::DOUBLE AS hdl, NULL::DOUBLE AS ldl, NULL::DOUBLE AS creatinine,
                       NULL::DOUBLE AS egfr, NULL::DOUBLE AS hemoglobin,
                       NULL::DOUBLE AS ast, NULL::DOUBLE AS alt, NULL::DOUBLE AS ggt,
                       NULL::VARCHAR AS bmi_cat
                FROM analysis_data WHERE FALSE
            """)

        if has_quest:
            # ★ QUALIFY로 중간 테이블 없이 직접 rn=1 추출
            self.dm.execute("""
                CREATE OR REPLACE TABLE quest_final AS
                SELECT ad.INDI_DSCM_NO,
                       -- 두 흡연 컬럼 모두 NULL이면 결측 유지 (비흡연 오분류 방지)
                       CASE WHEN gq.Q_SMK_NOW_YN IS NULL AND gq.Q_SMK_YN IS NULL THEN NULL
                            WHEN gq.Q_SMK_NOW_YN=1 THEN 'Current'
                            WHEN gq.Q_SMK_YN=1 THEN 'Former'
                            ELSE 'Never' END AS smoking_status,
                       -- Q_DRK_PER NULL은 결측으로 유지 (비음주 오분류 방지)
                       CASE WHEN gq.Q_DRK_PER IS NULL THEN NULL
                            WHEN gq.Q_DRK_PER=0 THEN 'Non'
                            WHEN gq.Q_DRK_PER<=2 THEN 'Mild'
                            WHEN gq.Q_DRK_PER<=4 THEN 'Moderate'
                            ELSE 'Heavy' END AS drinking_status
                FROM analysis_data ad
                INNER JOIN GJ_QUEST gq ON ad.INDI_DSCM_NO=gq.INDI_DSCM_NO
                -- GJ_QUEST에는 HC_DT 없음 → 연도 단위 비교 유지 (동년 index 이전 문진 제외)
                WHERE CAST(gq.HC_BZ_YYYY AS INT) < CAST(SUBSTR(ad.index_date,1,4) AS INT)
                QUALIFY ROW_NUMBER() OVER (PARTITION BY ad.INDI_DSCM_NO
                    ORDER BY CAST(gq.HC_BZ_YYYY AS INT) DESC) = 1
            """)
        else:
            self.dm.execute("""
                CREATE OR REPLACE TABLE quest_final AS
                SELECT INDI_DSCM_NO, 'Unknown'::VARCHAR AS smoking_status, 'Unknown'::VARCHAR AS drinking_status
                FROM analysis_data WHERE FALSE
            """)

    def _create_t40_filtered(self):
        """T40 사전 필터링 테이블 생성 — comorbidity/complication/CCI에서 재사용 (T40 1회 스캔)"""
        lookback_years = int(STUDY_SETTINGS.get('LOOKBACK_YEARS', 1))
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE _t40_pre_index AS
            SELECT t40.INDI_DSCM_NO, t40.MCEX_SICK_SYM
            FROM T40 t40
            INNER JOIN analysis_data ad
                ON t40.INDI_DSCM_NO = ad.INDI_DSCM_NO
                AND t40.MDCARE_STRT_DT <= ad.index_date
                AND t40.MDCARE_STRT_DT >= CAST(CAST(SUBSTR(ad.index_date,1,4) AS INT) - {lookback_years} AS VARCHAR)
                    || SUBSTR(ad.index_date, 5)
        """)

    def _drop_t40_filtered(self):
        """T40 사전 필터링 임시 테이블 삭제"""
        self.dm.execute("DROP TABLE IF EXISTS _t40_pre_index")

    def generate_comorbidities(self, cb=None):
        if cb: cb("동반질환 변수 생성 중...")
        selects = []
        for cname, codes in COMORBIDITY_CODES.items():
            cond = icd_like('t40.MCEX_SICK_SYM', codes)
            selects.append(f"MAX(CASE WHEN {cond} THEN 1 ELSE 0 END) AS comor_{cname.lower()}")

        # _t40_pre_index 재사용 (T40 1회 스캔으로 3개 변수 그룹 생성)
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE comorbidity_vars AS
            SELECT ad.INDI_DSCM_NO, {', '.join(selects)}
            FROM analysis_data ad
            LEFT JOIN _t40_pre_index t40 ON ad.INDI_DSCM_NO = t40.INDI_DSCM_NO
            GROUP BY ad.INDI_DSCM_NO
        """)

    def generate_dm_complications(self, cb=None):
        if cb: cb("당뇨 합병증 변수 생성 중...")
        selects = []
        for cname, codes in DM_COMPLICATION_CODES.items():
            cond = icd_like('t40.MCEX_SICK_SYM', codes)
            selects.append(f"MAX(CASE WHEN {cond} THEN 1 ELSE 0 END) AS comp_{cname.lower()}")

        # _t40_pre_index 재사용
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE complication_vars AS
            SELECT ad.INDI_DSCM_NO, {', '.join(selects)}
            FROM analysis_data ad
            LEFT JOIN _t40_pre_index t40 ON ad.INDI_DSCM_NO = t40.INDI_DSCM_NO
            GROUP BY ad.INDI_DSCM_NO
        """)

    def generate_dm_duration(self, cb=None):
        if cb: cb("당뇨 유병기간 생성 중...")
        self.dm.execute("""
            CREATE OR REPLACE TABLE dm_duration_vars AS
            SELECT INDI_DSCM_NO, first_dm_date,
                   CASE WHEN first_dm_date IS NOT NULL THEN
                       -- 실제 날짜 기반 일수 차이로 연수 산출 (음수 방지: GREATEST 적용)
                       GREATEST(DATEDIFF('day',
                           CAST(SUBSTR(first_dm_date,1,4)||'-'||SUBSTR(first_dm_date,5,2)||'-'||SUBSTR(first_dm_date,7,2) AS DATE),
                           CAST(SUBSTR(index_date,1,4)||'-'||SUBSTR(index_date,5,2)||'-'||SUBSTR(index_date,7,2) AS DATE)
                       ), 0) / 365.25
                   END AS dm_duration_years,
                   CASE
                     WHEN first_dm_date IS NULL THEN 'No DM'
                     WHEN GREATEST(DATEDIFF('day',
                           CAST(SUBSTR(first_dm_date,1,4)||'-'||SUBSTR(first_dm_date,5,2)||'-'||SUBSTR(first_dm_date,7,2) AS DATE),
                           CAST(SUBSTR(index_date,1,4)||'-'||SUBSTR(index_date,5,2)||'-'||SUBSTR(index_date,7,2) AS DATE)
                          ), 0) / 365.25 < 5 THEN '<5yr'
                     WHEN GREATEST(DATEDIFF('day',
                           CAST(SUBSTR(first_dm_date,1,4)||'-'||SUBSTR(first_dm_date,5,2)||'-'||SUBSTR(first_dm_date,7,2) AS DATE),
                           CAST(SUBSTR(index_date,1,4)||'-'||SUBSTR(index_date,5,2)||'-'||SUBSTR(index_date,7,2) AS DATE)
                          ), 0) / 365.25 < 10 THEN '5-10yr'
                     ELSE '>=10yr'
                   END AS dm_duration_cat
            FROM analysis_data
        """)

    def generate_cci(self, cb=None):
        if cb: cb("CCI 계산 중...")
        selects = []
        for cname, (codes, w) in CCI_CODES.items():
            cond = icd_like('t40.MCEX_SICK_SYM', codes)
            selects.append(f"{w} * MAX(CASE WHEN {cond} THEN 1 ELSE 0 END) AS cci_{cname.lower()}")

        sums = ' + '.join(f"COALESCE(cci_{c.lower()},0)" for c in CCI_CODES)

        # _t40_pre_index 재사용
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE cci_detail AS
            SELECT ad.INDI_DSCM_NO, {', '.join(selects)}
            FROM analysis_data ad
            LEFT JOIN _t40_pre_index t40 ON ad.INDI_DSCM_NO = t40.INDI_DSCM_NO
            GROUP BY ad.INDI_DSCM_NO
        """)
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE cci_vars AS
            SELECT INDI_DSCM_NO, ({sums}) AS cci_score,
                   CASE WHEN ({sums})=0 THEN '0' WHEN ({sums})<=2 THEN '1-2' WHEN ({sums})<=4 THEN '3-4' ELSE '5+' END AS cci_category
            FROM cci_detail
        """)

    def merge_all_variables(self, cb=None):
        if cb: cb("변수 통합 중...")
        self.dm.execute("""
            CREATE OR REPLACE TABLE final_analysis AS
            SELECT ad.*,
                   dv.age_at_index, dv.age_group, dv.income_quintile, dv.insurance_type, dv.region_code,
                   he.bmi, he.sbp, he.dbp, he.fbs, he.total_chol, he.tg, he.hdl, he.ldl,
                   he.creatinine, he.egfr, he.hemoglobin, he.ast, he.alt, he.ggt, he.bmi_cat,
                   qf.smoking_status, qf.drinking_status,
                   cv.comor_hypertension, cv.comor_dyslipidemia,
                   cv.comor_ischemic_stroke, cv.comor_hemorrhagic_stroke,
                   cv.comor_tia, cv.comor_depression, cv.comor_anxiety,
                   cv.comor_hypothyroidism, cv.comor_obesity, cv.comor_ckd,
                   cv.comor_ihd, cv.comor_atrial_fib, cv.comor_heart_failure, cv.comor_pvd,
                   cp.comp_retinopathy, cp.comp_nephropathy, cp.comp_neuropathy,
                   cp.comp_foot, cp.comp_hypoglycemia,
                   dd.dm_duration_years, dd.dm_duration_cat,
                   cci.cci_score, cci.cci_category
            FROM analysis_data ad
            LEFT JOIN demo_vars dv ON ad.INDI_DSCM_NO=dv.INDI_DSCM_NO
            LEFT JOIN health_exam_final he ON ad.INDI_DSCM_NO=he.INDI_DSCM_NO
            LEFT JOIN quest_final qf ON ad.INDI_DSCM_NO=qf.INDI_DSCM_NO
            LEFT JOIN comorbidity_vars cv ON ad.INDI_DSCM_NO=cv.INDI_DSCM_NO
            LEFT JOIN complication_vars cp ON ad.INDI_DSCM_NO=cp.INDI_DSCM_NO
            LEFT JOIN dm_duration_vars dd ON ad.INDI_DSCM_NO=dd.INDI_DSCM_NO
            LEFT JOIN cci_vars cci ON ad.INDI_DSCM_NO=cci.INDI_DSCM_NO
        """)
        return self.dm.storage.get_row_count('final_analysis')

    def generate_all(self, cb=None):
        step_errors = {}

        def _safe_step(name, fn):
            try:
                fn()
            except Exception as e:
                step_errors[name] = str(e)
                logger.exception("변수 생성 단계 오류 (%s)", name)
                if cb: cb(f"[경고] {name} 변수 생성 실패, 계속 진행: {e}")
            finally:
                mem_manager.cleanup_after_step(name)

        _safe_step('demographics', lambda: self.generate_demographics(cb))
        _safe_step('health_behaviors', lambda: self.generate_health_behaviors(cb))

        # T40 사전 필터링 1회 생성 → comorbidity/complication/CCI에서 재사용 (3회 스캔→1회)
        try:
            self._create_t40_filtered()
        except Exception as e:
            logger.warning("T40 사전 필터링 실패: %s", e)
            if cb: cb(f"[경고] T40 필터링 실패: {e}")

        _safe_step('comorbidities', lambda: self.generate_comorbidities(cb))
        _safe_step('complications', lambda: self.generate_dm_complications(cb))
        _safe_step('duration', lambda: self.generate_dm_duration(cb))

        try:
            self.generate_cci(cb)
        except Exception as e:
            step_errors['cci'] = str(e)
            logger.exception("변수 생성 단계 오류 (cci)")
            if cb: cb(f"[경고] cci 변수 생성 실패: {e}")
        finally:
            self._drop_t40_filtered()
            mem_manager.cleanup_after_step('cci')

        n = self.merge_all_variables(cb)
        mem_manager.cleanup_after_step('merge')
        if cb:
            if step_errors:
                cb(f"변수 생성 완료 (일부 실패: {', '.join(step_errors)})")
            else:
                cb("모든 변수 생성 완료!")
        return n
