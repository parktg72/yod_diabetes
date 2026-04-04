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

    def _load_data(self):
        """메모리 안전 데이터 로드 — 1회 로드 후 캐시 재사용"""
        if self._cached_df is not None:
            return self._cached_df, self._sampling_info

        max_rows = mem_manager.get_safe_analysis_rows()
        total = self.dm.storage.get_row_count('final_analysis')
        if total > max_rows:
            logger.warning(f"분석 데이터 {total:,}건 > 안전 한도 {max_rows:,}건 → 층화 샘플링")
            # 각 노출군의 실제 비율에 비례하여 max_rows 배분
            group_counts_df = self.dm.query(
                "SELECT exposure_group, COUNT(*) AS cnt FROM final_analysis "
                "WHERE follow_up_days > 0 GROUP BY exposure_group"
            )
            group_counts = dict(zip(group_counts_df['exposure_group'], group_counts_df['cnt']))
            valid_total = sum(group_counts.values())

            if valid_total == 0:
                logger.error("샘플링 분기: total=%d, valid_total=0 — EmptyDataError", total)
                raise pd.errors.EmptyDataError(self._MSG_NO_VALID_ROWS)

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
                total_rows=total,
                sampled_rows=len(self._cached_df),
                seed=seed,
            )
        else:
            self._cached_df = self.dm.query("SELECT * FROM final_analysis WHERE follow_up_days > 0")
            if self._cached_df.empty:
                logger.error("비샘플링 분기: total=%d, valid_rows=0 — EmptyDataError", total)
                raise pd.errors.EmptyDataError(self._MSG_NO_VALID_ROWS)
            self._sampling_info = SamplingInfo(
                applied=False,
                total_rows=total,
                sampled_rows=len(self._cached_df),
            )

        # dtype 최적화
        self._cached_df = mem_manager.optimize_dtypes(self._cached_df)
        logger.info(f"분석 데이터 로드: {len(self._cached_df):,}건, "
                   f"{self._cached_df.memory_usage(deep=True).sum() / 1024**2:.1f}MB")
        return self._cached_df, self._sampling_info

    def _release_cache(self):
        """캐시된 데이터 해제"""
        if self._cached_df is not None:
            del self._cached_df
            self._cached_df = None
            gc.collect()

    def _prepare(self, df):
        """공변량 전처리 — 캐시 원본 보호를 위해 1회 copy 후 파생변수 추가"""
        prepared = df.copy()  # 캐시(_cached_df) 원본 변경 방지를 위해 copy 필요

        prepared['is_t1dm'] = (prepared['exposure_group'] == 'T1DM').astype('int8')
        prepared['is_t2dm_oha'] = (prepared['exposure_group'] == 'T2DM_OHA').astype('int8')
        prepared['is_t2dm_insulin'] = (prepared['exposure_group'] == 'T2DM_INSULIN').astype('int8')
        prepared['is_t2dm_nomed'] = (prepared['exposure_group'] == 'T2DM_NOMED').astype('int8')
        prepared['male'] = (prepared['SEX_TYPE'] == '1').astype('int8')

        for col in ['age_at_index', 'income_quintile', 'bmi', 'cci_score',
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
        if cb: cb(f"Cox 회귀 ({outcome})...")
        if df_prepared is None:
            raw, _ = self._load_data()
            df_prepared = self._prepare(raw)

        T, E = 'follow_up_years', outcome
        results = {}
        exposure = ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed']

        models = {
            'model1_age_sex': exposure + ['age_at_index', 'male'],
            'model2_socio': exposure + ['age_at_index', 'male', 'income_q',
                             'comor_hypertension', 'comor_dyslipidemia', 'comor_depression'],
            'model3_full': exposure + ['age_at_index', 'male', 'income_q',
                            'comor_hypertension', 'comor_dyslipidemia', 'comor_depression',
                            'comp_retinopathy', 'comp_nephropathy', 'comp_neuropathy',
                            'comor_ischemic_stroke', 'comor_hemorrhagic_stroke',
                            'comor_ihd', 'comor_atrial_fib', 'comor_heart_failure',
                            'comp_hypoglycemia'],
        }

        for mname, mcols in models.items():
            cols = [c for c in mcols if c in df_prepared.columns] + [T, E]
            df_model = df_prepared[cols].dropna()
            try:
                cph = CoxPHFitter()
                cph.fit(df_model, duration_col=T, event_col=E)
                result_entry = {'summary': cph.summary, 'concordance': cph.concordance_index_}
                # PH 가정 검정 (Schoenfeld residuals)
                try:
                    ph_test = proportional_hazard_test(cph, df_model, time_transform='rank')
                    result_entry['ph_test'] = ph_test.summary
                    violated = ph_test.summary[ph_test.summary['p'] < 0.05]
                    if not violated.empty:
                        logger.warning(f"Cox {mname}: PH 가정 위반 변수 — "
                                     f"{', '.join(violated.index.tolist())}")
                except Exception as ph_e:
                    logger.info(f"PH 검정 생략 ({mname}): {ph_e}")
                results[mname] = result_entry
            except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
                logger.exception(f"분석 오류 (Cox {mname})")
                logger.warning(f"Cox {mname} 실패: {e}")
            except Exception as e:
                logger.exception(f"예기치 않은 오류 (Cox {mname})")
                logger.warning(f"Cox {mname} 실패: {e}")
            finally:
                del df_model
                gc.collect()

        # PH 검정 요약을 모델별로 취합하여 최상위에 저장
        ph_combined = {}
        for mname, entry in results.items():
            if 'ph_test' in entry:
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
            raw, _ = self._load_data()
            df_prepared = self._prepare(raw)

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

        ps_vars = ['age_at_index', 'male', 'income_q', 'comor_hypertension',
                    'comor_dyslipidemia', 'dm_duration_years']
        ps_vars = [c for c in ps_vars if c in df_dm.columns]
        df_ps = df_dm[ps_vars + ['is_t1dm']].dropna()

        # PSM 실행 가능 여부 검증: T1DM과 non-T1DM 모두 존재해야 함
        n_treated = (df_ps['is_t1dm'] == 1).sum()
        n_control = (df_ps['is_t1dm'] == 0).sum()
        if n_treated < 2 or n_control < 2:
            msg = (f"PSM 스킵: T1DM={n_treated}명, non-T1DM={n_control}명 "
                   f"— 로지스틱 회귀를 위해 각 그룹 최소 2명 이상 필요")
            logger.warning(msg)
            if cb: cb(msg)
            self.results['psm'] = {'skipped': True, 'reason': msg}
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
        # caliper: 0.2 × pooled SD of logit(PS) — treated/control 합산 분산 기준
        pooled_sd = np.sqrt((lps_t.var() + lps_c.var()) / 2)
        caliper = 0.2 * pooled_sd

        if len(control) < 1:
            msg = f"PSM 스킵: control 수({len(control)})가 0이라 매칭 불가"
            logger.warning(msg)
            if cb: cb(msg)
            self.results['psm'] = {'skipped': True, 'reason': msg}
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
            self.results['psm'] = {'skipped': True, 'reason': 'caliper 내 매칭 쌍 없음'}
            if cb: cb("PSM 스킵: caliper 내 매칭 가능한 쌍이 없습니다.")
            return self.results.get('psm', {})

        matched = pd.concat([df_dm.loc[mt_list], df_dm.loc[mc_list]])

        # Balance
        balance = {}
        for col in ps_vars:
            tm = matched.loc[matched['is_t1dm'] == 1, col].mean()
            cm = matched.loc[matched['is_t1dm'] == 0, col].mean()
            ts = matched.loc[matched['is_t1dm'] == 1, col].std()
            cs = matched.loc[matched['is_t1dm'] == 0, col].std()
            ps2 = np.sqrt((ts**2 + cs**2) / 2)
            smd = abs(tm - cm) / (ps2 if ps2 > 0 else 1)
            balance[col] = {'treated_mean': round(tm, 4), 'control_mean': round(cm, 4),
                           'smd': round(smd, 4), 'balanced': smd < 0.1}

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
                    except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
                        logger.exception(f"분석 오류 (PSM Cox {oc})")
                        logger.warning(f"PSM Cox ({oc}) 실패: {e}")
                    except Exception as e:
                        logger.exception(f"예기치 않은 오류 (PSM Cox {oc})")
                        logger.warning(f"PSM Cox ({oc}) 실패: {e}")

        self.results['psm'] = {
            'n_treated': len(mt_list), 'n_control': len(mc_list),
            'balance': balance, 'cox_results': psm_cox,
            # ★ matched_df 저장하지 않음 → 메모리 대폭 절약
        }

        # ★ 중간 객체 모두 삭제
        del matched, df_dm, df_ps, treated, control, dists, idxs, mt_list, mc_list
        gc.collect()

        return self.results['psm']

    def run_interaction(self, cb=None, df_prepared=None):
        if cb: cb("상호작용 분석 중...")
        if df_prepared is None:
            raw, _ = self._load_data()
            df_prepared = self._prepare(raw)

        df_dm = df_prepared[df_prepared['exposure_group'] != 'NON_DM']
        if 'dm_duration_cat' not in df_dm.columns:
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

        try:
            cph = CoxPHFitter()
            cph.fit(d, duration_col='follow_up_years', event_col='dementia_event')
            self.results['interaction'] = {'summary': cph.summary}
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError, MemoryError) as e:
            logger.exception("분석 오류 (run_interaction)")
            logger.warning(f"상호작용 분석 실패: {e}")
        except Exception as e:
            logger.exception("예기치 않은 오류 (run_interaction)")
            logger.warning(f"상호작용 분석 실패: {e}")
        finally:
            del d; gc.collect()

        return self.results.get('interaction')

    def run_subgroup(self, cb=None, df_prepared=None):
        if cb: cb("하위그룹 분석 중...")
        if df_prepared is None:
            raw, _ = self._load_data()
            df_prepared = self._prepare(raw)
        df = df_prepared  # 참조만 (copy 안 함)

        subgroups = {
            'sex_male': df['male'] == 1, 'sex_female': df['male'] == 0,
            'age_40_54': df['age_group'] == '40-54', 'age_55_64': df['age_group'] == '55-64',
        }
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

        model_cols_base = ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin', 'is_t2dm_nomed',
                           'age_at_index', 'male', 'cci_score',
                           'follow_up_years', 'dementia_event']

        sg_results = {}
        for name, mask in subgroups.items():
            try:
                # ★ copy 대신 필요 컬럼만 선택하여 새 df 생성
                cols = [c for c in model_cols_base if c in df.columns]
                if 'sex' in name and 'male' in cols:
                    cols.remove('male')

                dm = df.loc[mask, cols].dropna()
                dm = dm[dm['follow_up_years'] > 0]

                if len(dm) < 100 or dm['dementia_event'].sum() < 5:
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
        order = np.argsort(times)
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

    def run_competing_risks(self, cb=None, df_prepared=None):
        """경쟁위험 분석: Aalen-Johansen CIF + IPCW Fine-Gray 근사

        사망/탈퇴를 경쟁위험으로 처리.

        [방법론 주의사항]
        - CIF: Aalen-Johansen 추정기로 구현. 동률(ties) 처리는 표준 방식 적용.
        - Fine-Gray: lifelines CoxPHFitter + IPCW 가중치 기반 근사 구현.
          G(t)는 역KM 추정(검열=사건, 실제 사건=검열). 경쟁위험 대상자의 추적시간을
          max_time으로 연장하고 G(t_max)/G(t_j) 가중치를 적용하는 고정 가중치 방식.
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
            raw, _ = self._load_data()
            df_prepared = self._prepare(raw)

        if 'competing_death_event' not in df_prepared.columns:
            logger.warning("competing_death_event 컬럼 없음 — 코호트 재구축 필요")
            self.results['competing_risks'] = {
                'implemented': False,
                'reason': 'competing_death_event 컬럼 없음. 코호트를 재구축하세요.'
            }
            return self.results['competing_risks']

        T = 'follow_up_years'
        results = {}

        for outcome in ['dementia_event', 'ad_event', 'vad_event']:
            if outcome not in df_prepared.columns:
                continue

            need_cols = [T, outcome, 'competing_death_event', 'dementia_event',
                         'is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin',
                         'is_t2dm_nomed', 'age_at_index', 'male']
            need_cols = [c for c in need_cols if c in df_prepared.columns]
            df_cr = df_prepared[need_cols].dropna().copy()
            df_cr = df_cr[df_cr[T] > 0]

            if len(df_cr) < 100:
                continue

            # --- 1) 이벤트 유형 분류 ---
            # dementia_event: 경쟁위험 = 사망/탈퇴만
            # ad_event: 경쟁위험 = 사망/탈퇴 + non-AD 치매 (dementia=1 but ad=0)
            # vad_event: 경쟁위험 = 사망/탈퇴 + non-VaD 치매 (dementia=1 but vad=0)
            event_type = np.zeros(len(df_cr), dtype=int)
            event_type[df_cr[outcome].values == 1] = 1
            competing_mask = df_cr['competing_death_event'].values == 1
            if outcome in ('ad_event', 'vad_event') and 'dementia_event' in df_cr.columns:
                # 비대상 치매 유형도 경쟁위험으로 분류
                other_dementia = ((df_cr['dementia_event'].values == 1) &
                                  (df_cr[outcome].values == 0))
                competing_mask = competing_mask | other_dementia
            event_type[(df_cr[outcome].values == 0) & competing_mask] = 2

            # --- 2) 노출군별 CIF 추정 ---
            cif_by_group = {}
            for group_col, group_name in [('is_t1dm', 'T1DM'),
                                           ('is_t2dm_oha', 'T2DM_OHA'),
                                           ('is_t2dm_insulin', 'T2DM_INSULIN'),
                                           ('is_t2dm_nomed', 'T2DM_NOMED')]:
                if group_col not in df_cr.columns:
                    continue
                mask = df_cr[group_col].values == 1
                if mask.sum() < 10:
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
            non_dm_mask = ((df_cr.get('is_t1dm', 0) == 0) &
                           (df_cr.get('is_t2dm_oha', 0) == 0) &
                           (df_cr.get('is_t2dm_insulin', 0) == 0) &
                           (df_cr.get('is_t2dm_nomed', 0) == 0))
            if non_dm_mask.sum() >= 10:
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
                    # Fine-Gray IPCW: G(t)/G(t_j) — 시간이 지날수록 기여 감소 (≤ 1)
                    weights = np.maximum(g_at_max, 1e-10) / np.maximum(g_at_event, 1e-10)
                    weights = np.clip(weights, 0.01, 1.0)

                    df_fg.loc[is_competing, T] = max_time
                    df_fg.loc[is_competing, outcome] = 0
                    df_fg.loc[is_competing, '_weight'] = weights

                covars = [c for c in ['is_t1dm', 'is_t2dm_oha', 'is_t2dm_insulin',
                                      'is_t2dm_nomed', 'age_at_index', 'male'] if c in df_fg.columns]
                fit_cols = covars + [T, outcome, '_weight']
                df_fit = df_fg[fit_cols].dropna()
                df_fit = df_fit[df_fit[T] > 0]

                if len(df_fit) >= 100 and df_fit[outcome].sum() >= 5:
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
                'method_note': ('IPCW 고정 가중치 근사(G(t_max)/G(t_j)). '
                                '논문 게재 시 R cmprsk::crr()로 교차 검증 필요.'),
                'n_event': int((event_type == 1).sum()),
                'n_competing': int((event_type == 2).sum()),
                'n_censored': int((event_type == 0).sum()),
            }

            del df_cr, df_fg, event_type
            gc.collect()

        results['_method_warning'] = (
            "IPCW 고정 가중치 근사 방법 사용. "
            "논문 게재 전 R cmprsk::crr() 교차 검증 필수."
        )
        self.results['competing_risks'] = results
        return results

    def run_sensitivity(self, cb=None):
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
            logger.exception("분석 오류 (민감도-항치매약)")
            logger.warning(f"민감도(항치매약): {e}")
        except Exception as e:
            logger.exception("예기치 않은 오류 (민감도-항치매약)")
            logger.warning(f"민감도(항치매약): {e}")

        sens['fine_gray'] = {
            'implemented': True,
            'desc': ('Fine-Gray 경쟁위험모형 — IPCW 고정 가중치 근사 구현. '
                     '논문 게재 시 R cmprsk::crr()와 교차 검증 권장. '
                     'run_competing_risks() 참조.'),
        }
        self.results['sensitivity'] = sens
        gc.collect()
        return sens

    def generate_table1(self, cb=None, df_prepared=None):
        if cb: cb("Table 1 생성 중...")
        if df_prepared is None:
            raw, _ = self._load_data()
            df_prepared = self._prepare(raw)

        groups = sorted(df_prepared['exposure_group'].unique())
        rows = []
        for g in groups:
            # ★ copy 대신 boolean mask만 사용
            mask = df_prepared['exposure_group'] == g
            gd = df_prepared[mask]
            row = {'Group': g, 'N': int(mask.sum())}
            for col, lbl in [('age_at_index', 'Age'), ('bmi', 'BMI'), ('fbs', 'FBS'), ('cci_score', 'CCI')]:
                if col in gd.columns:
                    v = pd.to_numeric(gd[col], errors='coerce').dropna()
                    row[f'{lbl}_mean'] = round(v.mean(), 2)
                    row[f'{lbl}_sd'] = round(v.std(), 2)
            row['Male_pct'] = round((gd['male'] == 1).mean() * 100, 1) if 'male' in gd.columns else 0
            for c in [x for x in gd.columns if x.startswith('comor_')]:
                row[f'{c}_pct'] = round(pd.to_numeric(gd[c], errors='coerce').mean() * 100, 1)
            rows.append(row)

        # --- P-value 계산: 그룹 간 통계 검정 ---
        p_values = {}
        # 연속변수: Kruskal-Wallis test
        for col, lbl in [('age_at_index', 'Age'), ('bmi', 'BMI'), ('fbs', 'FBS'), ('cci_score', 'CCI')]:
            if col not in df_prepared.columns:
                continue
            group_vals = []
            for g in groups:
                v = pd.to_numeric(df_prepared.loc[df_prepared['exposure_group'] == g, col], errors='coerce').dropna()
                if len(v) > 0:
                    group_vals.append(v.values)
            if len(group_vals) >= 2:
                try:
                    _, pval = stats.kruskal(*group_vals)
                    p_values[f'{lbl}_mean'] = round(pval, 4)
                except Exception:
                    p_values[f'{lbl}_mean'] = np.nan

        # 이분형 변수: Chi-square test
        # Male_pct
        if 'male' in df_prepared.columns:
            try:
                ct = pd.crosstab(df_prepared['exposure_group'], df_prepared['male'])
                if ct.shape[0] >= 2 and ct.shape[1] >= 2:
                    _, pval, _, _ = stats.chi2_contingency(ct)
                    p_values['Male_pct'] = round(pval, 4)
            except Exception:
                p_values['Male_pct'] = np.nan

        # 동반질환 변수
        comor_cols = [c for c in df_prepared.columns if c.startswith('comor_')]
        for c in comor_cols:
            try:
                binary = pd.to_numeric(df_prepared[c], errors='coerce').fillna(0).astype(int)
                ct = pd.crosstab(df_prepared['exposure_group'], binary)
                if ct.shape[0] >= 2 and ct.shape[1] >= 2:
                    _, pval, _, _ = stats.chi2_contingency(ct)
                    p_values[f'{c}_pct'] = round(pval, 4)
            except Exception:
                p_values[f'{c}_pct'] = np.nan

        # P_value 열 추가 (첫 번째 행에만 기록, 나머지 행은 빈 값)
        for i, row in enumerate(rows):
            if i == 0:
                pv = {}
                for key, val in p_values.items():
                    pv[key] = val
                row['P_value'] = '; '.join(f"{k}={v}" for k, v in pv.items())
            else:
                row['P_value'] = ''

        self.results['table1'] = pd.DataFrame(rows)
        return self.results['table1']

    def run_selected(self, cb=None, run_cox=True, run_psm=True,
                     run_interaction=True, run_subgroup=True, run_sensitivity=True,
                     run_competing_risks=True):
        """선택된 분석만 실행 — 체크박스 상태를 그대로 반영"""
        raw, info = self._load_data()
        df_prepared = self._prepare(raw)
        logger.info(f"분석 데이터 준비 완료: {len(df_prepared):,}건, "
                   f"{df_prepared.memory_usage(deep=True).sum() / 1024**2:.1f}MB")

        from gpu_accelerator import get_gpu_status
        gpu_info = get_gpu_status()
        if gpu_info['gpu_enabled']:
            logger.info(f"GPU 가속 활성화 — cupy: {gpu_info['cupy_available']}, "
                       f"torch CUDA: {gpu_info['torch_cuda']}")
            if cb: cb("GPU 가속 모드로 분석을 실행합니다.")

        # Table 1은 항상 생성
        self.generate_table1(cb, df_prepared)
        mem_manager.cleanup_after_step('table1')

        if run_cox:
            for oc in ['dementia_event', 'ad_event', 'vad_event']:
                self.run_cox(oc, cb, df_prepared)
                mem_manager.cleanup_after_step(f'cox_{oc}')

        if run_psm:
            self.run_psm(cb, df_prepared)
            mem_manager.cleanup_after_step('psm')

        if run_interaction:
            self.run_interaction(cb, df_prepared)
            mem_manager.cleanup_after_step('interaction')

        if run_subgroup:
            self.run_subgroup(cb, df_prepared)
            mem_manager.cleanup_after_step('subgroup')

        if run_competing_risks:
            self.run_competing_risks(cb, df_prepared)
            mem_manager.cleanup_after_step('competing_risks')

        del df_prepared
        gc.collect()

        if run_sensitivity:
            self.run_sensitivity(cb)
            mem_manager.cleanup_after_step('sensitivity')

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
