# Stage U: Windows 빌드 호환성 수정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codex/Gemini/opencode 종합 리뷰에서 발견된 Windows/Python 3.12 빌드 호환성 문제(Critical 2건, Important 2건, Minor 2건)를 수정해 build.bat가 모든 Windows 환경에서 안정적으로 동작하도록 한다.

**Architecture:** Task 1(Critical: requirements.txt 플랫폼 마커 + build.bat chcp 위치) → Task 2(Important: 삭제된 scipy/sklearn 내부 모듈 hidden-import 정리) → Task 3(Minor: .gitattributes 커밋 + visualization.py 폰트 경고) 순서.

**Tech Stack:** Windows CMD (.bat), Python 3.12, PyInstaller>=6.11, pip platform markers, matplotlib

---

## 파일 변경 맵

| 파일 | 변경 유형 | 내용 |
|------|-----------|------|
| `requirements.txt` | Modify | `pywin32` 플랫폼 마커 `; sys_platform == "win32"` 추가 |
| `build.bat` | Modify | `chcp 65001` line 2로 이동; 삭제된 scipy/sklearn hidden-import 5줄 제거 |
| `NHIS_YOD_DM_Analyzer.spec` | Modify | 같은 5개 hidden-import 항목 제거 |
| `.gitattributes` | Add (stage) | 미추적 파일을 git에 추가 |
| `visualization.py` | Modify | 한국어 폰트 못 찾을 때 `logger.warning()` 추가 |

---

## Task 1: Critical — requirements.txt 플랫폼 마커 + build.bat chcp 위치 수정

**Files:**
- Modify: `requirements.txt:29`
- Modify: `build.bat:1-9`

### 배경

**C1 — requirements.txt**: `pywin32>=306` 에 플랫폼 마커가 없어 macOS/Linux에서 `pip install -r requirements.txt` 실패. pip는 주석을 조건으로 해석하지 않는다.

**C2 — build.bat**: `chcp 65001 > nul`(UTF-8 코드페이지 설정)이 line 8에 있으나, Korean echo text는 line 4(`스크립트 폴더로 이동 실패`)부터 시작된다. 영문 Windows(기본 코드페이지 437/1252)에서 `cd /d` 오류 발생 시 한국어 메시지가 깨진다.

- [ ] **Step 1: requirements.txt 수정**

현재 line 29:
```
pywin32>=306  # Windows 전용 — macOS/Linux 에서는 무시됨 (win32timezone PyInstaller 포함 필요)
```

변경 후:
```
pywin32>=306; sys_platform == "win32"  # win32timezone PyInstaller 포함 필요
```

- [ ] **Step 2: 마커 문법 검증**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -c "
import pip._internal.req.constructors as c
# pip 마커 파싱 테스트
from pip._vendor.packaging.requirements import Requirement
r = Requirement('pywin32>=306; sys_platform == \"win32\"')
print('marker:', r.marker)
print('OK')
"
```

기대 출력: `marker: sys_platform == "win32"` + `OK`

대안 (pip 없는 환경):
```bash
python -c "
from packaging.requirements import Requirement
r = Requirement('pywin32>=306; sys_platform == \"win32\"')
print('OK:', r.marker)
"
```

- [ ] **Step 3: build.bat chcp 위치 수정**

현재 (`build.bat` lines 1-9):
```bat
@echo off
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] 스크립트 폴더로 이동 실패: %~dp0
    pause & exit /b 1
)
setlocal enabledelayedexpansion
chcp 65001 > nul
echo === NHIS YOD-DM Analyzer v2.1 Build ===
```

변경 후:
```bat
@echo off
chcp 65001 > nul
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] 스크립트 폴더로 이동 실패: %~dp0
    pause & exit /b 1
)
setlocal enabledelayedexpansion
echo === NHIS YOD-DM Analyzer v2.1 Build ===
```

**변경 이유**: `chcp 65001`을 `@echo off` 바로 다음으로 이동. 이후 모든 Korean echo text는 UTF-8 코드페이지가 활성화된 상태에서 출력된다.

- [ ] **Step 4: build.bat 검증 — Korean 메시지 라인 목록 확인**

```bash
grep -n "echo.*[가-힣]" /Users/aidept/ptg_at_train/yod_diabetes_app/build.bat | head -5
```

기대: 가장 첫 번째 Korean echo 라인이 `chcp 65001` 이후(line 3 이후)에 있는지 확인.

- [ ] **Step 5: 전체 테스트 회귀 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: 210 passed, 4 pre-existing failures, 1 skipped — 회귀 없음

- [ ] **Step 6: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add requirements.txt build.bat
git commit -m "fix: pywin32 Windows 플랫폼 마커 추가 + chcp 65001 위치 수정 (Stage U T1)"
```

