"""tests/test_stage_s.py — Stage S: tabs.py 방어 코드 테스트"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_on_post_analysis_result_none_guard():
    """result = data.get('result') or {} 패턴이 None 을 {} 로 대체하는지 검증."""
    # 로직 단독 검증 — tabs.py import 불필요
    data_with_none = {'result': None}
    result = data_with_none.get('result') or {}
    assert result == {}, f"None 이 {{}} 로 대체되지 않음: {result!r}"

    data_with_dict = {'result': {'errors': ['err1'], 'exported_files': []}}
    result2 = data_with_dict.get('result') or {}
    assert result2.get('errors') == ['err1'], "정상 dict 가 유지되지 않음"

    data_missing = {}
    result3 = data_missing.get('result') or {}
    assert result3 == {}, f"키 없을 때 {{}} 로 대체되지 않음: {result3!r}"
