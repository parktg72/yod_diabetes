"""
Phase 2 Statistical Analysis 결과 시각화

생성 내용:
- KM plot: T2DM_OHA 약물전환별 생존곡선
- Forest plot: 약물전환 분층별 HR 및 95% CI
- Summary tables: 기초 특성, Cox 결과
"""

from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import rcParams
from lifelines import KaplanMeierFitter
from lifelines.statistics import logrank_test
import logging
from typing import Optional, Dict, Any

# Configure matplotlib for Korean text rendering
rcParams['font.family'] = 'DejaVu Sans'
rcParams['axes.unicode_minus'] = False

logger = logging.getLogger(__name__)


class Phase2Visualizer:
    """Phase 2 결과 시각화 클래스"""

    def __init__(self, output_dir: str = 'output') -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def plot_km_curves(self, df: pd.DataFrame, figname: str = 'km_t2dm_oha_switch.png') -> Optional[str]:
        """
        T2DM_OHA 약물전환별 KM 곡선

        Args:
            df: _prepare()에서 나온 분석 데이터
            figname: 저장 파일명

        Returns:
            저장된 파일 경로, 또는 데이터 부족 시 None
        """
        t2dm_oha = df[df['exposure_group'] == 'T2DM_OHA'].copy()

        if len(t2dm_oha) < 30:
            logger.warning(f"T2DM_OHA 환자 수 부족 ({len(t2dm_oha)}명), KM plot 생성 스킵")
            return None

        fig, ax = plt.subplots(figsize=(10, 6))

        kmf_noswitch = KaplanMeierFitter()
        kmf_switch = KaplanMeierFitter()

        # 그룹별 데이터
        noswitch = t2dm_oha[t2dm_oha['had_insulin_switch'] == 0]
        switch = t2dm_oha[t2dm_oha['had_insulin_switch'] == 1]

        # KM fitting
        if len(noswitch) >= 5:
            kmf_noswitch.fit(
                durations=noswitch['follow_up_years'],
                event_observed=noswitch['dementia_event'],
                label=f'No med switch (n={len(noswitch)})'
            )
            kmf_noswitch.plot_survival_function(ax=ax, ci_show=True, linewidth=2)

        if len(switch) >= 5:
            kmf_switch.fit(
                durations=switch['follow_up_years'],
                event_observed=switch['dementia_event'],
                label=f'Med switch (n={len(switch)})'
            )
            kmf_switch.plot_survival_function(ax=ax, ci_show=True, linewidth=2)

        # Log-rank test
        if len(noswitch) >= 5 and len(switch) >= 5:
            results = logrank_test(
                durations_A=noswitch['follow_up_years'],
                durations_B=switch['follow_up_years'],
                event_observed_A=noswitch['dementia_event'],
                event_observed_B=switch['dementia_event']
            )
            p_value = results.p_value

            ax.text(
                0.98, 0.05,
                f'Log-rank test p-value: {p_value:.6f}',
                transform=ax.transAxes,
                fontsize=10,
                verticalalignment='bottom',
                horizontalalignment='right',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5)
            )
            logger.info(f"Log-rank test p-value: {p_value:.6f}")

        ax.set_xlabel('Follow-up years', fontsize=12)
        ax.set_ylabel('Dementia-free probability', fontsize=12)
        ax.set_title('T2DM_OHA: Dementia-free survival by med switch status (Kaplan-Meier)', fontsize=14, fontweight='bold')
        ax.grid(True, alpha=0.3)
        ax.legend(loc='best', fontsize=11)

        filepath = self.output_dir / figname
        plt.tight_layout()
        try:
            plt.savefig(str(filepath), dpi=300, bbox_inches='tight')
            logger.info(f"KM plot saved: {filepath}")
        finally:
            plt.close()

        return str(filepath)

    def plot_forest_plot(self, cox_results: Dict[str, Any], figname: str = 'forest_t2dm_oha_switch.png') -> Optional[str]:
        """
        Forest plot 비활성화.

        T2DM_OHA 내부 약물전환 서브그룹에서 is_t1dm HR을 forest plot으로
        시각화하는 것은 임상적으로 무의미하여 항상 스킵한다.
        """
        logger.warning(
            "Forest plot 비활성화: T2DM_OHA 약물전환 서브그룹의 is_t1dm HR 시각화는 임상적으로 무의미합니다."
        )
        return None

    def create_baseline_table(self, df: pd.DataFrame, figname: str = 'table_baseline.csv') -> Optional[str]:
        """
        기초 특성 표 (T2DM_OHA 약물전환별)

        Args:
            df: _prepare() 데이터
            figname: 저장 파일명

        Returns:
            저장된 파일 경로
        """
        t2dm_oha = df[df['exposure_group'] == 'T2DM_OHA']

        table_data = []

        # 기초 특성 리스트
        characteristics = [
            ('N', None, 'count'),
            ('Age (years)', 'age_at_index', 'mean_std'),
            ('Male (%)', 'male', 'pct'),
            ('Follow-up (years)', 'follow_up_years', 'mean_std'),
            ('CCI score', 'cci_score', 'mean_std'),
            ('BMI', 'bmi', 'mean_std'),
            ('Dementia events', 'dementia_event', 'sum'),
            ('Event rate (%)', 'dementia_event', 'pct_event'),
        ]

        for char_name, col, stat_type in characteristics:
            if stat_type == 'count':
                n_noswitch = (t2dm_oha['had_insulin_switch'] == 0).sum()
                n_switch = (t2dm_oha['had_insulin_switch'] == 1).sum()
                table_data.append({
                    'Characteristic': char_name,
                    f'No switch (n={n_noswitch})': f"{n_noswitch}",
                    f'Med switch (n={n_switch})': f"{n_switch}"
                })
            elif col is not None and col in t2dm_oha.columns:
                noswitch_vals = t2dm_oha[t2dm_oha['had_insulin_switch'] == 0][col]
                switch_vals = t2dm_oha[t2dm_oha['had_insulin_switch'] == 1][col]

                if stat_type == 'mean_std':
                    n_noswitch = len(noswitch_vals.dropna())
                    n_switch = len(switch_vals.dropna())
                    val_noswitch = f"{noswitch_vals.mean():.1f}±{noswitch_vals.std():.1f}" if n_noswitch > 0 else "N/A"
                    val_switch = f"{switch_vals.mean():.1f}±{switch_vals.std():.1f}" if n_switch > 0 else "N/A"

                elif stat_type == 'pct':
                    pct_noswitch = (noswitch_vals == 1).sum() / len(noswitch_vals) * 100 if len(noswitch_vals) > 0 else 0
                    pct_switch = (switch_vals == 1).sum() / len(switch_vals) * 100 if len(switch_vals) > 0 else 0
                    val_noswitch = f"{pct_noswitch:.1f}"
                    val_switch = f"{pct_switch:.1f}"

                elif stat_type == 'sum':
                    val_noswitch = f"{int(noswitch_vals.sum())}"
                    val_switch = f"{int(switch_vals.sum())}"

                elif stat_type == 'pct_event':
                    n_noswitch = len(noswitch_vals)
                    n_switch = len(switch_vals)
                    pct_noswitch = (noswitch_vals.sum() / n_noswitch * 100) if n_noswitch > 0 else 0
                    pct_switch = (switch_vals.sum() / n_switch * 100) if n_switch > 0 else 0
                    val_noswitch = f"{pct_noswitch:.1f}"
                    val_switch = f"{pct_switch:.1f}"

                table_data.append({
                    'Characteristic': char_name,
                    'No switch': val_noswitch,
                    'Med switch': val_switch
                })

        table_df = pd.DataFrame(table_data)
        filepath = self.output_dir / figname
        table_df.to_csv(str(filepath), index=False, encoding='utf-8-sig')
        logger.info(f"기초 특성 표 저장: {filepath}")

        return str(filepath)

    def create_cox_results_table(self, subgroup_results: Dict[str, Any], figname: str = 'table_cox_results.csv') -> Optional[str]:
        """
        Cox 모델 결과 표

        Args:
            subgroup_results: run_subgroup() 반환값
            figname: 저장 파일명

        Returns:
            저장된 파일 경로, 또는 데이터 부족 시 None
        """
        table_data = []

        subgroups = {
            't2dm_oha_noswitch': 'T2DM_OHA (no switch)',
            't2dm_oha_switch': 'T2DM_OHA (med switch)'
        }

        for sg_key, sg_label in subgroups.items():
            if sg_key not in subgroup_results:
                continue

            sg = subgroup_results[sg_key]
            n = sg.get('n', 0)
            events = sg.get('events', 0)
            hr_data = sg.get('hr_data', {})

            for exp_name, hr_dict in hr_data.items():
                table_data.append({
                    'Subgroup': sg_label,
                    'N': f"{n}",
                    'Events': f"{events}",
                    'Exposure': exp_name.replace('is_', '').upper(),
                    'HR': f"{hr_dict['hr']:.4f}",
                    'CI_lower': f"{hr_dict['ci_lower']:.4f}",
                    'CI_upper': f"{hr_dict['ci_upper']:.4f}",
                    'p_value': f"{hr_dict['p_value']:.6f}"
                })

        if not table_data:
            logger.warning("Cox 결과 표 생성 불가: 데이터 부족")
            return None

        table_df = pd.DataFrame(table_data)
        filepath = self.output_dir / figname
        table_df.to_csv(str(filepath), index=False, encoding='utf-8-sig')
        logger.info(f"Cox 결과 표 저장: {filepath}")

        return str(filepath)
