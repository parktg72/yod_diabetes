"""
cohort_builder.py - 코호트 구축 모듈
프로토콜 반영: 외래2회/입원1회, T1+T2 제외, 항치매약 제외, 65세 censoring
"""

import logging
import re
import time
import duckdb
from config import (
    DM_CODES, DEMENTIA_CODES, DEMENTIA_DRUG_CODES,
    OHA_CODES, INSULIN_EFMDC, INSULIN_CODES, STUDY_SETTINGS
)
from memory_manager import mem_manager
from utils import icd_like, CohortStepError

logger = logging.getLogger(__name__)


class CohortBuilder:
    def __init__(self, data_manager):
        self.dm = data_manager
        self.settings = STUDY_SETTINGS

    def _run_step(self, step_num: int, step_name: str, sql: str, result_table: str) -> int:
        """단계 SQL 실행 + 1회 재시도 + 행 수 검증.

        성공 시 result_table의 행 수 반환.
        실패(duckdb.Error) 또는 결과 0건 시 CohortStepError 발생.

        Note: build_cohort 는 step_fn 기반 _safe_step 내부 함수를 사용한다.
              이 메서드는 단위 테스트(test_cohort_safety.py)에서 재시도·검증 로직
              직접 검증용으로 노출되어 있다.
        """
        for attempt in range(2):
            try:
                self.dm.execute(sql)
                break
            except duckdb.Error as e:
                if attempt == 0:
                    logger.warning(
                        f"[{step_num}/6] {step_name} 1차 실패, 1초 후 재시도: {e}"
                    )
                    time.sleep(1)
                else:
                    raise CohortStepError(step_num, step_name, e) from e

        n = self.dm.storage.get_row_count(result_table)
        if n == 0:
            raise CohortStepError(
                step_num, step_name,
                ValueError(f"{result_table} 결과 0건 — 데이터 적재 상태를 확인하세요.")
            )
        logger.info(f"[{step_num}/6] {step_name}: {n:,}건")
        return n

    def _flat_oha_codes(self):
        codes = []
        for v in OHA_CODES.values():
            codes.extend(v)
        return codes

    def step1_base_population(self, cb=None):
        """40-64세, 진입기간 2013-2016, 진입 전 1년 자격유지"""
        if cb: cb("Step 1: 기본 대상 인구 정의 중...")
        es = int(self.settings.get('ENROLLMENT_START', 2013))
        # NULL 품질 사전 점검 — BYEAR/STD_YYYY NULL 건수 로깅
        try:
            null_res = self.dm.query(
                "SELECT COUNT(*) AS n FROM JK WHERE BYEAR IS NULL OR STD_YYYY IS NULL"
            )
            n_null = int(null_res.iloc[0, 0]) if len(null_res) > 0 else 0
            if n_null > 0:
                msg = f"[경고] JK 테이블 BYEAR/STD_YYYY NULL 값 {n_null:,}건 — 해당 행은 자동 제외됩니다."
                logger.warning(msg)
                if cb: cb(msg)
        except Exception as _qe:
            logger.debug("JK NULL 품질 점검 건너뜀: %s", _qe)
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
              AND NOT EXISTS (SELECT 1 FROM prevalent_dm pd WHERE pd.INDI_DSCM_NO = t40.INDI_DSCM_NO)
        """)

        self.dm.execute(f"""
            INSERT INTO dm_claims
            SELECT t20.INDI_DSCM_NO, t20.SICK_SYM1, t20.MDCARE_STRT_DT, t20.CMN_KEY,
                   CASE WHEN {t1s} THEN 'T1DM' WHEN {t2s} THEN 'T2DM' END
            FROM T20 t20
            INNER JOIN base_population bp ON t20.INDI_DSCM_NO = bp.INDI_DSCM_NO
            WHERE ({t1s} OR {t2s})
              AND CAST(SUBSTR(t20.MDCARE_STRT_DT, 1, 4) AS INTEGER) >= bp.first_year
              AND NOT EXISTS (SELECT 1 FROM prevalent_dm pd WHERE pd.INDI_DSCM_NO = t20.INDI_DSCM_NO)
        """)
        return self.dm.storage.get_row_count('dm_claims')

    @staticmethod
    def _validate_medical_codes(codes, label):
        """의료 코드 allowlist 검증 — 영숫자와 하이픈만 허용 (SQL 인젝션 방지)"""
        _valid = re.compile(r'^[A-Za-z0-9\-]+$')
        for code in codes:
            if not _valid.match(str(code)):
                raise ValueError(f"유효하지 않은 {label} 코드: {code!r}")

    def step3_dm_medications(self, cb=None):
        """당뇨 약물 처방 식별"""
        if cb: cb("Step 3: 당뇨 약물 처방 식별 중...")
        oha_codes = self._flat_oha_codes()
        self._validate_medical_codes(oha_codes, 'OHA')
        self._validate_medical_codes(INSULIN_CODES, 'INSULIN_CODES')
        self._validate_medical_codes(INSULIN_EFMDC, 'INSULIN_EFMDC')
        oha = "'" + "','".join(oha_codes) + "'"
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

    def _create_med_pattern(self, lookback_days: int, table_suffix: str = ""):
        """약물 패턴 테이블 생성 (헬퍼 메서드).

        Args:
            lookback_days: 기저선 기간 (진단일로부터 며칠까지 포함)
            table_suffix: 테이블명 접미사 (민감도 분석용, 예: "_60day")
        """
        table_name = f"med_pattern{table_suffix}"
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE {table_name} AS
            SELECT m.INDI_DSCM_NO,
                   MAX(CASE WHEN m.med_type='INSULIN' THEN 1 ELSE 0 END) AS has_insulin,
                   MAX(CASE WHEN m.med_type='OHA'     THEN 1 ELSE 0 END) AS has_oha,
                   MIN(CASE WHEN m.med_type='INSULIN' THEN m.rx_date END) AS insulin_start_date
            FROM dm_medications m
            INNER JOIN dm_patients dp ON m.INDI_DSCM_NO = dp.INDI_DSCM_NO
            WHERE m.rx_date >= dp.first_dm_date
              AND m.rx_date <= REPLACE(CAST(
                  (CAST(SUBSTR(dp.first_dm_date,1,4)||'-'||SUBSTR(dp.first_dm_date,5,2)||'-'||SUBSTR(dp.first_dm_date,7,2) AS DATE)
                  + INTERVAL '{lookback_days}' DAY)::DATE
              AS VARCHAR), '-', '')
            GROUP BY m.INDI_DSCM_NO
        """)

    def step4_classify_groups(self, cb=None, lookback_days: int = 90):
        """노출군 분류: 외래2회+/입원1회+, T1+T2 동시보유 제외"""
        if cb: cb("Step 4: 노출군 분류 중...")
        mo = int(self.settings.get('MIN_DM_CLAIMS_OUTPATIENT', 2))
        mi = int(self.settings.get('MIN_DM_CLAIMS_INPATIENT', 1))

        # 입원 CMN_KEY 사전 추출 (T20 반복 서브쿼리 방지)
        inpt_form = self.settings.get('INPATIENT_FORM_CD', '02')
        self.dm.execute(f"""
            CREATE OR REPLACE TABLE _inpatient_keys AS
            SELECT DISTINCT CMN_KEY FROM T20 WHERE FORM_CD='{inpt_form}'
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
                   -- first_dm_date: 외래·입원 모두 있으면 더 이른 날짜, 한쪽만 있으면 그 값
                   -- LEAST(NULL, x) = NULL(표준 SQL)이므로 명시적 CASE로 안전하게 처리
                   -- GROUP BY 집계 컨텍스트이므로 MIN()으로 래핑
                   MIN(CASE
                     WHEN o.first_dt IS NOT NULL AND i.first_dt IS NOT NULL
                          THEN LEAST(o.first_dt, i.first_dt)
                     ELSE COALESCE(o.first_dt, i.first_dt)
                   END) AS first_dm_date
            FROM outpt o FULL OUTER JOIN inpt i ON o.INDI_DSCM_NO=i.INDI_DSCM_NO AND o.dm_type=i.dm_type
            WHERE COALESCE(o.n,0) >= {mo} OR COALESCE(i.n,0) >= {mi}
            GROUP BY COALESCE(o.INDI_DSCM_NO, i.INDI_DSCM_NO), COALESCE(o.dm_type, i.dm_type)
        """)

        # 임시 테이블 정리
        self.dm.execute("DROP TABLE IF EXISTS _inpatient_keys")

        # T1+T2 동시 보유자 제외
        self.dm.execute("""
            CREATE OR REPLACE TABLE dual_dm AS
            SELECT INDI_DSCM_NO FROM dm_patients GROUP BY INDI_DSCM_NO HAVING COUNT(DISTINCT dm_type) > 1
        """)

        # med_pattern: Phase 2 개정 — 초기 약물 치료 기간 정의
        # 약물 집계 기간: [first_dm_date, first_dm_date + lookback_days]
        # 근거: 한국 당뇨병 진료지침 및 ADA 기준 (초진 후 3개월 내 재평가)
        # 기본값: 90일 (임상적 근거 있음), 민감도 분석: 60일/180일로 비교
        self._create_med_pattern(lookback_days)

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
                   -- 주의: DM군의 index_date는 진단일이라 NON_DM보다 늦을 수 있음.
                   -- 이 calendar time 차이를 보정하기 위해 index_year를 Cox 공변량에 포함함.
                   COALESCE(
                       t1.first_dm_date,
                       t2.first_dm_date,
                       CAST(bp.first_year AS VARCHAR) || '0101'
                   ) AS index_date,
                   COALESCE(t1.first_dm_date, t2.first_dm_date) AS first_dm_date,
                   -- index_year: Cox/PSM에서 calendar time 보정용 (NON_DM vs DM 관찰시작 불일치 보정)
                   CAST(SUBSTR(COALESCE(
                       t1.first_dm_date,
                       t2.first_dm_date,
                       CAST(bp.first_year AS VARCHAR) || '0101'
                   ), 1, 4) AS INT) AS index_year,
                   -- Phase 2: 기저선 기간 내 첫 인슐린 처방일 (DM 환자만 해당, NON_DM은 NULL)
                   mp.insulin_start_date
            FROM base_population bp
            LEFT JOIN dm_patients t1 ON bp.INDI_DSCM_NO=t1.INDI_DSCM_NO AND t1.dm_type='T1DM'
            LEFT JOIN dm_patients t2 ON bp.INDI_DSCM_NO=t2.INDI_DSCM_NO AND t2.dm_type='T2DM'
            LEFT JOIN med_pattern mp ON bp.INDI_DSCM_NO=mp.INDI_DSCM_NO
            WHERE NOT EXISTS (SELECT 1 FROM dual_dm dd WHERE dd.INDI_DSCM_NO = bp.INDI_DSCM_NO)
        """)

        result = self.dm.query("SELECT exposure_group, COUNT(*) AS n FROM exposure_groups GROUP BY exposure_group ORDER BY 1")
        logger.info(f"Step 4:\n{result.to_string()}")

        # I9: T2DM_OHA/T2DM_INSULIN 0건 경고 — 약물코드 또는 처방기간 설정 오류 가능성 알림
        groups_n = dict(zip(result['exposure_group'], result['n']))
        warnings = []
        for grp in ('T2DM_OHA', 'T2DM_INSULIN'):
            if groups_n.get(grp, 0) == 0:
                msg = f"[경고] {grp} 코호트가 0건입니다 — 약물 코드 또는 처방 기간 설정을 확인하세요."
                logger.warning(f"Step 4: {msg}")
                if cb: cb(msg)
                warnings.append(msg)

        # Phase 2: med_switch 테이블 생성 (OHA→INSULIN 추적기간 전환)
        try:
            self.dm.execute("""
                CREATE OR REPLACE TABLE med_switch AS
                SELECT eg.INDI_DSCM_NO,
                       MIN(m.rx_date) AS insulin_switch_date
                FROM exposure_groups eg
                INNER JOIN dm_medications m
                    ON eg.INDI_DSCM_NO = m.INDI_DSCM_NO
                    AND m.med_type = 'INSULIN'
                    AND m.rx_date > eg.index_date
                WHERE eg.exposure_group IN ('T2DM_OHA', 'T2DM_NOMED')
                GROUP BY eg.INDI_DSCM_NO
                HAVING MIN(m.rx_date) IS NOT NULL
            """)
            n_switch = self.dm.storage.get_row_count('med_switch')
            logger.info(f"Step 4-ext: med_switch 테이블 생성 완료 ({n_switch}명 OHA→INSULIN 전환)")
            if cb: cb(f"✓ med_switch 생성: {n_switch}명 전환 추적")
        except Exception as e:
            logger.warning(f"Step 4-ext: med_switch 생성 실패: {e}")
            # 빈 스텁 테이블 생성 — variable_generator의 LEFT JOIN 크래시 방지
            try:
                self.dm.execute("""
                    CREATE OR REPLACE TABLE med_switch AS
                    SELECT INDI_DSCM_NO, NULL::VARCHAR AS insulin_switch_date
                    FROM analysis_data WHERE 1=0
                """)
                logger.info("Step 4-ext: med_switch 빈 테이블 생성 (LEFT JOIN 호환성)")
                if cb: cb(f"[경고] med_switch 생성 실패 → 빈 스텁 테이블 사용 (서브그룹 분석 미지원): {e}")
            except Exception as stub_e:
                logger.error(f"Step 4-ext: med_switch 스텁 테이블 생성도 실패: {stub_e}")
                raise RuntimeError(f"med_switch 테이블 생성 불가: 원본={e}, 스텁={stub_e}")

        return result, warnings

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
            WHERE NOT EXISTS (SELECT 1 FROM excl_dementia_dx dx WHERE dx.INDI_DSCM_NO = exposure_groups.INDI_DSCM_NO)
              AND NOT EXISTS (SELECT 1 FROM excl_dementia_drug dr WHERE dr.INDI_DSCM_NO = exposure_groups.INDI_DSCM_NO)
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

        # age65_date: 생년만 알고 생년월일이 없으므로 BYEAR+65의 AGE65_CENSOR_MONTH로 근사
        # 기본='0101': 최대 ~11개월 조기 censoring 발생 (행정DB 한계).
        # 민감도 분석 시 AGE65_CENSOR_MONTH='0701'로 설정하면 평균 편향 최소화 (약 ±6개월)
        # 사망일자: DEATH 테이블(DTH_ASSMD_DT) 우선, 없으면 JK.HHDT_DEATH fallback
        has_death_table = self.dm.storage.table_exists('DEATH')
        if not has_death_table and self.settings.get('JK_SOURCE') == 'hana_monthly':
            msg = (
                "[경고] JK_SOURCE=hana_monthly이지만 DEATH 테이블이 없습니다. "
                "HHDT_DEATH=NULL이므로 death_date가 모두 NULL로 처리되어 사망 censoring이 비활성화됩니다. "
                "정확한 분석을 위해 DEATH 테이블을 별도 로드하세요."
            )
            logger.warning(msg)
            if cb:
                cb(msg)

        if has_death_table:
            last_eligible_sql = f"""
            last_eligible AS (
                -- DEATH 테이블 연동: DTH_ASSMD_DT 우선, JK.HHDT_DEATH fallback
                SELECT jk.INDI_DSCM_NO,
                       MAX(CAST(jk.SURV_YR AS INTEGER)) || '1231' AS withdrawal_date,
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
                       MAX(CAST(SURV_YR AS INTEGER)) || '1231' AS withdrawal_date,
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
                       CAST((CAST(sc.BYEAR AS INT) + {yod}) || '{self.settings.get("AGE65_CENSOR_MONTH", "0101")}' AS VARCHAR) AS age65_date,
                       oa.event_date AS dementia_date,
                       ad.event_date AS ad_date,
                       vd.event_date AS vad_date,
                       le.death_date,
                       LEAST(
                           COALESCE(oa.event_date, '{ey}1231'),
                           CAST((CAST(sc.BYEAR AS INT) + {yod}) || '{self.settings.get("AGE65_CENSOR_MONTH", "0101")}' AS VARCHAR),
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
                   -- 경쟁위험: 치매 미발생 + 관찰기간 내 실제 사망 (탈퇴/검열 제외)
                   CASE WHEN (dementia_date IS NULL OR dementia_date > censor_date)
                             AND death_date IS NOT NULL
                             AND death_date <= censor_date
                             AND death_date > index_date
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

        # follow_up_days <= 0: censor_date <= index_date — 추적 불가능한 행 제거
        bad_fu = self.dm.query("SELECT COUNT(*) AS n FROM analysis_data WHERE follow_up_days <= 0")
        n_bad = int(bad_fu.iloc[0, 0]) if len(bad_fu) > 0 else 0
        if n_bad > 0:
            self.dm.execute("DELETE FROM analysis_data WHERE follow_up_days <= 0")
            msg = (
                f"[데이터 정리] follow_up_days <= 0인 {n_bad:,}건을 제거했습니다.\n"
                f"원인: index_date >= censor_date (자격 소멸·사망이 진입일과 동일하거나 이전).\n"
                f"비율이 높으면 config.py의 ENROLLMENT_END 또는 censor 조건을 확인하세요."
            )
            logger.warning(msg.replace('\n', ' '))
            if cb: cb(f"[경고] {msg}")

        events = self.dm.query("""
            SELECT exposure_group, COUNT(*) n, SUM(dementia_event) dem, SUM(ad_event) ad, SUM(vad_event) vad,
                   SUM(death_event) death_actual, SUM(competing_death_event) death_competing
            FROM analysis_data GROUP BY exposure_group
        """)
        logger.info(f"Step 6:\n{events.to_string()}")

        # 무결성 검증: 노출군 0건 경고
        integrity_warnings = []
        for _, row in events.iterrows():
            g, n_g = row['exposure_group'], int(row['n'])
            if n_g == 0:
                integrity_warnings.append(
                    f"노출군 '{g}' 0건 — 해당 군은 Cox/PSM 분석에서 자동 제외됩니다."
                )
            elif int(row.get('dem', 0) or 0) == 0:
                integrity_warnings.append(
                    f"노출군 '{g}' 치매 이벤트 0건 — Cox 회귀에서 해당 군 계수 추정 불가."
                )

        # follow_up_years 음수 잔류 확인 (혹시 DELETE 누락 시)
        neg_check = self.dm.query(
            "SELECT COUNT(*) AS n FROM analysis_data WHERE follow_up_years < 0"
        )
        n_neg = int(neg_check.iloc[0, 0]) if len(neg_check) > 0 else 0
        if n_neg > 0:
            integrity_warnings.append(
                f"follow_up_years 음수 {n_neg:,}건 잔류 — 추적기간 산출 오류 가능."
            )

        if integrity_warnings and cb:
            for w in integrity_warnings:
                cb(f"[무결성 경고] {w}")

        # n_bad를 events 반환값에 메타데이터로 포함 (UI 표시용)
        events.attrs['removed_bad_fu'] = n_bad
        events.attrs['integrity_warnings'] = integrity_warnings
        return events

    def build_cohort(self, cb=None):
        """6단계 코호트 파이프라인 실행.

        각 단계는 duckdb.Error 발생 시 1회 재시도 후 CohortStepError를 발생시킨다.
        단계 결과가 0건이면 CohortStepError를 발생시켜 후속 단계 실행을 막는다.
        예외: Step 3(dm_medications)는 T2DM_NOMED 코호트에서 0건이 정상이므로 허용.
        """
        results = {}

        def _safe_step(step_num, step_name, step_fn, result_table, allow_zero=False):
            """단계 함수를 실행하고 CohortStepError로 감싼다."""
            for attempt in range(2):
                try:
                    ret = step_fn(cb)
                    break
                except CohortStepError:
                    raise  # 이미 래핑된 예외는 그대로 전파
                except duckdb.Error as e:
                    if attempt == 0:
                        logger.warning(
                            f"[{step_num}/6] {step_name} 1차 실패, 1초 후 재시도: {e}"
                        )
                        time.sleep(1)
                    else:
                        raise CohortStepError(step_num, step_name, e)
                except Exception as e:
                    raise CohortStepError(step_num, step_name, e)

            n = self.dm.storage.get_row_count(result_table)
            if n == 0 and not allow_zero:
                raise CohortStepError(
                    step_num, step_name,
                    ValueError(f"{result_table} 결과 0건 — 데이터 적재 상태를 확인하세요.")
                )
            logger.info(f"[{step_num}/6] {step_name} 완료: {n:,}건")
            return ret, n

        results['base_n'], _ = _safe_step(
            1, "기본 대상 인구 정의",
            self.step1_base_population, "base_population"
        )
        mem_manager.cleanup_after_step('step1')

        results['dm_claims'], _ = _safe_step(
            2, "당뇨 진단 청구 식별",
            self.step2_dm_claims, "dm_claims"
        )
        mem_manager.cleanup_after_step('step2')

        results['dm_meds'], _ = _safe_step(
            3, "당뇨 약물 처방 식별",
            self.step3_dm_medications, "dm_medications",
            allow_zero=True
        )
        mem_manager.cleanup_after_step('step3')

        step4_ret, _ = _safe_step(
            4, "노출군 분류",
            self.step4_classify_groups, "exposure_groups"
        )
        # step4_classify_groups returns (groups_df, warnings_list)
        if isinstance(step4_ret, tuple):
            results['groups'], results['warnings'] = step4_ret
        else:
            results['groups'] = step4_ret
            results['warnings'] = []
        mem_manager.cleanup_after_step('step4')

        (n, excl), _ = _safe_step(
            5, "기존 치매 및 항치매약 제외",
            self.step5_exclude_dementia, "study_cohort"
        )
        results['final_n'] = n
        results['excluded_dementia'] = excl
        mem_manager.cleanup_after_step('step5')

        results['outcomes'], _ = _safe_step(
            6, "결과변수 및 추적기간 산출",
            self.step6_outcomes, "analysis_data"
        )
        mem_manager.cleanup_after_step('step6')

        # 무결성 경고를 results에 전달 (UI의 _on_cohort에서 표시)
        outcomes_df = results.get('outcomes')
        if outcomes_df is not None and hasattr(outcomes_df, 'attrs'):
            removed = outcomes_df.attrs.get('removed_bad_fu', 0)
            integrity_w = outcomes_df.attrs.get('integrity_warnings', [])
            if removed > 0:
                results['warnings'] = results.get('warnings', []) + [
                    f"추적기간 0일 이하 {removed:,}건 제거됨 — cohort 기준 확인 필요"
                ]
            if integrity_w:
                results['warnings'] = results.get('warnings', []) + integrity_w

        if cb: cb("코호트 구축 완료!")
        return results

    def sensitivity_analysis(self, lookback_days_list: list = None):
        """약물 집계 기간별 민감도 분석 (60일, 90일, 180일 비교).

        각 기간별로 약물 분류를 재실행하고 코호트 크기, 약물 분포를 비교.

        Returns:
            dict: 각 기간별 약물 분류 결과
              {
                '60days': {'T2DM_INSULIN': n, 'T2DM_OHA': n, 'T2DM_NOMED': n, ...},
                '90days': {...},
                '180days': {...}
              }
        """
        if lookback_days_list is None:
            lookback_days_list = [60, 90, 180]

        logger.info(f"민감도 분석: 약물 집계 기간 {lookback_days_list}일 비교 시작")
        results = {}

        for days in lookback_days_list:
            try:
                # 각 기간별로 med_pattern 생성
                suffix = f"_{days}day"
                self._create_med_pattern(days, suffix)

                # exposure_groups 재생성 (해당 med_pattern 사용)
                med_pattern_table = f"med_pattern{suffix}"
                self.dm.execute(f"""
                    CREATE OR REPLACE TABLE exposure_groups{suffix} AS
                    SELECT bp.INDI_DSCM_NO, bp.SEX_TYPE, bp.BYEAR,
                           CASE
                             WHEN t1.INDI_DSCM_NO IS NOT NULL THEN 'T1DM'
                             WHEN t2.INDI_DSCM_NO IS NOT NULL AND mp.has_insulin=1 THEN 'T2DM_INSULIN'
                             WHEN t2.INDI_DSCM_NO IS NOT NULL AND mp.has_oha=1 THEN 'T2DM_OHA'
                             WHEN t2.INDI_DSCM_NO IS NOT NULL THEN 'T2DM_NOMED'
                             ELSE 'NON_DM'
                           END AS exposure_group
                    FROM base_population bp
                    LEFT JOIN dm_patients t1 ON bp.INDI_DSCM_NO=t1.INDI_DSCM_NO AND t1.dm_type='T1DM'
                    LEFT JOIN dm_patients t2 ON bp.INDI_DSCM_NO=t2.INDI_DSCM_NO AND t2.dm_type='T2DM'
                    LEFT JOIN {med_pattern_table} mp ON bp.INDI_DSCM_NO=mp.INDI_DSCM_NO
                    WHERE NOT EXISTS (SELECT 1 FROM dual_dm dd WHERE dd.INDI_DSCM_NO = bp.INDI_DSCM_NO)
                """)

                # 각 기간별 분포 조회
                dist_df = self.dm.query(f"""
                    SELECT exposure_group, COUNT(*) AS n
                    FROM exposure_groups{suffix}
                    GROUP BY exposure_group
                    ORDER BY exposure_group
                """)

                # 결과 저장
                group_dict = dict(zip(dist_df['exposure_group'], dist_df['n']))
                results[f'{days}days'] = group_dict

                logger.info(f"{days}일 윈도우: T2DM_INSULIN={group_dict.get('T2DM_INSULIN', 0)}, "
                           f"T2DM_OHA={group_dict.get('T2DM_OHA', 0)}, "
                           f"T2DM_NOMED={group_dict.get('T2DM_NOMED', 0)}")

            except Exception as e:
                logger.error(f"민감도 분석 실패 ({days}일): {e}")
                results[f'{days}days'] = None

        # 결과 비교 표 출력
        logger.info("\n민감도 분석 결과:")
        logger.info("=" * 70)
        groups = set()
        for day_results in results.values():
            if day_results:
                groups.update(day_results.keys())

        for group in sorted(groups):
            row = f"{group:15}"
            for days in lookback_days_list:
                count = results[f'{days}days'].get(group, 0) if results[f'{days}days'] else 0
                row += f" | {days}일: {count:6,}"
            logger.info(row)
        logger.info("=" * 70)

        return results
