#!/usr/bin/env python3
"""
Phase 2 서브그룹 분석 예시: T2DM_OHA의 약물전환(med_switch) 효과

T2DM_OHA 환자를 약물전환 여부로 분층화하여 Cox 비례위험 모델 적용.
LRT 기반 상호작용 p-value로 약물전환과 치매 위험의 연관성 검정.

⚠️ 주의:
- had_insulin_switch는 index_date 이후 변수 → immortal time bias 위험
- 이 분석은 서술적(descriptive) 목적으로만 사용 가능
- 인과 효과 추정에는 사용 불가 (시변공변량 또는 랜드마크 분석 필요)

실행: python3 phase2_subgroup_analysis_example.py
"""

from db_connector import DataManager
from cohort_builder import CohortBuilder
from statistical_analysis import StatisticalAnalyzer
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def run_phase2_subgroup_analysis(db_path: str):
    """
    Phase 2 서브그룹 분석 실행.

    T2DM_OHA 환자를 약물전환(had_insulin_switch) 여부로 분층화.
    각 분층에서 Cox 모델을 실행하고 상호작용 p-value 계산.
    """
    dm = DataManager(db_path)
    cb = CohortBuilder(dm)

    logger.info("Step 1-6: 코호트 구성 중...")
    cb.step1_base_population()
    cb.step2_dm_claims()
    cb.step3_dm_medications()
    cb.step4_classify_groups(lookback_days=90)
    cb.step5_exclude_dementia()
    cb.step6_outcomes()

    logger.info("데이터 통합 및 분석 준비 중...")
    sa = StatisticalAnalyzer(dm)
    raw, _ = sa._load_data()
    prepared = sa._prepare(raw)

    logger.info("\n" + "=" * 80)
    logger.info("Phase 2: T2DM_OHA 약물전환 서브그룹 분석")
    logger.info("=" * 80)

    # T2DM_OHA 환자 현황
    t2dm_oha = prepared[prepared['exposure_group'] == 'T2DM_OHA']
    logger.info(f"\nT2DM_OHA 환자 수: {len(t2dm_oha):,}명")

    if len(t2dm_oha) > 0:
        n_switched = (t2dm_oha['had_insulin_switch'] == 1).sum()
        n_not_switched = (t2dm_oha['had_insulin_switch'] == 0).sum()
        logger.info(f"  - 약물전환: {n_switched}명 ({n_switched / len(t2dm_oha) * 100:.1f}%)")
        logger.info(f"  - 미전환: {n_not_switched}명 ({n_not_switched / len(t2dm_oha) * 100:.1f}%)")

        # 약물전환 분층별 기초 통계
        logger.info("\n약물전환 분층별 기초 특성:")
        logger.info("-" * 80)

        for switch_status, group_label in [(0, "미전환"), (1, "전환")]:
            sg = t2dm_oha[t2dm_oha['had_insulin_switch'] == switch_status]
            n_events = sg['dementia_event'].sum()
            fu_median = sg['follow_up_years'].median()

            logger.info(f"{group_label} ({len(sg):,}명):")
            logger.info(f"  - 치매 이벤트: {int(n_events)}명 ({n_events / len(sg) * 100:.1f}%)")
            logger.info(f"  - 추적 기간(중앙값): {fu_median:.1f}년")

            if 'age_at_index' in sg.columns:
                age_mean = sg['age_at_index'].mean()
                logger.info(f"  - 평균 나이: {age_mean:.1f}세")
            if 'cci_score' in sg.columns:
                cci_mean = sg['cci_score'].mean()
                logger.info(f"  - CCI 점수(평균): {cci_mean:.1f}")

    # 하위그룹 분석 실행
    logger.info("\n하위그룹 분석 실행 중...")
    try:
        sg_results = sa.run_subgroup(df_prepared=prepared)

        if sg_results:
            logger.info("\n" + "=" * 80)
            logger.info("서브그룹 분석 결과")
            logger.info("=" * 80)

            # T2DM_OHA 약물전환 서브그룹 결과 추출 및 출력
            med_switch_subgroups = {
                'noswitch': 't2dm_oha_noswitch',
                'switch': 't2dm_oha_switch'
            }

            results_table = []
            for label, sg_key in med_switch_subgroups.items():
                if sg_key in sg_results:
                    sg = sg_results[sg_key]
                    n = sg.get('n', 0)
                    events = sg.get('events', 0)

                    if n > 0:
                        results_table.append({
                            'subgroup': f"T2DM_OHA_{label}",
                            'n': f"{n:,}",
                            'events': f"{events}",
                            'event_rate': f"{events / n * 100:.1f}%"
                        })

            # 결과 테이블 출력
            if results_table:
                logger.info("\nT2DM_OHA 약물전환 분층별 이벤트 발생률:")
                logger.info("-" * 80)
                logger.info(f"{'Subgroup':<25} {'N':>10} {'Events':>10} {'Event Rate':>15}")
                logger.info("-" * 80)
                for row in results_table:
                    logger.info(f"{row['subgroup']:<25} {row['n']:>10} {row['events']:>10} {row['event_rate']:>15}")
                logger.info("-" * 80)

            # 상호작용 p-value 확인 (각 서브그룹에서 추출)
            for sg_key in med_switch_subgroups.values():
                if sg_key in sg_results:
                    p_int = sg_results[sg_key].get('p_interaction')
                    if p_int is not None:
                        logger.info(f"\n약물전환 × 치매 상호작용 p-value: {p_int:.6f}")
                        break

            logger.info("\n" + "=" * 80)
            logger.info("해석:")
            logger.info("-" * 80)
            logger.info("• 두 분층의 이벤트 발생률이 유사 → 약물전환이 결과에 영향 없음 (가능성)")
            logger.info("• 전환군의 이벤트 발생률이 높음 → 질병 진행도 반영 가능 (인과성 아님)")
            logger.info("• 전환군의 이벤트 발생률이 낮음 → immortal time bias 위험")
            logger.info("\n⚠️ 주의: 약물전환은 index_date 이후 변수로 인과 효과를 나타내지 않습니다.")
            logger.info("         임상적 의미는 '질병 악화에 따른 치료 강화'를 반영할 수 있습니다.")
            logger.info("=" * 80)

    except Exception:
        logger.exception("서브그룹 분석 실패")


if __name__ == '__main__':
    import sys

    # 사용: python3 phase2_subgroup_analysis_example.py [db_path]
    db_path = sys.argv[1] if len(sys.argv) > 1 else ':memory:'

    logger.info(f"데이터베이스: {db_path}")
    run_phase2_subgroup_analysis(db_path)
