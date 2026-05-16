from pathlib import Path


def test_setup_bat_escapes_parentheses_inside_hana_skip_echo():
    """cmd.exe parses unescaped ')' inside IF blocks as block terminators."""
    setup_text = Path("setup.bat").read_text(encoding="utf-8")

    assert "로컬 파일^(.parquet/.sas7bdat^)만" in setup_text


def test_setup_bat_recreates_invalid_existing_venv():
    """Moved or stale venv launchers must be detected before installing packages."""
    setup_text = Path("setup.bat").read_text(encoding="utf-8")

    assert 'set "VENV_DIR=venv"' in setup_text
    assert 'set "VENV_PY=%VENV_DIR%\\Scripts\\python.exe"' in setup_text
    assert '"%VENV_PY%" -c "import sys"' in setup_text
    assert 'rmdir /s /q "%VENV_DIR%"' in setup_text


def test_setup_bat_installs_packages_with_venv_python():
    """Package installation should target the project venv, not global Python."""
    setup_text = Path("setup.bat").read_text(encoding="utf-8")

    assert '"%VENV_PY%" -m pip install --no-cache-dir --upgrade pip' in setup_text
    assert '"%VENV_PY%" -m pip install --no-cache-dir -r requirements.txt' in setup_text
    assert '"%VENV_PY%" -m pip install --no-cache-dir -r requirements-hana.txt' in setup_text


def test_setup_bat_clears_python_environment_before_direct_venv_python():
    """Direct venv Python calls should not inherit global PYTHONHOME/PYTHONPATH."""
    setup_text = Path("setup.bat").read_text(encoding="utf-8")

    assert "set PYTHONHOME=" in setup_text
    assert "set PYTHONPATH=" in setup_text
    assert setup_text.index("set PYTHONHOME=") < setup_text.index('set "VENV_PY=')
    assert setup_text.index("set PYTHONPATH=") < setup_text.index('set "VENV_PY=')
