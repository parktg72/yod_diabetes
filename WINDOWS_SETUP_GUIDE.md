# Windows Python 3.12 설치 및 Phase 2 실행 가이드

**환경**: Windows OS + Python 3.12  
**인코딩**: UTF-8 + CRLF (Windows CMD 호환)  
**작성일**: 2026-04-19

---

## 1. 사전 요구사항 확인

### ✓ Python 3.12 설치
```bash
# Windows CMD에서 확인
python --version
```
- **필요**: Python 3.12.x (3.12.0 이상)
- **다운로드**: https://www.python.org/downloads/
- **설치 시**: "Add Python to PATH" 체크박스 반드시 선택

### ✓ 디스크 공간
- Python venv: ~2GB
- 패키지 설치: ~3GB
- **총 최소**: 6GB

### ✓ 네트워크 접근
- pip 패키지 다운로드 (인터넷 필수)
- 회사 네트워크: 프록시 설정 필요할 수 있음

---

## 2. 설치 단계

### Step 1: 간편 설치 (권장)
```batch
# Windows 탐색기에서:
# 1) 프로젝트 폴더로 이동
# 2) setup.bat 더블클릭
# 3) 화면의 지시 따르기
```

또는 CMD에서:
```batch
cd C:\path\to\yod_diabetes_app
setup.bat
```

**예상 시간**: 5-10분 (네트워크 속도에 따라 다름)

### Step 2: 설치 확인
```batch
# 가상환경 활성화
call venv\Scripts\activate.bat

# 필수 패키지 확인
python -c "import pandas, lifelines, matplotlib; print('OK')"
```

---

## 3. Phase 2 폐쇄망 실행

### 기본 실행
```batch
# 프로젝트 폴더에서:
phase2_run.bat

# 기본값 사용:
# - DB: nhis_analysis.duckdb
# - 출력: phase2_output/
```

### 커스텀 경로 지정
```batch
# 사용 형식:
phase2_run.bat [db_경로] [출력_디렉토리]

# 예시:
phase2_run.bat "C:\NHIS\nhis_data.duckdb" "C:\Reports\Phase2_2026"
phase2_run.bat :memory: phase2_output  # 테스트용
```

### 출력 파일
```
phase2_output/
├── phase2_report_YYYYMMDD_HHMMSS.log      # 실행 로그
├── km_t2dm_oha_switch.png                 # KM 생존곡선
├── forest_t2dm_oha_switch.png             # Forest plot
├── table_baseline.csv                     # 기초 특성 표
├── table_cox_results.csv                  # Cox 결과 표
└── INTERPRETATION_GUIDE.md                # 임상 해석 가이드
```

---

## 4. 패키지 상태 확인

### 설치된 주요 패키지 (Phase 2 필수)

| 패키지 | 버전 | 용도 |
|--------|------|------|
| pandas | ≥2.2.0 | 데이터 조작 |
| numpy | ≥1.26.4 | 수치 계산 |
| scipy | ≥1.13.0 | 통계 함수 |
| lifelines | ≥0.28.0 | **KM 곡선, Cox 모델** |
| matplotlib | ≥3.8.4 | **시각화** |
| scikit-learn | ≥1.4.0 | 머신러닝 유틸 |
| duckdb | ≥1.0.0 | 데이터베이스 |

### 패키지 수동 확인
```batch
call venv\Scripts\activate.bat
pip list | findstr "pandas lifelines matplotlib scipy"
```

---

## 5. 인코딩 상태 확인

### 파일 인코딩 (Windows CMD 호환)

✅ **UTF-8 + CRLF 적용 완료**:
- `requirements.txt` — UTF-8 + CRLF
- `build.bat` — UTF-8 + CRLF + BOM
- `setup.bat` — UTF-8 + CRLF
- `run.bat` — UTF-8 + CRLF
- `phase2_run.bat` — UTF-8 + CRLF

✅ **Python 소스 파일**:
- `phase2_*.py` — UTF-8, pathlib 사용, `encoding='utf-8'` 명시

