"""
statistical_analysis.py - 통계 분석 모듈
프로토콜 반영: 3단계 Cox, PSM 1:3, 하위그룹(저혈당,CVD), Fine-Gray
메모리 최적화: 단일 로드 + 각 단계 후 GC + matched_df 미저장
"""

import gc
import logging
import numpy as np
import pandas as pd
from lifelines import CoxPHFitter, KaplanMeierFitter
from lifelines.statistics import proportional_hazard_test
from scipy import stats
from config import STUDY_SETTINGS, DEMENTIA_DRUG_CODES
from memory_manager import mem_manager
from utils import (
    InsufficientDataError,
    format_error_for_user,
    make_error_result,
    make_skip_result,
    make_model_failure,
)
from dataclasses import dataclass
import duckdb

logger = logging.getLogger(__name__)


@dataclass
class SamplingInfo:
    """층화 샘플링 적용 여부 및 규모 정보.

    applied: 샘플링이 적용되었으면 True
    total_rows: 원본 전체 행 수
    sampled_rows: 실제 분석에 사용된 행 수
    seed: 재현성을 위한 DuckDB setseed 값 (0–99 정수)
    """
    applied: bool
    total_rows: int
    sampled_rows: int
    seed: int = 0

    @property
    def ratio_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return self.sampled_rows / self.total_rows * 100

    @property
    def label(self) -> str:
        """UI 및 Excel 헤더용 한줄 요약. 샘플링 없으면 빈 문자열."""
        if not self.applied:
            return ""
        return (
            f"층화 샘플링: {self.sampled_rows:,}/{self.total_rows:,}건 "
            f"({self.ratio_pct:.1f}%, seed={self.seed})"
        )


