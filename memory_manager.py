"""
memory_manager.py - 메모리/GPU 관리 모듈
RAM 모니터링, Pandas 메모리 누수 방지, GPU 제어, dtype 최적화
"""

import gc
import os
import sys
import logging
import psutil
import numpy as np
import pandas as pd
from config import MEMORY_SETTINGS, GPU_SETTINGS, CHUNK_SETTINGS

logger = logging.getLogger(__name__)


class MemoryManager:
    """시스템 메모리 관리자"""

    def __init__(self):
        self.ram_limit_gb = MEMORY_SETTINGS['RAM_LIMIT_GB']
        self.warning_pct = MEMORY_SETTINGS['RAM_WARNING_PCT']
        self.gc_after_step = MEMORY_SETTINGS['GC_AFTER_EACH_STEP']

    # -------------------------------------------------------
    # RAM 모니터링
    # -------------------------------------------------------
    def get_memory_info(self):
        """현재 시스템 메모리 정보 반환"""
        vm = psutil.virtual_memory()
        proc = psutil.Process(os.getpid())
        proc_mem = proc.memory_info()
        return {
            'total_gb': vm.total / (1024**3),
            'available_gb': vm.available / (1024**3),
            'used_gb': vm.used / (1024**3),
            'percent': vm.percent,
            'process_rss_gb': proc_mem.rss / (1024**3),
            'process_rss_mb': proc_mem.rss / (1024**2),
        }

    def check_memory(self):
        """메모리 상태 확인 → 경고/위험 반환"""
        info = self.get_memory_info()
        if info['process_rss_gb'] > self.ram_limit_gb:
            logger.warning(f"프로세스 메모리 {info['process_rss_gb']:.1f}GB > 제한 {self.ram_limit_gb}GB")
            return 'CRITICAL', info
        if info['percent'] > self.warning_pct:
            logger.warning(f"시스템 메모리 {info['percent']}% > 경고 {self.warning_pct}%")
            return 'WARNING', info
        return 'OK', info

    def force_cleanup(self):
        """강제 메모리 정리: gc + 미참조 객체 삭제"""
        collected = gc.collect()
        gc.collect()  # 2차 수집
        logger.info(f"GC 실행: {collected}개 객체 수집됨")
        return collected

    def cleanup_after_step(self, step_name=''):
        """각 단계 후 호출 — 조건부 GC"""
        if self.gc_after_step:
            n = self.force_cleanup()
            status, info = self.check_memory()
            logger.info(f"[{step_name}] GC {n}개 수집, "
                       f"프로세스 {info['process_rss_mb']:.0f}MB, "
                       f"시스템 {info['percent']:.0f}%")
            return status
        return 'OK'

    # -------------------------------------------------------
    # Pandas 메모리 최적화
    # -------------------------------------------------------
    # ID성 컬럼: dtype 최적화 시 정밀도 손실 방지를 위해 제외
    _ID_COLUMNS = {'INDI_DSCM_NO', 'CMN_KEY', 'MCARE_DESC_LN_NO',
                   'MPRSC_GRANT_NO', 'MPRSC_SEQ_NO', 'MDCARE_SYM'}

    @staticmethod
    def optimize_dtypes(df):
        """DataFrame dtype 최적화 (메모리 절감).
        주의: df를 in-place로 수정합니다. 캐시된 원본을 보호하려면 호출 전 .copy()가 필요합니다.
        """
        if not MEMORY_SETTINGS.get('DTYPE_OPTIMIZE', True):
            return df

        start_mem = df.memory_usage(deep=True).sum() / (1024**2)

        for col in df.columns:
            # ID성 컬럼은 정밀도 보호를 위해 최적화 제외
            if col in MemoryManager._ID_COLUMNS:
                continue

            col_type = df[col].dtype

            if col_type == 'float64':
                # float64 → float32 (의료 데이터는 float32 정밀도 충분)
                try:
                    df[col] = df[col].astype('float32')
                except (ValueError, OverflowError):
                    pass  # 범위 초과 시 float64 유지

            elif col_type == 'int64' or str(col_type).startswith('Int'):
                if df[col].isna().any():
                    continue  # nullable integer + NaN → numpy int 변환 불가, 건너뜀
                c_min, c_max = df[col].min(), df[col].max()
                if c_min >= 0:
                    if c_max < 255:
                        df[col] = df[col].astype('uint8')
                    elif c_max < 65535:
                        df[col] = df[col].astype('uint16')
                    elif c_max < 4294967295:
                        df[col] = df[col].astype('uint32')
                else:
                    if c_min > -128 and c_max < 127:
                        df[col] = df[col].astype('int8')
                    elif c_min > -32768 and c_max < 32767:
                        df[col] = df[col].astype('int16')
                    elif c_min > -2147483648 and c_max < 2147483647:
                        df[col] = df[col].astype('int32')

            elif col_type == 'object':
                # 문자열 카테고리화 (고유값이 적으면)
                n_unique = df[col].nunique()
                n_total = len(df[col])
                if n_unique / max(n_total, 1) < 0.5:
                    df[col] = df[col].astype('category')

        end_mem = df.memory_usage(deep=True).sum() / (1024**2)
        reduction = (1 - end_mem / max(start_mem, 0.001)) * 100
        if reduction > 5:
            logger.info(f"dtype 최적화: {start_mem:.1f}MB → {end_mem:.1f}MB ({reduction:.0f}% 절감)")

        return df

    # -------------------------------------------------------
    # 청크 크기 자동 조절
    # -------------------------------------------------------
    def auto_chunk_size(self, estimated_row_bytes=500, target_mb=200):
        """현재 가용 메모리 기반 자동 chunk 크기 계산"""
        info = self.get_memory_info()
        available_mb = info['available_gb'] * 1024

        # 가용 메모리의 일부만 사용 (안전 마진)
        usable_mb = min(available_mb * 0.3, target_mb)
        chunk = int(usable_mb * 1024 * 1024 / max(estimated_row_bytes, 100))

        # 범위 제한
        chunk = max(chunk, CHUNK_SETTINGS['MIN_CHUNK'])
        chunk = min(chunk, CHUNK_SETTINGS['MAX_CHUNK'])

        logger.info(f"자동 chunk 크기: {chunk:,} (가용 {available_mb:.0f}MB)")
        return chunk

    def get_safe_analysis_rows(self):
        """분석 시 메모리에 올릴 안전한 최대 행수"""
        info = self.get_memory_info()
        available_gb = info['available_gb']
        # 행당 약 2KB 추정, 가용 메모리의 30% 사용
        safe_rows = int(available_gb * 0.3 * 1024 * 1024 * 1024 / 2048)
        max_rows = MEMORY_SETTINGS.get('MAX_DF_ROWS_IN_MEMORY', 500000)
        return min(safe_rows, max_rows)


