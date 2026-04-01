"""
utils.py - 유틸리티 함수
"""
import os, sys, time, logging
from pathlib import Path
from functools import wraps
from config import APP_SETTINGS

def setup_logging(log_dir='.'):
    log_path = Path(log_dir) / APP_SETTINGS['LOG_FILE']
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
