"""
config.py - 연구 설정 및 코드 정의
T1DM vs T2DM에서 조기발병 치매(YOD) 위험 비교 연구
프로토콜 반영 (2026-03-27)
"""

# ============================================================
# ICD-10 진단 코드 정의
# ============================================================

DM_CODES = {
    'T1DM': ['E10'],
    'T2DM': ['E11', 'E12', 'E13', 'E14'],
}

DEMENTIA_CODES = {
    'ALL_CAUSE': ['F00', 'F01', 'F02', 'F03', 'G30', 'G310', 'G311', 'G318', 'G319'],
    'AD': ['F00', 'G30'],
    'VAD': ['F01'],
}

DM_COMPLICATION_CODES = {
    'RETINOPATHY': ['E103', 'E113', 'E123', 'E133', 'E143', 'H360'],
    'NEPHROPATHY': ['E102', 'E112', 'E122', 'E132', 'E142', 'N083'],
    'NEUROPATHY': ['E104', 'E114', 'E124', 'E134', 'E144', 'G632'],
    'FOOT': ['E105', 'E115', 'E125', 'E135', 'E145'],
    'HYPOGLYCEMIA': ['E160', 'E161', 'E162'],
}

COMORBIDITY_CODES = {
    'HYPERTENSION': ['I10', 'I11', 'I12', 'I13', 'I15'],
    'DYSLIPIDEMIA': ['E780', 'E781', 'E782', 'E783', 'E784', 'E785'],
    'ISCHEMIC_STROKE': ['I63'],
    'HEMORRHAGIC_STROKE': ['I60', 'I61', 'I62'],
    'TIA': ['G45'],
    'DEPRESSION': ['F32', 'F33'],
    'ANXIETY': ['F40', 'F41'],
    'HYPOTHYROIDISM': ['E03'],
    'OBESITY': ['E66'],
    'CKD': ['N181', 'N182', 'N183', 'N184', 'N185'],
    'IHD': ['I20', 'I21', 'I22', 'I23', 'I24', 'I25'],
    'ATRIAL_FIB': ['I48'],
    'HEART_FAILURE': ['I50'],
    'PVD': ['I739', 'I74'],
}