class GPUManager:
    """GPU 메모리 관리자"""

    def __init__(self):
        self.use_gpu = GPU_SETTINGS['USE_GPU']
        self.memory_fraction = GPU_SETTINGS['GPU_MEMORY_FRACTION']
        self.device_id = GPU_SETTINGS['GPU_DEVICE_ID']
        self.gpu_available = False
        self.gpu_name = 'N/A'
        self.gpu_total_mb = 0

        if self.use_gpu:
            self._init_gpu()

    def _init_gpu(self):
        """GPU 초기화 및 메모리 제한 설정"""
        try:
            import torch
            if torch.cuda.is_available():
                self.gpu_available = True
                self.gpu_name = torch.cuda.get_device_name(self.device_id)
                props = torch.cuda.get_device_properties(self.device_id)
                self.gpu_total_mb = props.total_memory / (1024**2)  # PyTorch: total_memory (not total_mem)

                # 메모리 제한 설정
                fraction = self.memory_fraction
                limit_mb = int(self.gpu_total_mb * fraction)
                torch.cuda.set_per_process_memory_fraction(fraction, self.device_id)

                logger.info(f"GPU 초기화: {self.gpu_name}, "
                           f"전체 {self.gpu_total_mb:.0f}MB, "
                           f"제한 {limit_mb}MB ({fraction*100:.0f}%)")
            else:
                logger.info("CUDA GPU 없음 → CPU 모드")
        except ImportError:
            logger.info("PyTorch 미설치 → GPU 사용 불가")
        except Exception as e:
            logger.warning(f"GPU 초기화 실패: {e}")

    def get_gpu_info(self):
        """GPU 메모리 정보"""
        if not self.gpu_available:
            return {'available': False, 'name': 'N/A', 'total_mb': 0, 'used_mb': 0, 'free_mb': 0}
        try:
            import torch
            allocated = torch.cuda.memory_allocated(self.device_id) / (1024**2)
            reserved = torch.cuda.memory_reserved(self.device_id) / (1024**2)
            return {
                'available': True,
                'name': self.gpu_name,
                'total_mb': self.gpu_total_mb,
                'used_mb': allocated,
                'reserved_mb': reserved,
                'free_mb': self.gpu_total_mb - allocated,
                'fraction': self.memory_fraction,
            }
        except Exception:
            return {'available': False, 'name': self.gpu_name, 'total_mb': self.gpu_total_mb,
                    'used_mb': 0, 'free_mb': 0}

    def cleanup_gpu(self):
        """GPU 메모리 해제"""
        if not self.gpu_available:
            return
        try:
            import torch
            torch.cuda.empty_cache()
            logger.info("GPU 캐시 해제됨")
        except Exception:
            pass

    def set_memory_fraction(self, fraction):
        """GPU 메모리 사용 비율 변경"""
        self.memory_fraction = max(0.1, min(fraction, 0.9))
        GPU_SETTINGS['GPU_MEMORY_FRACTION'] = self.memory_fraction
        if self.gpu_available:
            try:
                import torch
                torch.cuda.set_per_process_memory_fraction(self.memory_fraction, self.device_id)
                logger.info(f"GPU 메모리 비율 변경: {self.memory_fraction*100:.0f}%")
            except Exception as e:
                logger.warning(f"GPU 비율 변경 실패: {e}")


