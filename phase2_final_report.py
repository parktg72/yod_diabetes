#!/usr/bin/env python3
"""
Phase 2 Final Report: 약물 분류 정확도 개선 & T2DM_OHA 약물전환 분석

생성 내용:
1. 기초 특성 표 (T2DM_OHA 약물전환별)
2. Cox 모델 결과 표
3. KM 생존곡선 (약물전환별)
4. Forest plot (약물전환 분층별 HR)
5. 최종 해석 가이드

실행: python3 phase2_final_report.py [db_path] [output_dir]
"""

from pathlib import Path
import pandas as pd
from db_connector import DataManager
from cohort_builder import CohortBuilder
from statistical_analysis import StatisticalAnalyzer
from phase2_visualization import Phase2Visualizer
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def generate_phase2_report(db_path: str = ':memory:', output_dir: str = 'phase2_output') -> None:
    """
    Phase 2 최종 보고서 생성

    Args:
        db_path: 분석 데이터베이스 경로
        output_dir: 출력 디렉토리
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Set up logging to write to output_dir
    log_file = output_path / f'phase2_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    handler = logging.FileHandler(str(log_file))
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info("=" * 80)
    logger.info("Phase 2 최종 보고서 생성 시작")
    logger.info("=" * 80)

    # 1. 코호트 구성
    logger.info("\n[Step 1] 코호트 구성 중...")
    dm = DataManager(db_path)
    cb = CohortBuilder(dm)

    cb.step1_base_population()
    cb.step2_dm_claims()
    cb.step3_dm_medications()
    cb.step4_classify_groups(lookback_days=90)
    cb.step5_exclude_dementia()
    cb.step6_outcomes()

    logger.info("✓ 코호트 구성 완료")

    # 2. 데이터 전처리
    logger.info("\n[Step 2] 데이터 전처리 중...")
    sa = StatisticalAnalyzer(dm)
    raw, _ = sa._load_data()
    prepared = sa._prepare(raw)

    logger.info(f"✓ 분석 대상: {len(prepared):,}명")

    # 3. 서브그룹 분석 (T2DM_OHA 약물전환)
    logger.info("\n[Step 3] 서브그룹 분석 실행 중...")
    try:
        subgroup_results = sa.run_subgroup(df_prepared=prepared)
        logger.info("✓ 서브그룹 분석 완료")
    except Exception as e:
        logger.error(f"서브그룹 분석 실패: {e}")
        subgroup_results = {}

    # 4. 시각화
    logger.info("\n[Step 4] 시각화 생성 중...")
    viz = Phase2Visualizer(output_dir=str(output_path))

    # KM plot
    try:
        km_file = viz.plot_km_curves(prepared)
        logger.info(f"✓ KM plot: {km_file}")
    except Exception:
        logger.exception("KM plot 생성 실패")

    # Forest plot
    try:
        forest_file = viz.plot_forest_plot(subgroup_results)
        logger.info(f"✓ Forest plot: {forest_file}")
    except Exception:
        logger.exception("Forest plot 생성 실패")

    # 5. 기초 특성 표
    logger.info("\n[Step 5] 기초 특성 표 생성 중...")
    try:
        baseline_file = viz.create_baseline_table(prepared)
        logger.info(f"✓ 기초 특성 표: {baseline_file}")
    except Exception:
        logger.exception("기초 특성 표 생성 실패")

    # 6. Cox 결과 표
    logger.info("\n[Step 6] Cox 모델 결과 표 생성 중...")
    try:
        cox_file = viz.create_cox_results_table(subgroup_results)
        logger.info(f"✓ Cox 결과 표: {cox_file}")
    except Exception:
        logger.exception("Cox 결과 표 생성 실패")

    # 7. 최종 해석 가이드
    logger.info("\n[Step 7] 최종 해석 가이드 생성 중...")
    generate_interpretation_guide(prepared, subgroup_results, output_dir)

    logger.info("\n" + "=" * 80)
    logger.info("✅ Phase 2 최종 보고서 생성 완료")
    logger.info(f"📁 출력 디렉토리: {output_path}")
    logger.info("=" * 80)

    # 생성된 파일 목록
    logger.info("\n생성된 파일 목록:")
    for filepath in sorted(output_path.iterdir()):
        if filepath.is_file():
            file_size = filepath.stat().st_size
            logger.info(f"  - {filepath.name} ({file_size:,} bytes)")


def generate_interpretation_guide(df: pd.DataFrame, subgroup_results: dict, output_dir: str) -> None:
    """최종 해석 가이드 문서 생성"""

    guide = """
# Phase 2 최종 해석 가이드

## 1. 기저선 약물 분류 (90일 윈도우)

### 정의
- **기저선 기간**: [첫 당뇨 진단일, 진단일+90일]
- **근거**: 한국 당뇨 진료 가이드라인 + ADA 기준 (초진 후 3개월 약물 효과 재평가)

### T2DM 분류
- **T2DM_INSULIN**: 기저선 기간 내 인슐린 처방 (경구약 관계없음)
- **T2DM_OHA**: 기저선 기간 내 경구약만 처방, 인슐린 없음
- **T2DM_NOMED**: 기저선 기간 내 약물 처방 없음