CCI_CODES = {
    'MI': (['I21', 'I22', 'I252'], 1),
    'CHF': (['I099', 'I110', 'I130', 'I132', 'I255', 'I420', 'I425', 'I426', 'I427', 'I428', 'I429', 'I43', 'I50', 'P290'], 1),
    'PVD': (['I70', 'I71', 'I731', 'I738', 'I739', 'I771', 'I790', 'I792', 'K551', 'K558', 'K559', 'Z958', 'Z959'], 1),
    'CVD': (['G45', 'G46', 'I60', 'I61', 'I62', 'I63', 'I64', 'I65', 'I66', 'I67', 'I68', 'I69', 'H340'], 1),
    'DEMENTIA_CCI': (['F00', 'F01', 'F02', 'F03', 'F051', 'G30', 'G311'], 1),
    'COPD': (['I278', 'I279', 'J40', 'J41', 'J42', 'J43', 'J44', 'J45', 'J46', 'J47', 'J60', 'J61', 'J62', 'J63', 'J64', 'J65', 'J66', 'J67', 'J684', 'J701', 'J703'], 1),
    'RHEUMATIC': (['M05', 'M06', 'M315', 'M32', 'M33', 'M34', 'M351', 'M353', 'M360'], 1),
    'PUD': (['K25', 'K26', 'K27', 'K28'], 1),
    'MILD_LIVER': (['B18', 'K700', 'K701', 'K702', 'K703', 'K709', 'K713', 'K714', 'K715', 'K717', 'K73', 'K74', 'K760', 'K762', 'K763', 'K764', 'K768', 'K769', 'Z944'], 1),
    'DM_NO_COMP': (['E100', 'E101', 'E106', 'E108', 'E109', 'E110', 'E111', 'E116', 'E118', 'E119', 'E120', 'E121', 'E126', 'E128', 'E129', 'E130', 'E131', 'E136', 'E138', 'E139', 'E140', 'E141', 'E146', 'E148', 'E149'], 1),
    'DM_WITH_COMP': (['E102', 'E103', 'E104', 'E105', 'E107', 'E112', 'E113', 'E114', 'E115', 'E117', 'E122', 'E123', 'E124', 'E125', 'E127', 'E132', 'E133', 'E134', 'E135', 'E137', 'E142', 'E143', 'E144', 'E145', 'E147'], 2),
    'HEMIPLEGIA': (['G041', 'G114', 'G801', 'G802', 'G81', 'G82', 'G830', 'G831', 'G832', 'G833', 'G834', 'G839'], 2),
    'RENAL': (['I120', 'I131', 'N032', 'N033', 'N034', 'N035', 'N036', 'N037', 'N052', 'N053', 'N054', 'N055', 'N056', 'N057', 'N18', 'N19', 'N250', 'Z490', 'Z491', 'Z492', 'Z940', 'Z992'], 2),
    'MALIGNANCY': (['C00', 'C01', 'C02', 'C03', 'C04', 'C05', 'C06', 'C07', 'C08', 'C09', 'C10', 'C11', 'C12', 'C13', 'C14', 'C15', 'C16', 'C17', 'C18', 'C19', 'C20', 'C21', 'C22', 'C23', 'C24', 'C25', 'C26', 'C30', 'C31', 'C32', 'C33', 'C34', 'C37', 'C38', 'C39', 'C40', 'C41', 'C43', 'C45', 'C46', 'C47', 'C48', 'C49', 'C50', 'C51', 'C52', 'C53', 'C54', 'C55', 'C56', 'C57', 'C58', 'C60', 'C61', 'C62', 'C63', 'C64', 'C65', 'C66', 'C67', 'C68', 'C69', 'C70', 'C71', 'C72', 'C73', 'C74', 'C75', 'C76', 'C81', 'C82', 'C83', 'C84', 'C85', 'C88', 'C90', 'C91', 'C92', 'C93', 'C94', 'C95', 'C96', 'C97'], 2),
    'SEVERE_LIVER': (['I850', 'I859', 'I864', 'I982', 'K704', 'K711', 'K721', 'K729', 'K765', 'K766', 'K767'], 3),
    'META_TUMOR': (['C77', 'C78', 'C79', 'C80'], 6),
    'AIDS': (['B20', 'B21', 'B22', 'B24'], 6),
}

# ============================================================
# 약물 코드
# ============================================================

OHA_CODES = {
    'METFORMIN': ['148801', '148802', '148803', '148804', '148805'],
    'SULFONYLUREA': ['132201', '132301', '132401', '131701', '131702', '454401'],
    'MEGLITINIDE': ['385501', '385502', '427701'],
    'THIAZOLIDINEDIONE': ['381701', '381702', '394901'],
    'DPP4_INHIBITOR': ['469801', '483301', '487401', '497401', '502901', '512401', '524301'],
    'SGLT2_INHIBITOR': ['510001', '519301', '519401', '528601'],
    'ALPHA_GLUCOSIDASE': ['249101', '249102'],
}

INSULIN_EFMDC: list[str] = ['39620']  # EFMDC 분류코드
INSULIN_CODES: list[str] = [  # 성분코드
    '167401', '167402', '167403', '167404',
    '412201', '412202', '430701', '430702',
    '467901', '467902', '175301', '175302',
    '387301', '387302', '369001', '369002',
]

DEMENTIA_DRUG_CODES: list[str] = [
    '372701', '372702',  # 도네페질
    '390701', '390702',  # 리바스티그민
    '378901', '378902',  # 갈란타민
    '330001', '330002',  # 메만틴
]

# ============================================================
# 건강검진 테이블 연도별 구조
# ============================================================

