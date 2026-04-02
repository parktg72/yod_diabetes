"""
analysis_runner.py - 분석 후처리: 시각화 + 결과 내보내기
GUI 비의존 모듈 — 테스트 및 CLI 활용 가능
"""

import gc
import logging
from visualization import Visualizer
from results_exporter import ResultsExporter

logger = logging.getLogger(__name__)


def run_post_analysis(dm, analysis_results, results_dir, log=None):
    """분석 결과 시각화 + 내보내기 (GUI 비의존)

    Args:
        dm: DataManager 인스턴스 (KM 샘플링 쿼리용)
        analysis_results: StatisticalAnalyzer.run_selected() 반환값
        results_dir: 결과 저장 디렉토리 (Path 또는 str)
        log: 로그 콜백 함수 (없으면 logger.info 사용)

    Returns:
        dict: {'errors': list[str], 'exported_files': list[str]}
    """
    if log is None:
        log = logger.info

    errors = []
    viz = Visualizer(str(results_dir))

    # KM 곡선
    try:
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
        log(f"KM 오류: {e}")

    # Forest Plot
    if 'subgroup' in analysis_results:
        try:
            viz.plot_forest(analysis_results['subgroup'])
        except Exception as e:
            errors.append(f"Forest plot 오류: {e}")
            log(f"Forest plot 오류: {e}")

    # PSM Balance
    if 'psm' in analysis_results:
        try:
            viz.plot_psm_balance(analysis_results['psm'].get('balance', {}))
        except Exception as e:
            errors.append(f"PSM balance plot 오류: {e}")
            log(f"PSM balance plot 오류: {e}")

    # CIF
    if 'competing_risks' in analysis_results:
        try:
            for oc, oc_data in analysis_results['competing_risks'].items():
                if isinstance(oc_data, dict) and 'cif_by_group' in oc_data:
                    viz.plot_cif(oc_data['cif_by_group'],
                                 title=f'CIF: {oc}', filename=f'cif_{oc}.png')
        except Exception as e:
            errors.append(f"CIF plot 오류: {e}")
            log(f"CIF plot 오류: {e}")

    # Export
    exported_files = []
    try:
        exporter = ResultsExporter(str(results_dir))
        exported_files = exporter.export_all(analysis_results)
    except Exception as e:
        errors.append(f"결과 내보내기 오류: {e}")
        log(f"결과 내보내기 오류: {e}")

    return {'errors': errors, 'exported_files': exported_files}
