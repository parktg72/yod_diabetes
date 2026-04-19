# Phase 2 Statistical Analysis — Step 2: T2DM_OHA 서브그룹 분석 (완료)

**상태**: ✅ Step 2 완료  
**날짜**: 2026-04-19  
**다음**: Step 3 (결과 시각화 및 보고서 작성)

---

## 1. Step 2 구현 내용

### 1.1 T2DM_OHA 약물전환 분층화
**파일**: `statistical_analysis.py` (run_subgroup 메서드)

**추가 기능**:
- T2DM_OHA 환자를 `had_insulin_switch` 여부로 이분화
- `subgroups['t2dm_oha_noswitch']`: 약물전환 미유무
- `subgroups['t2dm_oha_switch']`: 약물전환 유무
- 각 서브그룹별 Cox PH 모델 적용

**코드**:
```python
# Phase 2: T2DM_OHA 약물전환 서브그룹
if 'had_insulin_switch' in df.columns:
    is_t2dm_oha = df['exposure_group'] == 'T2DM_OHA'
    subgroups['t2dm_oha_noswitch'] = is_t2dm_oha & (df['had_insulin_switch'] == 0)
    subgroups['t2dm_oha_switch'] = is_t2dm_oha & (df['had_insulin_switch'] == 1)
```

### 1.2 LRT 기반 상호작용 검정
**기존 기능 활용**:
- `_compute_interaction_pval()`: 노출군 × 서브그룹 간 상호작용 p-value 계산
- 방법: Likelihood Ratio Test (χ² = 2 × (LL_full − LL_base))
- Bonferroni 보정: 다중 검정 오정률 제어

**Phase 2 적용**:
- T2DM_OHA × med_switch 상호작용 p-value 계산
- 약물전환이 노출군(T1DM, T2DM_OHA, T2DM_INSULIN, T2DM_NOMED) 효과에 미치는 영향 검정

### 1.3 결과 해석 가능성
**출력 내용**:
- T2DM_OHA 분층별 (전환/미전환)
  * 환자 수 (n)
  * 치매 이벤트 수
  * HR 및 95% CI
  * 상호작용 p-value

**임상적 해석**:
1. **이벤트 발생률 유사** → 약물전환이 결과에 영향 없음 가능성
2. **전환군 이벤트 높음** → 질병 진행도를 반영 (인과성 아님)
3. **전환군 이벤트 낮음** → Immortal Time Bias 위험 신호

---

## 2. 제약사항 & 주의점

### ⚠️ Immortal Time Bias
- `had_insulin_switch`는 정의상 index_date > 이후의 변수
- **허용**: 서술적 분층 비교 (이 분석)
- **불가**: 인과 효과 추정 (Cox 공변량)

### 📋 향후 개선 (필요 시)
**시변공변량 (Time-Varying Covariate)**:
```python
# 데이터 구조: (person_id, start_time, stop_time, exposure, outcome)
# index_date → med_switch_date 구간을 별도 행으로 분할
# CoxPHFitter(entry_col='start_time', exit_col='stop_time')
```

**랜드마크 분석 (Landmark)**:
```python
# 추적 12개월 후 시점의 약물전환 여부만 분석
# 12개월 생존자만 포함 (immortal time 제거)
```

---

## 3. 코드 변경사항

### statistical_analysis.py
**run_subgroup() 메서드**:
1. T2DM_OHA 약물전환 서브그룹 추가 (라인 828-841)
2. `_sg_parent`, `_sg_indicators` 업데이트 (라인 856-859)
3. df_int 컬럼 리스트에 had_insulin_switch 포함 (라인 922-927)

**변경 전후 비교**:
| 항목 | Before | After |
|------|--------|-------|
| 서브그룹 수 | ~12개 (성별, 연령, 소득, BMI, CCI, 합병증 등) | ~14개 (+ T2DM_OHA 약물전환) |
| 상호작용 검정 | 기존 하위그룹만 | + med_switch 추가 |
| 약물전환 분석 | None | 추가 |

### tests/test_statistical_analysis.py
**신규 테스트**:
```python
test_run_subgroup_includes_med_switch()
  - run_subgroup() 실행 확인
  - T2DM_OHA 약물전환 서브그룹 생성 검증
  - 이벤트 발생률 기본 통계
```

### phase2_subgroup_analysis_example.py (신규)
**예시 스크립트**:
- T2DM_OHA 분층별 기초 통계 계산
- 치매 이벤트 발생률 비교
- 상호작용 p-value 해석 가이드

---

## 4. 테스트 상태

✅ **통과**:
- Phase 2 서브그룹 추가 로직 (t2dm_oha_noswitch, t2dm_oha_switch)
- LRT 상호작용 계산 (기존 _compute_interaction_pval 활용)
- Bonferroni 보정 (기존 로직 유지)

⏳ **실제 데이터 검증 필수**:
- NHIS 폐쇄망 코호트에서 약물전환 분포 확인
- 약물전환 분층별 기초 특성 (연령, CCI, 합병증) 확인
- 상호작용 p-value의 임상적 의미

---

## 5. 다음 단계 (Step 3)

### 결과 시각화
- [ ] KM plot: T2DM_OHA 약물전환별 생존곡선
- [ ] Forest plot: 약물전환 분층별 HR 및 95% CI
- [ ] 상호작용 그래프: 노출군 × 약물전환 2-way interaction

### 보고서 작성
- [ ] Tables: 분층별 기초 특성, Cox 결과, 상호작용 p-value
- [ ] Figures: KM, Forest, Interaction plots
- [ ] 결론: 약물전환의 임상적 의미 해석

### Competing Risk 분석 (선택)
- [ ] Fine-Gray 모델: 사망 경쟁위험 보정
- [ ] 약물전환과 사망의 연관성 검토

---

## 6. 주요 학습포인트

### 통계적
1. **Immortal Time Bias**: 시간 영점에 대한 명확한 정의 필수
2. **LRT 기반 상호작용**: 노출-하위그룹 간 효과 이질성 검정
3. **Bonferroni 보정**: 다중 검정 오정률 제어의 필요성

### 임상적
1. **약물 적응도 마커**: 약물전환은 질병 진행의 반영일 수 있음
2. **기술 vs 인과**: "약물전환이 결과를 낮춘다" ≠ "인슐린이 보호한다"
3. **시간 구조**: 기저선 약물 vs 추적 중 약물전환 구분

---

## 7. 코드 검증 체크리스트

- [x] run_subgroup()에 T2DM_OHA 약물전환 분층 추가
- [x] _sg_parent, _sg_indicators 업데이트
- [x] df_int에 had_insulin_switch 포함
- [x] LRT 상호작용 계산 (기존 로직)
- [x] Bonferroni 보정 (기존 로직)
- [x] 테스트 추가
- [x] 예시 스크립트 (phase2_subgroup_analysis_example.py)
- [x] 제약사항 문서화 (Immortal Time Bias)

---

**상태**: ✅ Step 1-2 완료  
**검수자**: Code Reviewer (Step 2 구현)  
**다음 리뷰**: Step 3 시작 전 코드 리뷰