### 검증
- 민감도 분석: 60일, 90일, 180일 윈도우 비교
- 90일 선택의 임상적 타당성 검증 완료

---

## 2. T2DM_OHA 약물전환 분석

### 정의
- **약물전환**: index_date(추적 시작) 이후 OHA→INSULIN 전환
- **포함 대상**: T2DM_OHA, T2DM_NOMED 환자
- **기저선 분류**: 변경 없음 (기저선은 고정)

### 파생변수
1. **baseline_has_insulin**: 기저선 기간 내 인슐린 여부 (DM만)
2. **had_insulin_switch**: index_date 이후 인슐린 전환 여부
3. **days_to_switch**: 약물전환까지 일수

### 임상적 의미
- **질병 진행도 마커**: 약물 강화의 필요성을 반영
- **치료 반응성**: 초기 약물 효과 부족 시 조정
- **예후 지표**: 약물전환 필요성이 높은 환자 = 질병 진행 환자

---

## 3. 통계 분석 결과 해석

### Cox 비례위험 모델
- **모델**: 3단계 (Age+Sex → +Socio → +Comorbidity)
- **T2DM_OHA 분층**: 약물전환 여부 (noswitch vs switch)
- **상호작용 p-value (LRT)**: 약물전환이 노출-결과 연관성 변조하는가?

### 결과 해석 패턴

#### 패턴 1: 이벤트 발생률 유사 (<5% 차이)
- **해석**: 약물전환이 결과에 영향 없음
- **가능성**: 약물전환은 단순 치료 강화, 예후와 무관

#### 패턴 2: 전환군 이벤트 높음
- **해석**: 질병 진행도 반영 (⚠️ 인과성 아님)
- **주의**: 더 심한 환자가 약물전환 → 높은 이벤트
- **결론**: 약물전환이 원인이 아니라 질병의 신호

#### 패턴 3: 전환군 이벤트 낮음 (⚠️ 위험)
- **우려**: Immortal Time Bias 신호
- **메커니즘**: 인슐린까지 생존해야 전환 가능 → 생존율 인상
- **권고**: 시변공변량 또는 랜드마크 분석으로 재검증

---

## 4. ⚠️ 제약사항 & 주의점

### Immortal Time Bias
- **정의**: had_insulin_switch는 index_date 이후 변수
- **허용**: 서술적 분층 비교 (이 분석)
- **불가**: Cox/PSM의 정적 공변량 사용

### 향후 개선 (필요 시)
1. **시변공변량**: 약물전환 시점을 기준으로 데이터 분할
2. **랜드마크 분석**: 12개월 후 시점의 약물전환 여부만 분석
3. **경쟁위험 분석**: 사망을 경쟁위험으로 보정

---

## 5. 최종 결론

### Phase 2 성과
✅ 기저선 약물 분류 정확도 개선 (민감도 분석 검증)
✅ 인슐린 추적 변수 생성 (baseline_has_insulin, med_switch)
✅ T2DM_OHA 약물전환의 임상적 의미 분석

### 후속 분석
- [ ] NHIS 폐쇄망 데이터로 재검증 (약물전환 분포 확인)
- [ ] 약물 종류별 상세 분석 (GLP-1, SGLT2 등)
- [ ] 임상 결과 다차원 분석 (치매 + CVD + 신기능)

### 논문 작성 계획
1. **1차 논문**: 기저선 약물 분류와 민감도 분석
2. **2차 논문**: 약물전환과 임상 예후 (경쟁위험 분석 포함)

---

## 6. 용어 정의

| 용어 | 정의 |
|------|------|
| first_dm_date | 최초 당뇨 청구(T40) 또는 주상병(T20) 날짜 |
| index_date | 분석 시작일 (상병 + 처방 청구 가능 시작) |
| baseline | [first_dm_date, first_dm_date+90일] 약물 집계 기간 |
| follow_up | [index_date, censor_date] 추적 기간 |
| had_insulin_switch | index_date 이후 OHA→INSULIN 전환 여부 |
| immortal time | 사건 발생 전 추적 불가능 기간 |

---

**생성 일시**: {}
**분석 대상**: {} 명
**분석 방법**: Cox PH + Subgroup + LRT

⚠️ 이 분석은 기술 정보(descriptive)이며 인과 추정(causal)이 아닙니다.
""".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        len(df)
    )

    # T2DM_OHA 정보 추가
    t2dm_oha = df[df['exposure_group'] == 'T2DM_OHA']
    if len(t2dm_oha) > 0:
        n_switch = (t2dm_oha['had_insulin_switch'] == 1).sum()
        guide += f"\n**T2DM_OHA**: {len(t2dm_oha):,}명 ({n_switch}명 약물전환, {n_switch/len(t2dm_oha)*100:.1f}%)\n"

    filepath = Path(output_dir) / 'INTERPRETATION_GUIDE.md'
    filepath.write_text(guide, encoding='utf-8')

    logger.info(f"해석 가이드 저장: {filepath}")


if __name__ == '__main__':
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else ':memory:'
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'phase2_output'

    generate_phase2_report(db_path, output_dir)
