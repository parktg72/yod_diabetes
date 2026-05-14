# Agent Collaboration Summary - 2026-05-12

Codex, Claude, Hermes가 같은 기준으로 이어서 작업할 수 있도록 현재까지의 협업 결과와 결정 사항을 정리한다.

## Current State

- Working tree: clean at `80b6244`; P4/P5 docs are pending after that commit.
- Current baseline: `pytest tests/ -q` -> `450 passed`.
- Latest completed work commit before P4/P5 docs: `80b6244 fix: HANA 가드와 Phase2 검증 정리`.
- Main detailed history: `docs/reviews/agent_debug_log_20260510.md`,
  `docs/reviews/debug_log_20260512.md`, and `docs/reviews/debug_log_20260514.md`.
- Active approved specs: `docs/specs/20260512_round_b_hana_reconnect_guard.md`.

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
- `2ead92e refactor: HANA UI 경계 broad except에 intentional 주석 추가 (Round A)`
  - `ConnectionTab.test_hana`, `HanaBrowserTab.load_schemas`, `on_tree_click`, `search_hana_tables`의 broad except 의도를 주석으로 명시.
  - `docs/reviews/debug_log_20260512.md`에 Round A 기록 추가.
- `fb64ec1 feat: HANA 세션 만료 재연결 가드 추가 (Round B)`
  - `HANAConnector._reconnect_if_stale()` 추가.
  - `fetch_table_chunked` 진입부에서 stale HANA 세션을 ping 후 `-10821`/`Session not connected`일 때 재연결.
  - `_reconnect_if_stale` 단위 테스트 4개 추가.
  - `docs/specs/20260512_round_b_hana_reconnect_guard.md`, `docs/specs/TEMPLATE.md` 추가.
- `80b6244 fix: HANA 가드와 Phase2 검증 정리`
  - `DataManager` HANA browsing wrapper 4개에 stale reconnect guard 적용.
  - `Phase2Visualizer.plot_forest_plot()` 비활성화 및 회귀 테스트 추가.
  - 폐쇄망 실제 데이터 검증 절차와 forest plot 미생성 정상 기준 문서화.

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
- HANA reconnect guard:
  - `fetch_table_chunked` 시작 전에 `SELECT 'OK' FROM DUMMY` ping을 수행한다.
  - `-10821` 또는 `Session not connected`일 때만 자동 재연결하고, 다른 ping 오류는 그대로 전파한다.
  - Cursor close 실패는 debug log로 남기되 원래 재연결/오류 흐름을 막지 않는다.

## Current Test Baseline

- `pytest tests/ -q` -> `450 passed`.
- `AGENTS.md` has been updated to this baseline.

## Known Non-Blocking Follow-Ups

- HANA mocking test can be strengthened later by using a more unique error string for `search_hana_tables`.
- Round A closed the known HANA connection UI broad exception audit. Remaining GUI broad exceptions should be changed only with focused regression risk.
- Any future GUI helper extraction should be done in a separate small round with manual QA awareness.
- Round B should be runtime-observed against real HANA sessions before expanding reconnect handling to other methods or error codes.

## Recommended Next Candidates

1. Run Phase 2 in the NHIS closed network using `phase2_run.bat` and review
   `phase2_output/INTERPRETATION_GUIDE.md`.
2. Treat missing `forest_t2dm_oha_switch.png` as normal; use
   `table_cox_results.csv` for subgroup HR interpretation.
3. If runtime logs show another stale-session code/path, create a new Claude
   spec before expanding reconnect handling.