# 2002~2017: 검진결과 + 문진 통합 1개 테이블
# 2018~: 검진결과 / 문진 각각 별도 연도별 테이블
EXAM_STRUCTURE = {
    'LEGACY_RANGE': (2002, 2017),       # 통합 테이블 연도 범위
    'SPLIT_RANGE': (2018, 2024),        # 분리 테이블 연도 범위
    # 2002-2017 통합 테이블 변수명 매핑
    'LEGACY_KEY_MAP': {
        'HC_BZ_YYYY': 'EXMD_BZ_YYYY',
        'HC_DT': 'HME_DT',
        'HCR_JUDG_DT': 'EXMDRST_JUDG_DT',
    },
    # 2018+ 검진결과 공통 변수 (매년 동일)
    'RESULT_COMMON_COLS': [
        'INDI_DSCM_NO', 'HC_BZ_YYYY', 'HC_DT',
        'G1E_HGHT', 'G1E_WGHT', 'G1E_WSTC', 'G1E_BMI',
        'G1E_BP_SYS', 'G1E_BP_DIA',
        'G1E_FBS', 'G1E_TOT_CHOL', 'G1E_TG', 'G1E_HDL', 'G1E_LDL_CALC',
        'G1E_CRTN', 'G1E_GFR', 'G1E_HGB',
        'G1E_SGOT', 'G1E_SGPT', 'G1E_GGT',
    ],
    # 2018+ 검진결과 공통 변수 타입 선언 (DuckDB 타입)
    'RESULT_COMMON_COL_TYPES': {
        'INDI_DSCM_NO': 'VARCHAR',
        'HC_BZ_YYYY': 'VARCHAR',
        'HC_DT': 'VARCHAR',
        'G1E_HGHT': 'DOUBLE',
        'G1E_WGHT': 'DOUBLE',
        'G1E_WSTC': 'DOUBLE',
        'G1E_BMI': 'DOUBLE',
        'G1E_BP_SYS': 'DOUBLE',
        'G1E_BP_DIA': 'DOUBLE',
        'G1E_FBS': 'DOUBLE',
        'G1E_TOT_CHOL': 'DOUBLE',
        'G1E_TG': 'DOUBLE',
        'G1E_HDL': 'DOUBLE',
        'G1E_LDL_CALC': 'DOUBLE',
        'G1E_CRTN': 'DOUBLE',
        'G1E_GFR': 'DOUBLE',
        'G1E_HGB': 'DOUBLE',
        'G1E_SGOT': 'DOUBLE',
        'G1E_SGPT': 'DOUBLE',
        'G1E_GGT': 'DOUBLE',
    },
    # 2018+ 문진 공통 변수
    'QUEST_COMMON_COLS': [
        'INDI_DSCM_NO', 'HC_BZ_YYYY',
        'Q_PHX_DX_DM', 'Q_PHX_TX_DM',
        'Q_PHX_DX_HTN', 'Q_PHX_TX_HTN',
        'Q_PHX_DX_DLD', 'Q_PHX_TX_DLD',
        'Q_PHX_DX_STK', 'Q_PHX_DX_HTDZ',
        'Q_SMK_YN', 'Q_SMK_NOW_YN',
        'Q_DRK_PER', 'Q_DRK_FRQ',
    ],
    # 2018+ 문진 공통 변수 타입 선언 (DuckDB 타입)
    'QUEST_COMMON_COL_TYPES': {
        'INDI_DSCM_NO': 'VARCHAR',
        'HC_BZ_YYYY': 'VARCHAR',
        'Q_PHX_DX_DM': 'INTEGER',
        'Q_PHX_TX_DM': 'INTEGER',
        'Q_PHX_DX_HTN': 'INTEGER',
        'Q_PHX_TX_HTN': 'INTEGER',
        'Q_PHX_DX_DLD': 'INTEGER',
        'Q_PHX_TX_DLD': 'INTEGER',
        'Q_PHX_DX_STK': 'INTEGER',
        'Q_PHX_DX_HTDZ': 'INTEGER',
        'Q_SMK_YN': 'INTEGER',
        'Q_SMK_NOW_YN': 'INTEGER',
        'Q_DRK_PER': 'INTEGER',
        'Q_DRK_FRQ': 'INTEGER',
    },
    # 2002-2017 통합 테이블에서 문진 변수 매핑
    'LEGACY_QUEST_MAP': {
        'Q_SMK_YN': 'G1E_HB_SMK',
        'Q_DRK_PER': 'G1E_HB_DRK',
        'Q_PHX_DX_DM': 'G1E_PHX_DM',
        'Q_PHX_DX_HTN': 'G1E_PHX_HTN',
        'Q_PHX_DX_STK': 'G1E_PHX_STK',
        'Q_PHX_DX_HTDZ': 'G1E_PHX_HTDZ',
    },
}

