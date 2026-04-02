"""
cohort_builder.py - 코호트 구축 모듈
프로토콜 반영: 외래2회/입원1회, T1+T2 제외, 항치매약 제외, 65세 censoring
"""

import logging
from config import (
    DM_CODES, DEMENTIA_CODES, DEMENTIA_DRUG_CODES,
    OHA_CODES, INSULIN_EFMDC, INSULIN_CODES, STUDY_SETTINGS
)
from memory_manager import mem_manager
from utils import icd_like

logger = logging.getLogger(__name__)


class CohortBuilder:
    def __init__(self, data_manager):
        self.dm = data_manager
        self.settings = STUDY_SETTINGS

    def _flat_oha_codes(self):
        codes = []
        for v in OHA_CODES.values():
            codes.extend(v)
        return codes

    def step1_base_population(self, cb=None):
        """40-64세, 진입기간 2013-2016, 진입 전 1년 자격유지"""
        if cb: cb("Step 1: 기본 대상 인구 정의 중...")
        es = int(self.settings.get('ENROLLMENT_START', 2013))
        ee = int(self.settings.get('ENROLLMENT_END', 2016))
        washout = int(self.settings.get('WASHOUT_YEARS', 1))

        # 1) 진입기간(es~ee) 내 자격 보유자 후보 추출
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE base_population AS
            WITH candidates AS (
                SELECT INDI_DSCM_NO, SEX_TYPE, BYEAR,
                       MIN(CAST(STD_YYYY AS INTEGER)) AS first_year,
                       MAX(CAST(STD_YYYY AS INTEGER)) AS last_year
                FROM JK
                WHERE CAST(STD_YYYY AS INTEGER) BETWEEN {es} AND {ee}
                  AND CAST(STD_YYYY AS INTEGER) - CAST(BYEAR AS INTEGER)
                      BETWEEN {self.settings['MIN_AGE']} AND {self.settings['MAX_AGE']}
                  AND (FOREIGNER_Y IS NULL OR FOREIGNER_Y != '1')
                GROUP BY INDI_DSCM_NO, SEX_TYPE, BYEAR
            ),
            -- 2) 개인별 first_year 바로 이전 연도(first_year - washout)에 JK 레코드가 있는 사람만 포함
            --    (전역 es-1 기준이 아니라 각자의 진입연도 - 1 기준으로 체크)
            jk_years AS (
                SELECT INDI_DSCM_NO, CAST(STD_YYYY AS INTEGER) AS yr
                FROM JK
            )
            SELECT c.*
            FROM candidates c
            WHERE EXISTS (
                SELECT 1 FROM jk_years j
                WHERE j.INDI_DSCM_NO = c.INDI_DSCM_NO
                  AND j.yr = c.first_year - {washout}
            )
        """)
        return self.dm.storage.get_row_count('base_population')

    def step2_dm_claims(self, cb=None):
        """당뇨 진단 청구 식별 (T40 + T20) — 개인별 first_year 이후 청구만 포함
        진입기간 이전 청구를 포함하면 유병 당뇨가 신규 당뇨로 오분류되어 prevalent bias 발생.
        """
        if cb: cb("Step 2: 당뇨 진단 청구 식별 중...")
        t1c = icd_like('t40.MCEX_SICK_SYM', DM_CODES['T1DM'])
        t2c = icd_like('t40.MCEX_SICK_SYM', DM_CODES['T2DM'])

        # first_year 이후 청구만 포함 + 진입 이전 DM 청구 있는 환자 제외 (prevalent bias 방지)
        # 진입 전 T40/T20에 DM 청구 기록이 있으면 유병 당뇨로 간주하여 제외
        # T40 + T20을 UNION으로 통합하여 한 번에 중복 제거 (INSERT + NOT IN 패턴 제거)
        t1s = icd_like('t20.SICK_SYM1', DM_CODES['T1DM'])
        t2s = icd_like('t20.SICK_SYM1', DM_CODES['T2DM'])
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE prevalent_dm AS
            SELECT DISTINCT INDI_DSCM_NO FROM (
                SELECT t40.INDI_DSCM_NO
                FROM T40 t40
                INNER JOIN base_population bp ON t40.INDI_DSCM_NO = bp.INDI_DSCM_NO
                WHERE ({t1c} OR {t2c})
                  AND CAST(SUBSTR(t40.MDCARE_STRT_DT, 1, 4) AS INTEGER) < bp.first_year
                UNION ALL
                SELECT t20.INDI_DSCM_NO
                FROM T20 t20
                INNER JOIN base_population bp ON t20.INDI_DSCM_NO = bp.INDI_DSCM_NO
                WHERE ({t1s} OR {t2s})
                  AND CAST(SUBSTR(t20.MDCARE_STRT_DT, 1, 4) AS INTEGER) < bp.first_year
            )
        """)

        self.dm.execute(f"""
            CREATE OR REPLACE TABLE dm_claims AS
            SELECT t40.INDI_DSCM_NO, t40.MCEX_SICK_SYM AS dx_code,
                   t40.MDCARE_STRT_DT AS claim_date, t40.CMN_KEY,
                   CASE WHEN {t1c} THEN 'T1DM' WHEN {t2c} THEN 'T2DM' END AS dm_type
            FROM T40 t40
            INNER JOIN base_population bp ON t40.INDI_DSCM_NO = bp.INDI_DSCM_NO
            WHERE ({t1c} OR {t2c})
              AND CAST(SUBSTR(t40.MDCARE_STRT_DT, 1, 4) AS INTEGER) >= bp.first_year
              AND t40.INDI_DSCM_NO NOT IN (SELECT INDI_DSCM_NO FROM prevalent_dm)
        """)

        self.dm.execute(f"""
            INSERT INTO dm_claims
            SELECT t20.INDI_DSCM_NO, t20.SICK_SYM1, t20.MDCARE_STRT_DT, t20.CMN_KEY,
                   CASE WHEN {t1s} THEN 'T1DM' WHEN {t2s} THEN 'T2DM' END
            FROM T20 t20
            INNER JOIN base_population bp ON t20.INDI_DSCM_NO = bp.INDI_DSCM_NO
            WHERE ({t1s} OR {t2s})
              AND CAST(SUBSTR(t20.MDCARE_STRT_DT, 1, 4) AS INTEGER) >= bp.first_year
              AND t20.INDI_DSCM_NO NOT IN (SELECT INDI_DSCM_NO FROM prevalent_dm)
        """)
        return self.dm.storage.get_row_count('dm_claims')

    def step3_dm_medications(self, cb=None):
        """당뇨 약물 처방 식별"""
        if cb: cb("Step 3: 당뇨 약물 처방 식별 중...")
        oha = "'" + "','".join(self._flat_oha_codes()) + "'"
        ins = "'" + "','".join(INSULIN_CODES) + "'"
        ief = "'" + "','".join(INSULIN_EFMDC) + "'"

        self.dm.execute(f"""
            CREATE OR REPLACE TABLE dm_medications AS
            SELECT t30.INDI_DSCM_NO, t30.MDCARE_STRT_DT AS rx_date, t30.CMN_KEY,
                   t30.WK_COMPN_CD, t30.RVSN_WK_COMPN_CD, t30.EFMDC_CLSF_NO, t30.TOT_MCNT,
                   CASE
                     WHEN t30.EFMDC_CLSF_NO IN ({ief})
                       OR SUBSTR(t30.WK_COMPN_CD,1,6) IN ({ins})
                       OR SUBSTR(t30.RVSN_WK_COMPN_CD,1,6) IN ({ins}) THEN 'INSULIN'
                     WHEN SUBSTR(t30.WK_COMPN_CD,1,6) IN ({oha})
                       OR SUBSTR(t30.RVSN_WK_COMPN_CD,1,6) IN ({oha}) THEN 'OHA'
                   END AS med_type
            FROM T30 t30
            INNER JOIN base_population bp ON t30.INDI_DSCM_NO = bp.INDI_DSCM_NO
            WHERE (t30.EFMDC_CLSF_NO IN ({ief})
                OR SUBSTR(t30.WK_COMPN_CD,1,6) IN ({oha}) OR SUBSTR(t30.RVSN_WK_COMPN_CD,1,6) IN ({oha})
                OR SUBSTR(t30.WK_COMPN_CD,1,6) IN ({ins}) OR SUBSTR(t30.RVSN_WK_COMPN_CD,1,6) IN ({ins}))
        """)

        # T60도 추가
        self.dm.execute(f"""
            INSERT INTO dm_medications
            SELECT t60.INDI_DSCM_NO, t60.MDCARE_STRT_DT, t60.CMN_KEY,
                   '' AS WK_COMPN_CD, t60.RVSN_WK_COMPN_CD, '' AS EFMDC_CLSF_NO, t60.TOT_MCNT,
                   CASE
                     WHEN SUBSTR(t60.GNL_NM_CD,1,6) IN ({ins}) OR SUBSTR(t60.RVSN_WK_COMPN_CD,1,6) IN ({ins}) THEN 'INSULIN'
                     WHEN SUBSTR(t60.GNL_NM_CD,1,6) IN ({oha}) OR SUBSTR(t60.RVSN_WK_COMPN_CD,1,6) IN ({oha}) THEN 'OHA'
                   END
            FROM T60 t60
            INNER JOIN base_population bp ON t60.INDI_DSCM_NO = bp.INDI_DSCM_NO
            WHERE (SUBSTR(t60.GNL_NM_CD,1,6) IN ({oha}) OR SUBSTR(t60.RVSN_WK_COMPN_CD,1,6) IN ({oha})
                OR SUBSTR(t60.GNL_NM_CD,1,6) IN ({ins}) OR SUBSTR(t60.RVSN_WK_COMPN_CD,1,6) IN ({ins}))
        """)
        return self.dm.storage.get_row_count('dm_medications')

    def step4_classify_groups(self, cb=None):
        """노출군 분류: 외래2회+/입원1회+, T1+T2 동시보유 제외"""
        if cb: cb("Step 4: 노출군 분류 중...")
        mo = int(self.settings.get('MIN_DM_CLAIMS_OUTPATIENT', 2))
        mi = int(self.settings.get('MIN_DM_CLAIMS_INPATIENT', 1))

        # 입원 CMN_KEY 사전 추출 (T20 반복 서브쿼리 방지)
        self.dm.execute("""
            CREATE OR REPLACE TABLE _inpatient_keys AS
            SELECT DISTINCT CMN_KEY FROM T20 WHERE FORM_CD='02'
        """)

        self.dm.execute(f"""
            CREATE OR REPLACE TABLE dm_patients AS
            WITH outpt AS (
                SELECT INDI_DSCM_NO, dm_type, MIN(claim_date) AS first_dt, COUNT(DISTINCT claim_date) AS n
                FROM dm_claims WHERE CMN_KEY NOT IN (SELECT CMN_KEY FROM _inpatient_keys)
                GROUP BY INDI_DSCM_NO, dm_type
            ), inpt AS (
                SELECT INDI_DSCM_NO, dm_type, MIN(claim_date) AS first_dt, COUNT(DISTINCT claim_date) AS n
                FROM dm_claims WHERE CMN_KEY IN (SELECT CMN_KEY FROM _inpatient_keys)
                GROUP BY INDI_DSCM_NO, dm_type
            )
            SELECT COALESCE(o.INDI_DSCM_NO, i.INDI_DSCM_NO) AS INDI_DSCM_NO,
                   COALESCE(o.dm_type, i.dm_type) AS dm_type,
                   LEAST(COALESCE(o.first_dt, i.first_dt), COALESCE(i.first_dt, o.first_dt)) AS first_dm_date
            FROM outpt o FULL OUTER JOIN inpt i ON o.INDI_DSCM_NO=i.INDI_DSCM_NO AND o.dm_type=i.dm_type
            WHERE COALESCE(o.n,0) >= {mo} OR COALESCE(i.n,0) >= {mi}
        """)

        # 임시 테이블 정리
        self.dm.execute("DROP TABLE IF EXISTS _inpatient_keys")

        # T1+T2 동시 보유자 제외
        self.dm.execute("""
            CREATE OR REPLACE TABLE dual_dm AS
            SELECT INDI_DSCM_NO FROM dm_patients GROUP BY INDI_DSCM_NO HAVING COUNT(DISTINCT dm_type) > 1
        """)

        # med_pattern: index_date 전후 1년 이내 처방만 집계
        # (전체 이력 집계 시 OHA→인슐린 전환 환자가 index_date부터 T2DM_INSULIN으로 오분류됨)
        self.dm.execute("""
            CREATE OR REPLACE TABLE med_pattern AS
            SELECT m.INDI_DSCM_NO,
                   MAX(CASE WHEN m.med_type='INSULIN' THEN 1 ELSE 0 END) AS has_insulin,
                   MAX(CASE WHEN m.med_type='OHA'     THEN 1 ELSE 0 END) AS has_oha
            FROM dm_medications m
            INNER JOIN dm_patients dp ON m.INDI_DSCM_NO = dp.INDI_DSCM_NO
            WHERE m.rx_date >= dp.first_dm_date
              AND m.rx_date <= CAST(
                  STRFTIME(
                      CAST(SUBSTR(dp.first_dm_date,1,4)||'-'||SUBSTR(dp.first_dm_date,5,2)||'-'||SUBSTR(dp.first_dm_date,7,2) AS DATE)
                      + INTERVAL 365 DAYS,
                  '%Y%m%d') AS VARCHAR)
            GROUP BY m.INDI_DSCM_NO
        """)

        self.dm.execute("""
            CREATE OR REPLACE TABLE exposure_groups AS
            SELECT bp.INDI_DSCM_NO, bp.SEX_TYPE, bp.BYEAR,
                   CASE
                     WHEN t1.INDI_DSCM_NO IS NOT NULL THEN 'T1DM'
                     WHEN t2.INDI_DSCM_NO IS NOT NULL AND mp.has_insulin=1 THEN 'T2DM_INSULIN'
                     WHEN t2.INDI_DSCM_NO IS NOT NULL AND mp.has_oha=1 THEN 'T2DM_OHA'
                     WHEN t2.INDI_DSCM_NO IS NOT NULL THEN 'T2DM_NOMED'
                     ELSE 'NON_DM'
                   END AS exposure_group,
                   -- NON_DM은 전역 진입연도 대신 개인별 첫 관찰연도(first_year)의 1월 1일을 index_date로 사용
                   -- (전역 시작일을 쓰면 실제 관찰 시작 전 기간이 추적기간에 포함되어 편향 발생)
                   COALESCE(
                       t1.first_dm_date,
                       t2.first_dm_date,
                       CAST(bp.first_year AS VARCHAR) || '0101'
                   ) AS index_date,
                   COALESCE(t1.first_dm_date, t2.first_dm_date) AS first_dm_date
            FROM base_population bp
            LEFT JOIN dm_patients t1 ON bp.INDI_DSCM_NO=t1.INDI_DSCM_NO AND t1.dm_type='T1DM'
            LEFT JOIN dm_patients t2 ON bp.INDI_DSCM_NO=t2.INDI_DSCM_NO AND t2.dm_type='T2DM'
            LEFT JOIN med_pattern mp ON bp.INDI_DSCM_NO=mp.INDI_DSCM_NO
            WHERE bp.INDI_DSCM_NO NOT IN (SELECT INDI_DSCM_NO FROM dual_dm)
        """)

        result = self.dm.query("SELECT exposure_group, COUNT(*) AS n FROM exposure_groups GROUP BY exposure_group ORDER BY 1")
        logger.info(f"Step 4:\n{result.to_string()}")
        return result

    def step5_exclude_dementia(self, cb=None):
        """기존 치매 진단 + 항치매약 사용자 제외
        진단: T40(상병내역) + T20(진료명세서 주상병) 모두 확인
        약물: T30(진료내역) + T60(처방전내역) 모두 확인
        """
        if cb: cb("Step 5: 기존 치매 및 항치매약 제외 중...")

        # 진단 제외: T40(상병내역) + T20(주상병) UNION으로 통합 중복 제거
        dc40 = icd_like('t40.MCEX_SICK_SYM', DEMENTIA_CODES['ALL_CAUSE'])
        dc20 = icd_like('t20.SICK_SYM1', DEMENTIA_CODES['ALL_CAUSE'])
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE excl_dementia_dx AS
            SELECT DISTINCT INDI_DSCM_NO FROM (
                SELECT t40.INDI_DSCM_NO FROM T40 t40
                INNER JOIN exposure_groups eg ON t40.INDI_DSCM_NO=eg.INDI_DSCM_NO
                WHERE {dc40} AND t40.MDCARE_STRT_DT <= eg.index_date
                UNION ALL
                SELECT t20.INDI_DSCM_NO FROM T20 t20
                INNER JOIN exposure_groups eg ON t20.INDI_DSCM_NO=eg.INDI_DSCM_NO
                WHERE {dc20} AND t20.MDCARE_STRT_DT <= eg.index_date
            )
        """)

        dl = "'" + "','".join(DEMENTIA_DRUG_CODES) + "'"

        # 약물 제외: T30(진료내역) + T60(처방전내역) UNION으로 통합 중복 제거
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE excl_dementia_drug AS
            SELECT DISTINCT INDI_DSCM_NO FROM (
                SELECT t30.INDI_DSCM_NO FROM T30 t30
                INNER JOIN exposure_groups eg ON t30.INDI_DSCM_NO=eg.INDI_DSCM_NO
                WHERE (SUBSTR(t30.WK_COMPN_CD,1,6) IN ({dl}) OR SUBSTR(t30.RVSN_WK_COMPN_CD,1,6) IN ({dl}))
                  AND t30.MDCARE_STRT_DT <= eg.index_date
                UNION ALL
                SELECT t60.INDI_DSCM_NO FROM T60 t60
                INNER JOIN exposure_groups eg ON t60.INDI_DSCM_NO=eg.INDI_DSCM_NO
                WHERE (SUBSTR(t60.GNL_NM_CD,1,6) IN ({dl}) OR SUBSTR(t60.RVSN_WK_COMPN_CD,1,6) IN ({dl}))
                  AND t60.MDCARE_STRT_DT <= eg.index_date
            )
        """)

        self.dm.execute("""
            CREATE OR REPLACE TABLE study_cohort AS
            SELECT * FROM exposure_groups
            WHERE INDI_DSCM_NO NOT IN (SELECT INDI_DSCM_NO FROM excl_dementia_dx)
              AND INDI_DSCM_NO NOT IN (SELECT INDI_DSCM_NO FROM excl_dementia_drug)
        """)
        n = self.dm.storage.get_row_count('study_cohort')
        e1 = self.dm.storage.get_row_count('excl_dementia_dx')
        e2 = self.dm.storage.get_row_count('excl_dementia_drug')
        # 두 소스 모두 해당하는 환자 중복 카운트 방지
        e_both = self.dm.query("""
            SELECT COUNT(*) AS cnt FROM (
                SELECT INDI_DSCM_NO FROM excl_dementia_dx
                INTERSECT
                SELECT INDI_DSCM_NO FROM excl_dementia_drug
            )
        """).iloc[0, 0]
        excluded_total = e1 + e2 - int(e_both)
        logger.info(f"Step 5: 치매진단 {e1:,} + 항치매약 {e2:,} (중복 {int(e_both):,}) 제외 → {n:,}명")
        return n, excluded_total

    def step6_outcomes(self, cb=None):
        """결과변수 식별 + 65세 도달 censoring"""
        if cb: cb("Step 6: 결과변수 + censoring 처리 중...")
        ey = int(self.settings['STUDY_END_YEAR'])
        yod = int(self.settings['YOD_AGE_CUTOFF'])

        # 결과변수: T40(상병내역)과 T20(진료명세서 주상병) 모두에서 치매 진단 확인
        for oname, codes in DEMENTIA_CODES.items():
            cond40 = icd_like('t40.MCEX_SICK_SYM', codes)
            cond20 = icd_like('t20.SICK_SYM1', codes)
            self.dm.execute(f"""
                CREATE OR REPLACE TABLE outcome_{oname.lower()} AS
                WITH events_t40 AS (
                    SELECT t40.INDI_DSCM_NO, t40.MDCARE_STRT_DT AS event_date
                    FROM T40 t40
                    INNER JOIN study_cohort sc ON t40.INDI_DSCM_NO=sc.INDI_DSCM_NO
                    WHERE {cond40}
                      AND t40.MDCARE_STRT_DT > sc.index_date
                      AND (CAST(SUBSTR(t40.MDCARE_STRT_DT,1,4) AS INT) - CAST(sc.BYEAR AS INT)) < {yod}
                ),
                events_t20 AS (
                    SELECT t20.INDI_DSCM_NO, t20.MDCARE_STRT_DT AS event_date
                    FROM T20 t20
                    INNER JOIN study_cohort sc ON t20.INDI_DSCM_NO=sc.INDI_DSCM_NO
                    WHERE {cond20}
                      AND t20.MDCARE_STRT_DT > sc.index_date
                      AND (CAST(SUBSTR(t20.MDCARE_STRT_DT,1,4) AS INT) - CAST(sc.BYEAR AS INT)) < {yod}
                ),
                all_events AS (
                    SELECT * FROM events_t40
                    UNION ALL
                    SELECT * FROM events_t20
                )
                SELECT INDI_DSCM_NO, MIN(event_date) AS event_date
                FROM all_events
                GROUP BY INDI_DSCM_NO
            """)

        # age65_date: 생년만 알고 생년월일이 없으므로 BYEAR+65의 1월 1일로 근사
        # (실제 생일이 1월 이후인 환자는 최대 ~1년 조기 censoring 가능 — 행정DB 한계)
        # 사망일자: DEATH 테이블(DTH_ASSMD_DT) 우선, 없으면 JK.HHDT_DEATH fallback
        has_death_table = self.dm.storage.table_exists('DEATH')

        if has_death_table:
            last_eligible_sql = f"""
            last_eligible AS (
                -- DEATH 테이블 연동: DTH_ASSMD_DT 우선, JK.HHDT_DEATH fallback
                SELECT jk.INDI_DSCM_NO,
                       MAX(CAST(jk.SURV_YR AS VARCHAR)) || '1231' AS withdrawal_date,
                       COALESCE(NULLIF(MAX(d.DTH_ASSMD_DT), ''), MAX(jk.HHDT_DEATH)) AS death_date
                FROM JK jk
                LEFT JOIN DEATH d ON jk.INDI_DSCM_NO = d.INDI_DSCM_NO
                WHERE CAST(jk.STD_YYYY AS INTEGER) <= {ey}
                GROUP BY jk.INDI_DSCM_NO
            )"""
            logger.info("DEATH 테이블 연동: DTH_ASSMD_DT 우선, JK HHDT_DEATH fallback")
        else:
            last_eligible_sql = f"""
            last_eligible AS (
                SELECT INDI_DSCM_NO,
                       MAX(CAST(SURV_YR AS VARCHAR)) || '1231' AS withdrawal_date,
                       MAX(HHDT_DEATH) AS death_date
                FROM JK
                WHERE CAST(STD_YYYY AS INTEGER) <= {ey}
                GROUP BY INDI_DSCM_NO
            )"""

        self.dm.execute(f"""
            CREATE OR REPLACE TABLE analysis_data AS
            WITH {last_eligible_sql},
            -- censor_date를 먼저 산출하여 이벤트 플래그 판정에 재사용
            base AS (
                SELECT sc.*,
                       CAST((CAST(sc.BYEAR AS INT) + {yod}) || '0101' AS VARCHAR) AS age65_date,
                       oa.event_date AS dementia_date,
                       ad.event_date AS ad_date,
                       vd.event_date AS vad_date,
                       le.death_date,
                       LEAST(
                           COALESCE(oa.event_date, '{ey}1231'),
                           CAST((CAST(sc.BYEAR AS INT) + {yod}) || '0101' AS VARCHAR),
                           '{ey}1231',
                           COALESCE(le.death_date, le.withdrawal_date, '{ey}1231')
                       ) AS censor_date
                FROM study_cohort sc
                LEFT JOIN outcome_all_cause oa ON sc.INDI_DSCM_NO=oa.INDI_DSCM_NO
                LEFT JOIN outcome_ad ad ON sc.INDI_DSCM_NO=ad.INDI_DSCM_NO
                LEFT JOIN outcome_vad vd ON sc.INDI_DSCM_NO=vd.INDI_DSCM_NO
                LEFT JOIN last_eligible le ON sc.INDI_DSCM_NO=le.INDI_DSCM_NO
            )
            -- 이벤트 플래그는 censor_date 이내 발생한 경우만 1로 설정
            SELECT *,
                   CASE WHEN dementia_date IS NOT NULL
                             AND dementia_date <= censor_date THEN 1 ELSE 0 END AS dementia_event,
                   CASE WHEN ad_date IS NOT NULL
                             AND ad_date <= censor_date THEN 1 ELSE 0 END AS ad_event,
                   CASE WHEN vad_date IS NOT NULL
                             AND vad_date <= censor_date THEN 1 ELSE 0 END AS vad_event,
                   -- 사망 이벤트: DEATH.DTH_ASSMD_DT 우선, JK.HHDT_DEATH fallback
                   CASE WHEN death_date IS NOT NULL
                             AND death_date <= censor_date
                             AND death_date > index_date
                        THEN 1 ELSE 0 END AS death_event,
                   -- 경쟁위험: 치매 미발생 상태에서 사망 또는 탈퇴
                   CASE WHEN (dementia_date IS NULL OR dementia_date > censor_date)
                             AND censor_date < '{ey}1231'
                             AND censor_date < age65_date
                        THEN 1 ELSE 0 END AS competing_death_event
            FROM base
        """)

        self.dm.execute("""
            ALTER TABLE analysis_data ADD COLUMN IF NOT EXISTS follow_up_days INTEGER;
            ALTER TABLE analysis_data ADD COLUMN IF NOT EXISTS follow_up_years DOUBLE;
            UPDATE analysis_data SET
                follow_up_days = CAST(
                    CAST(SUBSTR(censor_date,1,4)||'-'||SUBSTR(censor_date,5,2)||'-'||SUBSTR(censor_date,7,2) AS DATE)
                    - CAST(SUBSTR(index_date,1,4)||'-'||SUBSTR(index_date,5,2)||'-'||SUBSTR(index_date,7,2) AS DATE)
                AS INTEGER);
            UPDATE analysis_data SET follow_up_years = follow_up_days / 365.25;
        """)

        # follow_up_days <= 0 건수 경고
        bad_fu = self.dm.query("SELECT COUNT(*) AS n FROM analysis_data WHERE follow_up_days <= 0")
        n_bad = int(bad_fu.iloc[0, 0]) if len(bad_fu) > 0 else 0
        if n_bad > 0:
            logger.warning(f"follow_up_days <= 0: {n_bad:,}건 (index_date >= censor_date). "
                          f"분석 시 자동 제외됩니다.")

        events = self.dm.query("""
            SELECT exposure_group, COUNT(*) n, SUM(dementia_event) dem, SUM(ad_event) ad, SUM(vad_event) vad,
                   SUM(death_event) death_actual, SUM(competing_death_event) death_competing
            FROM analysis_data GROUP BY exposure_group
        """)
        logger.info(f"Step 6:\n{events.to_string()}")
        return events

    def build_cohort(self, cb=None):
        results = {}
        results['base_n'] = self.step1_base_population(cb)
        mem_manager.cleanup_after_step('step1')

        results['dm_claims'] = self.step2_dm_claims(cb)
        mem_manager.cleanup_after_step('step2')

        results['dm_meds'] = self.step3_dm_medications(cb)
        mem_manager.cleanup_after_step('step3')

        results['groups'] = self.step4_classify_groups(cb)
        mem_manager.cleanup_after_step('step4')

        n, excl = self.step5_exclude_dementia(cb)
        results['final_n'] = n
        results['excluded_dementia'] = excl
        mem_manager.cleanup_after_step('step5')

        results['outcomes'] = self.step6_outcomes(cb)
        mem_manager.cleanup_after_step('step6')

        if cb: cb("코호트 구축 완료!")
        return results
