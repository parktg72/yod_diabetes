"""
results_exporter.py - 결과 내보내기 (CSV/Excel)
"""
import logging
import pandas as pd
from pathlib import Path

logger = logging.getLogger(__name__)

class ResultsExporter:
    def __init__(self, output_dir='./results'):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_table1(self, df, filename='table1.xlsx'):
        path = self.output_dir / filename
        df.to_excel(path, index=False, sheet_name='Table 1')
        return str(path)

    def export_cox_results(self, cox_results, filename='cox_regression.xlsx'):
        # 저장할 summary가 하나도 없으면 빈 워크북 생성 시도 차단 (openpyxl은 시트 없는 저장 불허)
        summaries = {k: v for k, v in cox_results.items() if 'summary' in v}
        if not summaries:
            logger.warning(f"Cox 결과 내보내기 생략: 저장할 모델 요약 없음 ({filename})")
            return None
        path = self.output_dir / filename
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            for name, data in summaries.items():
                data['summary'].copy().to_excel(writer, sheet_name=name[:31])
        return str(path)

    def export_psm_results(self, psm_results, filename='psm_results.xlsx'):
        # PSM이 스킵된 경우 빈 워크북 저장 시도를 방지 (openpyxl은 시트 없는 저장 불허)
        if psm_results.get('skipped'):
            reason = psm_results.get('reason', 'PSM 스킵됨')
            logger.warning(f"PSM 결과 내보내기 생략: {reason}")
            return None

        path = self.output_dir / filename
        # 실제로 쓸 데이터가 있는지 먼저 확인
        balance = psm_results.get('balance', {})
        cox_results = {k: v for k, v in psm_results.get('cox_results', {}).items()
                       if 'summary' in v}
        if not balance and not cox_results:
            logger.warning("PSM 결과 내보내기 생략: 저장할 데이터 없음")
            return None

        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            if balance:
                pd.DataFrame(balance).T.to_excel(writer, sheet_name='Balance')
            for outcome, data in cox_results.items():
                data['summary'].to_excel(writer, sheet_name=f'Cox_{outcome[:20]}')
        return str(path)

    def export_subgroup_results(self, subgroup_results, filename='subgroup.xlsx'):
        path = self.output_dir / filename
        rows = []
        for sg_name, sg_data in subgroup_results.items():
            for var, hr in sg_data.get('hr_data', {}).items():
                rows.append({
                    'Subgroup': sg_name, 'Variable': var,
                    'N': sg_data.get('n', ''), 'Events': sg_data.get('events', ''),
                    'HR': hr.get('hr', ''), 'CI_Lower': hr.get('ci_lower', ''),
                    'CI_Upper': hr.get('ci_upper', ''), 'P_value': hr.get('p_value', ''),
                })
        if not rows:
            logger.warning("하위그룹 결과 내보내기 생략: 저장할 데이터 없음")
            return None
        pd.DataFrame(rows).to_excel(path, index=False)
        return str(path)

    def export_competing_risks(self, cr_results, filename='competing_risks.xlsx'):
        """경쟁위험 분석 결과 내보내기"""
        if not cr_results or cr_results.get('implemented') is False:
            logger.warning("경쟁위험 결과 내보내기 생략: 데이터 없음")
            return None

        sheets = {}
        for outcome, data in cr_results.items():
            if not isinstance(data, dict) or 'fine_gray_summary' not in data:
                continue
            # Fine-Gray summary
            fg = data.get('fine_gray_summary')
            if fg is not None:
                sheets[f'FG_{outcome[:20]}'] = fg.copy()
            # CIF summary (이벤트/경쟁위험/검열 건수)
            rows = []
            for group, cif in data.get('cif_by_group', {}).items():
                if cif['cif_event']:
                    rows.append({
                        'Group': group,
                        'Final_CIF_event': round(cif['cif_event'][-1], 6),
                        'Final_CIF_competing': round(cif['cif_competing'][-1], 6),
                    })
            if rows:
                sheets[f'CIF_{outcome[:20]}'] = pd.DataFrame(rows)

        if not sheets:
            logger.warning("경쟁위험 결과 내보내기 생략: 저장할 시트 없음")
            return None

        path = self.output_dir / filename
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=name[:31])
        return str(path)

    def export_ph_tests(self, cox_results_all, filename='ph_tests.xlsx'):
        """PH 가정 검정 결과 내보내기"""
        sheets = {}
        for cox_key, cox_data in cox_results_all.items():
            if not isinstance(cox_data, dict):
                continue
            for model_name, model_data in cox_data.items():
                if not isinstance(model_data, dict):
                    continue
                ph = model_data.get('ph_test')
                if ph is not None and hasattr(ph, 'to_excel'):
                    sheet_name = f'{cox_key}_{model_name}'[:31]
                    sheets[sheet_name] = ph.copy()

        if not sheets:
            logger.warning("PH 검정 결과 내보내기 생략: 저장할 데이터 없음")
            return None

        path = self.output_dir / filename
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=name)
        return str(path)

    def export_all(self, results, prefix=''):
        exported = []
        if 'table1' in results:
            exported.append(self.export_table1(results['table1'], f'{prefix}table1.xlsx'))
        for key in results:
            if key.startswith('cox_'):
                path = self.export_cox_results(results[key], f'{prefix}{key}.xlsx')
                if path:
                    exported.append(path)
        # PH test results
        cox_all = {k: v for k, v in results.items() if k.startswith('cox_')}
        if cox_all:
            path = self.export_ph_tests(cox_all, f'{prefix}ph_tests.xlsx')
            if path:
                exported.append(path)
        if 'psm' in results:
            path = self.export_psm_results(results['psm'], f'{prefix}psm.xlsx')
            if path:
                exported.append(path)
        if 'subgroup' in results:
            path = self.export_subgroup_results(results['subgroup'], f'{prefix}subgroup.xlsx')
            if path:
                exported.append(path)
        if 'competing_risks' in results:
            path = self.export_competing_risks(results['competing_risks'], f'{prefix}competing_risks.xlsx')
            if path:
                exported.append(path)
        return exported