---

## Task 2: Important — 삭제된 scipy/sklearn hidden-import 정리

**Files:**
- Modify: `build.bat:94-104` (해당 5줄 제거)
- Modify: `NHIS_YOD_DM_Analyzer.spec:6` (hiddenimports 리스트에서 5개 항목 제거)

### 배경

다음 모듈들은 최신 버전에서 **삭제됨**:
- `scipy._lib.messagestream` — scipy 1.14+에서 제거
- `scipy._lib.array_api_compat` — scipy 1.14+에서 제거 (외부 패키지로 분리)
- `scipy._lib.array_api_compat.numpy` — 동일
- `sklearn.utils._heap` — scikit-learn 1.5+에서 제거
- `sklearn.utils._sorting` — scikit-learn 1.5+에서 제거

`requirements.txt`가 하한만 지정(`scipy>=1.13.0`, `scikit-learn>=1.4.0`)하므로 오늘 설치하면 scipy 1.15.x, sklearn 1.6.x가 설치되어 해당 모듈이 존재하지 않는다. PyInstaller는 이를 경고로 처리하고 빌드는 완료되지만, `--collect-all scipy`와 `--collect-all sklearn`이 이미 필요한 모든 파일을 수집하므로 이 5개 hidden-import는 완전히 불필요하다.

- [ ] **Step 1: build.bat 수정 — 5줄 제거**

`build.bat`에서 다음 5줄을 찾아 삭제:
```bat
 --hidden-import scipy._lib.messagestream^
 --hidden-import scipy._lib.array_api_compat^
 --hidden-import scipy._lib.array_api_compat.numpy^
```
그리고:
```bat
 --hidden-import sklearn.utils._heap^
 --hidden-import sklearn.utils._sorting^
```

삭제 후 앞뒤 줄의 `^` 계속자가 올바르게 연결되는지 확인:
- `sklearn.utils._typedefs^` 다음이 `sklearn.neighbors^` 또는 `scipy.stats^` 등으로 이어져야 함
- `scipy.optimize^` 다음이 `matplotlib^` 등으로 이어져야 함

- [ ] **Step 2: NHIS_YOD_DM_Analyzer.spec 수정 — 5개 항목 제거**

`NHIS_YOD_DM_Analyzer.spec` line 6의 `hiddenimports` 리스트에서 아래 5개 문자열을 삭제:
- `'scipy._lib.messagestream'`
- `'scipy._lib.array_api_compat'`
- `'scipy._lib.array_api_compat.numpy'`
- `'sklearn.utils._heap'`
- `'sklearn.utils._sorting'`

각 항목은 쉼표로 구분된 리스트의 일부이므로, 항목과 그 뒤의 쉼표(또는 앞의 쉼표)를 함께 제거해 파이썬 리스트 문법이 유효하게 유지되도록 한다.

- [ ] **Step 3: .spec 파이썬 문법 검증**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -c "
import ast
with open('NHIS_YOD_DM_Analyzer.spec', 'r', encoding='utf-8') as f:
    src = f.read()
