"""
tabs.py - Tab widget classes extracted from MainWindow
Each tab is a self-contained QWidget subclass sharing state via AppContext.
"""

import sys, os, logging, traceback
import pandas as pd
import duckdb
from pathlib import Path
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QTextEdit, QProgressBar,
    QFileDialog, QComboBox, QTableWidget, QTableWidgetItem,
    QMessageBox, QCheckBox, QSpinBox,
    QSplitter, QTreeWidget, QTreeWidgetItem, QInputDialog, QScrollArea
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont

from config import APP_SETTINGS, STUDY_SETTINGS, MEMORY_SETTINGS, GPU_SETTINGS, CHUNK_SETTINGS, DUCKDB_SETTINGS
from db_connector import DataManager
from cohort_builder import CohortBuilder
from variable_generator import VariableGenerator
from statistical_analysis import StatisticalAnalyzer
from visualization import Visualizer
from memory_manager import mem_manager, gpu_manager, chunk_controller
from results_exporter import ResultsExporter
from utils import format_number, CohortStepError, format_error_for_user

logger = logging.getLogger(__name__)


class AppContext:
    """Shared state between tabs and MainWindow."""
    def __init__(self):
        self.dm = None
        self.worker = None
        self.all_results = {}
        self.work_dir = Path('./work')
        self.results_dir = Path('./results')
        self.main_window = None  # set after MainWindow.__init__
        self.sampling_label = ""  # 샘플링 적용 시 결과 제목에 표시할 레이블


# ---------------------------------------------------------------------------
# Tab 1: DB 연결
# ---------------------------------------------------------------------------
class ConnectionTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)

        g = QGroupBox("SAP HANA DB 연결")
        gl = QGridLayout(g)
        gl.addWidget(QLabel("Host:"), 0, 0)
        self.hana_host = QLineEdit()
        self.hana_host.setPlaceholderText("192.168.1.100")
        gl.addWidget(self.hana_host, 0, 1)

        gl.addWidget(QLabel("Port:"), 0, 2)
        self.hana_port = QLineEdit()
        self.hana_port.setPlaceholderText("30015")
        gl.addWidget(self.hana_port, 0, 3)

        gl.addWidget(QLabel("User:"), 1, 0)
        self.hana_user = QLineEdit()
        gl.addWidget(self.hana_user, 1, 1)

        gl.addWidget(QLabel("Password:"), 1, 2)
        self.hana_pass = QLineEdit()
        self.hana_pass.setEchoMode(QLineEdit.Password)
        gl.addWidget(self.hana_pass, 1, 3)

        btn_test = QPushButton("연결 테스트")
        btn_test.clicked.connect(self.test_hana)
        gl.addWidget(btn_test, 2, 0, 1, 2)

        btn_init = QPushButton("작업공간 초기화")
        btn_init.clicked.connect(self.init_workspace)
        gl.addWidget(btn_init, 2, 2, 1, 2)
        ly.addWidget(g)

        wg = QGroupBox("작업 디렉토리")
        wl = QHBoxLayout(wg)
        self.work_dir_edit = QLineEdit(str(self.ctx.work_dir.absolute()))
        btn_b = QPushButton("찾아보기")
        btn_b.clicked.connect(lambda: self._browse_dir(self.work_dir_edit))
        wl.addWidget(self.work_dir_edit)
        wl.addWidget(btn_b)
        ly.addWidget(wg)
        ly.addStretch()

    # --- actions ---
    def _browse_dir(self, edit):
        d = QFileDialog.getExistingDirectory(self, "폴더 선택")
        if d:
            edit.setText(d)

    def _init_dm(self):
        new_dir = Path(self.work_dir_edit.text())
        if self.ctx.dm and new_dir != self.ctx.work_dir:
            self.ctx.dm.close()
            self.ctx.dm = None
        if not self.ctx.dm:
            self.ctx.work_dir = new_dir
            self.ctx.work_dir.mkdir(parents=True, exist_ok=True)
            self.ctx.dm = DataManager(str(self.ctx.work_dir))

    def test_hana(self):
        try:
            self._init_dm()
            ok = self.ctx.dm.connect_hana(self.hana_host.text(), int(self.hana_port.text()),
                                           self.hana_user.text(), self.hana_pass.text())
            QMessageBox.information(self, "결과", "연결 성공!" if ok else "연결 실패")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("HANA 연결 테스트 실패")
            QMessageBox.critical(self, "오류", format_error_for_user(e))
        except Exception as e:
            logger.exception("HANA 연결 테스트 중 예기치 않은 오류")
            QMessageBox.critical(self, "오류", format_error_for_user(e))

    def init_workspace(self):
        self._init_dm()
        self.ctx.dm.reset_storage()
        self.log_signal.emit("작업공간 초기화 완료 (기존 테이블 삭제됨)")


