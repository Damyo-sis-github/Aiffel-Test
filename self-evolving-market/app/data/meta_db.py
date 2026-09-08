"""§4.2 메타·로그 저장 (SQLite). evolution_log 는 append-only 로 강제한다 (§13.4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pandas as pd

from app.paths import meta_db_path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS predictions (
  pred_date TEXT NOT NULL, horizon INTEGER NOT NULL, rank INTEGER NOT NULL,
  symbol TEXT NOT NULL, market TEXT NOT NULL, side INTEGER NOT NULL,
  score REAL NOT NULL, scored_at TEXT, hit INTEGER,
  PRIMARY KEY (pred_date, horizon, side, rank)
);
CREATE INDEX IF NOT EXISTS ix_pred_scored ON predictions(scored_at, horizon);

CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY, account TEXT NOT NULL, strategy_id TEXT NOT NULL,
  symbol TEXT NOT NULL, market TEXT NOT NULL, side INTEGER NOT NULL,
  instrument TEXT NOT NULL, family TEXT NOT NULL,
  signal_date TEXT NOT NULL, fill_date TEXT NOT NULL, fill_px REAL NOT NULL,
  exit_date TEXT, exit_px REAL, qty REAL NOT NULL,
  cost REAL NOT NULL DEFAULT 0, borrow_cost REAL NOT NULL DEFAULT 0,
  pnl REAL, pnl_pct REAL, closed INTEGER NOT NULL DEFAULT 0, exit_reason TEXT,
  bt_px REAL                      -- §9 백테스트 가정 체결가. 실제 체결가와의 괴리를 추적한다.
);
CREATE INDEX IF NOT EXISTS ix_trades_strat ON trades(strategy_id, account, closed, exit_date);

CREATE TABLE IF NOT EXISTS positions (
  date TEXT NOT NULL, account TEXT NOT NULL, strategy_id TEXT NOT NULL,
  symbol TEXT NOT NULL, qty REAL NOT NULL, mv REAL NOT NULL,
  notional_exposure REAL NOT NULL, leverage REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (date, account, strategy_id, symbol)
);

CREATE TABLE IF NOT EXISTS nav (
  date TEXT NOT NULL, account TEXT NOT NULL, nav REAL NOT NULL, cash REAL NOT NULL,
  gross_notional_pct REAL, short_notional_pct REAL, lev_inv_notional_pct REAL,
  drawdown REAL, source TEXT,
  PRIMARY KEY (date, account)
);

CREATE TABLE IF NOT EXISTS strategy_runs (
  run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
  fold INTEGER, window TEXT, metrics_json TEXT NOT NULL,
  code_hash TEXT NOT NULL, data_hash TEXT NOT NULL, gates_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_strat ON strategy_runs(strategy_id, created_at);

CREATE TABLE IF NOT EXISTS strategy_state (
  strategy_id TEXT PRIMARY KEY, family TEXT NOT NULL, version TEXT NOT NULL,
  status TEXT NOT NULL, since TEXT NOT NULL, allocation_pct REAL NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0, note TEXT
);

CREATE TABLE IF NOT EXISTS hypotheses (
  hyp_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, family TEXT NOT NULL,
  text TEXT NOT NULL, rationale TEXT NOT NULL, falsifier TEXT NOT NULL,
  code_path TEXT, status TEXT NOT NULL, k_index INTEGER NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, cycle_id TEXT, trigger TEXT, action TEXT NOT NULL,
  before TEXT, after TEXT, reason TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, level TEXT NOT NULL, channel TEXT NOT NULL,
  message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_log (
  run_date TEXT NOT NULL, task TEXT NOT NULL, mode TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  result_hash TEXT, catchup INTEGER NOT NULL DEFAULT 0, note TEXT,
  PRIMARY KEY (run_date, task)
);

CREATE TABLE IF NOT EXISTS integrity_events (
  ts TEXT NOT NULL, run_date TEXT NOT NULL, check_name TEXT NOT NULL,
  grade TEXT NOT NULL, symbol TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS multiple_testing (
  k_index INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- §13.4 evolution_log append-only 강제
CREATE TRIGGER IF NOT EXISTS evolution_log_no_update
BEFORE UPDATE ON evolution_log
BEGIN SELECT RAISE(ABORT, 'evolution_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evolution_log_no_delete
BEFORE DELETE ON evolution_log
BEGIN SELECT RAISE(ABORT, 'evolution_log is append-only'); END;
"""


class MetaDB:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else meta_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            self._add_missing_columns(con)

    # CREATE TABLE IF NOT EXISTS 는 **이미 있는 표에 새 컬럼을 붙이지 않는다.**
    # 운영 기기에는 이미 거래 이력이 든 DB 가 있다. 지우고 다시 만들면 그 이력이
    # 사라지고 W_trade·E 가 리셋된다. 그래서 없는 컬럼만 덧붙인다 — 절대 지우지 않는다.
    ADDED_COLUMNS = (
        ("trades", "bt_px", "REAL"),
    )

    def _add_missing_columns(self, con: sqlite3.Connection) -> None:
        for table, column, decl in self.ADDED_COLUMNS:
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if have and column not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            yield con
            if con.in_transaction:
                con.execute("COMMIT")
        except Exception:
            # 원래 예외를 가리지 않도록 롤백 실패는 삼킨다.
            if con.in_transaction:
                with suppress(sqlite3.Error):
                    con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    # ------------------------------------------------------------ 일반

    def upsert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0])
        sql = (
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})"
        )
        with self.connect() as con:
            con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)

    def query(self, sql: str, params: Iterable = ()) -> pd.DataFrame:
        with self.connect() as con:
            return pd.DataFrame([dict(r) for r in con.execute(sql, tuple(params)).fetchall()])

    def scalar(self, sql: str, params: Iterable = ()):
        with self.connect() as con:
            row = con.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    # ------------------------------------------------------------ evolution_log

    def log_evolution(
        self,
        *,
        ts: str,
        action: str,
        cycle_id: str | None = None,
        trigger: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str = "",
    ) -> None:
        """append-only. UPDATE/DELETE 는 트리거가 막는다."""
        with self.connect() as con:
            con.execute(
                "INSERT INTO evolution_log (ts, cycle_id, trigger, action, before, after, reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    ts,
                    cycle_id,
                    trigger,
                    action,
                    None if before is None else json.dumps(before, ensure_ascii=False, default=str),
                    None if after is None else json.dumps(after, ensure_ascii=False, default=str),
                    reason,
                ),
            )

    def next_k_index(self) -> int:
        """§10.3-5 누적 K. **리셋 없음.**"""
        cur = self.scalar("SELECT COALESCE(MAX(k_index), 0) FROM multiple_testing")
        return int(cur or 0) + 1

    def current_k(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(k_index), 0) FROM multiple_testing") or 0)
