"""
visualization.py - 시각화 (KM, Forest Plot, 코호트 흐름도, PSM 밸런스)
"""

import logging
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch
from lifelines import KaplanMeierFitter
from pathlib import Path

logger = logging.getLogger(__name__)

def setup_korean_font():
    import platform
    system = platform.system()

    # OS별 후보 폰트 경로
    candidates = []
    if system == 'Windows':
        candidates = [
            'C:/Windows/Fonts/malgun.ttf',
            'C:/Windows/Fonts/NanumGothic.ttf',
        ]
    elif system == 'Darwin':  # macOS
        candidates = [
            '/Library/Fonts/AppleGothic.ttf',
            '/System/Library/Fonts/Supplemental/AppleGothic.ttf',
            '/Library/Fonts/NanumGothic.ttf',
            str(Path.home() / 'Library/Fonts/NanumGothic.ttf'),
        ]
    else:  # Linux
        candidates = [
            '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]

    for fp in candidates:
        if Path(fp).exists():
            fm.fontManager.addfont(fp)
            plt.rcParams['font.family'] = fm.FontProperties(fname=fp).get_name()
            break
    else:
        # 시스템에서 한국어 폰트 자동 탐색
        for font in fm.fontManager.ttflist:
            if any(k in font.name for k in ['Gothic', 'Nanum', 'Malgun', 'CJK', 'Noto']):
                plt.rcParams['font.family'] = font.name
                break

    plt.rcParams['axes.unicode_minus'] = False

setup_korean_font()

COLORS = {'T1DM': '#E74C3C', 'T2DM_OHA': '#3498DB', 'T2DM_INSULIN': '#F39C12', 'T2DM_NOMED': '#9B59B6', 'NON_DM': '#2ECC71'}
LABELS = {'T1DM': 'T1DM', 'T2DM_OHA': 'T2DM (OHA)', 'T2DM_INSULIN': 'T2DM (Insulin)', 'T2DM_NOMED': 'T2DM (No Med)', 'NON_DM': 'Non-DM'}


class Visualizer:
    def __init__(self, output_dir='./results'):
        self.out = Path(output_dir)
        self.out.mkdir(parents=True, exist_ok=True)

    def plot_km(self, df, outcome='dementia_event', title='KM Survival', filename='km.png'):
        fig, ax = plt.subplots(figsize=(12, 8))
        for g in ['NON_DM', 'T2DM_NOMED', 'T2DM_OHA', 'T2DM_INSULIN', 'T1DM']:
            gd = df[df['exposure_group'] == g]
            if len(gd) < 10:
                continue
            T = pd.to_numeric(gd['follow_up_years'], errors='coerce')
            E = pd.to_numeric(gd[outcome], errors='coerce')
            m = T.notna() & E.notna() & (T > 0)
            if m.sum() < 10:
                continue
            kmf = KaplanMeierFitter()
            kmf.fit(T[m], E[m], label=LABELS.get(g, g))
            kmf.plot_survival_function(ax=ax, color=COLORS.get(g, '#333'), linewidth=2, ci_show=True, ci_alpha=0.1)
        ax.set_xlabel('Follow-up (years)', fontsize=14)
        ax.set_ylabel('Survival Probability', fontsize=14)
        ax.set_title(title, fontsize=16, fontweight='bold')
        ax.legend(fontsize=12, loc='lower left')
        ax.set_ylim(0.8, 1.01)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = self.out / filename
        fig.savefig(p, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return str(p)

    def plot_forest(self, sg_results, var='is_t1dm', title='Forest Plot: T1DM HR', filename='forest.png'):
        data = []
        for sn, sd in sg_results.items():
            if var in sd.get('hr_data', {}):
                d = sd['hr_data'][var]
                data.append({'sg': sn.replace('_', ' ').title(), 'hr': d['hr'],
                             'lo': d['ci_lower'], 'hi': d['ci_upper'], 'p': d['p_value'],
                             'n': sd['n'], 'ev': sd['events']})
        if not data:
            return None
        fdf = pd.DataFrame(data)
        fig, ax = plt.subplots(figsize=(14, max(6, len(fdf) * 0.8 + 2)))
        yp = list(range(len(fdf)))[::-1]
        for i, (_, r) in enumerate(fdf.iterrows()):
            y = yp[i]
            c = '#E74C3C' if r['hr'] > 1 else '#3498DB'
            ax.plot([r['lo'], r['hi']], [y, y], color=c, linewidth=2)
            ax.plot(r['hr'], y, 'D', color=c, markersize=10)
            ax.text(-0.05, y, f"{r['sg']} (n={r['n']:,})", ha='right', va='center', fontsize=10,
                    transform=ax.get_yaxis_transform())
            ax.text(1.02, y, f"{r['hr']:.2f} ({r['lo']:.2f}-{r['hi']:.2f})", ha='left', va='center',
                    fontsize=10, transform=ax.get_yaxis_transform())
        ax.axvline(x=1, color='black', linestyle='--', alpha=0.7)
        ax.set_xscale('log')
        ax.set_xlabel('Hazard Ratio (95% CI)', fontsize=13)
        ax.set_title(title, fontsize=15, fontweight='bold')
        ax.set_yticks(yp)
        ax.set_yticklabels([''] * len(fdf))
        ax.grid(True, axis='x', alpha=0.3)
        fig.tight_layout()
        p = self.out / filename
        fig.savefig(p, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return str(p)

    def plot_psm_balance(self, balance, filename='psm_balance.png'):
        if not balance:
            return None
        fig, ax = plt.subplots(figsize=(10, max(5, len(balance) * 0.5)))
        vs = list(balance.keys())
        smds = [balance[v]['smd'] for v in vs]
        colors = ['#2ECC71' if s < 0.1 else '#E74C3C' for s in smds]
        ax.barh(range(len(vs)), smds, color=colors, height=0.6)
        ax.axvline(x=0.1, color='red', linestyle='--', label='Threshold (0.1)')
        ax.set_yticks(range(len(vs)))
        ax.set_yticklabels([v.replace('_', ' ').title() for v in vs])
        ax.set_xlabel('SMD')
        ax.set_title('Covariate Balance After PSM', fontweight='bold')
        ax.legend()
        fig.tight_layout()
        p = self.out / filename
        fig.savefig(p, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return str(p)

    def plot_cif(self, cif_data, title='Cumulative Incidence', filename='cif.png'):
        """누적발생률(CIF) 곡선 — 경쟁위험 분석 결과 시각화"""
        fig, axes = plt.subplots(1, 2, figsize=(18, 8))

        # 좌: 관심사건(치매) CIF
        ax1 = axes[0]
        for group in ['NON_DM', 'T2DM_NOMED', 'T2DM_OHA', 'T2DM_INSULIN', 'T1DM']:
            if group not in cif_data:
                continue
            d = cif_data[group]
            ax1.step(d['times'], d['cif_event'], where='post',
                     color=COLORS.get(group, '#333'), linewidth=2,
                     label=LABELS.get(group, group))
        ax1.set_xlabel('Follow-up (years)', fontsize=14)
        ax1.set_ylabel('Cumulative Incidence', fontsize=14)
        ax1.set_title(f'{title} - Dementia', fontsize=15, fontweight='bold')
        ax1.legend(fontsize=11, loc='upper left')
        ax1.grid(True, alpha=0.3)

        # 우: 경쟁위험(사망/탈퇴) CIF
        ax2 = axes[1]
        for group in ['NON_DM', 'T2DM_NOMED', 'T2DM_OHA', 'T2DM_INSULIN', 'T1DM']:
            if group not in cif_data:
                continue
            d = cif_data[group]
            ax2.step(d['times'], d['cif_competing'], where='post',
                     color=COLORS.get(group, '#333'), linewidth=2, linestyle='--',
                     label=LABELS.get(group, group))
        ax2.set_xlabel('Follow-up (years)', fontsize=14)
        ax2.set_ylabel('Cumulative Incidence', fontsize=14)
        ax2.set_title(f'{title} - Competing (Death/Withdrawal)', fontsize=15, fontweight='bold')
        ax2.legend(fontsize=11, loc='upper left')
        ax2.grid(True, alpha=0.3)

        fig.tight_layout()
        p = self.out / filename
        fig.savefig(p, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return str(p)

    def plot_cohort_flow(self, cr, filename='flow.png'):
        fig, ax = plt.subplots(figsize=(12, 14))
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 18)
        ax.axis('off')
        def box(x, y, w, h, txt, bg='#ECF0F1', tc='black'):
            ax.add_patch(FancyBboxPatch((x-w/2, y-h/2), w, h, boxstyle="round,pad=0.1", fc=bg, ec='#34495E', lw=2))
            ax.text(x, y, txt, ha='center', va='center', fontsize=9, color=tc, fontweight='bold')
        def arrow(x1, y1, x2, y2):
            ax.annotate('', xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle='->', color='#34495E', lw=2))

        bn = cr.get('base_n', 'N/A')
        box(5, 16.5, 6, 1.2, f'NHIS 40-64세 (2013-2016)\nN={bn:,}' if isinstance(bn, int) else 'NHIS 40-64', '#AED6F1')
        arrow(5, 15.9, 5, 14.7)

        ex = cr.get('excluded_dementia', 'N/A')
        box(8.5, 15.3, 3, 0.8, f'Excluded: {ex:,}' if isinstance(ex, int) else 'Excluded', '#FADBD8')

        fn = cr.get('final_n', 'N/A')
        box(5, 14, 6, 1.2, f'Study Cohort\nN={fn:,}' if isinstance(fn, int) else 'Study Cohort', '#ABEBC6')

        fig.tight_layout()
        p = self.out / filename
        fig.savefig(p, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return str(p)
