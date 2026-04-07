# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['PyQt5.sip', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets', 'duckdb', 'pyreadstat', 'lifelines', 'lifelines.statistics', 'lifelines.fitters', 'lifelines.fitters.coxph_fitter', 'lifelines.fitters.kaplan_meier_fitter', 'lifelines.utils', 'formulaic', 'autograd', 'autograd_gamma', 'sklearn', 'sklearn.linear_model', 'sklearn.linear_model._logistic', 'sklearn.neighbors', 'sklearn.neighbors._ball_tree', 'sklearn.neighbors._kd_tree', 'sklearn.utils._typedefs', 'sklearn.utils._param_validation', 'scipy.stats', 'scipy.linalg', 'scipy.special', 'scipy.sparse', 'scipy.optimize', 'matplotlib', 'matplotlib.backends.backend_agg', 'matplotlib.backends.backend_pdf', 'matplotlib.figure', 'matplotlib.patches', 'matplotlib.font_manager', 'pandas', 'pandas.io.formats.excel', 'pandas.io.excel._openpyxl', 'openpyxl', 'openpyxl.workbook', 'openpyxl.styles', 'openpyxl.styles.differential', 'openpyxl.cell', 'openpyxl.utils', 'openpyxl.utils.dataframe', 'psutil', 'win32timezone', 'numpy']
tmp_ret = collect_all('lifelines')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('duckdb')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sklearn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('scipy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('formulaic')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyreadstat')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('matplotlib')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pandas')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('openpyxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NHIS_YOD_DM_Analyzer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NHIS_YOD_DM_Analyzer',
)
