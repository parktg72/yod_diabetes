import pytest
import duckdb
import pandas as pd
from statistical_analysis import SamplingInfo


def test_sampling_info_applied_false_when_no_sampling():
    info = SamplingInfo(applied=False, total_rows=1000, sampled_rows=1000)
    assert info.applied is False
    assert info.ratio_pct == pytest.approx(100.0)


def test_sampling_info_applied_true_when_sampled():
    info = SamplingInfo(applied=True, total_rows=1_000_000, sampled_rows=400_000)
    assert info.applied is True
    assert info.ratio_pct == pytest.approx(40.0)


def test_sampling_info_ratio_pct_rounds_correctly():
    info = SamplingInfo(applied=True, total_rows=3, sampled_rows=1)
    assert info.ratio_pct == pytest.approx(33.333, rel=1e-3)


def test_sampling_info_label():
    info = SamplingInfo(applied=True, total_rows=1_000_000, sampled_rows=400_000)
    label = info.label
    assert "400,000" in label
    assert "1,000,000" in label
    assert "40.0%" in label


def test_sampling_info_label_not_applied():
    info = SamplingInfo(applied=False, total_rows=500, sampled_rows=500)
    assert info.label == ""
