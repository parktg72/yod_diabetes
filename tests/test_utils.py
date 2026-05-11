"""utils.py 핵심 함수 단위 테스트"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import (
    icd_like,
    format_hr,
    format_number,
    make_error_result,
    make_skip_result,
    make_model_failure,
)


class TestIcdLike:
    def test_single_code(self):
        result = icd_like('t40.MCEX_SICK_SYM', ['E10'])
        assert result == "(t40.MCEX_SICK_SYM LIKE 'E10%')"

    def test_multiple_codes(self):
        result = icd_like('t40.MCEX_SICK_SYM', ['E11', 'E12'])
        assert "E11%" in result
        assert "E12%" in result
        assert " OR " in result

    def test_dotted_column(self):
        result = icd_like('t20.SICK_SYM1', ['F00'])
        assert result == "(t20.SICK_SYM1 LIKE 'F00%')"

    def test_invalid_column_injection(self):
        with pytest.raises(ValueError, match="유효하지 않은 컬럼명"):
            icd_like("t40.col; DROP TABLE", ['E10'])

    def test_invalid_column_space(self):
        with pytest.raises(ValueError, match="유효하지 않은 컬럼명"):
            icd_like("col name", ['E10'])

    def test_invalid_code_special_chars(self):
        with pytest.raises(ValueError, match="유효하지 않은 ICD 코드"):
            icd_like('t40.MCEX_SICK_SYM', ["E10'; DROP"])

    def test_invalid_code_space(self):
        with pytest.raises(ValueError, match="유효하지 않은 ICD 코드"):
            icd_like('t40.MCEX_SICK_SYM', ["E10 E11"])

    def test_empty_codes(self):
        result = icd_like('t40.MCEX_SICK_SYM', [])
        assert result == "()"


class TestFormatHr:
    def test_significant(self):
        result = format_hr(1.52, 1.10, 2.05, 0.003)
        assert "1.52" in result
        assert "1.10" in result
        assert "2.05" in result
        assert "**" in result

    def test_highly_significant(self):
        result = format_hr(2.0, 1.5, 2.8, 0.0001)
        assert "***" in result

    def test_not_significant(self):
        result = format_hr(1.05, 0.80, 1.30, 0.55)
        assert "*" not in result

    def test_marginal(self):
        result = format_hr(1.20, 1.01, 1.42, 0.03)
        assert result.endswith("*")


class TestFormatNumber:
    def test_integer(self):
        assert format_number(1234567) == "1,234,567"

    def test_float(self):
        # f"{n:,.0f}" uses banker's rounding: 1234.5 → 1234
        assert format_number(1234.5) == "1,234"

    def test_string(self):
        assert format_number("N/A") == "N/A"

    def test_zero(self):
        assert format_number(0) == "0"


class TestMakeErrorResult:
    def test_includes_required_fields(self):
        error = ValueError("bad input")
        result = make_error_result("ANALYSIS_ERROR", error)

        assert result["reason_code"] == "ANALYSIS_ERROR"
        assert result["reason"] == "bad input"
        assert result["exception_type"] == "ValueError"

    def test_omits_stage_when_none(self):
        error = RuntimeError("oops")
        result = make_error_result("RUNTIME_ERROR", error, stage=None)

        assert "stage" not in result

    def test_includes_stage_when_provided(self):
        error = RuntimeError("oops")
        result = make_error_result("RUNTIME_ERROR", error, stage="stage_n")

        assert result["stage"] == "stage_n"

    def test_merges_extra_fields(self):
        error = KeyError("missing")
        result = make_error_result(
            "MISSING_KEY",
            error,
            stage="stage_o",
            failed_outcome="ad_event",
            retryable=False,
        )

        assert result["stage"] == "stage_o"
        assert result["failed_outcome"] == "ad_event"
        assert result["retryable"] is False


class TestMakeSkipResult:
    def test_includes_required_fields(self):
        result = make_skip_result("INSUFFICIENT_DATA", "rows<min")

        assert result["skipped"] is True
        assert result["reason_code"] == "INSUFFICIENT_DATA"
        assert result["reason"] == "rows<min"

    def test_omits_stage_when_none(self):
        result = make_skip_result("NO_MATCH", "no rows", stage=None)

        assert "stage" not in result

    def test_includes_stage_and_extra(self):
        result = make_skip_result(
            "NO_MATCH",
            "no rows",
            stage="psm",
            matched_n=0,
            retryable=False,
        )

        assert result["stage"] == "psm"
        assert result["matched_n"] == 0
        assert result["retryable"] is False


class TestMakeModelFailure:
    def test_includes_required_fields_and_default_stage(self):
        result = make_model_failure("COX_MODEL_FAILED", "convergence error")

        assert result["reason_code"] == "COX_MODEL_FAILED"
        assert result["reason"] == "convergence error"
        assert result["stage"] == "cox"
        assert "skipped" not in result

    def test_omits_stage_when_none(self):
        result = make_model_failure("COX_MODEL_FAILED", "convergence error", stage=None)

        assert "stage" not in result

    def test_merges_extra_fields(self):
        result = make_model_failure(
            "COX_MODEL_FAILED",
            "singular matrix",
            stage="psm",
            failed_outcome="ad_event",
            retryable=True,
        )

        assert result["stage"] == "psm"
        assert result["failed_outcome"] == "ad_event"
        assert result["retryable"] is True
