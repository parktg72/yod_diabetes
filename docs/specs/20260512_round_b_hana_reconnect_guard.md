# Spec: Round B — HANA Reconnect Guard

> 작성자: Claude | 날짜: 2026-05-12 | 상태: approved

## 목표 (Goal)

`fetch_table_chunked` 호출 중 HANA 세션이 만료(`-10821 Session not connected`)되면
`self.conn`이 non-None 상태를 유지한 채 깨진 연결로 남는다.
이후 호출마다 동일한 -10821이 연쇄 발생해 144개월 추출 전체가 실패한다.
이를 방지하기 위해 `fetch_table_chunked` 진입부에 경량 ping + 자동 재연결 가드를 추가한다.

## 범위 (In-Scope)

- `db_connector.py` `HANAConnector` 클래스
  - `_reconnect_if_stale()` 메서드 추가
  - `fetch_table_chunked` 진입부의 `if not self.conn: self.connect()` → `self._reconnect_if_stale()` 교체
- `tests/test_db_connector.py`
  - `_reconnect_if_stale` 유닛 테스트 4개 (TDD — 테스트 먼저)

## 비범위 (Out-of-Scope)

- `get_hana_schemas`, `get_hana_tables`, `get_hana_columns` 등 브라우징 메서드: 사용자 대화형 호출이라 cascade 위험 낮음, 이번 라운드 제외
- -10821 외 다른 HANA 에러 코드 처리 (별도 관찰 후 추가)
- `MonthlyHanaExtractor`, `DataLoadTab` 레벨 변경 없음

## 설계 (Design)

### 실패 경로

```
fetch_table_chunked(table_A) → HANA 오류 → self.conn 여전히 non-None(깨진 상태)
fetch_table_chunked(table_B) → if not self.conn 조건 False → 재연결 없음 → -10821
fetch_table_chunked(table_C) → 동일 → -10821  ← cascade
```

### 수정 내용

| 파일 | 변경 | 이유 |
|------|------|------|
| `db_connector.py` | `_reconnect_if_stale()` 추가 | 경량 ping 후 -10821 감지 시 재연결 |
| `db_connector.py:801` | `if not self.conn: self.connect()` → `self._reconnect_if_stale()` | cascade 차단 진입부 |

### `_reconnect_if_stale()` 구현

```python
def _reconnect_if_stale(self):
    """self.conn이 없으면 연결, 있지만 만료됐으면(-10821) 재연결."""
    if not self.conn:
        self.connect()
        return
    try:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 'OK' FROM DUMMY")
        cursor.fetchone()
        cursor.close()
    except Exception as e:
        err = str(e)
        if '-10821' in err or 'Session not connected' in err.lower():
            logger.warning("HANA 세션 만료 감지 (-10821), 재연결 시도")
            self.conn = None
            self.connect()
        else:
            raise
```

### 핵심 결정 사항

- **proactive ping vs. reactive retry**: proactive ping 선택.
  - reactive retry는 `fetch_table_chunked` 내부(keyset/OFFSET 분기)를 모두 감싸야 해 복잡도 증가.
  - `SELECT FROM DUMMY` 1회 = LAN 기준 1ms 미만. 월별 추출 144개월 × 10테이블 = 1440회 추가 왕복이지만 총 오버헤드 수 초 미만, 허용 가능.
- **에러 문자열 매칭**: hdbcli는 표준 `errno` 속성 없이 문자열에 `-10821` 포함. 문자열 포함 검사로 충분.

## 테스트 기준 (Test Criteria)

TDD 순서 준수: 테스트 먼저 작성(RED) → 구현(GREEN) → 기준선 확인.

| # | 테스트명 | 시나리오 |
|---|---------|---------|
| 1 | `test_reconnect_if_stale_connects_when_no_conn` | `self.conn = None` → `connect()` 호출됨 |
| 2 | `test_reconnect_if_stale_noop_when_healthy` | ping 성공 → `connect()` 미호출 |
| 3 | `test_reconnect_if_stale_reconnects_on_10821` | ping에서 `-10821` 예외 → `self.conn = None` 후 `connect()` 재호출 |
| 4 | `test_reconnect_if_stale_raises_on_other_errors` | ping에서 다른 예외 → 그대로 re-raise |

기준선: `pytest tests/ -q` → **440 + 4 = 444 passed** (신규 4개 추가)

## 리스크 (Risk)

| 리스크 | 가능성 | 완화 방법 |
|--------|--------|----------|
| `SELECT FROM DUMMY` ping이 HANA 권한 문제로 실패 | 낮음 | DUMMY는 모든 HANA 사용자 접근 가능한 시스템 뷰 |
| 재연결 시도 중 `_password_buf`가 소거된 상태 | 낮음 | `destroy()` 호출 후에만 소거됨; 정상 세션 만료 시는 유지 |
| ping 오버헤드로 추출 시간 증가 | 낮음 | `SELECT FROM DUMMY` ≈ 1ms, 전체 영향 미미 |

## 참조

- 관련 이슈: `project_open_issues.md` §1 — HANA -10821 모니터링
- 관련 코드: `db_connector.py:785` `fetch_table_chunked`, `db_connector.py:380` `HANAConnector.connect`
- 기존 재시도 로직: `HANAConnector.connect(max_retries=2)` — 초기 연결 전용, 세션 만료 후 재연결에 미적용
