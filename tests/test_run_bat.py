import re
from pathlib import Path

BAT_FILES = ["run.bat", "setup.bat", "phase2_run.bat", "build.bat"]
CMD_TEXT_FILES = BAT_FILES + ["requirements.txt", "requirements-hana.txt"]


def test_run_bat_repairs_missing_pyqt5_before_launch():
    """run.bat should repair a stale venv that is missing the GUI dependency."""
    run_text = Path("run.bat").read_text(encoding="utf-8")

    assert '"%VENV_PY%" -c "import PyQt5"' in run_text
    assert '"%VENV_PY%" -m pip install --no-cache-dir -r requirements.txt' in run_text
    assert "필수 GUI 패키지^(PyQt5^)가 누락되었습니다" in run_text


def test_run_bat_uses_validated_venv_python_for_app_launch():
    """run.bat must not fall back to global Python when activation points elsewhere."""
    run_text = Path("run.bat").read_text(encoding="utf-8")

    assert 'set "VENV_PY=venv\\Scripts\\python.exe"' in run_text
    assert '"%VENV_PY%" -c "import sys"' in run_text
    assert '"%VENV_PY%" -c "import PyQt5"' in run_text
    assert '"%VENV_PY%" -m pip install --no-cache-dir -r requirements.txt' in run_text
    assert '"%VENV_PY%" main_app.py' in run_text


def test_run_bat_clears_python_environment_before_direct_venv_python():
    """Direct venv Python calls should not inherit global PYTHONHOME/PYTHONPATH."""
    run_text = Path("run.bat").read_text(encoding="utf-8")

    assert "set PYTHONHOME=" in run_text
    assert "set PYTHONPATH=" in run_text
    assert run_text.index("set PYTHONHOME=") < run_text.index('set "VENV_PY=')
    assert run_text.index("set PYTHONPATH=") < run_text.index('set "VENV_PY=')


def test_run_bat_scopes_environment_changes_with_setlocal():
    """Clearing Python env vars must not leak into the caller's CMD session."""
    run_text = Path("run.bat").read_text(encoding="utf-8")
    lower_text = run_text.lower()

    assert "setlocal" in lower_text
    assert "endlocal" in lower_text
    assert lower_text.index("setlocal") < lower_text.index("set pythonhome=")


def test_run_bat_does_not_use_double_percent_env_vars_outside_for_loop():
    """%%VAR%% is not expanded as an environment variable outside FOR syntax."""
    run_text = Path("run.bat").read_text(encoding="utf-8")

    for line_no, line in enumerate(run_text.splitlines(), start=1):
        if re.match(r"\s*for\b", line, re.IGNORECASE):
            continue
        match = re.search(r"%%([A-Za-z_][A-Za-z_0-9]*)%%", line)
        assert match is None, (
            f"run.bat line {line_no}: use %{match.group(1)}% instead of "
            f"%%{match.group(1)}%% outside FOR syntax"
        )


def test_cmd_read_files_are_pure_crlf():
    """CMD-facing files must use CRLF-only line endings for Windows compatibility."""
    for name in CMD_TEXT_FILES:
        path = Path(name)
        if not path.exists():
            continue
        data = path.read_bytes()
        lf_only = data.count(b"\n") - data.count(b"\r\n")
        assert lf_only == 0, (
            f"{name} contains {lf_only} LF-only line(s). "
            "Windows CMD requires pure CRLF. Re-save with CRLF line endings."
        )


def test_cmd_read_files_are_utf8():
    """CMD-facing files must be decodable as UTF-8."""
    for name in CMD_TEXT_FILES:
        path = Path(name)
        if not path.exists():
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"{name} is not valid UTF-8: {exc}") from exc


def test_bat_files_set_chcp_65001_before_non_ascii_text():
    """BAT files must switch CMD to UTF-8 before Korean text is parsed or echoed."""
    for name in BAT_FILES:
        lines = Path(name).read_bytes().replace(b"\r\n", b"\n").decode("utf-8").splitlines()
        chcp_index = next((idx for idx, line in enumerate(lines) if "chcp 65001" in line), None)
        non_ascii_index = next(
            (
                idx
                for idx, line in enumerate(lines)
                if any(ord(char) > 127 for char in line.lstrip("\ufeff"))
            ),
            None,
        )

        assert chcp_index is not None, f"{name} must call 'chcp 65001'"
        if non_ascii_index is not None:
            assert chcp_index < non_ascii_index, (
                f"{name} must call 'chcp 65001' before the first non-ASCII line "
                f"(line {non_ascii_index + 1})"
            )