### Windows CMD 한글 표시 확인
```batch
@echo off
chcp 65001 > nul
echo [테스트] 한글이 정상 표시되어야 합니다.
```

---

## 6. 문제 해결

### 가상환경 활성화 실패
```batch
# 가상환경 재생성
rmdir /s /q venv
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
```

### 패키지 설치 실패
```batch
# 캐시 비우고 재설치
pip install --no-cache-dir -r requirements.txt

# 회사 프록시 설정 (필요 시)
pip install -r requirements.txt ^
  --index-url http://내부미러/simple
```

### Phase 2 실행 오류
```batch
# 로그 파일 확인
# phase2_output/phase2_report_*.log 파일 내용 확인

# 가상환경이 활성화되어 있는지 확인
where python  # venv\Scripts\python.exe 경로 표시되어야 함

# 수동 실행으로 상세 오류 확인
call venv\Scripts\activate.bat
python phase2_final_report.py nhis_analysis.duckdb phase2_output
```

### 메모리 부족
```batch
# 다른 프로그램 종료 후 재실행
# 또는 배경 작업 중 실행 가능

# 진행 상황 모니터링
# phase2_report_*.log 실시간 확인
```

---

## 7. 성능 최적화

### 병렬 처리 활성화 (선택)
```batch
# 윈도우 작업 관리자에서 CPU 코어 수 확인
# Phase 2는 자동으로 최대 코어 사용
```

### 대용량 데이터 처리
```batch
# 메모리 제한 설정 (GB 단위)
# 환경변수 설정 후 phase2_run.bat 실행
set PYTHONHASHSEED=0
phase2_run.bat
```

---

## 8. 폐쇄망 배포 체크리스트

### 필수 파일
- [ ] `requirements.txt` (UTF-8 + CRLF)
- [ ] `build.bat` (UTF-8 + CRLF + BOM)
- [ ] `setup.bat` (UTF-8 + CRLF)
- [ ] `phase2_run.bat` (UTF-8 + CRLF)
- [ ] `phase2_final_report.py` (UTF-8)
- [ ] `phase2_visualization.py` (UTF-8)
- [ ] `statistical_analysis.py` (UTF-8)
- [ ] 기타 Python 모듈들

### 실행 명령어
```batch
REM Step 1: 설치 (처음 1회만)
setup.bat

REM Step 2: Phase 2 실행
phase2_run.bat nhis_analysis.duckdb phase2_output
```

### 결과 검증
- [ ] `phase2_output/` 디렉토리 생성됨
- [ ] `.log` 파일에 "[SUCCESS]" 메시지 있음
- [ ] PNG 파일 (KM, forest) 생성됨
- [ ] CSV 파일 (baseline, cox) 생성됨
- [ ] INTERPRETATION_GUIDE.md 생성됨

---

## 9. 추가 정보

### Phase 2 코드 구조
```
phase2_visualization.py      # KM curves, tables
  ├── Phase2Visualizer class
  ├── plot_km_curves()
  ├── plot_forest_plot()
  ├── create_baseline_table()
  └── create_cox_results_table()

phase2_final_report.py       # 통합 오케스트레이션
  ├── CohortBuilder steps 1-6
  ├── StatisticalAnalyzer._prepare()
  ├── StatisticalAnalyzer.run_subgroup()
  └── Phase2Visualizer 메서드 호출

statistical_analysis.py      # Phase 2 변수 추가
  ├── baseline_has_insulin (int8)
  ├── had_insulin_switch (int8)
  └── days_to_switch (Int64 nullable)
```

### 임상 해석
- **KM 곡선**: T2DM_OHA 환자의 약물전환 여부별 치매 위험
- **Forest plot**: 서브그룹별 HR 비교
- **Baseline table**: 약물전환 그룹의 특성 차이
- **Cox results**: 다변량 분석 결과

---

## 10. 지원 연락처

- **문제 보고**: 로그 파일 (`phase2_report_*.log`) 첨부
- **데이터 검증**: `phase2_output/INTERPRETATION_GUIDE.md` 검토

---

**마지막 업데이트**: 2026-04-19  
**상태**: ✅ Windows Python 3.12 호환성 확인 완료