ast.parse(src)
print('spec 문법 OK')
"
```

기대: `spec 문법 OK`

- [ ] **Step 4: build.bat 줄 계속자 검증**

```bash
# ^ 계속자 줄에 trailing space가 없는지 확인 (trailing space after ^ breaks bat)
python -c "
with open('build.bat', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        stripped = line.rstrip('\n\r')
        if stripped.endswith('^ ') or (stripped.endswith('^') and line != stripped + '\r\n' and line != stripped + '\n'):
            # 실제로 ^ 뒤에 공백이 있는지 확인
            if stripped.endswith('^ ') or '^ ' in stripped:
                print(f'Line {i}: trailing space after ^: {repr(stripped)}')
print('check done')
"
```

기대: trailing space 경고 없이 `check done`

- [ ] **Step 5: 테스트 회귀 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: 210 passed, 4 pre-existing failures, 1 skipped

- [ ] **Step 6: 커밋**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add build.bat NHIS_YOD_DM_Analyzer.spec
git commit -m "fix: scipy/sklearn 삭제된 hidden-import 제거 (Stage U T2)"
```

---

## Task 3: Minor — .gitattributes 커밋 + visualization.py 폰트 경고

**Files:**
- Stage: `.gitattributes` (기존 파일, untracked 상태)
- Modify: `visualization.py:42-54` (폰트 없을 때 logger.warning 추가)

### 배경

**M1 — .gitattributes**: 파일이 존재하지만 git에 추적되지 않아(`??` 상태) 다른 개발자 또는 opencode가 클론 시 적용되지 않는다. `*.bat text eol=crlf` 규칙이 없으면 bat 파일이 LF로 저장되어 Windows CMD에서 오류가 날 수 있다.

**M2 — visualization.py**: `setup_korean_font()`에서 후보 경로와 시스템 폰트 검색 모두 실패해도 아무런 경고 없이 진행된다. 결과: 그래프의 한국어 텍스트가 모두 □□□(tofu)로 표시되지만 사용자는 왜 그런지 알 수 없다.

### 현재 visualization.py 코드 (lines 42-54)

```python
    for fp in candidates:
        if Path(fp).exists():
            fm.fontManager.addfont(fp)
            plt.rcParams['font.family'] = fm.FontProperties(fname=fp).get_name()
            break
    else:
        # 시스템에서 한국어 폰트 자동 탐색
        for font in fm.fontManager.ttflist:
            if any(k in font.name for k in ['Gothic', 'Nanum', 'Malgun', 'CJK', 'Noto']):
                plt.rcParams['font.family'] = font.name
                break

    plt.rcParams['axes.unicode_minus'] = False
```

- [ ] **Step 1: visualization.py 수정 — 내부 for-else에 경고 추가**

변경 후:
```python
    for fp in candidates:
        if Path(fp).exists():
            fm.fontManager.addfont(fp)
            plt.rcParams['font.family'] = fm.FontProperties(fname=fp).get_name()
            break
    else:
        # 시스템에서 한국어 폰트 자동 탐색
        for font in fm.fontManager.ttflist:
            if any(k in font.name for k in ['Gothic', 'Nanum', 'Malgun', 'CJK', 'Noto']):
                plt.rcParams['font.family'] = font.name
                break
        else:
            logger.warning(
                "한국어 폰트를 찾을 수 없습니다. 그래프의 한국어 텍스트가 깨져 보일 수 있습니다. "
                "Windows: C:/Windows/Fonts/malgun.ttf 설치 확인"
            )

    plt.rcParams['axes.unicode_minus'] = False
```

**핵심**: `else:`를 내부 `for font` 루프에 추가. Python의 for-else 문법 — 루프가 `break` 없이 완료되면 else 블록 실행.

- [ ] **Step 2: 문법 확인**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
python -m py_compile visualization.py && echo "OK"
```

기대: `OK`

- [ ] **Step 3: 테스트 회귀 확인**

```bash
python -m pytest tests/ -q --tb=no 2>&1 | tail -5
```

기대: 210 passed, 4 pre-existing failures, 1 skipped

- [ ] **Step 4: 커밋 (.gitattributes + visualization.py 함께)**

```bash
cd /Users/aidept/ptg_at_train/yod_diabetes_app
git add .gitattributes visualization.py
git commit -m "fix: .gitattributes 추적 + 한국어 폰트 없을 때 경고 로그 추가 (Stage U T3)"
```

---

## 자체 점검

### 스펙 커버리지

| 이슈 | Task | 상태 |
|------|------|------|
| Critical C1: pywin32 플랫폼 마커 | Task 1 | ✅ |
| Critical C2: chcp 65001 위치 | Task 1 | ✅ |
| Important I1: scipy 삭제 모듈 hidden-import | Task 2 | ✅ |
| Important I2: sklearn 삭제 모듈 hidden-import | Task 2 | ✅ |
| Important I3: .gitattributes untracked | Task 3 | ✅ |
| Minor M2: 폰트 없을 때 경고 로그 | Task 3 | ✅ |

### 검증 전략 (PyInstaller 없는 환경)

build.bat는 macOS에서 실행 불가이므로 파일 텍스트 정확성만 검증한다:
- requirements.txt: `python -c "from packaging.requirements import Requirement..."` 로 마커 문법 확인
- .spec: `ast.parse()` 로 파이썬 문법 확인
- build.bat: grep으로 chcp 위치 및 삭제된 줄 확인
- visualization.py: `py_compile` 로 문법 확인

### .spec hiddenimports 일관성

build.bat와 .spec의 hiddenimports가 동일한 항목을 가져야 한다. Task 2에서 두 파일 모두 수정하므로 일관성 유지.
