"""
nhis_schema.py - NHIS 데이터 테이블 스키마 정의
실제 NHIS 맞춤형 DB 레이아웃 기반 (SAP HANA)
"""

T20_SCHEMA = {
    'table_name': 'T20', 'description': '진료명세서',
    'key_columns': ['CMN_KEY'],
    'essential_columns': [
        'CMN_KEY', 'INDI_DSCM_NO', 'SEX_TYPE', 'SUJIN_POTM_AGE_ID',
        'MDCARE_STRT_DT', 'MDCARE_STRT_YYYYMM',
        'SICK_SYM1', 'SICK_SYM2', 'SICK_SYM3', 'SICK_SYM4', 'SICK_SYM5',
        'YOYANG_CLSFC_CD', 'MCARE_TP', 'FORM_CD',
        'SUJIN_POTM_CTRB_20CLS', 'YEND_POTM_CTRB_20CLS',
    ],
}

T30_SCHEMA = {
    'table_name': 'T30', 'description': '진료내역',
    'key_columns': ['CMN_KEY', 'MCARE_DESC_LN_NO'],
    'essential_columns': [
        'CMN_KEY', 'INDI_DSCM_NO', 'MCARE_DIV_CD',
        'EFMDC_CLSF_NO', 'WK_COMPN_CD', 'RVSN_WK_COMPN_CD',
        'DD1_MQTY_FREQ', 'TOT_MCNT',
        'MDCARE_STRT_DT', 'MDCARE_STRT_YYYYMM',
    ],
}

T40_SCHEMA = {
    'table_name': 'T40', 'description': '상병내역',
    'key_columns': ['CMN_KEY', 'SICK_DESC_SEQ_NO'],
    'essential_columns': [
        'CMN_KEY', 'INDI_DSCM_NO', 'MCEX_SICK_SYM',
        'SICK_CLSF_TYPE', 'MDCARE_STRT_DT', 'MDCARE_STRT_YYYYMM',
    ],
}

T60_SCHEMA = {
    'table_name': 'T60', 'description': '처방전내역',
    'key_columns': ['CMN_KEY', 'MPRSC_GRANT_NO', 'MPRSC_SEQ_NO'],
    'essential_columns': [
        'CMN_KEY', 'INDI_DSCM_NO', 'MCARE_DIV_CD',
        'GNL_NM_CD', 'RVSN_WK_COMPN_CD',
        'TOT_MCNT', 'MDCARE_STRT_DT', 'MDCARE_STRT_YYYYMM',
    ],
}

JK_SCHEMA = {
    'table_name': 'JK', 'description': '자격DB',
    'key_columns': ['STD_YYYY', 'INDI_DSCM_NO'],
    'essential_columns': [
        'STD_YYYY', 'INDI_DSCM_NO', 'SEX_TYPE', 'BYEAR',
        'GAIBJA_TYPE', 'SES05', 'RVSN_ADDR_CD',
        'CALC_CTRB_VTILE_FD', 'FOREIGNER_Y', 'SURV_YR',
    ],
}

YK_SCHEMA = {
    'table_name': 'YK', 'description': '요양기관현황',
    'key_columns': ['STD_YYYY', 'MDCARE_SYM'],
    'essential_columns': [
        'STD_YYYY', 'MDCARE_SYM', 'YOYANG_CLSFC_CD',
        'ADDR_SGG_CD', 'DISP_SUBJ_TYPE',
    ],
}

ALL_SCHEMAS = {
    'T20': T20_SCHEMA, 'T30': T30_SCHEMA, 'T40': T40_SCHEMA,
    'T60': T60_SCHEMA, 'JK': JK_SCHEMA, 'YK': YK_SCHEMA,
}

def get_essential_columns(table_name):
    schema = ALL_SCHEMAS.get(table_name)
    if schema:
        return schema.get('essential_columns', [])
    return []

def get_table_names():
    return list(ALL_SCHEMAS.keys())
