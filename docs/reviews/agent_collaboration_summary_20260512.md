# Agent Collaboration Summary - 2026-05-12

Codex, Claude, Hermes가 같은 기준으로 이어서 작업할 수 있도록 현재까지의 협업 결과와 결정 사항을 정리한다.

## Current State

- Working tree: clean at the time this summary was written.
- Current baseline: `pytest tests/ -q` -> `444 passed`.
- Latest completed work commit before this summary: `1b58a79 docs: result helper wrapper 설명 정리`.
- Main detailed history: `docs/reviews/agent_debug_log_20260510.md`.

## Collaboration Rules

- 메시지를 보내기 전 수신자 작업 상태를 확인한다.
  - Claude/Hermes/Codex 중 한쪽이 작업 중이면 완료 또는 pending 상태를 먼저 확인한 뒤 메시지를 보낸다.
  - timeout 또는 응답 지연이 있으면 같은 작업을 즉시 중복 요청하지 않고, pending message와 작업트리 변경 여부를 확인한다.
- 구현 역할:
  - Hermes: coding 또는 테스트 작성.
  - Codex: 1차 검수, 테스트 재실행, 누락 보정, 커밋.
  - Claude: 2차 리뷰, 다음 범위 협의.
- 커밋 원칙:
  - 테스트 통과 전 커밋 금지.
  - `Co-Authored-By` trailer는 삽입하지 않는다.
  - `⚠️` 권한 항목 변경은 커밋 전 사용자 명시 승인 원문/시각/채널 기록이 필요하다.

## Completed Commits

- `2040385 feat: reason_code 오류 상세 및 GUI 표시 개선 (R2)`
  - R2 누적 변경 커밋.
  - reason_code 기반 skip/error/model failure 구조화.
  - `run_post_analysis.error_details`, `step_error_details` GUI 표시, Cox failed_models 구조화, ITB covariate guard 등 포함.
- `6c778cc docs: ⚠️ 권한 항목 승인 기록 의무 추가 (R3)`
  - AGENTS.md permission gate 문서화.
- `32c1c34 refactor: 오류 결과 helper 공통화`
  - `utils.make_error_result` 추가.
  - `analysis_runner.py`와 `StatisticalAnalyzer._error_result`가 공통 helper 사용.
- `9d6cb7e refactor: skip 및 model failure helper 공통화`
  - `utils.make_skip_result`, `utils.make_model_failure` 추가.
  - `StatisticalAnalyzer._skip_result`, `_model_failure`는 thin wrapper 유지.
- `1c2ab48 refactor: ResultsTab 예외 처리 중복 정리`
  - `ResultsTab.export`, `export_all`, `plot_km`, `plot_forest`의 중복 except 병합.
  - `MemoryTab._update_mem_status` bare pass를 debug logging으로 변경.
- `26d704e refactor: HanaBrowserTab 예외 처리 중복 정리`
  - `HanaBrowserTab.load_schemas`, `on_tree_click`, `search_hana_tables`의 중복 except 병합.
- `3d51dc8 refactor: tabs broad except 정리 마무리`
  - `AnalysisTab._confirm_sampling_if_needed` 로그를 `exc_info=True`로 개선.
  - `DataLoadTab.do_load`의 per-table broad except가 intentional continue-on-error임을 주석으로 명시.
- `15da83f test: HanaBrowserTab 실패 경로 회귀 테스트 추가`
  - QApplication 없이 unbound method + `MagicMock` self 패턴으로 HANA failure path 테스트 3개 추가.
- `1b58a79 docs: result helper wrapper 설명 정리`
  - `StatisticalAnalyzer` helper wrapper docstring을 `utils.* 위임 wrapper`로 정리.

## Important Technical Decisions

- `reason_code` schema:
  - skip result: `skipped=True`, `reason_code`, `reason`, optional `stage`, extra fields.
  - model failure: `reason_code`, `reason`, default `stage='cox'`, no `skipped`, no `exception_type`.
  - error result: `reason_code`, `reason=str(error)`, `exception_type`, optional `stage`, extra fields.
- Shared helper location:
  - `utils.make_skip_result`
  - `utils.make_model_failure`
  - `utils.make_error_result`
- `StatisticalAnalyzer` keeps wrapper methods:
  - `_skip_result`
  - `_model_failure`
  - `_error_result`
  This avoids call-site churn and keeps internal API stable.
- GUI display:
  - `run_post_analysis.error_details` is shown additively in `AnalysisTab._on_post_analysis`.
  - `step_error_details` is integrated into the existing "일부 분석 단계 실패" popup via `QMessageBox.setDetailedText`.
  - Cox failed model structured dicts and legacy strings are both supported.
- HANA GUI tests:
  - Do not instantiate `HanaBrowserTab(QWidget)` in tests without QApplication.
  - Use unbound method calls with `MagicMock` self.

## Current Test Baseline

- `pytest tests/ -q` -> `444 passed`.
- `AGENTS.md` has been updated to this baseline.

## Known Non-Blocking Follow-Ups

- HANA mocking test can be strengthened later by using a more unique error string for `search_hana_tables`.
- Remaining intentional broad exceptions:
  - `ConnectionTab.test_hana`: optional HANA dependency and connection UI boundary.
  - Connection-related UI event handlers where GUI resilience is the main purpose.
- Any future GUI helper extraction should be done in a separate small round with manual QA awareness.

## Recommended Next Candidates

1. Review remaining `tabs.py` broad exception sites and decide if they are intentionally broad.
2. Add focused tests only where a real regression risk exists.
3. Avoid broad refactors until the current reason_code/error-detail improvements have had runtime validation.