# ============================================================
# 연구 설정 (프로토콜 반영)
# ============================================================

STUDY_SETTINGS = {
    'STUDY_START_YEAR': 2013,
    'STUDY_END_YEAR': 2024,
    'ENROLLMENT_START': 2013,
    'ENROLLMENT_END': 2016,
    'WASHOUT_YEARS': 1,
    'LOOKBACK_YEARS': 1,
    'MIN_AGE': 40,
    'MAX_AGE': 64,
    'YOD_AGE_CUTOFF': 65,
    'MIN_DM_CLAIMS_OUTPATIENT': 2,
    'MIN_DM_CLAIMS_INPATIENT': 1,
    'DM_DURATION_BINS': [0, 5, 10, float('inf')],
    'DM_DURATION_LABELS': ['<5yr', '5-10yr', '>=10yr'],
    'AGE_SUBGROUPS': [(40, 54), (55, 64)],
    'PSM_RATIO': 3,
    'INCOME_DECILES': 10,
    'CENSORING_EVENTS': ['yod', 'age65', 'death', 'withdrawal', 'study_end'],
    'SAMPLING_SEED': 42,          # 층화 샘플링 재현성 시드 (0–99 정수)
    'MIN_VALID_ROWS': 30,         # Cox 분석 최소 유효 행 수 (EPV ≥ 10 기준)
    'MIN_EVENTS': 10,             # Cox 분석 최소 이벤트 수 (EPV heuristic)
    'MIN_SUBGROUP_EVENTS': 5,     # 서브그룹/Fine-Gray 분석 최소 이벤트 수
    'PH_ALPHA': 0.05,             # Cox PH 가정 검정 유의수준
    'PSM_CALIPER': 0.2,           # PSM caliper = PSM_CALIPER × pooled logit(PS) SD
    'PSM_SMD_THRESHOLD': 0.1,     # PSM 균형 판정 SMD 임계값
}

DUCKDB_SETTINGS = {
    'MEMORY_LIMIT': '4GB',
    'THREADS': 4,
    'TEMP_DIRECTORY': None,  # None → db_connector.py 가 _BASE_DIR 기준으로 해결
    'HANA_CACHE_DIR': None,  # None → _BASE_DIR / 'hana_cache'
}

# ============================================================
# 메모리 / GPU / 청크 설정
# ============================================================

MEMORY_SETTINGS = {
    'RAM_LIMIT_GB': 8.0,           # 앱 전체 RAM 사용 상한 (GB)
    'RAM_WARNING_PCT': 80,         # RAM 사용률 경고 임계치 (%)
    'GC_AFTER_EACH_STEP': True,    # 각 단계 후 gc.collect() 강제 실행
    'PANDAS_CHUNK_SIZE': 50000,    # Pandas read 시 기본 chunk 크기
    'DUCKDB_SPILL_TO_DISK': True,  # DuckDB 메모리 초과 시 디스크 spillover
    'MAX_DF_ROWS_IN_MEMORY': 500000,  # 분석 시 메모리에 올릴 최대 행수
    'DTYPE_OPTIMIZE': True,        # 자동 dtype 최적화 (int64→int32 등)
}