class StatisticalAnalyzer:
    def __init__(self, data_manager):
        self.dm = data_manager
        self.results = {}
        self._cached_df = None  # 데이터 1회 로드 후 캐시
        self._sampling_info = SamplingInfo(applied=False, total_rows=0, sampled_rows=0)

    # 유효 행 없음 에러 메시지 — 샘플링/비샘플링 양 경로 공통
    _MSG_NO_VALID_ROWS = (
        "추적 가능한 행(follow_up_days > 0)이 없습니다. "
        "코호트 구성 단계를 확인하세요."
    )

    # Phase 2 post-index 변수(immortal time bias 위험): baseline 공변량 분석에서 금지
    _ITB_REASON_CODE = 'ITB_POST_INDEX_COVARIATE'
    _POST_INDEX_COVARIATES = {
        'had_insulin_switch',
        'days_to_switch',
        'med_switch_date',
        'insulin_switch_date',
    }
    _RC_INSUFFICIENT_DATA = 'INSUFFICIENT_DATA'
    _RC_INSUFFICIENT_GROUPS = 'INSUFFICIENT_GROUPS'
    _RC_INVALID_PSM_CALIPER = 'INVALID_PSM_CALIPER'
    _RC_NO_PSM_MATCHES = 'NO_PSM_MATCHES'
    _RC_MISSING_REQUIRED_COLUMN = 'MISSING_REQUIRED_COLUMN'
    _RC_MISSING_UPSTREAM_RESULT = 'MISSING_UPSTREAM_RESULT'
    _RC_COX_MODEL_FAILED = 'COX_MODEL_FAILED'
    _RC_PH_VIOLATION = 'PH_VIOLATION'
    _RC_ALL_COX_MODELS_FAILED = 'ALL_COX_MODELS_FAILED'
    _RC_ANALYSIS_ERROR = 'ANALYSIS_ERROR'
    _RC_CROSS_VALIDATION_ERROR = 'CROSS_VALIDATION_ERROR'
    _RC_SENSITIVITY_ERROR = 'SENSITIVITY_ERROR'

    def _assert_no_post_index_covariates(self, covariates, context):
        """Baseline 모델 공변량에서 post-index 변수 사용을 차단한다."""
        forbidden = sorted(set(covariates) & set(self._POST_INDEX_COVARIATES))
        if forbidden:
            raise ValueError(
                f"{self._ITB_REASON_CODE}: context={context}; forbidden_covariates={','.join(forbidden)}"
            )

    def _skip_result(self, reason_code, reason, *, stage=None, **extra):
        """utils.make_skip_result 위임 wrapper."""
        return make_skip_result(reason_code, reason, stage=stage, **extra)

    def _model_failure(self, reason_code, reason, *, stage='cox', **extra):
        """utils.make_model_failure 위임 wrapper."""
        return make_model_failure(reason_code, reason, stage=stage, **extra)

    def _error_result(self, reason_code, error, *, stage=None, **extra):
        """utils.make_error_result 위임 wrapper."""
        return make_error_result(reason_code, error, stage=stage, **extra)

    def _load_data(self, cb=None):
        """메모리 안전 데이터 로드 — 1회 로드 후 캐시 재사용"""
        if self._cached_df is not None:
            return self._cached_df, self._sampling_info

        if cb: cb("분석 데이터 로딩 중...")
        min_valid = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
        if min_valid <= 0:
            raise ValueError(f"MIN_VALID_ROWS 는 양의 정수여야 합니다: {min_valid}")
        max_rows = mem_manager.get_safe_analysis_rows()
        total = self.dm.storage.get_row_count('final_analysis')
        if total > max_rows:
            logger.warning(f"분석 데이터 {total:,}건 > 안전 한도 {max_rows:,}건 → 층화 샘플링")
            if cb: cb(f"층화 샘플링 적용 중... ({total:,}건 → {max_rows:,}건 목표)")
            # 각 노출군의 실제 비율에 비례하여 max_rows 배분
            group_counts_df = self.dm.query(
                "SELECT exposure_group, COUNT(*) AS cnt FROM final_analysis "
                "WHERE follow_up_days > 0 GROUP BY exposure_group"
            )
            group_counts = dict(zip(group_counts_df['exposure_group'], group_counts_df['cnt']))
            valid_total = sum(group_counts.values())

            if valid_total == 0:
                logger.warning("샘플링 분기: total=%d, valid_total=0 — EmptyDataError", total)
                raise pd.errors.EmptyDataError(self._MSG_NO_VALID_ROWS)
            if valid_total < min_valid:
                logger.warning("샘플링 분기: valid_total=%d < min_valid=%d — InsufficientDataError",
                               valid_total, min_valid)
                raise InsufficientDataError(valid_rows=valid_total, min_rows=min_valid)

            # DM 그룹은 전부 유지, NON_DM만 남은 예산으로 샘플링
            # → DM 분석 underpowered 방지 + 노출군 비율 왜곡 최소화
            dm_total = sum(c for g, c in group_counts.items() if g != 'NON_DM')
            non_dm_budget = max(max_rows - dm_total, 0)

            alloc = {}
            for g, cnt in group_counts.items():
                if g == 'NON_DM':
                    alloc[g] = min(cnt, non_dm_budget)
                else:
                    alloc[g] = cnt  # DM 그룹 전수 포함

            # 할당 0인 그룹은 CASE 조건에서 제외 — ELSE 0 으로 rn <= 0 → 0건 반환
            # exposure_group 값의 단따옴표를 이스케이프하여 SQL 안전성 보장
            per_group_sql_cases = " ".join(
                f"WHEN exposure_group = '{g.replace(chr(39), chr(39)*2)}' THEN {n}"
                for g, n in alloc.items()
                if n > 0
            )

            seed = int(STUDY_SETTINGS.get('SAMPLING_SEED', 42))
            if not (0 <= seed <= 99):
                raise ValueError(
                    f"SAMPLING_SEED는 0-99 범위여야 합니다. 현재 값: {seed}"
                )
            seed_float = seed / 100.0  # DuckDB setseed: float in [0, 1]
            self.dm.execute(f"SELECT setseed({seed_float})")
            logger.info("샘플링 전략: dm_total=%d, non_dm_budget=%d, max_rows=%d, seed=%d",
                        dm_total, non_dm_budget, max_rows, seed)

            self._cached_df = self.dm.query(f"""
                SELECT * EXCLUDE rn
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY exposure_group ORDER BY RANDOM()
                           ) AS rn,
                           CASE {per_group_sql_cases} ELSE 0 END AS grp_limit
                    FROM final_analysis
                    WHERE follow_up_days > 0
                ) t
                WHERE rn <= grp_limit
            """)
            self._sampling_info = SamplingInfo(
                applied=True,
                total_rows=valid_total,  # follow_up_days>0 유효 행 기준 (Excel 헤더 비율 정확도)
                sampled_rows=len(self._cached_df),
                seed=seed,
            )
            gc.collect()
        else:
            self._cached_df = self.dm.query("SELECT * FROM final_analysis WHERE follow_up_days > 0")
            if self._cached_df.empty:
                logger.warning("비샘플링 분기: total=%d, valid_rows=0 — EmptyDataError", total)
                raise pd.errors.EmptyDataError(self._MSG_NO_VALID_ROWS)
            valid_rows = len(self._cached_df)
            if valid_rows < min_valid:
                logger.warning("비샘플링 분기: valid_rows=%d < min_valid=%d — InsufficientDataError",
                               valid_rows, min_valid)
                raise InsufficientDataError(valid_rows=valid_rows, min_rows=min_valid)
            self._sampling_info = SamplingInfo(
                applied=False,
                total_rows=total,
                sampled_rows=len(self._cached_df),
            )

        # dtype 최적화
        self._cached_df = mem_manager.optimize_dtypes(self._cached_df)
        if cb: cb(f"데이터 로드 완료: {len(self._cached_df):,}건")
        logger.info(f"분석 데이터 로드: {len(self._cached_df):,}건, "
                   f"{self._cached_df.memory_usage(deep=True).sum() / 1024**2:.1f}MB")
        return self._cached_df, self._sampling_info

    def _check_min_rows(self, df: pd.DataFrame, context: str = "") -> None:
        """dropna 등 필터 후 행 수가 MIN_VALID_ROWS 미만이면 InsufficientDataError 발생.

        run_cox, run_subgroup 등 분석 함수에서 cph.fit() 직전에 호출.
        """
        min_valid = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
        if len(df) < min_valid:
            logger.warning("%s: dropna 후 행 수 %d < min_valid %d — InsufficientDataError",
                           context, len(df), min_valid)
            raise InsufficientDataError(valid_rows=len(df), min_rows=min_valid)

    @staticmethod
    def _compute_interaction_pval(df_full: pd.DataFrame, exposure_cols: list,
                                   indicator_col: str, covariate_cols: list,
                                   outcome: str = 'dementia_event',
                                   duration: str = 'follow_up_years') -> float:
        """LRT 기반 노출×하위그룹 상호작용 p-value 계산.

        Args:
            df_full: 전체 분석 데이터프레임 (복사본 전달 권장 안 함 — 내부에서만 참조)
            exposure_cols: 노출 지시 변수 목록 (is_t1dm 등)
            indicator_col: 하위그룹 이진 지시 변수 컬럼명
            covariate_cols: 기저 공변량 목록

        Returns:
            LRT p-value (float), 계산 실패 시 np.nan
        """
        try:
            base_use = [c for c in exposure_cols + [indicator_col] + covariate_cols
                        if c in df_full.columns]

            # 상호작용 항 생성 (exposure × indicator)
            int_data = {}
            for exp_col in exposure_cols:
                if exp_col in df_full.columns and indicator_col in df_full.columns:
                    int_data[f'_int_{exp_col}'] = (
                        pd.to_numeric(df_full[exp_col], errors='coerce') *
                        pd.to_numeric(df_full[indicator_col], errors='coerce')
                    )
            if not int_data:
                return np.nan

            int_df = pd.DataFrame(int_data, index=df_full.index)
            int_cols = list(int_data.keys())
            full_use = base_use + int_cols

            # LRT는 동일 샘플에서 중첩 모델을 비교해야 유효
            # → 두 모델 공통 complete cases를 먼저 추출한 뒤 피팅
            all_cols = list(dict.fromkeys(full_use + [duration, outcome]))
            df_combined = pd.concat([df_full[base_use + [duration, outcome]], int_df], axis=1)
            df_common = df_combined[all_cols].dropna()
            df_common = df_common[df_common[duration] > 0]

            if len(df_common) < 30 or df_common[outcome].sum() < 5:
                return np.nan

            df_b = df_common[base_use + [duration, outcome]]
            df_f = df_common[full_use + [duration, outcome]]

            cph_base = CoxPHFitter()
            cph_base.fit(df_b, duration_col=duration, event_col=outcome)

            cph_full = CoxPHFitter()
            cph_full.fit(df_f, duration_col=duration, event_col=outcome)

            # LRT: χ² = 2 × (LL_full − LL_base), df = len(int_cols)
            lr_stat = max(0.0, 2.0 * (cph_full.log_likelihood_ - cph_base.log_likelihood_))
            return float(stats.chi2.sf(lr_stat, df=len(int_cols)))
        except Exception as e:
            logger.debug("Interaction p-value 계산 실패 (%s): %s", indicator_col, e)
            return np.nan

    def _release_cache(self):
        """캐시된 데이터 해제"""
        if self._cached_df is not None:
            del self._cached_df
            self._cached_df = None
            gc.collect()

    def _prepare(self, df, cb=None):
        """공변량 전처리 — 캐시 원본 보호를 위해 1회 copy 후 파생변수 추가

        Phase 2 통합: insulin_start_date, med_switch_date 처리
        - baseline_has_insulin: DM 환자 중 기저선 기간 내 인슐린 처방 여부
        - had_insulin_switch: T2DM_OHA/NOMED 환자 중 추적 중 인슐린 전환 여부 (from med_switch)
        - days_to_switch: 인슐린 전환까지 일수 (음수면 미전환)

        ⚠️ IMMORTAL TIME BIAS 경고:
        had_insulin_switch, days_to_switch는 정의상 index_date > 시점 변수.
        이들을 Cox/PSM의 정적 공변량으로 사용할 수 없음 (Suissa 2008).
        - 허용: 서술적 하위그룹 분석 (T2DM_OHA × switch 분층화 표)
        - 불가: Cox 회귀, PSM 공변량, 노출-결과 모델
        시변공변량/랜드마크 분석 필요 시 별도 구현 필요.
        """
        if cb: cb("데이터 전처리 중...")
        prepared = df.copy()  # 캐시(_cached_df) 원본 변경 방지를 위해 copy 필수

        prepared['is_t1dm'] = (prepared['exposure_group'] == 'T1DM').astype('int8')
        prepared['is_t2dm_oha'] = (prepared['exposure_group'] == 'T2DM_OHA').astype('int8')
        prepared['is_t2dm_insulin'] = (prepared['exposure_group'] == 'T2DM_INSULIN').astype('int8')
        prepared['is_t2dm_nomed'] = (prepared['exposure_group'] == 'T2DM_NOMED').astype('int8')
        prepared['male'] = (prepared['SEX_TYPE'] == '1').astype('int8')

        # Phase 2: insulin_start_date 처리 (VARCHAR YYYYMMDD → 분석 변수)
        if 'insulin_start_date' in prepared.columns:
            # DM 환자(T1DM, T2DM_*)만 해석, NON_DM은 0으로 설정 (분석에서 제외)
            is_dm = prepared['exposure_group'] != 'NON_DM'
            prepared['baseline_has_insulin'] = 0  # 기본값: 인슐린 없음

            # NULL이 아닌 insulin_start_date를 가진 DM 환자는 기저선 내 인슐린 사용
            has_insulin_date = is_dm & prepared['insulin_start_date'].notna()
            prepared.loc[has_insulin_date, 'baseline_has_insulin'] = 1
            prepared['baseline_has_insulin'] = prepared['baseline_has_insulin'].astype('int8')
            n_insulin = (has_insulin_date).sum()
            logger.debug(f"baseline_has_insulin: {n_insulin} DM patients with baseline insulin")
        else:
            prepared['baseline_has_insulin'] = 0  # 컬럼 부재 시 모두 0 (미포함)

        # Phase 2: med_switch_date 처리 (T2DM_OHA, T2DM_NOMED의 약물전환 추적)
        if 'med_switch_date' in prepared.columns and 'index_date' in prepared.columns:
            is_switch_eligible = prepared['exposure_group'].isin(['T2DM_OHA', 'T2DM_NOMED'])
            prepared['had_insulin_switch'] = 0  # 기본값: 전환 없음
            prepared['days_to_switch'] = pd.NA  # nullable Int64로 초기화

            # med_switch_date가 있는 T2DM_OHA/NOMED만 전환 플래그 설정
            had_switch = is_switch_eligible & prepared['med_switch_date'].notna()
            prepared.loc[had_switch, 'had_insulin_switch'] = 1
            prepared['had_insulin_switch'] = prepared['had_insulin_switch'].astype('int8')

            # days_to_switch 계산 (T2DM_OHA/NOMED)
            if had_switch.any():
                try:
                    # VARCHAR YYYYMMDD → date 변환 후 일수 계산 (벡터화 버전)
                    switch_dates = pd.to_datetime(
                        prepared.loc[had_switch, 'med_switch_date'],
                        format='%Y%m%d', errors='coerce'
                    )
                    index_dates = pd.to_datetime(
                        prepared.loc[had_switch, 'index_date'],
                        format='%Y%m%d', errors='coerce'
                    )
                    prepared.loc[had_switch, 'days_to_switch'] = (
                        (switch_dates - index_dates).dt.days
                    ).astype('Int64')
                    n_switched = prepared.loc[is_switch_eligible & prepared['days_to_switch'].notna()].shape[0]
                    logger.debug(f"days_to_switch: {n_switched} T2DM_OHA/NOMED switched")
                except Exception as e:
                    logger.warning(f"days_to_switch 계산 실패: {e}")
                    prepared['days_to_switch'] = pd.NA
        else:
            prepared['had_insulin_switch'] = np.nan
            prepared['days_to_switch'] = pd.NA

        for col in ['age_at_index', 'index_year', 'income_quintile', 'bmi', 'cci_score',
                     'dm_duration_years', 'follow_up_years']:
            if col in prepared.columns:
                prepared[col] = pd.to_numeric(prepared[col], errors='coerce')

        if 'income_quintile' in prepared.columns:
            prepared['income_q'] = prepared['income_quintile'].fillna(prepared['income_quintile'].median())
        if 'age_at_index' in prepared.columns:
            prepared['age_at_index'] = prepared['age_at_index'].fillna(prepared['age_at_index'].median())
        if 'bmi' in prepared.columns:
            prepared['bmi'] = prepared['bmi'].fillna(prepared['bmi'].median())
        if 'cci_score' in prepared.columns:
            prepared['cci_score'] = pd.to_numeric(prepared['cci_score'], errors='coerce').fillna(0)
        if 'dm_duration_years' in prepared.columns:
            # DM인데 NULL인 경우만 0으로 대체, NON_DM은 NaN 유지 (Cox에서 자동 제외)
            is_dm = prepared['exposure_group'] != 'NON_DM'
            prepared.loc[is_dm, 'dm_duration_years'] = prepared.loc[is_dm, 'dm_duration_years'].fillna(0)

        if 'smoking_status' in prepared.columns:
            # NULL은 비흡연(0)이 아니라 결측(NaN)으로 유지 — 비흡연 오분류 방지
            smk = prepared['smoking_status']
            smk_known = smk.isin(['Current', 'Former', 'Never'])
            prepared['smk_current'] = np.where(smk_known, (smk == 'Current').astype('float32'), np.nan).astype('float32')
            prepared['smk_former'] = np.where(smk_known, (smk == 'Former').astype('float32'), np.nan).astype('float32')
            prepared['smk_missing'] = smk.isna().astype('int8')

        if 'drinking_status' in prepared.columns:
            # 음주 변수도 동일 패턴: 결측 지시변수 생성
            drk = prepared['drinking_status']
            drk_known = drk.isin(['Non', 'Mild', 'Moderate', 'Heavy'])
            prepared['drk_heavy'] = np.where(drk_known, (drk == 'Heavy').astype('float32'), np.nan).astype('float32')
            prepared['drk_moderate'] = np.where(drk_known, drk.isin(['Moderate', 'Heavy']).astype('float32'), np.nan).astype('float32')
            prepared['drk_missing'] = drk.isna().astype('int8')

        prepared['follow_up_years'] = pd.to_numeric(prepared['follow_up_years'], errors='coerce')
        comor_cols = [c for c in prepared.columns if c.startswith('comor_') or c.startswith('comp_')]
        for col in comor_cols:
            prepared[col] = pd.to_numeric(prepared[col], errors='coerce').fillna(0).astype('int8')

        prepared = prepared[prepared['follow_up_years'] > 0]
        return prepared

    def run_cox(self, outcome='dementia_event', cb=None, df_prepared=None):
        """3단계 Cox — df_prepared 전달 시 재사용"""
        if df_prepared is None:
            raw, _ = self._load_data(cb=cb)
            df_prepared = self._prepare(raw, cb=cb)

        min_events = int(STUDY_SETTINGS.get('MIN_EVENTS', 10))
        if min_events <= 0:
            raise ValueError(f"MIN_EVENTS 는 양의 정수여야 합니다: {min_events}")
        event_count = int(df_prepared[outcome].sum())
        if event_count < min_events:
            logger.warning("run_cox: 이벤트 수 %d < min_events %d — InsufficientDataError",
                           event_count, min_events)
            raise InsufficientDataError(valid_rows=event_count, min_rows=min_events, kind='events')

        T, E = 'follow_up_years', outcome
        results = {}
        failed_models = {}  # {model_name: {reason_code, reason, stage, ...}}
        exposure = ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']

        # index_year: NON_DM(first_year 기준)과 DM(진단일 기준) 간 calendar time 차이 보정.
        # NON_DM은 2013-01-01부터, DM은 실제 진단연도부터 추적 시작 → 관찰 시작 시점 불일치를
        # 공변량으로 조정하여 time-period confounding을 최소화.
        models = {
            'model1_age_sex': exposure + ['age_at_index', 'male', 'index_year'],
            'model2_socio': exposure + ['age_at_index', 'male', 'index_year', 'income_q',
                             'comor_hypertension', 'comor_dyslipidemia', 'comor_depression'],
            'model3_full': exposure + ['age_at_index', 'male', 'index_year', 'income_q',
                           'comor_hypertension', 'comor_dyslipidemia', 'comor_depression',
                           'comp_retinopathy', 'comp_nephropathy', 'comp_neuropathy',
                           'comor_ischemic_stroke', 'comor_hemorrhagic_stroke',
                           'comor_ihd', 'comor_atrial_fib', 'comor_heart_failure',
                           'comp_hypoglycemia'],
        }
        for mname, mcols in models.items():
            self._assert_no_post_index_covariates(mcols, context=f"run_cox:{mname}")

        # A7: 전체 노출군별 Events/N/Person-years 요약 (모델 공통)
        exposure_group_summary = {}
        for g_name, g_col in [('T1DM', 'is_t1dm'), ('T2DM_OHA', 'is_t2dm_oha'),
                                ('T2DM_INSULIN', 'is_t2dm_insulin'), ('T2DM_NOMED', 'is_t2dm_nomed'),
                                ('NON_DM', None)]:
            if g_col is not None:
                mask = df_prepared.get(g_col, pd.Series(dtype='int8')) == 1 if g_col in df_prepared.columns else pd.Series(False, index=df_prepared.index)
            else:
                exp_cols = [c for c in ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']
                            if c in df_prepared.columns]
                mask = (df_prepared[exp_cols] == 0).all(axis=1) if exp_cols else pd.Series(False, index=df_prepared.index)
            if mask.sum() > 0 and E in df_prepared.columns and T in df_prepared.columns:
                sub = df_prepared.loc[mask]
                exposure_group_summary[g_name] = {
                    'n': int(mask.sum()),
                    'events': int(pd.to_numeric(sub[E], errors='coerce').fillna(0).sum()),
                    'person_years': float(pd.to_numeric(sub[T], errors='coerce').dropna().sum()),
                }
        results['_exposure_group_summary'] = exposure_group_summary

        for mname, mcols in models.items():
            if cb: cb(f"Cox 회귀 ({outcome}) — {mname} 피팅 중...")
            # C3: 0-count 노출군 자동 제외
            zero_exposure = [e for e in exposure if e in df_prepared.columns
                              and df_prepared[e].sum() == 0]
            active_mcols = [c for c in mcols if c not in zero_exposure]
            if zero_exposure:
                logger.warning("Cox %s(%s): 0건 노출군 제외 — %s", mname, outcome, zero_exposure)
                if cb: cb(f"[경고] Cox {mname}: {zero_exposure} 노출군이 0건으로 분석에서 제외됩니다.")

            cols = [c for c in active_mcols if c in df_prepared.columns] + [T, E]
            n_before_drop = len(df_prepared)
            df_model = df_prepared[cols].dropna()
            n_dropped = n_before_drop - len(df_model)
            if n_before_drop > 0 and n_dropped / n_before_drop > 0.10:
                drop_pct = n_dropped / n_before_drop * 100
                drop_msg = (f"Cox {mname}({outcome}): dropna로 {n_dropped:,}건({drop_pct:.1f}%) 제외 "
                            f"— 결측 공변량 확인 필요 (남은 행: {len(df_model):,}건)")
                logger.warning(drop_msg)
                if cb: cb(f"[경고] {drop_msg}")
            try:
                self._check_min_rows(df_model, context=f"run_cox {mname}")
                n_model_events = int(df_model[E].sum())
                n_vars = len([c for c in active_mcols if c in df_model.columns])
                cph = CoxPHFitter()
                cph.fit(df_model, duration_col=T, event_col=E)
                result_entry = {
                    'summary': cph.summary,
                    'concordance': cph.concordance_index_,
                    'n': len(df_model),
                    'events': n_model_events,
                    'person_years': float(df_model[T].sum()),
                }
                # A3: EPV check (Events Per Variable — 권장 ≥ 10)
                epv = n_model_events / n_vars if n_vars > 0 else 0
                result_entry['epv'] = round(epv, 1)
                if epv < 10:
                    epv_msg = (f"EPV={epv:.1f} (이벤트 {n_model_events}건, 변수 {n_vars}개) "
                               f"— EPV < 10 권장기준 미달. {mname} 결과 해석 주의.")
                    logger.warning("Cox %s(%s): %s", mname, outcome, epv_msg)
                    result_entry['epv_warning'] = epv_msg
                    if cb: cb(f"[경고] Cox {mname}: {epv_msg}")
                # PH 가정 검정 (Schoenfeld residuals)
                exposure_ph_violation = []
                try:
                    ph_test = proportional_hazard_test(cph, df_model, time_transform='rank')
                    result_entry['ph_test'] = ph_test.summary
                    _ph_alpha = float(STUDY_SETTINGS.get('PH_ALPHA', 0.05))
                    # Bonferroni 보정: 검정 변수 수로 나눔 (다중검증 1종오류 제어)
                    n_ph_vars = len(ph_test.summary)
                    if STUDY_SETTINGS.get('PH_BONFERRONI', True) and n_ph_vars > 1:
                        _ph_alpha = _ph_alpha / n_ph_vars
                    violated = ph_test.summary[ph_test.summary['p'] < _ph_alpha]
                    if not violated.empty:
                        violated_vars = violated.index.tolist()
                        exposure_ph_violation = [v for v in violated_vars if v in exposure]
                        non_exp = [v for v in violated_vars if v not in exposure]
                        if non_exp:
                            logger.warning(f"Cox {mname}: PH 가정 위반 변수(공변량) — "
                                         f"{', '.join(non_exp)}"
                                         f" (Bonferroni α={_ph_alpha:.4f})")
                except Exception as ph_e:
                    logger.info(f"PH 검정 생략 ({mname}): {ph_e}")

                # I11: 노출변수 PH 위반 → 해당 모델만 스킵 (다른 모델은 유지)
                if exposure_ph_violation:
                    reason = (
                        "노출변수 PH 가정 위반 — "
                        f"{', '.join(exposure_ph_violation)}. "
                        "해당 모델 결과 제외. 층화 Cox 또는 시간-변환 공변량을 검토하세요."
                    )
                    logger.warning(
                        f"Cox {mname}({outcome}): 노출변수 PH 가정 위반 — "
                        f"{', '.join(exposure_ph_violation)}. "
                        f"해당 모델 결과 제외. 층화 Cox 또는 시간-변환 공변량을 검토하세요."
                    )
                    failed_models[mname] = self._model_failure(
                        self._RC_PH_VIOLATION,
                        reason,
                        model=mname,
                        outcome=outcome,
                        violated_variables=exposure_ph_violation,
                    )
                    continue
                results[mname] = result_entry
            except RuntimeError:
                raise  # CoxPHFitter 내부 수렴 실패 등 복구 불가 오류 — 상위 _safe_run이 처리
            except InsufficientDataError as e:
                logger.warning(f"Cox {mname} 데이터 부족 — 스킵: {e}")
                failed_models[mname] = self._model_failure(
                    self._RC_INSUFFICIENT_DATA,
                    f"데이터 부족: {e}",
                    model=mname,
                    outcome=outcome,
                )
            except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
                logger.exception(f"분석 오류 (Cox {mname})")
                logger.warning(f"Cox {mname} 실패: {e}")
                failed_models[mname] = self._model_failure(
                    self._RC_COX_MODEL_FAILED,
                    str(e),
                    model=mname,
                    outcome=outcome,
                    exception_type=type(e).__name__,
                )
            except Exception as e:
                logger.exception(f"예기치 않은 오류 (Cox {mname})")
                logger.warning(f"Cox {mname} 실패: {e}")
                failed_models[mname] = self._model_failure(
                    self._RC_COX_MODEL_FAILED,
                    str(e),
                    model=mname,
                    outcome=outcome,
                    exception_type=type(e).__name__,
                )
            finally:
                del df_model
                gc.collect()

        # 전체 모델 실패 감지 — 메타데이터 키(_로 시작)는 제외하여 실제 모델 결과만 확인
        model_results = {k: v for k, v in results.items()
                         if not k.startswith('_') and k != 'failed_models'}
        if not model_results:
            err = RuntimeError(
                f"{self._RC_ALL_COX_MODELS_FAILED}: Cox 회귀 분석({outcome}) 실패: "
                f"모든 모델 피팅에 실패했습니다. "
                f"데이터 크기나 공변량 구성을 확인하세요."
            )
            err.reason_code = self._RC_ALL_COX_MODELS_FAILED
            err.failed_models = failed_models
            raise err

        # 부분 실패 요약 로깅
        if failed_models:
            failure_preview = {
                k: (v.get('reason', '') if isinstance(v, dict) else str(v))[:80]
                for k, v in failed_models.items()
            }
            logger.warning(
                "Cox(%s) 부분 실패 — 성공 %d개, 실패 %d개: %s",
                outcome, len(model_results), len(failed_models),
                failure_preview
            )
            results['failed_models'] = failed_models

        # PH 검정 요약을 모델별로 취합하여 최상위에 저장
        # failed_models 등 메타데이터 키는 dict가 아닐 수 있으므로 isinstance 가드
        ph_combined = {}
        for mname, entry in results.items():
            if isinstance(entry, dict) and 'ph_test' in entry:
                ph_combined[mname] = entry['ph_test']
        if ph_combined:
            results['ph_test_summary'] = ph_combined

        self.results[f'cox_{outcome}'] = results
        return results

    def run_psm(self, cb=None, df_prepared=None):
        """PSM: T1DM vs T2DM, 1:N 매칭 — matched_df 저장 안 함 (GPU 가속 지원)"""
        if cb: cb("PSM 실행 중...")
        from gpu_accelerator import get_logistic_regression, get_nearest_neighbors, is_gpu_enabled

        if df_prepared is None:
            raw, _ = self._load_data(cb=cb)
            df_prepared = self._prepare(raw, cb=cb)

        # ★ 필요 컬럼만 추출 (전체 복사 방지)
        need_cols = ['exposure_group', 'is_t1dm', 'age_at_index', 'male', 'income_q',
                     'comor_hypertension', 'comor_dyslipidemia', 'dm_duration_years',
                     'follow_up_years', 'dementia_event', 'ad_event', 'vad_event']
        need_cols = [c for c in need_cols if c in df_prepared.columns]
        df_dm = df_prepared.loc[
            df_prepared['exposure_group'].isin(['T1DM', 'T2DM_OHA', 'T2DM_INSULIN', 'T2DM_NOMED']),
            need_cols
        ].copy()
        df_dm['is_t1dm'] = (df_dm['exposure_group'] == 'T1DM').astype('int8')

        # index_year: T1DM vs T2DM의 진단 연도(calendar time) 매칭 — 연도별 DM 유형 분포 차이 보정
        ps_vars = ['age_at_index', 'male', 'index_year', 'income_q', 'comor_hypertension',
                    'comor_dyslipidemia', 'dm_duration_years']
        self._assert_no_post_index_covariates(ps_vars, context='run_psm')
        ps_vars = [c for c in ps_vars if c in df_dm.columns]
        df_ps = df_dm[ps_vars + ['is_t1dm']].dropna()

        try:
            self._check_min_rows(df_ps, context="run_psm")
        except InsufficientDataError as e:
            msg = f"PSM 스킵: {format_error_for_user(e)}"
            if cb: cb(msg)
            self.results['psm'] = self._skip_result(self._RC_INSUFFICIENT_DATA, msg, stage='psm')
            return self.results['psm']

        # PSM 실행 가능 여부 검증: T1DM과 non-T1DM 모두 존재해야 함
        n_treated = (df_ps['is_t1dm'] == 1).sum()
        n_control = (df_ps['is_t1dm'] == 0).sum()
        if n_treated < 2 or n_control < 2:
            msg = (f"PSM 스킵: T1DM={n_treated}명, non-T1DM={n_control}명 "
                   f"— 로지스틱 회귀를 위해 각 그룹 최소 2명 이상 필요")
            logger.warning(msg)
            if cb: cb(msg)
            self.results['psm'] = self._skip_result(self._RC_INSUFFICIENT_GROUPS, msg, stage='psm')
            return self.results['psm']

        lr = get_logistic_regression(max_iter=1000, random_state=42)
        if is_gpu_enabled() and cb:
            cb("PSM: GPU 가속 로지스틱 회귀 실행 중...")
        lr.fit(df_ps[ps_vars], df_ps['is_t1dm'])
        df_ps = df_ps.copy()
        df_ps['ps'] = lr.predict_proba(df_ps[ps_vars])[:, 1]
        # PS를 클리핑하여 log(0) / log(inf) 방지
        df_ps['ps'] = np.clip(df_ps['ps'], 1e-6, 1 - 1e-6)
        del lr; gc.collect()

        ratio = STUDY_SETTINGS.get('PSM_RATIO', 3)
        treated = df_ps[df_ps['is_t1dm'] == 1]
        control = df_ps[df_ps['is_t1dm'] == 0]
        lps_t = np.log(treated['ps'] / (1 - treated['ps']))
        lps_c = np.log(control['ps'] / (1 - control['ps']))
        # caliper: 0.2 × pooled SD of logit(PS) — Austin (2011) 표본크기 가중 공식
        # pooled_SD = sqrt(((n_t-1)*var_t + (n_c-1)*var_c) / (n_t+n_c-2))
        n_t, n_c = len(lps_t), len(lps_c)
        pooled_sd = np.sqrt(
            ((n_t - 1) * lps_t.var() + (n_c - 1) * lps_c.var()) / (n_t + n_c - 2)
        ) if (n_t + n_c - 2) > 0 else 0.0
        if pooled_sd == 0 or np.isnan(pooled_sd):
            msg = ("PSM 스킵: pooled_sd = 0 또는 NaN — caliper 가 무효화되어 모든 매칭 거부됩니다 "
                   "(treated/control logit(PS) 분산 부족, 데이터 다양성 확인 필요)")
            logger.warning(msg)
            if cb: cb(msg)
            self.results['psm'] = self._skip_result(self._RC_INVALID_PSM_CALIPER, msg, stage='psm')
            return self.results['psm']
        caliper = float(STUDY_SETTINGS.get('PSM_CALIPER', 0.2)) * pooled_sd

        if len(control) < 1:
            msg = f"PSM 스킵: control 수({len(control)})가 0이라 매칭 불가"
            logger.warning(msg)
            if cb: cb(msg)
            self.results['psm'] = self._skip_result(self._RC_INSUFFICIENT_GROUPS, msg, stage='psm')
            return self.results['psm']

        # ratio보다 많은 후보를 탐색해 used_controls 소진으로 인한 누락 매칭 방지
        # ratio * 5 또는 control 전체 중 작은 값으로 후보 풀 확대
        search_k = min(len(control), max(ratio * 5, ratio + 10))
        nn = get_nearest_neighbors(n_neighbors=search_k, metric='euclidean')
        nn.fit(lps_c.values.reshape(-1, 1))
        dists, idxs = nn.kneighbors(lps_t.values.reshape(-1, 1))
        del nn; gc.collect()

        # 1:N 매칭: control은 한 번만 사용(재사용 방지), treated는 적어도 1개 매칭돼야 포함
        used_controls = set()
        mt_list = []      # matched treated indices
        mc_list = []      # matched control indices (순서 보존, 중복 없음)

        for i, (ds, js) in enumerate(zip(dists, idxs)):
            assigned = []
            for d, j in zip(ds, js):
                if len(assigned) >= ratio:   # 설정된 1:N 비율 상한 준수
                    break
                ctrl_idx = control.index[j]
                if d <= caliper and ctrl_idx not in used_controls:
                    assigned.append(ctrl_idx)
                    used_controls.add(ctrl_idx)
            if assigned:
                mt_list.append(treated.index[i])
                mc_list.extend(assigned)

        if not mt_list or not mc_list:
            logger.warning("PSM: caliper 내 매칭 쌍 없음 → PSM 스킵")
            self.results['psm'] = self._skip_result(
                self._RC_NO_PSM_MATCHES,
                'caliper 내 매칭 쌍 없음',
                stage='psm',
            )
            if cb: cb("PSM 스킵: caliper 내 매칭 가능한 쌍이 없습니다.")
            return self.results.get('psm', {})

        matched = pd.concat([df_dm.loc[mt_list], df_dm.loc[mc_list]])

        # PSM 매칭 후 크기 검증
        min_valid = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
        n_treated_original = len(treated)
        if len(mt_list) < min_valid:
            match_warn = (f"PSM 매칭 결과 T1DM {len(mt_list)}명 — "
                          f"최소 기준({min_valid}명) 미달. PSM Cox 결과 해석 주의.")
            logger.warning(match_warn)
            if cb: cb(f"[경고] {match_warn}")
        elif n_treated_original > 0 and len(mt_list) / n_treated_original < 0.5:
            # 원본 T1DM 대비 50% 미만 매칭 → caliper가 너무 좁을 수 있음
            match_rate = len(mt_list) / n_treated_original * 100
            match_warn = (f"PSM T1DM 매칭률 {match_rate:.1f}% ({len(mt_list):,}/{n_treated_original:,}명) "
                          f"— PSM_CALIPER 또는 매칭 기준 조정 고려.")
            logger.warning(match_warn)
            if cb: cb(f"[경고] {match_warn}")

        # A2: PSM 전 Balance (Love plot용)
        balance_before = {}
        for col in ps_vars:
            if col not in df_ps.columns:
                continue
            tm_b = df_ps.loc[df_ps['is_t1dm'] == 1, col].mean()
            cm_b = df_ps.loc[df_ps['is_t1dm'] == 0, col].mean()
            ts_b = df_ps.loc[df_ps['is_t1dm'] == 1, col].std()
            cs_b = df_ps.loc[df_ps['is_t1dm'] == 0, col].std()
            ps2_b = np.sqrt((ts_b**2 + cs_b**2) / 2)
            smd_b = abs(tm_b - cm_b) / (ps2_b if ps2_b > 0 else 1)
            balance_before[col] = {'treated_mean': round(tm_b, 4), 'control_mean': round(cm_b, 4),
                                    'smd': round(smd_b, 4)}

        # PSM 후 Balance
        balance = {}
        for col in ps_vars:
            tm = matched.loc[matched['is_t1dm'] == 1, col].mean()
            cm = matched.loc[matched['is_t1dm'] == 0, col].mean()
            ts = matched.loc[matched['is_t1dm'] == 1, col].std()
            cs = matched.loc[matched['is_t1dm'] == 0, col].std()
            ps2 = np.sqrt((ts**2 + cs**2) / 2)
            smd = abs(tm - cm) / (ps2 if ps2 > 0 else 1)
            balance[col] = {'treated_mean': round(tm, 4), 'control_mean': round(cm, 4),
                           'smd': round(smd, 4), 'balanced': smd < float(STUDY_SETTINGS.get('PSM_SMD_THRESHOLD', 0.1))}

        # PSM 후 Cox
        psm_cox = {}
        for oc in ['dementia_event', 'ad_event', 'vad_event']:
            if oc in matched.columns:
                d2 = matched[['is_t1dm', 'follow_up_years', oc]].dropna()
                d2 = d2[d2['follow_up_years'] > 0]
                if len(d2) > 0 and d2[oc].sum() > 0:
                    try:
                        c = CoxPHFitter()
                        c.fit(d2, duration_col='follow_up_years', event_col=oc)
                        psm_cox[oc] = {'summary': c.summary}
                    except InsufficientDataError as e:
                        logger.warning("PSM Cox (%s) 데이터 부족 스킵: %s", oc, e)
                    except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
                        logger.warning("PSM Cox (%s) 분석 실패: %s", oc, e)
                    except Exception as e:
                        logger.exception("PSM Cox (%s) 예기치 않은 오류", oc)
                        logger.warning("PSM Cox (%s) 실패: %s", oc, e)

        self.results['psm'] = {
            'n_treated': len(mt_list), 'n_control': len(mc_list),
            'balance_before': balance_before,  # A2: Love plot용 PSM 전 SMD
            'balance': balance,
            'cox_results': psm_cox,
            # ★ matched_df 저장하지 않음 → 메모리 대폭 절약
        }

        # ★ 중간 객체 모두 삭제
        del matched, df_dm, df_ps, treated, control, dists, idxs, mt_list, mc_list
        gc.collect()

        return self.results['psm']

    def run_interaction(self, cb=None, df_prepared=None):
        if cb: cb("상호작용 분석 중...")
        if df_prepared is None:
            raw, _ = self._load_data(cb=cb)
            df_prepared = self._prepare(raw, cb=cb)

        df_dm = df_prepared[df_prepared['exposure_group'] != 'NON_DM']
        if 'dm_duration_cat' not in df_dm.columns:
            if cb: cb("상호작용 분석 스킵: dm_duration_cat 컬럼 없음")
            self.results['interaction'] = self._skip_result(
                self._RC_MISSING_REQUIRED_COLUMN,
                'dm_duration_cat 컬럼 없음',
                stage='interaction',
                missing_column='dm_duration_cat',
            )
            return None

        # ★ 필요 컬럼만 복사
        cols_need = ['is_t1dm', 'dm_duration_cat', 'age_at_index', 'male',
                     'income_q', 'cci_score', 'follow_up_years', 'dementia_event']
        cols_need = [c for c in cols_need if c in df_dm.columns]
        d = df_dm[cols_need].copy()

        d['dur510'] = (d['dm_duration_cat'] == '5-10yr').astype('int8')
        d['dur10p'] = (d['dm_duration_cat'] == '>=10yr').astype('int8')
        d['t1_x_510'] = d['is_t1dm'] * d['dur510']
        d['t1_x_10p'] = d['is_t1dm'] * d['dur10p']
        d = d.drop(columns=['dm_duration_cat'])
        d = d.dropna()
        d = d[d['follow_up_years'] > 0]

        _min_rows = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
        _min_events = int(STUDY_SETTINGS.get('MIN_EVENTS', 10))
        if len(d) < _min_rows or int(d['dementia_event'].sum()) < _min_events:
            logger.warning(
                "run_interaction: 데이터 부족 — 행 수 %d (최소 %d), 이벤트 수 %d (최소 %d) — 분석 스킵",
                len(d), _min_rows, int(d['dementia_event'].sum()), _min_events,
            )
            reason = (f"데이터 부족 ({len(d)}행/{int(d['dementia_event'].sum())}이벤트, "
                      f"최소 {_min_rows}행/{_min_events}이벤트 필요)")
            if cb: cb(f"상호작용 분석 스킵: {reason}")
            self.results['interaction'] = self._skip_result(
                self._RC_INSUFFICIENT_DATA,
                reason,
                stage='interaction',
                valid_rows=len(d),
                events=int(d['dementia_event'].sum()),
                min_rows=_min_rows,
                min_events=_min_events,
            )
            return None

        try:
            cph = CoxPHFitter()
            cph.fit(d, duration_col='follow_up_years', event_col='dementia_event')
            self.results['interaction'] = {'summary': cph.summary}
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
            logger.exception("분석 오류 (run_interaction)")
            logger.warning(f"상호작용 분석 실패: {e}")
            self.results['interaction'] = self._skip_result(
                self._RC_ANALYSIS_ERROR,
                str(e),
                stage='interaction',
                exception_type=type(e).__name__,
            )
        except Exception as e:
            logger.exception("예기치 않은 오류 (run_interaction)")
            logger.warning(f"상호작용 분석 실패: {e}")
            self.results['interaction'] = self._skip_result(
                self._RC_ANALYSIS_ERROR,
                str(e),
                stage='interaction',
                exception_type=type(e).__name__,
            )
        finally:
            del d; gc.collect()

        return self.results.get('interaction')

    def run_subgroup(self, cb=None, df_prepared=None):
        if cb: cb("하위그룹 분석 중...")
        if df_prepared is None:
            raw, _ = self._load_data(cb=cb)
            df_prepared = self._prepare(raw, cb=cb)
        df = df_prepared  # 참조만 (copy 안 함)

        subgroups = {
            'sex_male': df['male'] == 1,
            'sex_female': df['male'] == 0,
        }
        if 'age_group' in df.columns:
            subgroups['age_40_54'] = df['age_group'] == '40-54'
            subgroups['age_55_64'] = df['age_group'] == '55-64'
        else:
            logger.warning("age_group 컬럼 없음 — 연령 서브그룹 분석 생략")
        if 'income_q' in df.columns:
            med = df['income_q'].median()
            subgroups['income_low'] = df['income_q'] <= med
            subgroups['income_high'] = df['income_q'] > med
        if 'bmi' in df.columns:
            subgroups['bmi_normal'] = df['bmi'] < 25
            subgroups['bmi_obese'] = df['bmi'] >= 25
        if 'cci_score' in df.columns:
            subgroups['cci_low'] = df['cci_score'] <= 2
            subgroups['cci_high'] = df['cci_score'] > 2

        comp_cols = [c for c in df.columns if c.startswith('comp_') and c != 'comp_hypoglycemia']
        if comp_cols:
            any_comp = df[comp_cols].max(axis=1)
            subgroups['dm_comp_yes'] = any_comp == 1
            subgroups['dm_comp_no'] = any_comp == 0

        if 'comp_hypoglycemia' in df.columns:
            subgroups['hypo_yes'] = df['comp_hypoglycemia'] == 1
            subgroups['hypo_no'] = df['comp_hypoglycemia'] == 0

        cvd_cols = [c for c in df.columns if c in ['comor_ischemic_stroke', 'comor_hemorrhagic_stroke',
                                                     'comor_ihd', 'comor_atrial_fib', 'comor_heart_failure']]
        if cvd_cols:
            any_cvd = df[cvd_cols].max(axis=1)
            subgroups['cvd_yes'] = any_cvd == 1
            subgroups['cvd_no'] = any_cvd == 0

        # Phase 2: T2DM_OHA 약물전환 서브그룹 (had_insulin_switch 기반)
        if 'had_insulin_switch' in df.columns:
            is_t2dm_oha = df['exposure_group'] == 'T2DM_OHA'
            n_t2dm_oha = is_t2dm_oha.sum()
            if n_t2dm_oha >= int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30)):
                # T2DM_OHA만 포함하는 서브그룹
                subgroups['t2dm_oha_noswitch'] = is_t2dm_oha & (df['had_insulin_switch'] == 0)
                subgroups['t2dm_oha_switch'] = is_t2dm_oha & (df['had_insulin_switch'] == 1)
                logger.debug(f"Phase 2 T2DM_OHA 약물전환: "
                           f"미전환={subgroups['t2dm_oha_noswitch'].sum()}, "
                           f"전환={subgroups['t2dm_oha_switch'].sum()}")

        # A4: 상호작용 p-value 계산을 위한 지시 변수 정의
        # (각 이분형 하위그룹 변수에 대해 LRT 기반 interaction p-value 계산)
        exposure_cols = ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']
        sg_base_covars = ['age_at_index', 'male', 'cci_score']
        # (sg_name → parent_var) 역매핑 및 지시 변수 컬럼명 정의
        _sg_parent: dict[str, str] = {}
        _sg_indicators: dict[str, str] = {}  # parent_var → indicator col in df

        if 'male' in df.columns:
            _sg_parent.update({'sex_male': 'sex', 'sex_female': 'sex'})
            _sg_indicators['sex'] = 'male'
        if 'age_group' in df.columns:
            _sg_parent.update({'age_40_54': 'age_group', 'age_55_64': 'age_group'})
        if 'income_q' in df.columns:
            _sg_parent.update({'income_low': 'income', 'income_high': 'income'})
        if 'bmi' in df.columns:
            _sg_parent.update({'bmi_normal': 'bmi', 'bmi_obese': 'bmi'})
        if 'cci_score' in df.columns:
            _sg_parent.update({'cci_low': 'cci', 'cci_high': 'cci'})
        if comp_cols:
            _sg_parent.update({'dm_comp_yes': 'dm_comp', 'dm_comp_no': 'dm_comp'})
        if 'comp_hypoglycemia' in df.columns:
            _sg_parent.update({'hypo_yes': 'hypo', 'hypo_no': 'hypo'})
            _sg_indicators['hypo'] = 'comp_hypoglycemia'
        if cvd_cols:
            _sg_parent.update({'cvd_yes': 'cvd', 'cvd_no': 'cvd'})

        # Phase 2: T2DM_OHA 약물전환 상호작용
        if 't2dm_oha_switch' in subgroups:
            _sg_parent.update({'t2dm_oha_noswitch': 'med_switch', 't2dm_oha_switch': 'med_switch'})
            _sg_indicators['med_switch'] = 'had_insulin_switch'

        model_cols_base = ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed',
                           'age_at_index', 'male', 'cci_score',
                           'follow_up_years', 'dementia_event']

        sg_results = {}
        _min_sg_rows = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
        _min_sg_events = int(STUDY_SETTINGS.get('MIN_SUBGROUP_EVENTS', 5))
        for name, mask in subgroups.items():
            try:
                # ★ copy 대신 필요 컬럼만 선택하여 새 df 생성
                cols = [c for c in model_cols_base if c in df.columns]
                if 'sex' in name and 'male' in cols:
                    cols.remove('male')

                dm = df.loc[mask, cols].dropna()
                dm = dm[dm['follow_up_years'] > 0]

                if len(dm) < _min_sg_rows or dm['dementia_event'].sum() < _min_sg_events:
                    continue

                cph = CoxPHFitter()
                cph.fit(dm, duration_col='follow_up_years', event_col='dementia_event')

                hr_data = {}
                for v in ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']:
                    if v in cph.summary.index:
                        hr_data[v] = {
                            'hr': round(cph.summary.loc[v, 'exp(coef)'], 4),
                            'ci_lower': round(cph.summary.loc[v, 'exp(coef) lower 95%'], 4),
                            'ci_upper': round(cph.summary.loc[v, 'exp(coef) upper 95%'], 4),
                            'p_value': round(cph.summary.loc[v, 'p'], 6),
                        }
                sg_results[name] = {'n': len(dm), 'events': int(dm['dementia_event'].sum()),
                                     'hr_data': hr_data, 'summary': cph.summary}
            except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
                logger.exception(f"분석 오류 (하위그룹 {name})")
                logger.warning(f"하위그룹 {name} 실패: {e}")
            except Exception as e:
                logger.exception(f"예기치 않은 오류 (하위그룹 {name})")
                logger.warning(f"하위그룹 {name} 실패: {e}")
            finally:
                gc.collect()

        # A4: 상호작용 p-value (LRT) 계산 — 하위그룹 변수별
        # 지시 변수가 없는 경우 임시 컬럼을 df 복사본에 추가
        cols_int = model_cols_base + list(_sg_indicators.values())
        # Phase 2: had_insulin_switch는 post-index 변수이므로 LRT interaction 검정에만 사용한다.
        # HR 효과 추정용 정적 Cox/PSM 공변량으로 해석하거나 재사용하면 안 된다.
        if 'had_insulin_switch' in df.columns:
            cols_int.append('had_insulin_switch')
        df_int = df[[c for c in cols_int if c in df.columns]].copy()

        # 임시 지시 변수 추가
        if 'age_group' in df.columns:
            df_int['_age_4054'] = (df['age_group'] == '40-54').astype(float)
            _sg_indicators['age_group'] = '_age_4054'
        if 'income_q' in df.columns:
            df_int['_income_low'] = (df['income_q'] <= df['income_q'].median()).astype(float)
            _sg_indicators['income'] = '_income_low'
        if 'bmi' in df.columns:
            df_int['_bmi_normal'] = (df['bmi'] < 25).astype(float)
            _sg_indicators['bmi'] = '_bmi_normal'
        if 'cci_score' in df.columns:
            df_int['_cci_low'] = (df['cci_score'] <= 2).astype(float)
            _sg_indicators['cci'] = '_cci_low'
        if comp_cols:
            df_int['_dm_comp'] = df[comp_cols].max(axis=1).astype(float)
            _sg_indicators['dm_comp'] = '_dm_comp'
        if cvd_cols:
            df_int['_cvd'] = df[cvd_cols].max(axis=1).astype(float)
            _sg_indicators['cvd'] = '_cvd'

        # 고유 parent 변수별 1회 계산
        interaction_pvals: dict[str, float] = {}
        unique_parents = {pv for pv in _sg_parent.values() if pv in _sg_indicators}
        for parent_var in unique_parents:
            ind_col = _sg_indicators[parent_var]
            if ind_col not in df_int.columns:
                continue
            exp_in_df = [c for c in exposure_cols if c in df_int.columns]
            if not exp_in_df:
                continue
            base_covars = [c for c in sg_base_covars if c in df_int.columns]
            p_int = self._compute_interaction_pval(
                df_int, exp_in_df, ind_col, base_covars
            )
            interaction_pvals[parent_var] = p_int

        # Bonferroni 보정: 유효한 p_interaction 수로 보정
        n_valid_tests = sum(1 for p in interaction_pvals.values() if not np.isnan(p))
        bonf_n = max(n_valid_tests, 1)

        # 각 하위그룹 결과에 p_interaction 및 보정값 기입
        for sg_name in list(sg_results.keys()):
            parent = _sg_parent.get(sg_name)
            if parent and parent in interaction_pvals:
                p_int = interaction_pvals[parent]
                sg_results[sg_name]['p_interaction'] = float(p_int) if not np.isnan(p_int) else None
                if not np.isnan(p_int):
                    p_int_bonf = min(float(p_int) * bonf_n, 1.0)
                    sg_results[sg_name]['p_interaction_bonferroni'] = p_int_bonf
                else:
                    sg_results[sg_name]['p_interaction_bonferroni'] = None

        sg_results['_interaction_bonferroni_n'] = bonf_n
        del df_int
        gc.collect()

        self.results['subgroup'] = sg_results
        return sg_results

    @staticmethod
    def _compute_cif(times, event_type):
        """Aalen-Johansen 누적발생률(CIF) 추정
        Args:
            times: 관찰 시간 배열
            event_type: 0=censored, 1=관심사건, 2=경쟁위험
        Returns:
            (unique_event_times, cif1, cif2)
        """
        # stable sort: 동일 시점 tie 발생 시 원래 순서 유지 → 재현성 보장
        order = np.argsort(times, kind='stable')
        t_sorted = times[order]
        e_sorted = event_type[order]
        n = len(times)

        unique_event_times = np.unique(t_sorted[e_sorted > 0])
        cif1_list, cif2_list = [], []
        surv = 1.0
        cum_inc1 = 0.0
        cum_inc2 = 0.0

        ptr = 0
        for ut in unique_event_times:
            # ptr은 한 방향으로만 전진 → 전체 O(n+T) 복잡도
            # '<' 조건: t==ut인 피험자(사건·검열 모두)는 at-risk에 포함
            # (검열 대상은 해당 시점 이후 제거 — 표준 Aalen-Johansen 관례)
            while ptr < n and t_sorted[ptr] < ut:
                ptr += 1
            at_risk = n - ptr
            d1 = int(np.sum((t_sorted == ut) & (e_sorted == 1)))
            d2 = int(np.sum((t_sorted == ut) & (e_sorted == 2)))
            if at_risk > 0:
                cum_inc1 += surv * d1 / at_risk
                cum_inc2 += surv * d2 / at_risk
                surv *= (1 - (d1 + d2) / at_risk)
            cif1_list.append(cum_inc1)
            cif2_list.append(cum_inc2)

        return unique_event_times, np.array(cif1_list), np.array(cif2_list)

    def _prepare_cr_data(
        self,
        df_prepared: 'pd.DataFrame',
        outcome: str,
        min_rows: int = 30,
    ) -> 'tuple[pd.DataFrame, np.ndarray] | None':
        """경쟁위험 분석용 df_cr 및 event_type 배열을 준비한다.

        run_competing_risks() 와 run_cross_validation() 에서 공유 사용.

        Returns:
            (df_cr, event_type) 또는 데이터 부족·컬럼 누락 시 None
        """
        T = 'follow_up_years'
        if outcome not in df_prepared.columns:
            return None
        if 'competing_death_event' not in df_prepared.columns:
            return None

        need_cols = [T, outcome, 'competing_death_event', 'dementia_event',
                     'is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin',
                     'is_t2dm_nomed', 'age_at_index', 'male']
        need_cols = list(dict.fromkeys(c for c in need_cols if c in df_prepared.columns))
        df_cr = df_prepared[need_cols].dropna().copy()
        df_cr = df_cr[df_cr[T] > 0]

        if len(df_cr) < min_rows:
            return None

        event_type = np.zeros(len(df_cr), dtype=int)
        event_type[df_cr[outcome].values == 1] = 1
        competing_mask = df_cr['competing_death_event'].values == 1
        if outcome in ('ad_event', 'vad_event') and 'dementia_event' in df_cr.columns:
            other_dementia = ((df_cr['dementia_event'].values == 1) &
                              (df_cr[outcome].values == 0))
            competing_mask = competing_mask | other_dementia
        event_type[(df_cr[outcome].values == 0) & competing_mask] = 2

        return df_cr, event_type

    def run_competing_risks(self, cb=None, df_prepared=None):
        """경쟁위험 분석: Aalen-Johansen CIF + IPCW Fine-Gray 근사

        사망/탈퇴를 경쟁위험으로 처리.

        [방법론 주의사항]
        - CIF: Aalen-Johansen 추정기로 구현. 동률(ties) 처리는 표준 방식 적용.
        - Fine-Gray: lifelines CoxPHFitter + IPCW 가중치 기반 근사 구현.
          G(t)는 역KM 추정(검열=사건, 실제 사건=검열). 경쟁위험 대상자의 추적시간을
          max_time으로 연장하고 G(t_j)/G(t_max) 가중치를 적용하는 고정 가중치 방식.
        - 이 구현은 원 Fine-Gray (1999) 모형의 시간변동 가중치를 고정 가중치로
          근사하므로, 논문 제출 전 R cmprsk::crr()와의 교차 검증을 권장합니다.
        - 전체 SHR(subdistribution HR) 결과에 근사 방법론 고지가 포함됩니다.
        """
        if cb: cb("경쟁위험 분석 (Fine-Gray) 실행 중...")
        from gpu_accelerator import is_gpu_enabled, compute_cif_gpu
        use_gpu_cif = is_gpu_enabled()
        if use_gpu_cif and cb:
            cb("경쟁위험 분석: GPU 가속 CIF 계산 활성화")
        if df_prepared is None:
            raw, _ = self._load_data(cb=cb)
            df_prepared = self._prepare(raw, cb=cb)

        if 'competing_death_event' not in df_prepared.columns:
            logger.warning("competing_death_event 컬럼 없음 — 코호트 재구축 필요")
            self.results['competing_risks'] = self._skip_result(
                self._RC_MISSING_REQUIRED_COLUMN,
                'competing_death_event 컬럼 없음. 코호트를 재구축하세요.',
                stage='competing_risks',
                implemented=False,
                missing_column='competing_death_event',
            )
            return self.results['competing_risks']

        T = 'follow_up_years'
        results = {}
        _min_cr = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))
        _min_cr_events = int(STUDY_SETTINGS.get('MIN_SUBGROUP_EVENTS', 5))

        for outcome in ['dementia_event', 'ad_event', 'vad_event']:
            if outcome not in df_prepared.columns:
                continue
            if cb: cb(f"경쟁위험 분석: {outcome} 처리 중...")

            cr_data = self._prepare_cr_data(df_prepared, outcome, min_rows=_min_cr)
            if cr_data is None:
                if cb: cb(f"경쟁위험 분석: {outcome} 스킵 (데이터 부족 또는 컬럼 누락)")
                continue
            df_cr, event_type = cr_data

            # --- 2) 노출군별 CIF 추정 ---
            cif_by_group = {}
            for group_col, group_name in [('is_t1dm', 'T1DM'),
                                           ('is_t2dm_oha', 'T2DM_OHA'),
                                           ('is_t2dm_insulin', 'T2DM_INSULIN'),
                                           ('is_t2dm_nomed', 'T2DM_NOMED')]:
                if group_col not in df_cr.columns:
                    continue
                mask = df_cr[group_col].values == 1
                if mask.sum() < _min_cr or (event_type[mask] == 1).sum() < _min_cr_events:
                    continue
                times_g = df_cr.loc[mask, T].values.astype(float)
                events_g = event_type[mask]
                if use_gpu_cif:
                    ut, c1, c2 = compute_cif_gpu(times_g, events_g)
                else:
                    ut, c1, c2 = self._compute_cif(times_g, events_g)
                cif_by_group[group_name] = {
                    'times': ut.tolist(), 'cif_event': c1.tolist(),
                    'cif_competing': c2.tolist()
                }

            # NON_DM CIF
            _exposure_cols = ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']
            if not all(c in df_cr.columns for c in _exposure_cols):
                logger.warning("run_competing_risks: NON_DM CIF 계산 스킵 — 노출군 컬럼 누락")
                non_dm_mask = pd.Series(False, index=df_cr.index)
            else:
                non_dm_mask = (
                    (df_cr['is_t1dm'] == 0) &
                    (df_cr['is_t2dm_oha'] == 0) &
                    (df_cr['is_t2dm_insulin'] == 0) &
                    (df_cr['is_t2dm_nomed'] == 0)
                )
            if (non_dm_mask.sum() >= _min_cr and
                    (event_type[non_dm_mask.values] == 1).sum() >= _min_cr_events):
                times_g = df_cr.loc[non_dm_mask, T].values.astype(float)
                events_g = event_type[non_dm_mask.values]
                if use_gpu_cif:
                    ut, c1, c2 = compute_cif_gpu(times_g, events_g)
                else:
                    ut, c1, c2 = self._compute_cif(times_g, events_g)
                cif_by_group['NON_DM'] = {
                    'times': ut.tolist(), 'cif_event': c1.tolist(),
                    'cif_competing': c2.tolist()
                }

            # --- 3) IPCW Fine-Gray 근사 ---
            # 경쟁위험 대상자를 risk set에 유지 (시간 연장 + 가중치 축소)
            fg_summary = None
            try:
                max_time = df_cr[T].max()
                is_competing = event_type == 2

                # 검열 생존함수 G(t) 추정: "검열"이 사건인 KM
                censored_indicator = (event_type == 0).astype(int)
                kmf_g = KaplanMeierFitter()
                kmf_g.fit(df_cr[T].values, event_observed=censored_indicator)

                df_fg = df_cr.copy()
                df_fg['_weight'] = 1.0

                if is_competing.sum() > 0:
                    comp_times = df_cr.loc[is_competing, T].values
                    # 벡터화: O(n) 루프 → 단일 predict 호출로 성능 개선
                    g_at_event = kmf_g.predict(comp_times).values
                    g_at_max = float(kmf_g.predict(max_time).iloc[0])
                    # Fine-Gray (1999) IPCW: 경쟁위험 대상자 가중치 = G(t_j) / G(t_max)
                    # G는 감소함수이므로 t_j <= t_max → G(t_j) >= G(t_max) → 가중치 >= 1
                    weights = np.maximum(g_at_event, 1e-10) / np.maximum(g_at_max, 1e-10)
                    weights = np.clip(weights, 1.0, 100.0)

                    df_fg.loc[df_fg.index[is_competing], T] = max_time
                    df_fg.loc[df_fg.index[is_competing], outcome] = 0
                    df_fg.loc[df_fg.index[is_competing], '_weight'] = weights

                covars = [c for c in ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin',
                                      'is_t2dm_nomed', 'age_at_index', 'male'] if c in df_fg.columns]
                fit_cols = covars + [T, outcome, '_weight']
                df_fit = df_fg[fit_cols].dropna()
                df_fit = df_fit[df_fit[T] > 0]

                if len(df_fit) >= _min_cr and df_fit[outcome].sum() >= _min_cr_events:
                    cph = CoxPHFitter()
                    cph.fit(df_fit, duration_col=T, event_col=outcome, weights_col='_weight')
                    fg_summary = cph.summary
            except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
                logger.exception(f"분석 오류 (Fine-Gray {outcome})")
                logger.warning(f"Fine-Gray ({outcome}) 실패: {e}")
            except Exception as e:
                logger.exception(f"예기치 않은 오류 (Fine-Gray {outcome})")
                logger.warning(f"Fine-Gray ({outcome}) 실패: {e}")

            results[outcome] = {
                'cif_by_group': cif_by_group,
                'fine_gray_summary': fg_summary,
                'method': 'Aalen-Johansen CIF + IPCW Fine-Gray approximation',
                'method_note': ('IPCW 고정 가중치 근사(G(t_j)/G(t_max)). '
                                '논문 게재 시 R cmprsk::crr()로 교차 검증 필요.'),
                'n_event': int((event_type == 1).sum()),
                'n_competing': int((event_type == 2).sum()),
                'n_censored': int((event_type == 0).sum()),
                # A5: 방법론 검증 상태 명시 — 논문 제출 시 교차 검증 필수
                'validation_status': 'NOT_VALIDATED',
                'validation_note': (
                    '이 구현은 IPCW 고정 가중치 근사입니다. '
                    '논문 게재 전 R cmprsk::crr() 또는 SAS PHREG EVENTCODE= 옵션으로 '
                    '교차 검증하여 결과 일치성을 확인하세요. '
                    'subdistribution HR이 원 Fine-Gray와 최대 ±5% 이내이면 허용 가능.'
                ),
            }

            del df_cr, event_type
            try:
                del df_fg
            except NameError:
                pass
            gc.collect()

        results['_method_warning'] = (
            "[NOT_VALIDATED] IPCW 고정 가중치 근사 방법 사용. "
            "논문 게재 전 R cmprsk::crr() 또는 SAS PHREG로 교차 검증 필수. "
            "validation_status='NOT_VALIDATED' 확인 후 제출."
        )
        self.results['competing_risks'] = results
        return results

    def run_cross_validation(self, cb=None, df_prepared=None):
        """Fine-Gray Python 결과와 R cmprsk::crr() 결과를 교차 검증한다.

        run_competing_risks() 이후에 호출. Python IPCW 근사 결과를 R의 원 Fine-Gray
        구현과 비교하여 HR 차이(%)와 concordant 여부를 반환한다.

        결과:
            self.results['cross_validation'] = {
                outcome: {
                    'csv_path': str,
                    'r_script_path': str,
                    'r_available': bool,
                    'r_results': dict | None,
                    'comparison_df': pd.DataFrame,
                    'validation_status': 'VALIDATED'|'DISCREPANT'|'R_NOT_AVAILABLE'|'NO_COMPARISON_DATA',
                    'temp_dir': str,  # cleanup_temp_dir()에 전달
                }
            }
        """
        from cross_validator import CrossValidator
        if cb: cb("교차 검증 (Python vs R cmprsk::crr) 실행 중...")

        cv = CrossValidator()
        cv_results = {}

        cr_results = self.results.get('competing_risks', {})
        if not cr_results:
            logger.warning("교차 검증 스킵: run_competing_risks() 결과 없음")
            self.results['cross_validation'] = self._skip_result(
                self._RC_MISSING_UPSTREAM_RESULT,
                '경쟁위험 분석 결과 없음',
                stage='cross_validation',
                upstream='competing_risks',
            )
            return self.results['cross_validation']

        if df_prepared is None:
            raw, _ = self._load_data(cb=None)
            df_prepared = self._prepare(raw, cb=None)

        _min_cr = int(STUDY_SETTINGS.get('MIN_VALID_ROWS', 30))

        for outcome in ['dementia_event', 'ad_event', 'vad_event']:
            oc_data = cr_results.get(outcome, {})
            if not isinstance(oc_data, dict):
                continue
            if cb: cb(f"교차 검증: {outcome} 처리 중...")

            cr_data = self._prepare_cr_data(df_prepared, outcome, min_rows=_min_cr)
            if cr_data is None:
                logger.info("교차 검증 %s 스킵: 데이터 부족", outcome)
                continue
            df_cr, event_type = cr_data

            # Python Fine-Gray summary 가져오기
            py_summary = oc_data.get('fine_gray_summary')

            temp_dir = None
            csv_path = r_script_path = None
            r_available = False
            r_results = None
            comparison_df = pd.DataFrame()

            try:
                csv_path = cv.export_csv_for_r(df_cr, event_type, outcome)
                temp_dir = csv_path.parent
                r_script_path = cv.generate_r_script(csv_path, outcome)

                if cb: cb(f"교차 검증: {outcome} — R 스크립트 생성 완료. R 실행 중...")
                r_results = cv.run_r_script(r_script_path)
                r_available = r_results is not None

                comparison_df = cv.compare_results(py_summary, r_results)
                status = CrossValidator.validation_status(comparison_df, r_available)

                if cb:
                    if status == 'VALIDATED':
                        cb(f"교차 검증: {outcome} — VALIDATED (모든 HR 차이 5% 이내)")
                    elif status == 'DISCREPANT':
                        n_disc = int((~comparison_df['concordant']).sum())
                        cb(f"교차 검증: {outcome} — DISCREPANT ({n_disc}개 공변량 차이 초과)")
                    else:
                        cb(f"교차 검증: {outcome} — {status}")

            except Exception as e:
                logger.exception("교차 검증 오류 (%s): %s", outcome, e)
                status = 'ERROR'
                error_info = self._error_result(
                    self._RC_CROSS_VALIDATION_ERROR,
                    e,
                    stage='cross_validation',
                    outcome=outcome,
                )
                if cb: cb(f"[오류] 교차 검증 {outcome}: {e}")

            cv_results[outcome] = {
                'csv_path':          str(csv_path) if csv_path else None,
                'r_script_path':     str(r_script_path) if r_script_path else None,
                'r_available':       r_available,
                'r_results':         r_results,
                'comparison_df':     comparison_df,
                'validation_status': status,
                'temp_dir':          str(temp_dir) if temp_dir else None,
            }
            if status == 'ERROR':
                cv_results[outcome].update(error_info)

        self.results['cross_validation'] = cv_results
        if cb: cb("교차 검증 완료!")
        return cv_results

    def run_sensitivity(self, cb=None, df_prepared=None):
        if cb: cb("민감도 분석 중...")
        sens = {}
        try:
            # T30(진료내역) + T60(처방전내역) 모두에서 항치매약 처방 확인
            # (step5 제외 로직과 동일한 소스 범위 유지)
            dl = "'" + "','".join(DEMENTIA_DRUG_CODES) + "'"
            r = self.dm.query(f"""
                WITH drug_patients AS (
                    SELECT DISTINCT INDI_DSCM_NO FROM T30
                    WHERE SUBSTR(WK_COMPN_CD,1,6)      IN ({dl})
                       OR SUBSTR(RVSN_WK_COMPN_CD,1,6) IN ({dl})
                    UNION
                    SELECT DISTINCT INDI_DSCM_NO FROM T60
                    WHERE SUBSTR(GNL_NM_CD,1,6)        IN ({dl})
                       OR SUBSTR(RVSN_WK_COMPN_CD,1,6) IN ({dl})
                )
                SELECT COUNT(DISTINCT a.INDI_DSCM_NO) AS n
                FROM outcome_all_cause a
                INNER JOIN drug_patients dp ON a.INDI_DSCM_NO = dp.INDI_DSCM_NO
            """)
            sens['dementia_with_drug'] = {'n': int(r.iloc[0, 0]) if len(r) > 0 else 0,
                                          'desc': '치매진단 + 항치매약 동반처방 (T30+T60)'}
            del r
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
            logger.warning("민감도(항치매약) 쿼리 실패: %s", e)
            sens['dementia_with_drug'] = {
                'n': None,
                'desc': f'쿼리 실패: {e}',
                **self._error_result(self._RC_SENSITIVITY_ERROR, e, stage='sensitivity'),
            }
            if cb: cb(f"[경고] 민감도(항치매약) 쿼리 실패: {e}")
        except Exception as e:
            logger.exception("예기치 않은 오류 (민감도-항치매약)")
            sens['dementia_with_drug'] = {
                'n': None,
                'desc': f'오류: {e}',
                **self._error_result(self._RC_SENSITIVITY_ERROR, e, stage='sensitivity'),
            }
            if cb: cb(f"[오류] 민감도(항치매약): {e}")

        sens['fine_gray'] = {
            'implemented': True,
            'desc': ('Fine-Gray 경쟁위험모형 — IPCW 고정 가중치 근사 구현. '
                     '논문 게재 시 R cmprsk::crr()와 교차 검증 권장. '
                     'run_competing_risks() 참조.'),
        }

        # A6-1: 추적기간 절단 변형 (1년, 2년, 5년)
        # df_prepared가 전달된 경우 재사용하여 불필요한 reload + _prepare 3회 복사 방지
        if cb: cb("민감도 분석 — 추적기간 절단 변형 분석 중...")
        if df_prepared is None:
            raw, _ = self._load_data(cb=None)
            _sens_base = self._prepare(raw, cb=None)
        else:
            _sens_base = df_prepared
        for cutoff_yr in [1, 2, 5]:
            key = f'followup_cutoff_{cutoff_yr}y'
            try:
                df_cut = _sens_base.copy()
                # 추적기간 절단: follow_up_years > cutoff → 절단 처리
                cut_mask = df_cut['follow_up_years'] > cutoff_yr
                df_cut.loc[cut_mask, 'dementia_event'] = 0
                df_cut.loc[cut_mask, 'follow_up_years'] = cutoff_yr
                df_cut = df_cut[df_cut['follow_up_years'] > 0]
                n_events = int(df_cut['dementia_event'].sum()) if 'dementia_event' in df_cut.columns else 0
                exp_cols_cut = [c for c in ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']
                                if c in df_cut.columns]
                cox_results = {}
                failed_models = {}
                if n_events >= int(STUDY_SETTINGS.get('MIN_EVENTS', 10)):
                    for exp_var in exp_cols_cut:
                        cols_m = [exp_var, 'age_at_index', 'male', 'follow_up_years', 'dementia_event']
                        cols_m = [c for c in cols_m if c in df_cut.columns]
                        dm2 = df_cut[cols_m].dropna()
                        dm2 = dm2[dm2['follow_up_years'] > 0]
                        if len(dm2) >= 30 and dm2['dementia_event'].sum() >= 5:
                            try:
                                cph = CoxPHFitter()
                                cph.fit(dm2, duration_col='follow_up_years', event_col='dementia_event')
                                if exp_var in cph.summary.index:
                                    cox_results[exp_var] = {
                                        'hr': round(cph.summary.loc[exp_var, 'exp(coef)'], 4),
                                        'ci_lower': round(cph.summary.loc[exp_var, 'exp(coef) lower 95%'], 4),
                                        'ci_upper': round(cph.summary.loc[exp_var, 'exp(coef) upper 95%'], 4),
                                        'p_value': round(cph.summary.loc[exp_var, 'p'], 6),
                                    }
                            except Exception as e2:
                                logger.debug("민감도 cutoff %dy Cox (%s) 실패: %s", cutoff_yr, exp_var, e2)
                                failed_models[exp_var] = self._model_failure(
                                    self._RC_COX_MODEL_FAILED,
                                    str(e2),
                                    stage='sensitivity_cutoff_cox',
                                    model=exp_var,
                                    cutoff_year=cutoff_yr,
                                    exception_type=type(e2).__name__,
                                )
                sens[key] = {
                    'n': len(df_cut), 'n_events': n_events,
                    'desc': f'추적기간 {cutoff_yr}년 절단 민감도 분석',
                    'cox_results': cox_results,
                    'failed_models': failed_models,
                }
                del df_cut
                gc.collect()
            except Exception as e:
                logger.warning("민감도(추적기간 절단 %dy): %s", cutoff_yr, e)
                logger.debug("Hermes R2-3a: follow-up cutoff exception structured at %dy", cutoff_yr)
                sens[key] = {
                    'n': None,
                    'desc': f'추적기간 {cutoff_yr}년 절단',
                    'error': str(e),
                    **self._error_result(self._RC_SENSITIVITY_ERROR, e, stage='sensitivity'),
                }

        # A6-2: DM 정의 변형 (외래 방문 ≥2회) — 실제 데이터에서만 의미 있음
        # 코호트 빌더가 이미 필터링하므로 여기서는 메타데이터만 기록
        sens['dm_definition_outpatient_ge2'] = {
            'implemented': False,
            'desc': (
                'DM 정의 민감도: 외래 ≥2회 방문 기준. '
                '현재 구현은 cohort_builder에서 설정 가능 (MIN_OUTPATIENT_VISITS 파라미터). '
                '코호트 재구축 후 분석 재실행 필요.'
            ),
        }

        # A6-3: 검열 연령 변형 (AGE65 vs AGE70 vs AGE75) — 메타데이터 기록
        current_censor_month = STUDY_SETTINGS.get('AGE65_CENSOR_MONTH', '0101')
        sens['censoring_age_variation'] = {
            'implemented': False,
            'current_setting': f'AGE65_CENSOR_MONTH={current_censor_month}',
            'desc': (
                '검열 연령 민감도: 65세/70세/75세 비교. '
                'config.py의 AGE65_CENSOR_MONTH 및 cohort_builder를 수정하여 '
                '각 기준으로 코호트 재구축 후 분석을 재실행하세요.'
            ),
        }

        self.results['sensitivity'] = sens
        gc.collect()
        return sens

    def generate_table1(self, cb=None, df_prepared=None):
        """Table 1 생성 — 역학 논문 표준 형식.

        구조: 행=특성 변수, 열=노출군 + P_value
        포함 항목:
          - N (건수)
          - 연속변수: mean ± SD  (Kruskal-Wallis p)
          - 이분형 변수: n (%)   (chi-square p)
          - Person-years (총합)
          - Follow-up (years): Median (IQR)
          - Events (dementia/AD/VaD/death): n, Incidence Rate per 1000 PY
        """
        if cb: cb("Table 1 생성 중...")
        if df_prepared is None:
            raw, _ = self._load_data(cb=cb)
            df_prepared = self._prepare(raw, cb=cb)

        groups = sorted(df_prepared['exposure_group'].unique())
        rows = []

        def _fmt_p(p):
            if p is None or (isinstance(p, float) and np.isnan(p)):
                return ''
            return '<0.001' if p < 0.001 else f'{p:.3f}'

        def _kruskal_p(col):
            gvs = [pd.to_numeric(df_prepared.loc[df_prepared['exposure_group'] == g, col],
                                  errors='coerce').dropna().values for g in groups]
            gvs = [v for v in gvs if len(v) > 0]
            if len(gvs) < 2:
                return np.nan
            try:
                _, p = stats.kruskal(*gvs)
                return p
            except Exception:
                return np.nan

        def _chi2_p(col):
            try:
                binary = pd.to_numeric(df_prepared[col], errors='coerce').fillna(0).astype(int)
                ct = pd.crosstab(df_prepared['exposure_group'], binary)
                if ct.shape[0] >= 2 and ct.shape[1] >= 2:
                    _, p, _, _ = stats.chi2_contingency(ct)
                    return p
            except Exception:
                pass
            return np.nan

        # ---- N ----
        row_n = {'Variable': 'N', 'Category': ''}
        for g in groups:
            row_n[g] = str(int((df_prepared['exposure_group'] == g).sum()))
        row_n['P_value'] = ''
        rows.append(row_n)

        # ---- 연속 변수: mean ± SD ----
        continuous_vars = [
            ('age_at_index', 'Age (years)'),
            ('bmi', 'BMI (kg/m²)'),
            ('fbs', 'FBS (mg/dL)'),
            ('sbp', 'SBP (mmHg)'),
            ('egfr', 'eGFR (mL/min/1.73m²)'),
            ('cci_score', 'CCI score'),
            ('dm_duration_years', 'DM duration (years)'),
        ]
        for col, label in continuous_vars:
            if col not in df_prepared.columns:
                continue
            row = {'Variable': label, 'Category': 'Mean ± SD'}
            for g in groups:
                v = pd.to_numeric(df_prepared.loc[df_prepared['exposure_group'] == g, col],
                                   errors='coerce').dropna()
                row[g] = f'{v.mean():.1f} ± {v.std():.1f}' if len(v) > 0 else 'N/A'
            row['P_value'] = _fmt_p(_kruskal_p(col))
            rows.append(row)

        # ---- 이분형 변수: n (%) ----
        binary_vars = [('male', 'Male sex')]
        for c in sorted(c for c in df_prepared.columns if c.startswith('comor_')):
            binary_vars.append((c, c.replace('comor_', '').replace('_', ' ').title()))
        for c in sorted(c for c in df_prepared.columns if c.startswith('comp_')):
            binary_vars.append((c, 'DM complication: ' + c.replace('comp_', '').replace('_', ' ').title()))

        for col, label in binary_vars:
            if col not in df_prepared.columns:
                continue
            row = {'Variable': label, 'Category': 'n (%)'}
            for g in groups:
                v = pd.to_numeric(df_prepared.loc[df_prepared['exposure_group'] == g, col],
                                   errors='coerce').dropna()
                if len(v) > 0:
                    row[g] = f'{int(v.sum())} ({v.mean() * 100:.1f}%)'
                else:
                    row[g] = 'N/A'
            row['P_value'] = _fmt_p(_chi2_p(col))
            rows.append(row)

        # ---- 추적 기간 ----
        if 'follow_up_years' in df_prepared.columns:
            row_py = {'Variable': 'Person-years', 'Category': 'Total', 'P_value': ''}
            row_fu = {'Variable': 'Follow-up (years)', 'Category': 'Median (IQR)', 'P_value': ''}
            for g in groups:
                v = pd.to_numeric(df_prepared.loc[df_prepared['exposure_group'] == g, 'follow_up_years'],
                                   errors='coerce').dropna()
                row_py[g] = f'{v.sum():.1f}' if len(v) > 0 else 'N/A'
                if len(v) > 0:
                    med = v.median()
                    q1, q3 = v.quantile(0.25), v.quantile(0.75)
                    row_fu[g] = f'{med:.1f} ({q1:.1f}–{q3:.1f})'
                else:
                    row_fu[g] = 'N/A'
            rows.append(row_py)
            rows.append(row_fu)

        # ---- 이벤트 수 및 발생률 ----
        event_defs = [
            ('dementia_event', 'Dementia'),
            ('ad_event', "Alzheimer's disease"),
            ('vad_event', 'Vascular dementia'),
            ('competing_death_event', 'Death (competing risk)'),
        ]
        has_py = 'follow_up_years' in df_prepared.columns
        for ecol, elabel in event_defs:
            if ecol not in df_prepared.columns:
                continue
            row_ev = {'Variable': elabel + ' events', 'Category': 'n', 'P_value': ''}
            row_ir = {'Variable': elabel + ' events', 'Category': 'IR per 1000 PY', 'P_value': ''}
            for g in groups:
                mask = df_prepared['exposure_group'] == g
                ev = pd.to_numeric(df_prepared.loc[mask, ecol], errors='coerce').fillna(0).sum()
                row_ev[g] = str(int(ev))
                if has_py:
                    py = pd.to_numeric(df_prepared.loc[mask, 'follow_up_years'],
                                        errors='coerce').dropna().sum()
                    row_ir[g] = f'{ev / py * 1000:.2f}' if py > 0 else 'N/A'
            rows.append(row_ev)
            if has_py:
                rows.append(row_ir)

        cols = ['Variable', 'Category'] + list(groups) + ['P_value']
        self.results['table1'] = pd.DataFrame(rows, columns=cols)
        return self.results['table1']

    def run_selected(self, cb=None, run_cox=True, run_psm=True,
                     run_interaction=True, run_subgroup=True, run_sensitivity=True,
                     run_competing_risks=True, run_cross_validation=False,
                     results_dir=None, resume=False):
        """선택된 분석만 실행 — 체크박스 상태를 그대로 반영.

        Args:
            results_dir: 결과 저장 경로 (체크포인트 파일 위치). None이면 체크포인트 비활성.
            resume: True면 이전 체크포인트에서 완료된 단계 건너뜀.
        """
        # B2: 체크포인트 초기화
        checkpoint = None
        if results_dir is not None:
            from analysis_checkpoint import AnalysisCheckpoint
            checkpoint = AnalysisCheckpoint(results_dir, STUDY_SETTINGS)
            if resume and checkpoint.completed_steps():
                msg = f"이전 체크포인트에서 재개: {', '.join(checkpoint.completed_steps())}"
                logger.info(msg)
                if cb: cb(f"[재개] {msg}")
            elif not resume:
                checkpoint.reset()  # 새 실행 시 이전 체크포인트 초기화

        raw, info = self._load_data(cb=cb)
        df_prepared = self._prepare(raw, cb=cb)
        logger.info(f"분석 데이터 준비 완료: {len(df_prepared):,}건, "
                   f"{df_prepared.memory_usage(deep=True).sum() / 1024**2:.1f}MB")

        from gpu_accelerator import get_gpu_status
        gpu_info = get_gpu_status()
        if gpu_info['gpu_enabled']:
            logger.info(f"GPU 가속 활성화 — cupy: {gpu_info['cupy_available']}, "
                       f"torch CUDA: {gpu_info['torch_cuda']}")
            if cb: cb("GPU 가속 모드로 분석을 실행합니다.")

        step_errors = {}  # {step_name: error_message}
        step_error_details = {}  # {step_name: structured_error}

        # B1: 진행률(%) 계산 — 활성 단계 수 기준
        _active_steps = (['table1'] +
                         ([f'cox_{oc}' for oc in ['dementia_event', 'ad_event', 'vad_event']] if run_cox else []) +
                         (['psm'] if run_psm else []) +
                         (['interaction'] if run_interaction else []) +
                         (['subgroup'] if run_subgroup else []) +
                         (['competing_risks'] if run_competing_risks else []) +
                         (['cross_validation'] if run_cross_validation and run_competing_risks else []) +
                         (['sensitivity'] if run_sensitivity else []))
        _total_steps = len(_active_steps)
        _done_steps = [0]  # 가변 카운터 (클로저용 리스트)

        def _safe_run(step_name, fn):
            # B2: 이미 완료된 단계는 체크포인트 기반 건너뜀
            if checkpoint and resume and checkpoint.can_resume(step_name):
                logger.info("체크포인트: %s 건너뜀 (이미 완료)", step_name)
                if cb: cb(f"[재개] {step_name} 건너뜀 (이미 완료)")
                _done_steps[0] += 1
                pct = int(_done_steps[0] / _total_steps * 100) if _total_steps > 0 else 100
                if cb: cb(f"[{pct}%] {step_name} 건너뜀")
                return
            try:
                fn()
                # B2: 성공 시 체크포인트 저장
                if checkpoint:
                    checkpoint.mark_done(step_name, self.results.get(step_name))
            except (InsufficientDataError, RuntimeError) as e:
                step_errors[step_name] = str(e)
                reason_code = getattr(e, 'reason_code', None) or 'STEP_SKIPPED'
                step_error_details[step_name] = self._error_result(
                    reason_code,
                    e,
                    stage=step_name,
                )
                logger.warning("분석 단계 스킵 (%s): %s", step_name, e)
                if cb: cb(f"[경고] {step_name} 스킵: {e}")
            except Exception as e:
                step_errors[step_name] = str(e)
                reason_code = getattr(e, 'reason_code', None) or 'STEP_ERROR'
                step_error_details[step_name] = self._error_result(
                    reason_code,
                    e,
                    stage=step_name,
                )
                logger.exception("분석 단계 오류 (%s)", step_name)
                if cb: cb(f"[오류] {step_name}: {e}")
            finally:
                _done_steps[0] += 1
                pct = int(_done_steps[0] / _total_steps * 100) if _total_steps > 0 else 100
                if cb: cb(f"[{pct}%] {step_name} 완료")
                mem_manager.cleanup_after_step(step_name)  # 성공/실패 무관 항상 정리

        # Table 1은 항상 생성
        _safe_run('table1', lambda: self.generate_table1(cb=cb, df_prepared=df_prepared))

        if run_cox:
            for oc in ['dementia_event', 'ad_event', 'vad_event']:
                _safe_run(f'cox_{oc}', lambda o=oc: self.run_cox(o, cb=cb, df_prepared=df_prepared))

        if run_psm:
            _safe_run('psm', lambda: self.run_psm(cb=cb, df_prepared=df_prepared))

        if run_interaction:
            _safe_run('interaction', lambda: self.run_interaction(cb=cb, df_prepared=df_prepared))

        if run_subgroup:
            _safe_run('subgroup', lambda: self.run_subgroup(cb=cb, df_prepared=df_prepared))

        if run_competing_risks:
            _safe_run('competing_risks', lambda: self.run_competing_risks(cb=cb, df_prepared=df_prepared))

        if run_cross_validation and run_competing_risks:
            _safe_run('cross_validation', lambda: self.run_cross_validation(cb=cb, df_prepared=df_prepared))

        if run_sensitivity:
            _safe_run('sensitivity', lambda: self.run_sensitivity(cb=cb, df_prepared=df_prepared))

        del df_prepared
        gc.collect()

        if step_errors:
            self.results['step_errors'] = step_errors
            self.results['step_error_details'] = step_error_details
            logger.warning("분석 단계 오류 요약: %s", step_errors)

        self.results['sampling_info'] = info
        # tabs.py 기존 호환성: _sampling_note 문자열 유지
        self.results['_sampling_note'] = info.label if info.applied else None
        self._release_cache()
        if info.applied and cb:
            cb(f"[경고] {info.label}")
        if cb: cb("선택된 분석 완료!")
        return self.results

    def run_all(self, cb=None):
        """전체 분석 — 모든 단계 실행 (하위 호환용)"""
        return self.run_selected(cb)
