"""
results_exporter.py - 결과 내보내기 (CSV/Excel)
"""
import logging
import pandas as pd
from pathlib import Path
from datetime import date

logger = logging.getLogger(__name__)

class ResultsExporter:
    def __init__(self, output_dir='./results'):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _write_df_with_sampling_header(self, writer, df, sheet_name, sampling_info):
        """샘플링 정보가 Excel 첫 번째 행에 오도록 시트를 작성한다.

        sampling_info.applied == True 이면:
          - Row 1: 샘플링 정보 텍스트 (openpyxl 직접 기록)
          - Row 2+: DataFrame (헤더 포함)
        그 외:
          - Row 1+: DataFrame (헤더 포함, 일반 방식)
        """
        if sampling_info is not None and sampling_info.applied:
            # DataFrame을 startrow=1 로 써서 헤더가 Row 2에 오게 함
            df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=1)
            ws = writer.sheets[sheet_name]
            ws.cell(1, 1).value = f"[샘플링] {sampling_info.label}"
            ws.cell(1, 2).value = f"분석일: {date.today()}"
        else:
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    def export_table1(self, df, filename='table1.xlsx', sampling_info=None):
        if df is None or (hasattr(df, 'empty') and df.empty):
            logger.warning("Table 1 내보내기 생략: 데이터 없음")
            return None
        try:
            path = self.output_dir / filename
            with pd.ExcelWriter(path, engine='openpyxl') as writer:
                self._write_df_with_sampling_header(
                    writer, df.copy(), 'Table 1', sampling_info
                )
            return str(path)
        except Exception as e:
            logger.warning("Table 1 내보내기 실패: %s", e)
            return None

    def export_cox_results(self, cox_results, filename='cox_regression.xlsx', sampling_info=None):
        # 저장할 summary가 하나도 없으면 빈 워크북 생성 시도 차단 (openpyxl은 시트 없는 저장 불허)
        summaries = {k: v for k, v in cox_results.items() if isinstance(v, dict) and 'summary' in v}
        if not summaries:
            logger.warning(f"Cox 결과 내보내기 생략: 저장할 모델 요약 없음 ({filename})")
            return None
        path = self.output_dir / filename
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            for name, data in summaries.items():
                df_out = data['summary'].copy()
                if df_out.index.name:
                    df_out = df_out.reset_index()
                self._write_df_with_sampling_header(
                    writer, df_out, name[:31], sampling_info
                )
        return str(path)

    def export_psm_results(self, psm_results, filename='psm_results.xlsx', sampling_info=None):
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
                df_bal = pd.DataFrame(balance).T
                if df_bal.index.name is None:
                    df_bal.index.name = 'Variable'
                df_bal = df_bal.reset_index()
                self._write_df_with_sampling_header(
                    writer, df_bal, 'Balance', sampling_info
                )
            for outcome, data in cox_results.items():
                df_cox = data['summary'].copy()
                if df_cox.index.name:
                    df_cox = df_cox.reset_index()
                self._write_df_with_sampling_header(
                    writer, df_cox, f'Cox_{outcome[:20]}', sampling_info
                )
        return str(path)

    def export_subgroup_results(self, subgroup_results, filename='subgroup.xlsx', sampling_info=None):
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
        df_out = pd.DataFrame(rows)
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            self._write_df_with_sampling_header(
                writer, df_out, 'Subgroup', sampling_info
            )
        return str(path)

    def export_competing_risks(self, cr_results, filename='competing_risks.xlsx', sampling_info=None):
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
                df_fg = fg.copy()
                if df_fg.index.name:
                    df_fg = df_fg.reset_index()
                sheets[f'FG_{outcome[:20]}'] = df_fg
            # CIF summary (이벤트/경쟁위험/검열 건수)
            rows = []
            for group, cif in data.get('cif_by_group', {}).items():
                if cif.get('cif_event') and cif.get('cif_competing'):
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
                self._write_df_with_sampling_header(
                    writer, df, name[:31], sampling_info
                )
        return str(path)

    def export_interaction_results(self, interaction_results, filename='interaction.xlsx', sampling_info=None):
        """인터랙션(DM 유병기간 × 노출군) 분석 결과 내보내기."""
        if not interaction_results:
            logger.warning("인터랙션 결과 내보내기 생략: 데이터 없음")
            return None
        if interaction_results.get('skipped'):
            reason = interaction_results.get('reason', '인터랙션 스킵됨')
            logger.warning(f"인터랙션 결과 내보내기 생략: {reason}")
            return None
        summary = interaction_results.get('summary')
        if summary is None:
            logger.warning("인터랙션 결과 내보내기 생략: summary 없음")
            return None

        path = self.output_dir / filename
        df_out = summary.copy()
        if df_out.index.name:
            df_out = df_out.reset_index()
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            self._write_df_with_sampling_header(
                writer, df_out, 'Interaction', sampling_info
            )
        return str(path)

    def export_sensitivity_results(self, sensitivity_results, filename='sensitivity.xlsx', sampling_info=None):
        """민감도 분석 결과 내보내기."""
        if not sensitivity_results:
            logger.warning("민감도 결과 내보내기 생략: 데이터 없음")
            return None

        rows = []
        for key, val in sensitivity_results.items():
            if isinstance(val, dict):
                row = {'항목': key}
                row.update(val)
                rows.append(row)

        if not rows:
            logger.warning("민감도 결과 내보내기 생략: 저장할 행 없음")
            return None

        df_out = pd.DataFrame(rows)
        path = self.output_dir / filename
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            self._write_df_with_sampling_header(
                writer, df_out, 'Sensitivity', sampling_info
            )
        return str(path)

    def export_ph_tests(self, cox_results_all, filename='ph_tests.xlsx', sampling_info=None):
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
                    df_ph = ph.copy()
                    if df_ph.index.name:
                        df_ph = df_ph.reset_index()
                    sheets[f'{cox_key}_{model_name}'[:31]] = df_ph

        if not sheets:
            logger.warning("PH 검정 결과 내보내기 생략: 저장할 데이터 없음")
            return None

        path = self.output_dir / filename
        with pd.ExcelWriter(path, engine='openpyxl') as writer:
            for name, df in sheets.items():
                self._write_df_with_sampling_header(
                    writer, df, name, sampling_info
                )
        return str(path)

    def export_all(self, results, prefix='', sampling_info=None):
        exported = []
        if 'table1' in results:
            path = self.export_table1(results['table1'], f'{prefix}table1.xlsx', sampling_info)
            if path:
                exported.append(path)
        for key in results:
            if key.startswith('cox_'):
                path = self.export_cox_results(results[key], f'{prefix}{key}.xlsx', sampling_info)
                if path:
                    exported.append(path)
        # PH test results
        cox_all = {k: v for k, v in results.items() if k.startswith('cox_')}
        if cox_all:
            path = self.export_ph_tests(cox_all, f'{prefix}ph_tests.xlsx', sampling_info)
            if path:
                exported.append(path)
        if 'psm' in results:
            path = self.export_psm_results(results['psm'], f'{prefix}psm.xlsx', sampling_info)
            if path:
                exported.append(path)
        if 'subgroup' in results:
            path = self.export_subgroup_results(results['subgroup'], f'{prefix}subgroup.xlsx', sampling_info)
            if path:
                exported.append(path)
        if 'competing_risks' in results:
            path = self.export_competing_risks(results['competing_risks'], f'{prefix}competing_risks.xlsx', sampling_info)
            if path:
                exported.append(path)
        if 'interaction' in results:
            path = self.export_interaction_results(results['interaction'], f'{prefix}interaction.xlsx', sampling_info)
            if path:
                exported.append(path)
        if 'sensitivity' in results:
            path = self.export_sensitivity_results(results['sensitivity'], f'{prefix}sensitivity.xlsx', sampling_info)
            if path:
                exported.append(path)
        return exported