class ChunkController:
    """청크 크기 제어"""

    def __init__(self, memory_manager=None):
        self.mm = memory_manager or MemoryManager()
        self.sas_chunk = CHUNK_SETTINGS['SAS_CHUNK']
        self.hana_chunk = CHUNK_SETTINGS['HANA_CHUNK']
        self.csv_chunk = CHUNK_SETTINGS['CSV_CHUNK']
        self.analysis_chunk = CHUNK_SETTINGS['ANALYSIS_CHUNK']

    def get_chunk(self, data_type='sas'):
        """데이터 타입별 chunk 크기 반환"""
        if data_type == 'sas':
            return self.sas_chunk
        elif data_type == 'hana':
            return self.hana_chunk
        elif data_type == 'csv':
            return self.csv_chunk
        elif data_type == 'analysis':
            return self.analysis_chunk
        return CHUNK_SETTINGS.get('SAS_CHUNK', 50000)

    def set_chunk(self, data_type, size):
        """chunk 크기 설정 (범위 검증)"""
        size = max(CHUNK_SETTINGS['MIN_CHUNK'], min(size, CHUNK_SETTINGS['MAX_CHUNK']))
        if data_type == 'sas':
            self.sas_chunk = size
            CHUNK_SETTINGS['SAS_CHUNK'] = size
        elif data_type == 'hana':
            self.hana_chunk = size
            CHUNK_SETTINGS['HANA_CHUNK'] = size
        elif data_type == 'csv':
            self.csv_chunk = size
            CHUNK_SETTINGS['CSV_CHUNK'] = size
        elif data_type == 'analysis':
            self.analysis_chunk = size
            CHUNK_SETTINGS['ANALYSIS_CHUNK'] = size
        logger.info(f"청크 크기 변경: {data_type} = {size:,}")

    def set_all_chunks(self, size):
        """모든 chunk 크기 일괄 설정"""
        for dt in ['sas', 'hana', 'csv', 'analysis']:
            self.set_chunk(dt, size)

    def auto_adjust(self):
        """메모리 상태에 따라 자동 조절"""
        status, info = self.mm.check_memory()
        if status == 'CRITICAL':
            # 메모리 위험 → chunk 절반으로 줄임
            for dt in ['sas', 'hana', 'csv', 'analysis']:
                current = self.get_chunk(dt)
                self.set_chunk(dt, max(current // 2, CHUNK_SETTINGS['MIN_CHUNK']))
            logger.warning("메모리 위험 → chunk 크기 자동 축소")
        elif status == 'WARNING':
            # 경고 → 20% 줄임
            for dt in ['sas', 'hana', 'csv', 'analysis']:
                current = self.get_chunk(dt)
                self.set_chunk(dt, max(int(current * 0.8), CHUNK_SETTINGS['MIN_CHUNK']))
            logger.warning("메모리 경고 → chunk 크기 자동 축소")


# 전역 인스턴스
mem_manager = MemoryManager()
gpu_manager = GPUManager()
chunk_controller = ChunkController(mem_manager)
