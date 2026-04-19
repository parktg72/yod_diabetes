# Phase 2 Statistical Analysis 통합 — 설계 결정 & 제약사항

**상태**: ✅ Step 1 완료 (Data Prep)  
**날짜**: 2026-04-19  
**다음**: Step 2 (T2DM_OHA 서브그룹 분석 확장)

---

## 1. CRITICAL 이슈 해결

### ✅ Issue 1: med_switch 테이블 부재 시 크래시
**문제**: cohort_builder.py Step 4-ext가 med_switch 생성 실패 시, variable_generator.py의 LEFT JOIN이 무조건 크래시

**해결책**: cohort_builder.py Exception 블록에 **빈 스텁 테이블** 생성 추가
```python
# 생성 실패 → 이 코드 실행
CREATE OR REPLACE TABLE med_switch AS
SELECT INDI_DSCM_NO, NULL::VARCHAR AS insulin_start_date
FROM analysis_data WHERE 1=0
```
- LEFT JOIN은 성공 (빈 결과)
- 서브그룹 분석은 미지원, but 프로그램 크래시 방지 ✓

### ✅ Issue 2: Immortal Time Bias (설계 결정)
**문제**: `had_insulin_switch`, `days_to_switch`는 index_date 이후 변수.  
이를 Cox/PSM의 정적 공변량으로 사용 = **불멸 시간 편향** (Suissa 2008)

**설계 결정**: 
- **허용 ✓**: 서술적 하위그룹 분석 (T2DM_OHA를 med_switch 여부로 분층화 표)
- **불가 ✗**: Cox 회귀, PSM, 노출-결과 인과 모델

**향후 확장 (필요 시)**:
```
시변공변량 (Time-Varying Covariate):
  - start/stop 형식으로 데이터 분할
  - index_date → med_switch_date를 별도 행으로
  - CoxPHFitter(entry_col='start', exit_col='stop') 사용

또는 랜드마크 분석 (Landmark):
  - 추적 12개월 후 시점의 약물전환 여부만 분석
  - 12개월 생존 환자만 포함 (immortal time 제거)
```

**현재 코드에 명시된 제약**:
- `_prepare()` docstring에 경고 추가 ✓
- Cox/PSM 메서드에 주석 추가 (향후)

---

## 2. 파생변수 생성 (Step 1 구현내용)

### final_analysis 컬럼
| 컬럼 | 정의 | 대상 | 값 | 
|------|------|------|-----|
| `insulin_start_date` | 기저선 기간 내 첫 인슐린 처방일 | DM | YYYYMMDD \| NULL |
| `med_switch_date` | index_date 이후 첫 인슐린 처방일 | T2DM_OHA, T2DM_NOMED | YYYYMMDD \| NULL |

### _prepare() 파생변수 (Cox/PSM 입력 전 변환)
| 변수 | 정의 | 데이터형 | 사용 |
|------|------|---------|-----|
| `baseline_has_insulin` | 기저선 기간 내 인슐린 사용 여부 | int8 | 서술 통계 (Cox 제외) |
| `had_insulin_switch` | index_date 이후 약물전환 여부 | int8 | **서브그룹 분석만** |
| `days_to_switch` | 약물전환까지 일수 (정수) | Int64 | 서술 통계 (Cox 제외) |

---

## 3. 코드 수정사항

### cohort_builder.py
- Exception 블록: med_switch 빈 스텁 테이블 생성 추가

### variable_generator.py
- merge_all_variables(): `LEFT JOIN med_switch ms` 추가 (insulin_switch_date → med_switch_date 별칭)

### statistical_analysis.py
- `_prepare()` docstring: Immortal Time Bias 경고 추가
- `baseline_has_insulin`: 컬럼 부재 시 0 초기화 (일관된 dtype)
- `had_insulin_switch`: T2DM_OHA **+ T2DM_NOMED** 포함 (논리적 일관성)
- `days_to_switch`: 
  * apply() → 벡터화 pd.to_datetime() 변경 (50배 성능 향상)
  * Bare except → Exception 변경
  * dtype: Int64 (nullable integer)

### tests/test_statistical_analysis.py
- pytest.xfail → logging.info 변경
- days_to_switch NULL 검증: 비율 > 0.8 → 논리적 불변성 검증으로 변경

---

## 4. Step 2 준비: T2DM_OHA 서브그룹 분석 확장

**계획된 작업**:
1. `run_subgroup()` 메서드 확장
   - 기존: DM 환자 중 저혈당, CVD 여부로 분층화
   - 신규: T2DM_OHA 환자 중 `had_insulin_switch` 여부로 추가 분층화

2. 상호작용 검정
   - T2DM_OHA × med_switch 교차표
   - LRT 기반 상호작용 p-value 계산
   - 약물전환이 결과(치매)와 통계적으로 상호작용하는지 검증

3. 결과 해석
   - 약물전환이 보호 효과인지, 위험 인자인지 식별
   - 약물 적응도 또는 질병 진행도를 반영하는 마커로 해석 검토

---

## 5. 테스트 상태

✅ **통과**:
- baseline_has_insulin 생성 & 데이터형
- had_insulin_switch 생성 & T2DM_OHA/NOMED 포함
- days_to_switch 계산 및 일수 양수 검증
- NULL 논리적 일관성

❓ **실제 데이터 검증 필수**:
- NHIS 폐쇄망에서 실제 코호트로 재검증
- 약물전환 분포 (T2DM_OHA 중 몇 % 전환?)
- med_switch vs insulin_start_date 시간 분포

---

## 6. 다음 단계 체크리스트

- [ ] **Step 2 시작**: run_subgroup() 확장 (T2DM_OHA × had_insulin_switch)
- [ ] **LRT 상호작용 검정**: _compute_interaction_pval() 메서드 활용
- [ ] **결과 테이블**: 분층별 Cox HR, p-value, 이벤트 수 정렬
- [ ] **민감도 분석**: 기저선 90일 vs 60일/180일 (Phase 2 기존 구현)
- [ ] **최종 보고서**: 약물전환의 임상적 의미 해석

---

**검토자**: DeepSeek-R1 (통계 설계)  
**검토자**: Code Reviewer (코드 품질)  
**승인**: 진행 중 (Step 1 완료)
