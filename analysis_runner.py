"""
analysis_runner.py - 분석 후처리: 시각화 + 결과 내보내기
GUI 비의존 모듈 — 테스트 및 CLI 활용 가능
"""

import gc
import logging
from visualization import Visualizer
from results_exporter import ResultsExporter

logger = logging.getLogger(__name__)


def make_error_result(reason_code, error, *, stage=None, **extra):
    """예외 기반 후처리 실패 결과의 공통 스키마."""
    result = {
        'reason_code': reason_code,
        'reason': str(error),
        'exception_type': type(error).__name__,
    }
    if stage is not None:
        result['stage'] = stage
    result.update(extra)
    return result


def run_post_analysis(dm, analysis_results, results_dir, log=None):
    """분석 결과 시각화 + 내보내기 (GUI 비의존)

    Args:
        dm: DataManager 인스턴스 (KM 샘플링 쿼리용)
        analysis_results: StatisticalAnalyzer.run_selected() 반환값
        results_dir: 결과 저장 디렉토리 (Path 또는 str)
        log: 로그 콜백 함수 (없으면 logger.info 사용)

    Returns:
        dict: {'errors': list[str], 'error_details': list[dict], 'exported_files': list[str]}
    """
    if log is None:
        log = logger.info

    errors = []
    error_details = []
    viz = Visualizer(str(results_dir))
    sampling_info = analysis_results.get('sampling_info')

    # KM 곡선
    try:
        from config import STUDY_SETTINGS as _ss
        _seed_float = int(_ss.get('SAMPLING_SEED', 42)) / 100.0
        dm.execute(f"SELECT setseed({_seed_float})")
        km_sample = dm.query("""
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
        viz.plot_km(km_sample, 'dementia_event', 'KM: All-cause YOD', 'km_allcause.png')
        viz.plot_km(km_sample, 'ad_event', 'KM: AD', 'km_ad.png')
        viz.plot_km(km_sample, 'vad_event', 'KM: VaD', 'km_vad.png')
        del km_sample
        gc.collect()
    except Exception as e:
        errors.append(f"KM 오류: {e}")
        error_details.append(make_error_result('VIZ_KM_ERROR', e, stage='post_analysis_km'))
        log(f"KM 오류: {e}")

    # Forest Plot
    if 'subgroup' in analysis_results:
        try:
            viz.plot_forest(analysis_results['subgroup'])
        except Exception as e:
            errors.append(f"Forest plot 오류: {e}")
            error_details.append(make_error_result('VIZ_FOREST_ERROR', e, stage='post_analysis_forest'))
            log(f"Forest plot 오류: {e}")

    # PSM Balance (After)
    if 'psm' in analysis_results:
        try:
            viz.plot_psm_balance(analysis_results['psm'].get('balance', {}))
        except Exception as e:
            errors.append(f"PSM balance plot 오류: {e}")
            error_details.append(make_error_result('VIZ_PSM_BALANCE_ERROR', e, stage='post_analysis_psm_balance'))
            log(f"PSM balance plot 오류: {e}")

    # A2: Love Plot (Before vs After PSM)
    if 'psm' in analysis_results:
        try:
            b_before = analysis_results['psm'].get('balance_before', {})
            b_after = analysis_results['psm'].get('balance', {})
            if b_before and b_after:
                viz.plot_love(b_before, b_after, filename='love_plot.png')
        except Exception as e:
            errors.append(f"Love plot 오류: {e}")
            error_details.append(make_error_result('VIZ_LOVE_ERROR', e, stage='post_analysis_love'))
            log(f"Love plot 오류: {e}")

    # CIF
    if 'competing_risks' in analysis_results:
        try:
            for oc, oc_data in analysis_results['competing_risks'].items():
                if isinstance(oc_data, dict) and 'cif_by_group' in oc_data:
                    viz.plot_cif(oc_data['cif_by_group'],
                                 title=f'CIF: {oc}', filename=f'cif_{oc}.png')
        except Exception as e:
            errors.append(f"CIF plot 오류: {e}")
            error_details.append(make_error_result('VIZ_CIF_ERROR', e, stage='post_analysis_cif'))
            log(f"CIF plot 오류: {e}")

    # Export
    exported_files = []
    try:
        exporter = ResultsExporter(str(results_dir))
        exported_files = exporter.export_all(analysis_results, sampling_info=sampling_info)
    except Exception as e:
        errors.append(f"결과 내보내기 오류: {e}")
        error_details.append(make_error_result('EXPORT_ERROR', e, stage='post_analysis_export'))
        log(f"결과 내보내기 오류: {e}")

    return {'errors': errors, 'error_details': error_details, 'exported_files': exported_files}
