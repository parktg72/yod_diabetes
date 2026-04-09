"""
analysis_checkpoint.py - 분석 단계 체크포인트 저장/복원

장시간 분석(코호트 구축 + 통계 분석) 중 중단 시 완료된 단계부터 재개.

저장 형식: JSON (results 디렉토리 내 .checkpoint.json)
포함 정보:
  - 완료된 단계 목록 (completed_steps)
  - 각 단계의 핵심 결과 요약 (n, events 등 scalar만)
  - 저장 시각 (saved_at)
  - 설정 해시 (settings_hash) — 설정 변경 시 체크포인트 무효화
"""

import json
import logging
import os
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

_CHECKPOINT_FILENAME = '.analysis_checkpoint.json'

# 체크포인트에 저장할 scalar 결과만 추출하는 키 정의
_SCALAR_KEYS = {
    'table1': lambda v: {'rows': len(v) if hasattr(v, '__len__') else None},
    'cox_dementia_event': lambda v: {'n_models': len([k for k in v if k.startswith('model')])},
    'cox_ad_event': lambda v: {'n_models': len([k for k in v if k.startswith('model')])},
    'cox_vad_event': lambda v: {'n_models': len([k for k in v if k.startswith('model')])},
    'psm': lambda v: {'n_treated': v.get('n_treated'), 'n_control': v.get('n_control'),
                      'skipped': v.get('skipped', False)},
    'interaction': lambda v: {'skipped': v.get('skipped', False)},
    'subgroup': lambda v: {'n_subgroups': len([k for k in v if not k.startswith('_')])},
    'competing_risks': lambda v: {'n_outcomes': len([k for k in v
                                                      if not k.startswith('_')])},
    'sensitivity': lambda v: {'n_scenarios': len([k for k in v if not k.startswith('_')])},
}


def _settings_hash(study_settings: dict) -> str:
    """STUDY_SETTINGS 주요 값의 MD5 해시 — 설정 변경 감지용."""
    keys = ['ENROLLMENT_START', 'ENROLLMENT_END', 'YOD_AGE', 'MIN_EVENTS',
            'MIN_VALID_ROWS', 'PSM_RATIO', 'PH_BONFERRONI']
    relevant = {k: study_settings.get(k) for k in keys}
    payload = json.dumps(relevant, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:8]


class AnalysisCheckpoint:
    """분석 체크포인트 관리자.

    사용법:
        cp = AnalysisCheckpoint(results_dir, study_settings)
        if cp.can_resume('cox_dementia_event'):
            return  # 이미 완료
        # ... 분석 실행 ...
        cp.mark_done('cox_dementia_event', result)
    """

    def __init__(self, results_dir, study_settings: dict):
        self.path = Path(results_dir) / _CHECKPOINT_FILENAME
        self._hash = _settings_hash(study_settings)
        self._data = self._load()

    def _load(self) -> dict:
        """저장된 체크포인트 파일 로드. 없거나 설정 변경되면 초기화."""
        if not self.path.exists():
            return self._empty()
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            # 설정이 바뀌면 체크포인트 무효화
            if data.get('settings_hash') != self._hash:
                logger.info(
                    "체크포인트: STUDY_SETTINGS 변경 감지 → 기존 체크포인트 무효화"
                )
                return self._empty()
            logger.info(
                "체크포인트 로드: %d단계 완료 상태 복원 (%s)",
                len(data.get('completed_steps', {})),
                data.get('saved_at', '?')
            )
            return data
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("체크포인트 로드 실패 (무시됨): %s", e)
            return self._empty()

    def _empty(self) -> dict:
        return {
            'settings_hash': self._hash,
            'completed_steps': {},
            'saved_at': None,
        }

    def _save(self) -> None:
        """atomic write로 체크포인트 저장."""
        self._data['saved_at'] = datetime.now().isoformat(timespec='seconds')
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix='.json', dir=self.path.parent
            )
            os.close(tmp_fd)
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2,
                          default=str)
            os.replace(tmp_path, self.path)
        except OSError as e:
            logger.warning("체크포인트 저장 실패: %s", e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def can_resume(self, step: str) -> bool:
        """이미 완료된 단계면 True 반환."""
        return step in self._data['completed_steps']

    def mark_done(self, step: str, result=None) -> None:
        """단계 완료 표시 + 즉시 저장."""
        summary = {}
        if result is not None and step in _SCALAR_KEYS:
            try:
                summary = _SCALAR_KEYS[step](result)
            except Exception:
                pass
        self._data['completed_steps'][step] = {
            'completed_at': datetime.now().isoformat(timespec='seconds'),
            'summary': summary,
        }
        self._save()
        logger.info("체크포인트 저장: %s %s", step, summary)

    def completed_steps(self) -> list[str]:
        return list(self._data['completed_steps'].keys())

    def reset(self) -> None:
        """체크포인트 초기화 (전체 재실행 시 사용)."""
        self._data = self._empty()
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError as e:
                logger.warning("체크포인트 파일 삭제 실패: %s", e)
        logger.info("체크포인트 초기화 완료")

    def summary_text(self) -> str:
        """UI 표시용 완료 단계 요약 문자열."""
        steps = self._data['completed_steps']
        if not steps:
            return "이전 체크포인트 없음"
        lines = [f"저장 시각: {self._data.get('saved_at', '?')}"]
        for step, info in steps.items():
            summary = info.get('summary', {})
            summary_str = ', '.join(f"{k}={v}" for k, v in summary.items()) if summary else ''
            lines.append(f"  ✓ {step}" + (f" ({summary_str})" if summary_str else ''))
        return '\n'.join(lines)