GPU_SETTINGS = {
    'USE_GPU': False,              # GPU 사용 여부
    'GPU_MEMORY_FRACTION': 0.3,    # GPU 메모리 사용 비율 (0.0~1.0)
    'GPU_DEVICE_ID': 0,            # GPU 디바이스 번호
    'CUDF_ENABLED': False,         # cuDF (GPU DataFrame) 사용 여부
}

CHUNK_SETTINGS = {
    'SAS_CHUNK': 50000,            # SAS 파일 읽기 chunk
    'HANA_CHUNK': 50000,           # HANA fetch chunk
    'CSV_CHUNK': 100000,           # CSV 읽기 chunk
    'ANALYSIS_CHUNK': 200000,      # 분석 시 chunk
    'MIN_CHUNK': 5000,             # 최소 chunk 크기
    'MAX_CHUNK': 1000000,          # 최대 chunk 크기
}

APP_SETTINGS = {
    'APP_NAME': 'NHIS YOD-DM Analyzer',
    'VERSION': '2.1.0',
    'WINDOW_WIDTH': 1400,
    'WINDOW_HEIGHT': 900,
    'CHUNK_SIZE': 50000,           # 기본 chunk (조절 가능)
    'LOG_FILE': 'yod_analysis.log',
}

# ============================================================
# 설정 저장/불러오기
# ============================================================

import json
import os
import sys
from pathlib import Path

_BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


def _resolve_settings_file() -> Path:
    """설정 파일 경로 결정. frozen(PyInstaller) + Windows 에서는 %APPDATA%\\YodApp 우선."""
    if getattr(sys, 'frozen', False) and os.name == 'nt':
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            d = Path(appdata) / 'YodApp'
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d / 'yod_settings.json'
            except OSError:
                pass  # APPDATA 쓰기 실패 시 _BASE_DIR 로 fallback
    return _BASE_DIR / 'yod_settings.json'


_SETTINGS_FILE = _resolve_settings_file()

_SAVEABLE_SETTINGS = {
    'STUDY_SETTINGS': STUDY_SETTINGS,
    'MEMORY_SETTINGS': MEMORY_SETTINGS,
    'GPU_SETTINGS': GPU_SETTINGS,
    'CHUNK_SETTINGS': CHUNK_SETTINGS,
    'DUCKDB_SETTINGS': DUCKDB_SETTINGS,
    'APP_SETTINGS': APP_SETTINGS,
}


def save_settings(path=None):
    """현재 설정을 JSON 파일로 저장"""
    p = Path(path) if path else _SETTINGS_FILE
    data = {}
    for name, settings_dict in _SAVEABLE_SETTINGS.items():
        # float('inf') is not JSON serializable — convert to string
        serializable = {}
        for k, v in settings_dict.items():
            if isinstance(v, float) and v == float('inf'):
                serializable[k] = "Infinity"
            elif isinstance(v, list):
                serializable[k] = [("Infinity" if isinstance(x, float) and x == float('inf') else x) for x in v]
            else:
                serializable[k] = v
        data[name] = serializable
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    return str(p)


def load_settings(path=None):
    """JSON 파일에서 설정 복원"""
    p = Path(path) if path else _SETTINGS_FILE
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding='utf-8'))
    for name, settings_dict in _SAVEABLE_SETTINGS.items():
        if name in data:
            for k, v in data[name].items():
                if k in settings_dict:
                    # Restore Infinity
                    if v == "Infinity":
                        settings_dict[k] = float('inf')
                    elif isinstance(v, list):
                        settings_dict[k] = [(float('inf') if x == "Infinity" else x) for x in v]
                    else:
                        # 원래 타입 유지를 위해 type cast는 기본형만 적용
                        orig = settings_dict[k]
                        if orig is not None and not isinstance(v, type(orig)):
                            try:
                                settings_dict[k] = type(orig)(v)
                            except (TypeError, ValueError):
                                settings_dict[k] = v
                        else:
                            settings_dict[k] = v
    return True
