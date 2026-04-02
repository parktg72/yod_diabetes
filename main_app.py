"""
main_app.py - NHIS YOD-DM Analyzer v2.0
HANA 스키마/테이블 검색 + 검진 연도별 분리 처리 + 프로토콜 반영
"""

import sys, logging, traceback
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QLabel, QTextEdit, QProgressBar,
    QMessageBox, QStatusBar
)
from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont

from config import APP_SETTINGS, MEMORY_SETTINGS, CHUNK_SETTINGS, save_settings, load_settings
from memory_manager import mem_manager, chunk_controller
from utils import setup_logging

from tabs import (
    AppContext,
    ConnectionTab, MemoryTab, HanaBrowserTab,
    DataLoadTab, CohortTab, AnalysisTab, ResultsTab
)

logger = logging.getLogger(__name__)


class WorkerThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.requestInterruption()

    @property
    def is_cancelled(self):
        return self._cancelled or self.isInterruptionRequested()

    def run(self):
        try:
            result = self.func(*self.args, progress_callback=lambda m: self.progress.emit(m), **self.kwargs)
            if not self.is_cancelled:
                self.finished.emit({'result': result})
        except Exception as e:
            if not self.is_cancelled:
                self.error.emit(f"{e}\n{traceback.format_exc()}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ctx = AppContext()
        self.ctx.main_window = self
        self.worker = None
        load_settings()  # 이전 세션 설정 복원
        # 복원된 설정을 싱글톤 매니저에 반영 (모듈 임포트 시 기본값으로 생성됨)
        mem_manager.ram_limit_gb = MEMORY_SETTINGS['RAM_LIMIT_GB']
        mem_manager.warning_pct = MEMORY_SETTINGS['RAM_WARNING_PCT']
        mem_manager.gc_after_step = MEMORY_SETTINGS['GC_AFTER_EACH_STEP']
        for dt in ('sas', 'hana', 'csv', 'analysis'):
            chunk_controller.set_chunk(dt, CHUNK_SETTINGS.get(f'{dt.upper()}_CHUNK', chunk_controller.get_chunk(dt)))
        self.setWindowTitle(f"{APP_SETTINGS['APP_NAME']} v{APP_SETTINGS['VERSION']}")
        self.resize(APP_SETTINGS['WINDOW_WIDTH'], APP_SETTINGS['WINDOW_HEIGHT'])
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- Create tabs ---
        self.connection_tab = ConnectionTab(self.ctx)
        self.memory_tab = MemoryTab(self.ctx)
        self.hana_browser_tab = HanaBrowserTab(self.ctx, self.connection_tab)
        self.data_load_tab = DataLoadTab(self.ctx, self.connection_tab)
        self.cohort_tab = CohortTab(self.ctx, self.connection_tab)
        self.analysis_tab = AnalysisTab(self.ctx, self.connection_tab)
        self.results_tab = ResultsTab(self.ctx)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.connection_tab, "1. DB 연결")
        self.tabs.addTab(self.memory_tab, "2. 메모리/GPU")
        self.tabs.addTab(self.hana_browser_tab, "3. HANA 탐색")
        self.tabs.addTab(self.data_load_tab, "4. 데이터 로드")
        self.tabs.addTab(self.cohort_tab, "5. 코호트 구축")
        self.tabs.addTab(self.analysis_tab, "6. 분석 실행")
        self.tabs.addTab(self.results_tab, "7. 결과 확인")
        layout.addWidget(self.tabs)

        # --- Wire up log signals ---
        for tab in [self.connection_tab, self.memory_tab, self.hana_browser_tab,
                     self.data_load_tab, self.cohort_tab, self.analysis_tab, self.results_tab]:
            tab.log_signal.connect(self.log)

        # 메모리 모니터 타이머
        self.mem_timer = QTimer()
        self.mem_timer.timeout.connect(self.memory_tab._update_mem_status)
        self.mem_timer.start(5000)  # 5초마다 갱신

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(130)
        self.log_text.setFont(QFont("Consolas", 9))
        layout.addWidget(QLabel("실행 로그:"))
        layout.addWidget(self.log_text)

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.statusBar.addPermanentWidget(self.progress_bar)

    # ===================== Worker 안전성 =====================
    def _is_worker_running(self):
        """Worker 스레드 중복 실행 방지"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "대기", "이전 작업이 실행 중입니다. 완료 후 다시 시도하세요.")
            return True
        return False

    def _set_action_buttons_enabled(self, enabled):
        """장시간 작업 중 액션 버튼 비활성화/활성화"""
        for attr_name, tab in [('btn_load', self.data_load_tab),
                                ('btn_merge', self.data_load_tab),
                                ('btn_cohort', self.cohort_tab),
                                ('btn_analysis', self.analysis_tab)]:
            btn = getattr(tab, attr_name, None)
            if btn:
                btn.setEnabled(enabled)

    # ===================== Actions =====================
    def log(self, msg):
        self.log_text.append(msg)
        self.statusBar.showMessage(msg)

    def _on_error(self, msg):
        self.progress_bar.setVisible(False)
        self._set_action_buttons_enabled(True)
        self.log(f"오류: {msg}")
        QMessageBox.critical(self, "오류", msg[:500])

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.quit()
            if not self.worker.wait(3000):
                logger.warning("워커 스레드 3초 내 종료 실패 — 강제 종료")
        if self.ctx.dm:
            self.ctx.dm.close()
        save_settings()  # 현재 설정 저장
        event.accept()


def main():
    setup_logging()
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setFont(QFont("맑은 고딕", 10))
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
