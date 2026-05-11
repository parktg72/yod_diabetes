"""
utils.py - 유틸리티 함수
"""
import os, sys, time, logging
from pathlib import Path
from functools import wraps
from config import APP_SETTINGS

def setup_logging(log_dir=None):
    """로그 설정.

    log_dir 생략 시:
      - Windows: %LOCALAPPDATA%\\NHIS_YOD_DM_Analyzer\\logs
      - 기타:    현재 디렉토리('.')
    """
    if log_dir is None:
        if sys.platform == 'win32':
            base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
            log_dir_path = base / 'NHIS_YOD_DM_Analyzer' / 'logs'
        else:
            log_dir_path = Path('.')
    else:
        log_dir_path = Path(log_dir)

    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_path = log_dir_path / APP_SETTINGS['LOG_FILE']

    root = logging.getLogger()
    # 이미 핸들러가 설정된 경우 중복 추가 방지
    if not root.handlers:
        root.setLevel(logging.INFO)
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)
    return logging.getLogger(__name__)

def timer(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        logging.getLogger(__name__).info(f"{func.__name__}: {time.time()-start:.1f}s")
        return result
    return wrapper

def format_number(n):
    return f"{n:,.0f}" if isinstance(n, (int, float)) else str(n)

def format_hr(hr, ci_lower, ci_upper, p_value):
    sig = '***' if p_value < 0.001 else '**' if p_value < 0.01 else '*' if p_value < 0.05 else ''
    return f"{hr:.2f} ({ci_lower:.2f}-{ci_upper:.2f}){sig}"

import re as _re

_SAFE_COL_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*$')
_SAFE_CODE_RE = _re.compile(r'^[A-Za-z0-9]+$')


def icd_like(col, codes):
    """ICD-10 코드 LIKE 조건 SQL 생성 (codes는 config.py 상수만 사용할 것)"""
    if not _SAFE_COL_RE.match(col):
        raise ValueError(f"유효하지 않은 컬럼명: {col!r}")
    for c in codes:
        if not _SAFE_CODE_RE.match(c):
            raise ValueError(f"유효하지 않은 ICD 코드: {c!r}")
    return '(' + ' OR '.join(f"{col} LIKE '{c}%'" for c in codes) + ')'


def make_skip_result(reason_code, reason, *, stage=None, **extra):
    """명시적 분석 skip 결과의 공통 스키마."""
    result = {'skipped': True, 'reason_code': reason_code, 'reason': reason}
    if stage is not None:
        result['stage'] = stage
    result.update(extra)
    return result


def make_model_failure(reason_code, reason, *, stage='cox', **extra):
    """모델별 실패 결과의 공통 스키마."""
    result = {'reason_code': reason_code, 'reason': reason}
    if stage is not None:
        result['stage'] = stage
    result.update(extra)
    return result


def make_error_result(reason_code, error, *, stage=None, **extra):
    """예외 기반 실패 결과의 공통 스키마."""
    result = {
        'reason_code': reason_code,
        'reason': str(error),
        'exception_type': type(error).__name__,
    }
    if stage is not None:
        result['stage'] = stage
    result.update(extra)
    return result


def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def get_disk_usage(path='.'):
    total = 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            fp = os.path.join(dp, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total


class CohortStepError(Exception):
    """CohortBuilder 단계 실패 예외.

    step: 실패한 단계 번호 (1-7)
    step_name: 단계 이름 (예: '기본 인구 정의')
    cause: 원인 예외
    """
    def __init__(self, step: int, step_name: str, cause: Exception):
        self.step = step
        self.step_name = step_name
        self.cause = cause
        super().__init__(
            f"코호트 {step}단계({step_name}) 실패: {cause}"
        )


class InsufficientDataError(ValueError):
    """분석에 필요한 최소 유효 행/이벤트 수를 충족하지 못할 때 발생.

    kind='rows'  — 행 수 부족 (MIN_VALID_ROWS 관련)
    kind='events' — 이벤트 수 부족 (MIN_EVENTS/EPV 관련)
    """
    def __init__(self, valid_rows: int, min_rows: int, kind: str = "rows"):
        if kind not in ("rows", "events"):
            raise ValueError(f"kind 는 'rows' 또는 'events' 여야 합니다: {kind!r}")
        self.valid_rows = valid_rows
        self.min_rows = min_rows
        self.kind = kind
        if kind == "events":
            super().__init__(
                f"이벤트(결과 발생) 수 부족: {valid_rows:,}건 — EPV 최소 기준 {min_rows:,}건 필요.\n"
                "해결 방법:\n"
                "  1) 연구 기간을 확장하거나 진입 연령 기준을 완화하세요.\n"
                "  2) config.py의 MIN_EVENTS 값을 낮추세요 (단, 통계적 검정력 감소 주의).\n"
                "  3) 희귀 결과(AD/VaD)는 모든 치매를 복합 결과로 통합하는 방안을 고려하세요."
            )
        else:
            super().__init__(
                f"유효 데이터 행 수 부족: {valid_rows:,}건 — 최소 분석 기준 {min_rows:,}건 필요.\n"
                "해결 방법:\n"
                "  1) 데이터가 정상적으로 추출되었는지 DataLoad 탭에서 확인하세요.\n"
                "  2) 코호트 조건(연령, 진입기간 등)이 너무 좁지 않은지 검토하세요.\n"
                "  3) config.py의 MIN_VALID_ROWS 값을 낮출 수 있습니다 (최소 10 권장)."
            )


def format_error_for_user(exc: Exception) -> str:
    """예외를 사용자 친화적 메시지로 변환한다.

    tabs.py, statistical_analysis.py 등에서 except 블록에 사용.
    로그에는 별도로 logger.exception()으로 스택 트레이스를 남길 것.
    """
    import duckdb as _duckdb
    import pandas as _pd

    if isinstance(exc, CohortStepError):
        return str(exc)
    if isinstance(exc, _duckdb.Error):
        return (
            f"데이터베이스 오류: {exc}\n"
            "재시도하거나 데이터를 다시 적재해 주세요."
        )
    if isinstance(exc, InsufficientDataError):
        return str(exc)  # InsufficientDataError.__init__에 이미 해결 방법 포함
    if isinstance(exc, _pd.errors.EmptyDataError):
        return "분석 대상 데이터가 없습니다. 코호트 구성 단계를 확인해 주세요."
    if isinstance(exc, ValueError):
        return f"입력값 오류: {exc}"
    if isinstance(exc, MemoryError):
        return "메모리 부족 — 청크 크기를 줄이거나 데이터 범위를 축소하세요."
    return (
        f"예기치 않은 오류가 발생했습니다. 로그를 확인해 주세요: "
        f"{type(exc).__name__}: {exc}"
    )