# ---------------------------------------------------------------------------
# Tab 2: 메모리/GPU
# ---------------------------------------------------------------------------
class MemoryTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)

        # RAM 설정
        rg = QGroupBox("RAM 메모리 설정")
        rl = QGridLayout(rg)
        rl.addWidget(QLabel("RAM 사용 상한 (GB):"), 0, 0)
        self.spin_ram = QSpinBox()
        self.spin_ram.setRange(1, 128)
        self.spin_ram.setValue(int(MEMORY_SETTINGS['RAM_LIMIT_GB']))
        self.spin_ram.valueChanged.connect(lambda v: self._set_ram(v))
        rl.addWidget(self.spin_ram, 0, 1)

        rl.addWidget(QLabel("경고 임계치 (%):"), 0, 2)
        self.spin_ram_warn = QSpinBox()
        self.spin_ram_warn.setRange(50, 95)
        self.spin_ram_warn.setValue(MEMORY_SETTINGS['RAM_WARNING_PCT'])
        self.spin_ram_warn.valueChanged.connect(lambda v: (
            MEMORY_SETTINGS.update({'RAM_WARNING_PCT': v}),
            setattr(mem_manager, 'warning_pct', v)
        ))
        rl.addWidget(self.spin_ram_warn, 0, 3)

        rl.addWidget(QLabel("DuckDB 메모리 (GB):"), 1, 0)
        self.spin_duckdb_mem = QSpinBox()
        self.spin_duckdb_mem.setRange(1, 64)
        self.spin_duckdb_mem.setValue(int(DUCKDB_SETTINGS['MEMORY_LIMIT'].replace('GB', '')))
        self.spin_duckdb_mem.valueChanged.connect(lambda v: DUCKDB_SETTINGS.update({'MEMORY_LIMIT': f'{v}GB'}))
        rl.addWidget(self.spin_duckdb_mem, 1, 1)

        self.chk_gc = QCheckBox("각 단계 후 자동 GC")
        self.chk_gc.setChecked(MEMORY_SETTINGS['GC_AFTER_EACH_STEP'])
        self.chk_gc.stateChanged.connect(lambda s: (
            MEMORY_SETTINGS.update({'GC_AFTER_EACH_STEP': s == Qt.Checked}),
            setattr(mem_manager, 'gc_after_step', s == Qt.Checked)
        ))
        rl.addWidget(self.chk_gc, 1, 2, 1, 2)

        self.chk_dtype = QCheckBox("자동 dtype 최적화")
        self.chk_dtype.setChecked(MEMORY_SETTINGS['DTYPE_OPTIMIZE'])
        self.chk_dtype.stateChanged.connect(lambda s: MEMORY_SETTINGS.update({'DTYPE_OPTIMIZE': s == Qt.Checked}))
        rl.addWidget(self.chk_dtype, 2, 0, 1, 2)

        btn_gc = QPushButton("지금 GC 실행")
        btn_gc.clicked.connect(lambda: self.log_signal.emit(f"GC: {mem_manager.force_cleanup()}개 수집"))
        rl.addWidget(btn_gc, 2, 2, 1, 2)
        ly.addWidget(rg)

        # GPU 설정
        gg = QGroupBox("GPU 설정")
        gl = QGridLayout(gg)
        self.chk_gpu = QCheckBox("GPU 사용")
        self.chk_gpu.setChecked(GPU_SETTINGS['USE_GPU'])
        def _on_gpu_toggle(s):
            enabled = s == Qt.Checked
            GPU_SETTINGS['USE_GPU'] = enabled
            gpu_manager.use_gpu = enabled
            if enabled and not gpu_manager.gpu_available:
                gpu_manager._init_gpu()
        self.chk_gpu.stateChanged.connect(_on_gpu_toggle)
        gl.addWidget(self.chk_gpu, 0, 0)

        gl.addWidget(QLabel("GPU 메모리 비율 (%):"), 0, 1)
        self.spin_gpu_frac = QSpinBox()
        self.spin_gpu_frac.setRange(10, 90)
        self.spin_gpu_frac.setValue(int(GPU_SETTINGS['GPU_MEMORY_FRACTION'] * 100))
        self.spin_gpu_frac.valueChanged.connect(lambda v: gpu_manager.set_memory_fraction(v / 100))
        gl.addWidget(self.spin_gpu_frac, 0, 2)

        self.lbl_gpu = QLabel("GPU: 미감지")
        gl.addWidget(self.lbl_gpu, 1, 0, 1, 3)
        ly.addWidget(gg)

        # 청크 설정
        cg = QGroupBox("청크 (Chunk) 크기 설정")
        cl = QGridLayout(cg)
        self.chunk_spins = {}
        for i, (dt, label) in enumerate([('sas', 'SAS 파일'), ('hana', 'HANA DB'),
                                          ('csv', 'CSV 파일'), ('analysis', '분석')]):
            cl.addWidget(QLabel(f"{label}:"), i, 0)
            sp = QSpinBox()
            sp.setRange(CHUNK_SETTINGS['MIN_CHUNK'], CHUNK_SETTINGS['MAX_CHUNK'])
            sp.setSingleStep(5000)
            sp.setValue(chunk_controller.get_chunk(dt))
            sp.valueChanged.connect(lambda v, d=dt: chunk_controller.set_chunk(d, v))
            cl.addWidget(sp, i, 1)
            self.chunk_spins[dt] = sp

        btn_auto = QPushButton("메모리 기반 자동 조절")
        btn_auto.clicked.connect(self._auto_chunk)
        cl.addWidget(btn_auto, 4, 0, 1, 2)
        ly.addWidget(cg)

        # 실시간 모니터
        mg = QGroupBox("실시간 메모리 모니터")
        ml = QVBoxLayout(mg)
        self.mem_label = QLabel("로딩 중...")
        self.mem_label.setFont(QFont("Consolas", 10))
        ml.addWidget(self.mem_label)
        self.mem_bar = QProgressBar()
        self.mem_bar.setRange(0, 100)
        ml.addWidget(self.mem_bar)
        ly.addWidget(mg)

        ly.addStretch()
        self._update_mem_status()

    # --- actions ---
    def _set_ram(self, gb):
        MEMORY_SETTINGS['RAM_LIMIT_GB'] = float(gb)
        mem_manager.ram_limit_gb = float(gb)

    def _auto_chunk(self):
        chunk_controller.auto_adjust()
        for dt, sp in self.chunk_spins.items():
            sp.setValue(chunk_controller.get_chunk(dt))
        self.log_signal.emit("청크 크기 자동 조절 완료")

    def _update_mem_status(self):
        try:
            info = mem_manager.get_memory_info()
            txt = (f"시스템: {info['used_gb']:.1f}/{info['total_gb']:.1f} GB ({info['percent']:.0f}%)"
                   f"  |  프로세스: {info['process_rss_mb']:.0f} MB"
                   f"  |  가용: {info['available_gb']:.1f} GB")
            self.mem_label.setText(txt)
            self.mem_bar.setValue(int(info['percent']))

            # 상태바에도 표시
            mw = self.ctx.main_window
            if mw:
                mw.statusBar.showMessage(f"RAM: {info['process_rss_mb']:.0f}MB / {info['percent']:.0f}%")

            # GPU 정보
            gi = gpu_manager.get_gpu_info()
            if gi['available']:
                self.lbl_gpu.setText(f"GPU: {gi['name']} | {gi['used_mb']:.0f}/{gi['total_mb']:.0f} MB")
            else:
                self.lbl_gpu.setText("GPU: 미감지 (CPU 모드)")
        except Exception:
            # 실시간 모니터링 중 발생한 예외는 로그로만 남김
            pass


# ---------------------------------------------------------------------------
# Tab 3: HANA 탐색
# ---------------------------------------------------------------------------
class HanaBrowserTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, connection_tab: ConnectionTab, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.connection_tab = connection_tab
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)

        ctl = QHBoxLayout()
        btn_schema = QPushButton("스키마 목록 조회")
        btn_schema.clicked.connect(self.load_schemas)
        ctl.addWidget(btn_schema)

        ctl.addWidget(QLabel("검색:"))
        self.hana_search = QLineEdit()
        self.hana_search.setPlaceholderText("테이블명 키워드")
        ctl.addWidget(self.hana_search)
        btn_search = QPushButton("검색")
        btn_search.clicked.connect(self.search_hana_tables)
        ctl.addWidget(btn_search)
        ly.addLayout(ctl)

        splitter = QSplitter(Qt.Horizontal)

        # 스키마/테이블 트리
        self.hana_tree = QTreeWidget()
        self.hana_tree.setHeaderLabels(['이름', '유형', '행수'])
        self.hana_tree.itemClicked.connect(self.on_tree_click)
        splitter.addWidget(self.hana_tree)

        # 컬럼 정보
        self.column_table = QTableWidget(0, 5)
        self.column_table.setHorizontalHeaderLabels(['컬럼명', '타입', '길이', 'Null', '설명'])
        self.column_table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.column_table)
        splitter.setSizes([400, 600])
        ly.addWidget(splitter)

        # 선택한 테이블 → 데이터 로드 매핑
        btn_map = QPushButton("선택한 테이블을 데이터 로드 탭에 매핑")
        btn_map.clicked.connect(self.map_selected_to_load)
        ly.addWidget(btn_map)

    # --- helpers ---
    def _ensure_dm(self):
        self.connection_tab._init_dm()

    def _hana_creds(self):
        ct = self.connection_tab
        return ct.hana_host.text(), ct.hana_port.text(), ct.hana_user.text(), ct.hana_pass.text()

    # --- actions ---
    def load_schemas(self):
        try:
            self._ensure_dm()
            h, p, u, pw = self._hana_creds()
            if self.ctx.dm.hana:
                creds_changed = (self.ctx.dm.hana.host != h or str(self.ctx.dm.hana.port) != p
                                 or self.ctx.dm.hana.user != u)
                if creds_changed:
                    self.ctx.dm.hana.destroy()
                    self.ctx.dm.hana = None
            if not self.ctx.dm.hana:
                self.ctx.dm.connect_hana(h, int(p), u, pw)
            schemas = self.ctx.dm.get_hana_schemas()
            self.hana_tree.clear()
            for s in schemas:
                item = QTreeWidgetItem([s, 'SCHEMA', ''])
                item.setData(0, Qt.UserRole, {'type': 'schema', 'name': s})
                self.hana_tree.addTopLevelItem(item)
            self.log_signal.emit(f"스키마 {len(schemas)}개 로드됨")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("HANA 스키마 로드 실패")
            QMessageBox.critical(self, "오류", format_error_for_user(e))
        except Exception as e:
            logger.exception("HANA 스키마 로드 중 예기치 않은 오류")
            QMessageBox.critical(self, "오류", format_error_for_user(e))

    def on_tree_click(self, item, col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        try:
            if data['type'] == 'schema':
                if item.childCount() == 0:
                    tables = self.ctx.dm.get_hana_tables(data['name'])
                    for t in tables:
                        child = QTreeWidgetItem([t['name'], t['type'], ''])
                        child.setData(0, Qt.UserRole, {'type': 'table', 'schema': data['name'], 'name': t['name']})
                        item.addChild(child)
                    self.log_signal.emit(f"{data['name']}: {len(tables)}개 객체")
            elif data['type'] == 'table':
                cols = self.ctx.dm.get_hana_columns(data['schema'], data['name'])
                self.column_table.setRowCount(len(cols))
                for i, c in enumerate(cols):
                    self.column_table.setItem(i, 0, QTableWidgetItem(c['name']))
                    self.column_table.setItem(i, 1, QTableWidgetItem(c['type']))
                    self.column_table.setItem(i, 2, QTableWidgetItem(str(c.get('length', ''))))
                    self.column_table.setItem(i, 3, QTableWidgetItem(c.get('nullable', '')))
                    self.column_table.setItem(i, 4, QTableWidgetItem(c.get('comment', '') or ''))
                self.column_table.resizeColumnsToContents()
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("HANA 트리 클릭 오류")
            self.log_signal.emit(f"오류: {format_error_for_user(e)}")
        except Exception as e:
            logger.exception("HANA 트리 클릭 중 예기치 않은 오류")
            self.log_signal.emit(f"오류: {format_error_for_user(e)}")

    def search_hana_tables(self):
        try:
            kw = self.hana_search.text().strip()
            if not kw:
                return
            sel = self.hana_tree.currentItem()
            schema = None
            if sel:
                d = sel.data(0, Qt.UserRole)
                if d:
                    schema = d.get('schema', d.get('name'))
            if not schema:
                QMessageBox.warning(self, "안내", "먼저 스키마를 선택하세요")
                return
            results = self.ctx.dm.search_hana_tables(schema, kw)
            self.hana_tree.clear()
            parent = QTreeWidgetItem([schema, 'SCHEMA', f'{len(results)} results'])
            parent.setData(0, Qt.UserRole, {'type': 'schema', 'name': schema})
            for r in results:
                child = QTreeWidgetItem([r, 'TABLE/VIEW', ''])
                child.setData(0, Qt.UserRole, {'type': 'table', 'schema': schema, 'name': r})
                parent.addChild(child)
            self.hana_tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            self.log_signal.emit(f"'{kw}' 검색: {len(results)}개")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("HANA 검색 실패")
            self.log_signal.emit(f"검색 오류: {format_error_for_user(e)}")
        except Exception as e:
            logger.exception("HANA 검색 중 예기치 않은 오류")
            self.log_signal.emit(f"검색 오류: {format_error_for_user(e)}")

    def map_selected_to_load(self):
        """선택한 HANA 테이블을 데이터 로드 탭에 매핑"""
        sel = self.hana_tree.currentItem()
        if not sel:
            return
        d = sel.data(0, Qt.UserRole)
        if not d or d['type'] != 'table':
            QMessageBox.warning(self, "안내", "테이블을 선택하세요")
            return

        hana_table = d['name']
        hana_schema = d.get('schema', '')
        # DataLoadTab의 table_inputs에 접근
        data_load_tab = self.ctx.main_window.data_load_tab
        app_tables = list(data_load_tab.table_inputs.keys())
        choice, ok = QInputDialog.getItem(self, "매핑 대상 선택",
                                           f"'{hana_table}'을(를) 어떤 테이블로 로드할까요?",
                                           app_tables, 0, False)
        if ok and choice:
            inp = data_load_tab.table_inputs[choice]
            inp['combo'].setCurrentIndex(0)  # HANA DB
            inp['path'].setText(f"{hana_schema}.{hana_table}" if hana_schema else hana_table)
            self.log_signal.emit(f"매핑: {hana_schema}.{hana_table} → {choice}")


# ---------------------------------------------------------------------------
# Tab 4: 데이터 로드
# ---------------------------------------------------------------------------
class DataLoadTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, connection_tab: ConnectionTab, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.connection_tab = connection_tab
        self.table_inputs = {}
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)

        fg = QGroupBox("테이블별 데이터 소스")
        fl = QGridLayout(fg)

        tables = [
            ('T20', '진료명세서'), ('T30', '진료내역'), ('T40', '상병내역'),
            ('T60', '처방전'), ('JK', '자격DB'), ('YK', '요양기관'),
            ('DEATH', '사망(HHDT_DEATH)'),
            ('GJ_LEGACY', '검진(2002-2017 통합)'),
        ]

        # 2018-2024 연도별 검진결과/문진
        for y in range(2018, 2025):
            tables.append((f'GJ_RESULT_{y}', f'검진결과({y})'))
            tables.append((f'GJ_QUEST_{y}', f'검진문진({y})'))

        for i, (tname, tlabel) in enumerate(tables):
            fl.addWidget(QLabel(tlabel), i, 0)
            combo = QComboBox()
            combo.addItems(['HANA DB', 'SAS 파일', 'CSV 파일', '(미사용)'])
            combo.setCurrentIndex(3)  # 기본: 미사용
            fl.addWidget(combo, i, 1)

            path_edit = QLineEdit()
            path_edit.setPlaceholderText("HANA 테이블명 또는 파일경로 (분할파일: 세미콜론 구분)")
            fl.addWidget(path_edit, i, 2)

            btn = QPushButton("...")
            btn.setMaximumWidth(35)
            btn.clicked.connect(lambda _, e=path_edit: self._browse_file(e))
            fl.addWidget(btn, i, 3)

            self.table_inputs[tname] = {'combo': combo, 'path': path_edit}

        # 스크롤
        scroll = QScrollArea()
        scroll.setWidget(fg)
        scroll.setWidgetResizable(True)
        ly.addWidget(scroll)

        bly = QHBoxLayout()
        self.btn_load = QPushButton("데이터 로드 시작")
        self.btn_load.setStyleSheet("background-color: #3498DB; color: white; font-size: 14px; padding: 10px;")
        self.btn_load.clicked.connect(self.start_data_load)
        bly.addWidget(self.btn_load)

        self.btn_merge = QPushButton("검진 데이터 통합 (연도별 → 통합)")
        self.btn_merge.setStyleSheet("background-color: #8E44AD; color: white; font-size: 14px; padding: 10px;")
        self.btn_merge.clicked.connect(self.merge_exam_data)
        bly.addWidget(self.btn_merge)
        ly.addLayout(bly)

        self.load_status = QTableWidget(0, 3)
        self.load_status.setHorizontalHeaderLabels(['테이블', '건수', '상태'])
        self.load_status.horizontalHeader().setStretchLastSection(True)
        ly.addWidget(self.load_status)

    # --- helpers ---
    def _browse_file(self, edit):
        """단일 또는 다중 파일 선택 (세미콜론으로 구분 저장)"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "파일 선택 (여러 파일 선택 가능)", "",
            "CSV (*.csv);;SAS (*.sas7bdat);;All (*)"
        )
        if paths:
            edit.setText(';'.join(paths))
            if len(paths) > 1:
                self.log_signal.emit(f"분할 파일 {len(paths)}개 선택됨")

    def _ensure_dm(self):
        self.connection_tab._init_dm()

    # --- actions ---
    def start_data_load(self):
        mw = self.ctx.main_window
        if mw._is_worker_running():
            return
        self._ensure_dm()
        self.ctx.dm.init_storage()
        load_cfg = {}

        for tname, inp in self.table_inputs.items():
            src = inp['combo'].currentText()
            path = inp['path'].text().strip()
            if not path or src == '(미사용)':
                continue
            if src == 'HANA DB':
                if '.' in path:
                    hana_schema, hana_table = path.split('.', 1)
                else:
                    hana_schema, hana_table = '', path
                    self.log_signal.emit(f"[경고] {tname}: 스키마 없이 테이블명만 입력됨 ({path}). "
                                         f"'SCHEMA.TABLE' 형식 권장. 스키마 없이 시도합니다.")
                load_cfg[tname] = {'type': 'hana', 'schema': hana_schema, 'hana_table': hana_table}
            elif src == 'SAS 파일':
                paths = [p.strip() for p in path.split(';') if p.strip()]
                if len(paths) > 1:
                    load_cfg[tname] = {'type': 'sas_multi', 'paths': paths}
                else:
                    load_cfg[tname] = {'type': 'sas', 'path': paths[0]}
            else:
                paths = [p.strip() for p in path.split(';') if p.strip()]
                if len(paths) > 1:
                    load_cfg[tname] = {'type': 'csv_multi', 'paths': paths}
                else:
                    load_cfg[tname] = {'type': 'csv', 'path': paths[0]}

        if not load_cfg:
            QMessageBox.warning(self, "안내", "최소 하나의 소스를 지정하세요")
            return

        mw.progress_bar.setVisible(True)

        # HANA 자격증명을 메인 스레드에서 미리 캡처
        ct = self.connection_tab
        hana_host = ct.hana_host.text()
        hana_port = ct.hana_port.text()
        hana_user = ct.hana_user.text()
        hana_pass = ct.hana_pass.text()

        dm = self.ctx.dm  # capture for worker closure

        def do_load(progress_callback=None):
            results = {}
            for tn, src in load_cfg.items():
                if progress_callback:
                    progress_callback(f"{tn} 로드 중...")
                if src['type'] == 'hana':
                    if not dm.hana or not dm.hana.conn:
                        dm.connect_hana(hana_host, int(hana_port),
                                         hana_user, hana_pass)
                    cnt = dm.load_from_hana(tn, src['schema'], src.get('hana_table', tn))
                elif src['type'] == 'sas':
                    cnt = dm.load_from_sas(tn, src['path'])
                elif src['type'] == 'sas_multi':
                    cnt = dm.load_from_files_multi(tn, src['paths'], file_type='sas',
                                                    progress_callback=progress_callback)
                elif src['type'] == 'csv_multi':
                    cnt = dm.load_from_files_multi(tn, src['paths'], file_type='csv',
                                                    progress_callback=progress_callback)
                else:
                    cnt = dm.load_from_csv(tn, src['path'])
                results[tn] = cnt
            return results

        mw._set_action_buttons_enabled(False)
        from main_app import WorkerThread
        self.ctx.worker = WorkerThread(do_load)
        mw.worker = self.ctx.worker
        self.ctx.worker.progress.connect(self.log_signal.emit)
        self.ctx.worker.finished.connect(self._on_loaded)
        self.ctx.worker.error.connect(mw._on_error)
        self.ctx.worker.start()

    def _on_loaded(self, data):
        mw = self.ctx.main_window
        mw.progress_bar.setVisible(False)
        mw._set_action_buttons_enabled(True)
        results = data.get('result', {})
        self.load_status.setRowCount(len(results))
        for i, (t, c) in enumerate(results.items()):
            self.load_status.setItem(i, 0, QTableWidgetItem(t))
            self.load_status.setItem(i, 1, QTableWidgetItem(format_number(c)))
            self.load_status.setItem(i, 2, QTableWidgetItem("완료"))
        self.log_signal.emit(f"로드 완료: {len(results)}개 테이블")

    def merge_exam_data(self):
        """검진 데이터 통합 (연도별 → 통합) (WorkerThread로 GUI 블로킹 방지)"""
        mw = self.ctx.main_window
        if mw._is_worker_running():
            return
        self._ensure_dm()
        self.ctx.dm.init_storage()

        dm = self.ctx.dm

        def do_merge(progress_callback=None):
            nr, nq = dm.merge_exam_data(progress_callback)
            return {'nr': nr, 'nq': nq}

        def on_done(data):
            mw.progress_bar.setVisible(False)
            mw._set_action_buttons_enabled(True)
            nr = data.get('result', {}).get('nr', 0)
            nq = data.get('result', {}).get('nq', 0)
            self.log_signal.emit(f"검진결과 통합: {format_number(nr)}건, 문진 통합: {format_number(nq)}건")
            QMessageBox.information(self, "완료", f"GJ_RESULT: {format_number(nr)}건\nGJ_QUEST: {format_number(nq)}건")

        mw.progress_bar.setVisible(True)
        mw._set_action_buttons_enabled(False)
        from main_app import WorkerThread
        self.ctx.worker = WorkerThread(do_merge)
        mw.worker = self.ctx.worker
        self.ctx.worker.progress.connect(self.log_signal.emit)
        self.ctx.worker.finished.connect(on_done)
        self.ctx.worker.error.connect(mw._on_error)
        self.ctx.worker.start()


# ---------------------------------------------------------------------------
# Tab 5: 코호트 구축
# ---------------------------------------------------------------------------
class CohortTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, connection_tab: ConnectionTab, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.connection_tab = connection_tab
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)

        sg = QGroupBox("연구 설정 (프로토콜)")
        gl = QGridLayout(sg)
        gl.addWidget(QLabel("연구기간:"), 0, 0)
        self.spin_start = QSpinBox(); self.spin_start.setRange(2002, 2024); self.spin_start.setValue(STUDY_SETTINGS['STUDY_START_YEAR'])
        gl.addWidget(self.spin_start, 0, 1)
        gl.addWidget(QLabel("~"), 0, 2)
        self.spin_end = QSpinBox(); self.spin_end.setRange(2002, 2024); self.spin_end.setValue(STUDY_SETTINGS['STUDY_END_YEAR'])
        gl.addWidget(self.spin_end, 0, 3)

        gl.addWidget(QLabel("진입기간:"), 0, 4)
        self.spin_es = QSpinBox(); self.spin_es.setRange(2002, 2024); self.spin_es.setValue(STUDY_SETTINGS['ENROLLMENT_START'])
        gl.addWidget(self.spin_es, 0, 5)
        gl.addWidget(QLabel("~"), 0, 6)
        self.spin_ee = QSpinBox(); self.spin_ee.setRange(2002, 2024); self.spin_ee.setValue(STUDY_SETTINGS['ENROLLMENT_END'])
        gl.addWidget(self.spin_ee, 0, 7)

        gl.addWidget(QLabel("연령:"), 1, 0)
        self.spin_minage = QSpinBox(); self.spin_minage.setRange(20, 80); self.spin_minage.setValue(STUDY_SETTINGS['MIN_AGE'])
        gl.addWidget(self.spin_minage, 1, 1)
        gl.addWidget(QLabel("~"), 1, 2)
        self.spin_maxage = QSpinBox(); self.spin_maxage.setRange(20, 80); self.spin_maxage.setValue(STUDY_SETTINGS['MAX_AGE'])
        gl.addWidget(self.spin_maxage, 1, 3)

        gl.addWidget(QLabel("외래 최소:"), 1, 4)
        self.spin_outpt = QSpinBox(); self.spin_outpt.setRange(1, 10); self.spin_outpt.setValue(STUDY_SETTINGS['MIN_DM_CLAIMS_OUTPATIENT'])
        gl.addWidget(self.spin_outpt, 1, 5)
        gl.addWidget(QLabel("입원 최소:"), 1, 6)
        self.spin_inpt = QSpinBox(); self.spin_inpt.setRange(1, 10); self.spin_inpt.setValue(STUDY_SETTINGS['MIN_DM_CLAIMS_INPATIENT'])
        gl.addWidget(self.spin_inpt, 1, 7)

        gl.addWidget(QLabel("PSM 1:N"), 2, 0)
        self.spin_psm = QSpinBox(); self.spin_psm.setRange(1, 5); self.spin_psm.setValue(STUDY_SETTINGS['PSM_RATIO'])
        gl.addWidget(self.spin_psm, 2, 1)
        ly.addWidget(sg)

        self.btn_cohort = QPushButton("코호트 구축 + 변수 생성")
        self.btn_cohort.setStyleSheet("background-color: #27AE60; color: white; font-size: 14px; padding: 10px;")
        self.btn_cohort.clicked.connect(self.start_cohort)
        ly.addWidget(self.btn_cohort)

        self.cohort_text = QTextEdit(); self.cohort_text.setReadOnly(True)
        ly.addWidget(self.cohort_text)

    # --- helpers ---
    def _ensure_dm(self):
        self.connection_tab._init_dm()

    # --- actions ---
    def start_cohort(self):
        mw = self.ctx.main_window
        if mw._is_worker_running():
            return
        self._ensure_dm()
        STUDY_SETTINGS['STUDY_START_YEAR'] = self.spin_start.value()
        STUDY_SETTINGS['STUDY_END_YEAR'] = self.spin_end.value()
        STUDY_SETTINGS['ENROLLMENT_START'] = self.spin_es.value()
        STUDY_SETTINGS['ENROLLMENT_END'] = self.spin_ee.value()
        STUDY_SETTINGS['MIN_AGE'] = self.spin_minage.value()
        STUDY_SETTINGS['MAX_AGE'] = self.spin_maxage.value()
        STUDY_SETTINGS['MIN_DM_CLAIMS_OUTPATIENT'] = self.spin_outpt.value()
        STUDY_SETTINGS['MIN_DM_CLAIMS_INPATIENT'] = self.spin_inpt.value()
        STUDY_SETTINGS['PSM_RATIO'] = self.spin_psm.value()

        mw.progress_bar.setVisible(True)

        dm = self.ctx.dm

        def do_cohort(progress_callback=None):
            builder = CohortBuilder(dm)
            cr = builder.build_cohort(progress_callback)
            gen = VariableGenerator(dm)
            gen.generate_all(progress_callback)
            return cr

        mw._set_action_buttons_enabled(False)
        from main_app import WorkerThread
        self.ctx.worker = WorkerThread(do_cohort)
        mw.worker = self.ctx.worker
        self.ctx.worker.progress.connect(self.log_signal.emit)
        self.ctx.worker.progress.connect(lambda m: self.cohort_text.append(m))
        self.ctx.worker.finished.connect(self._on_cohort)
        self.ctx.worker.error.connect(mw._on_error)
        self.ctx.worker.start()

    def _on_cohort(self, data):
        mw = self.ctx.main_window
        mw.progress_bar.setVisible(False)
        mw._set_action_buttons_enabled(True)
        cr = data.get('result', {})
        self.ctx.all_results['cohort'] = cr
        txt = f"기본 대상: {format_number(cr.get('base_n', ''))}\n"
        txt += f"제외: {format_number(cr.get('excluded_dementia', ''))}\n"
        txt += f"최종: {format_number(cr.get('final_n', ''))}\n"
        self.cohort_text.append(txt)


# ---------------------------------------------------------------------------
# Tab 6: 분석 실행
# ---------------------------------------------------------------------------
class AnalysisTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, connection_tab: ConnectionTab, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.connection_tab = connection_tab
        self._sampling_label = ""
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)
        og = QGroupBox("분석 옵션")
        ol = QVBoxLayout(og)
        self.chk_cox = QCheckBox("Cox 회귀 (3단계 모형)"); self.chk_cox.setChecked(True); ol.addWidget(self.chk_cox)
        self.chk_psm = QCheckBox("PSM (T1DM vs T2DM)"); self.chk_psm.setChecked(True); ol.addWidget(self.chk_psm)
        self.chk_int = QCheckBox("상호작용 (유형×유병기간)"); self.chk_int.setChecked(True); ol.addWidget(self.chk_int)
        self.chk_sg = QCheckBox("하위그룹 (성별,연령,합병증,CVD)"); self.chk_sg.setChecked(True); ol.addWidget(self.chk_sg)
        self.chk_cr = QCheckBox("경쟁위험 분석 (Fine-Gray)"); self.chk_cr.setChecked(True); ol.addWidget(self.chk_cr)
        self.chk_sens = QCheckBox("민감도 분석"); self.chk_sens.setChecked(True); ol.addWidget(self.chk_sens)
        ly.addWidget(og)

        rl = QHBoxLayout()
        rl.addWidget(QLabel("결과 폴더:"))
        self.res_dir_edit = QLineEdit(str(self.ctx.results_dir.absolute()))
        rl.addWidget(self.res_dir_edit)
        btn_rb = QPushButton("찾아보기"); btn_rb.clicked.connect(lambda: self._browse_dir(self.res_dir_edit))
        rl.addWidget(btn_rb)
        ly.addLayout(rl)

        self.btn_analysis = QPushButton("분석 실행")
        self.btn_analysis.setStyleSheet("background-color: #E74C3C; color: white; font-size: 14px; padding: 10px;")
        self.btn_analysis.clicked.connect(self.start_analysis)
        ly.addWidget(self.btn_analysis)

        self.analysis_text = QTextEdit(); self.analysis_text.setReadOnly(True)
        ly.addWidget(self.analysis_text)

    # --- helpers ---
    def _browse_dir(self, edit):
        d = QFileDialog.getExistingDirectory(self, "폴더 선택")
        if d:
            edit.setText(d)

    def _ensure_dm(self):
        self.connection_tab._init_dm()

    def _show_sampling_dialog(self, info) -> bool:
        """샘플링 정보 모달 다이얼로그. 메인 스레드에서만 호출해야 함."""
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("데이터 샘플링 적용")
        msg.setText(
            f"<b>⚠ 층화 샘플링이 적용됩니다</b><br><br>"
            f"전체 데이터: <b>{info.total_rows:,}건</b><br>"
            f"분석 대상: <b>{info.sampled_rows:,}건</b> "
            f"({info.ratio_pct:.1f}% 샘플링)<br><br>"
            f"메모리 한계로 인해 비례 층화 샘플링이 적용됩니다.<br>"
            f"결과 해석 시 이 점을 반드시 고려하세요."
        )
        msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        msg.button(QMessageBox.Ok).setText("계속 진행")
        msg.button(QMessageBox.Cancel).setText("취소")
        return msg.exec_() == QMessageBox.Ok

    # --- actions ---
    def start_analysis(self):
        mw = self.ctx.main_window
        if mw._is_worker_running():
            return
        self._ensure_dm()
        self.ctx.results_dir = Path(self.res_dir_edit.text())
        self.ctx.results_dir.mkdir(parents=True, exist_ok=True)
        mw.progress_bar.setVisible(True)

        # 체크박스 상태를 메인 스레드에서 미리 읽어 워커로 전달
        run_cox  = self.chk_cox.isChecked()
        run_psm  = self.chk_psm.isChecked()
        run_int  = self.chk_int.isChecked()
        run_sg   = self.chk_sg.isChecked()
        run_cr   = self.chk_cr.isChecked()
        run_sens = self.chk_sens.isChecked()

        dm = self.ctx.dm

        def do_analysis(progress_callback=None):
            analyzer = StatisticalAnalyzer(dm)
            return analyzer.run_selected(
                progress_callback,
                run_cox=run_cox, run_psm=run_psm,
                run_interaction=run_int, run_subgroup=run_sg,
                run_sensitivity=run_sens,
                run_competing_risks=run_cr,
            )

        mw._set_action_buttons_enabled(False)
        from main_app import WorkerThread
        self.ctx.worker = WorkerThread(do_analysis)
        mw.worker = self.ctx.worker
        self.ctx.worker.progress.connect(self.log_signal.emit)
        self.ctx.worker.progress.connect(lambda m: self.analysis_text.append(m))
        self.ctx.worker.finished.connect(self._on_analysis)
        self.ctx.worker.error.connect(mw._on_error)
        self.ctx.worker.start()

    def _on_analysis(self, data):
        mw = self.ctx.main_window
        mw.progress_bar.setVisible(False)
        mw._set_action_buttons_enabled(True)
        ar = data.get('result', {})
        self.ctx.all_results['analysis'] = ar

        # 샘플링 경고 팝업 표시 (메인 스레드에서 실행됨)
        sampling_info = ar.get('sampling_info')
        if sampling_info is not None and sampling_info.applied:
            self._show_sampling_dialog(sampling_info)
            self._sampling_label = sampling_info.label
            self.ctx.sampling_label = sampling_info.label
        else:
            self._sampling_label = ""
            self.ctx.sampling_label = ""

        # 시각화 + 내보내기 (GUI 비의존 모듈로 분리)
        from analysis_runner import run_post_analysis
        result = run_post_analysis(self.ctx.dm, ar, self.ctx.results_dir, log=self.log_signal.emit)
        for err in result.get('errors', []):
            self.log_signal.emit(err)

        self.log_signal.emit(f"분석 완료! 결과: {self.ctx.results_dir}")
        QMessageBox.information(self, "완료", f"분석 완료\n{self.ctx.results_dir}")


# ---------------------------------------------------------------------------
# Tab 7: 결과 확인
# ---------------------------------------------------------------------------
class ResultsTab(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._init_ui()

    def _init_ui(self):
        ly = QVBoxLayout(self)
        self.res_combo = QComboBox()
        self.res_combo.addItems(['Table 1', 'Cox (All-cause)', 'Cox (AD)', 'Cox (VaD)', 'PSM', '하위그룹'])
        self.res_combo.currentIndexChanged.connect(self.show_result)
        ly.addWidget(self.res_combo)
        self.result_title_label = QLabel("")
        font = QFont(); font.setBold(True)
        self.result_title_label.setFont(font)
        ly.addWidget(self.result_title_label)
        self.result_table = QTableWidget()
        ly.addWidget(self.result_table)

        bl = QHBoxLayout()
        for lbl, fn in [('Excel 내보내기', lambda: self.export('xlsx')), ('전체 내보내기', self.export_all)]:
            b = QPushButton(lbl); b.clicked.connect(fn); bl.addWidget(b)
        for lbl, fn in [('KM 곡선', self.plot_km), ('Forest Plot', self.plot_forest)]:
            b = QPushButton(lbl); b.clicked.connect(fn); bl.addWidget(b)
        ly.addLayout(bl)

    # --- helpers ---
    def _get_result_df(self, idx):
        """인덱스에 해당하는 분석 결과 DataFrame 반환"""
        ar = self.ctx.all_results.get('analysis', {})
        if idx == 0:
            return ar.get('table1'), 'Table1'
        elif idx == 1:
            return ar.get('cox_dementia_event', {}).get('model3_full', {}).get('summary'), 'Cox_AllCause'
        elif idx == 2:
            return ar.get('cox_ad_event', {}).get('model3_full', {}).get('summary'), 'Cox_AD'
        elif idx == 3:
            return ar.get('cox_vad_event', {}).get('model3_full', {}).get('summary'), 'Cox_VaD'
        elif idx == 4:
            psm = ar.get('psm', {})
            balance = psm.get('balance', {})
            if balance:
                df = pd.DataFrame(balance).T.reset_index()
                df.columns = ['Variable'] + list(df.columns[1:])
                return df, 'PSM_Balance'
        elif idx == 5:
            sg = ar.get('subgroup', {})
            rows = []
            for sn, sd in sg.items():
                for var, hr in sd.get('hr_data', {}).items():
                    rows.append({'Subgroup': sn, 'Variable': var,
                                 'N': sd.get('n', ''), 'Events': sd.get('events', ''),
                                 'HR': hr.get('hr', ''), 'CI_Lower': hr.get('ci_lower', ''),
                                 'CI_Upper': hr.get('ci_upper', ''), 'P': hr.get('p_value', '')})
            if rows:
                return pd.DataFrame(rows), 'Subgroup'
        return None, 'Sheet1'

    # --- actions ---
    def show_result(self, idx):
        df, base_title = self._get_result_df(idx)
        sampling_suffix = f" ({self.ctx.sampling_label})" if getattr(self.ctx, 'sampling_label', '') else ""
        self.result_title_label.setText(f"{base_title}{sampling_suffix}")
        if df is not None:
            df2 = df.reset_index() if df.index.name else df
            self.result_table.setRowCount(len(df2))
            self.result_table.setColumnCount(len(df2.columns))
            self.result_table.setHorizontalHeaderLabels([str(c) for c in df2.columns])
            for i in range(len(df2)):
                for j in range(len(df2.columns)):
                    v = df2.iloc[i, j]
                    self.result_table.setItem(i, j, QTableWidgetItem(f"{v:.4f}" if isinstance(v, float) else str(v)))
            self.result_table.resizeColumnsToContents()

    def export(self, fmt):
        """현재 결과 탭에 표시된 DataFrame을 Excel로 내보내기"""
        if not self.ctx.all_results.get('analysis'):
            QMessageBox.warning(self, "안내", "분석 결과 없음")
            return
        idx = self.res_combo.currentIndex()
        df, sheet = self._get_result_df(idx)
        if df is None:
            QMessageBox.warning(self, "안내", "내보낼 데이터 없음")
            return
        path, _ = QFileDialog.getSaveFileName(self, "저장 위치", f"{sheet}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            ar = self.ctx.all_results.get('analysis', {})
            sampling_info = ar.get('sampling_info') if ar else None
            df2 = df.reset_index() if hasattr(df, 'index') and df.index.name else df
            exp = ResultsExporter(str(self.ctx.results_dir))
            with pd.ExcelWriter(path, engine='openpyxl') as writer:
                exp._write_df_with_sampling_header(writer, df2, sheet[:31], sampling_info)
            self.log_signal.emit(f"내보내기 완료: {path}")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("Excel 내보내기 실패")
            QMessageBox.critical(self, "오류", format_error_for_user(e))
        except Exception as e:
            logger.exception("Excel 내보내기 중 예기치 않은 오류")
            QMessageBox.critical(self, "오류", format_error_for_user(e))

    def export_all(self):
        ar = self.ctx.all_results.get('analysis', {})
        if not ar:
            QMessageBox.warning(self, "안내", "분석 결과 없음")
            return
        sampling_info = ar.get('sampling_info')
        exp = ResultsExporter(str(self.ctx.results_dir))
        try:
            files = exp.export_all(ar, sampling_info=sampling_info)
            QMessageBox.information(self, "완료", f"{len(files)}개 파일 저장")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("전체 내보내기 실패")
            QMessageBox.critical(self, "오류", format_error_for_user(e))
        except Exception as e:
            logger.exception("전체 내보내기 중 예기치 않은 오류")
            QMessageBox.critical(self, "오류", format_error_for_user(e))

    def plot_km(self):
        try:
            # 필요 컬럼만 + 샘플링으로 메모리 보호 (재현 가능한 시드 사용)
            seed_float = STUDY_SETTINGS.get('SAMPLING_SEED', 42) / 100.0
            self.ctx.dm.execute(f"SELECT setseed({seed_float})")
            df = self.ctx.dm.query("""
                SELECT exposure_group, follow_up_years, dementia_event, ad_event, vad_event
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY exposure_group ORDER BY RANDOM()
                    ) AS rn
                    FROM final_analysis
                    WHERE follow_up_days > 0
                ) t
                WHERE rn <= 10000
            """)
            viz = Visualizer(str(self.ctx.results_dir))
            p = viz.plot_km(df)
            del df; import gc; gc.collect()
            self.log_signal.emit(f"KM: {p}")
            if sys.platform == 'win32' and p:
                rp = Path(p).resolve()
                if rp.is_relative_to(self.ctx.results_dir.resolve()):
                    os.startfile(p)
                else:
                    logger.warning(f"결과 디렉토리 외부 경로 차단: {p}")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("KM 곡선 생성 실패")
            QMessageBox.warning(self, "오류", format_error_for_user(e))
        except Exception as e:
            logger.exception("KM 곡선 생성 중 예기치 않은 오류")
            QMessageBox.warning(self, "오류", format_error_for_user(e))

    def plot_forest(self):
        try:
            ar = self.ctx.all_results.get('analysis', {})
            if 'subgroup' in ar:
                viz = Visualizer(str(self.ctx.results_dir))
                p = viz.plot_forest(ar['subgroup'])
                if p and sys.platform == 'win32':
                    rp = Path(p).resolve()
                    if rp.is_relative_to(self.ctx.results_dir.resolve()):
                        os.startfile(p)
                    else:
                        logger.warning(f"결과 디렉토리 외부 경로 차단: {p}")
        except (duckdb.Error, pd.errors.EmptyDataError, ValueError,
                MemoryError, CohortStepError) as e:
            logger.exception("Forest Plot 생성 실패")
            QMessageBox.warning(self, "오류", format_error_for_user(e))
        except Exception as e:
            logger.exception("Forest Plot 생성 중 예기치 않은 오류")
            QMessageBox.warning(self, "오류", format_error_for_user(e))
