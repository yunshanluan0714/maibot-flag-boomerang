"""SQLite storage layer for github.yunshanluan0714.flag-boomerang with strict per-group isolation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterable


@dataclass(frozen=True)
class FlagRecord:
    id: int
    group_id: str
    session_id: str
    user_id: str
    user_name: str
    message_id: str
    original_text: str
    summary: str
    category: str
    created_at: float
    deadline_ts: float | None
    sleep_cutoff_ts: float | None
    sleep_window_end_ts: float | None
    bust_keywords: tuple[str, ...]
    status: str
    busted_at: float | None
    bust_text: str
    bust_message_id: str
    confidence: float
    memory_saved: bool = False


_VALID_STATUSES = frozenset({"active", "busted", "pending_roast", "due_wait", "reminded", "fulfilled", "expired"})


class FlagStore:
    """Thread-safe SQLite store scoped strictly by group_id (or private:<uid>)."""

    def __init__(self, data_dir: Path | str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "flag_boomerang.db"
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS flags (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            group_id TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            user_name TEXT NOT NULL,
                            message_id TEXT NOT NULL DEFAULT '',
                            original_text TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            category TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            deadline_ts REAL,
                            sleep_cutoff_ts REAL,
                            sleep_window_end_ts REAL,
                            bust_keywords TEXT NOT NULL DEFAULT '[]',
                            status TEXT NOT NULL DEFAULT 'active',
                            busted_at REAL,
                            bust_text TEXT NOT NULL DEFAULT '',
                            bust_message_id TEXT NOT NULL DEFAULT '',
                            confidence REAL NOT NULL DEFAULT 0.8,
                            memory_saved INTEGER NOT NULL DEFAULT 0
                        )
                        """
                    )
                    cols = {r["name"] for r in conn.execute("PRAGMA table_info(flags)").fetchall()}
                    if "memory_saved" not in cols:
                        conn.execute("ALTER TABLE flags ADD COLUMN memory_saved INTEGER NOT NULL DEFAULT 0")
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_flags_group_status ON flags (group_id, status)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_flags_group_user ON flags (group_id, user_id, status)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_flags_deadline ON flags (status, deadline_ts)"
                    )
            finally:
                conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> FlagRecord:
        try:
            raw_kw = json.loads(row["bust_keywords"] or "[]")
            keywords = tuple(str(x).strip() for x in raw_kw if str(x).strip()) if isinstance(raw_kw, list) else ()
        except Exception:
            keywords = ()
        return FlagRecord(
            id=int(row["id"]),
            group_id=str(row["group_id"]),
            session_id=str(row["session_id"]),
            user_id=str(row["user_id"]),
            user_name=str(row["user_name"]),
            message_id=str(row["message_id"] or ""),
            original_text=str(row["original_text"]),
            summary=str(row["summary"]),
            category=str(row["category"]),
            created_at=float(row["created_at"]),
            deadline_ts=float(row["deadline_ts"]) if row["deadline_ts"] is not None else None,
            sleep_cutoff_ts=float(row["sleep_cutoff_ts"]) if row["sleep_cutoff_ts"] is not None else None,
            sleep_window_end_ts=float(row["sleep_window_end_ts"]) if row["sleep_window_end_ts"] is not None else None,
            bust_keywords=keywords,
            status=str(row["status"]),
            busted_at=float(row["busted_at"]) if row["busted_at"] is not None else None,
            bust_text=str(row["bust_text"] or ""),
            bust_message_id=str(row["bust_message_id"] or ""),
            confidence=float(row["confidence"]),
            memory_saved=bool(row["memory_saved"]) if "memory_saved" in row.keys() else False,
        )

    def add_flag(
        self,
        *,
        group_id: str,
        session_id: str,
        user_id: str,
        user_name: str,
        message_id: str = "",
        original_text: str,
        summary: str,
        category: str,
        created_at: float | None = None,
        deadline_ts: float | None = None,
        sleep_cutoff_ts: float | None = None,
        sleep_window_end_ts: float | None = None,
        bust_keywords: Iterable[str] = (),
        confidence: float = 0.8,
        max_active_per_user: int = 5,
    ) -> tuple[FlagRecord, bool]:
        now = float(created_at if created_at is not None else time.time())
        clean_orig = str(original_text or "").strip()[:300]
        clean_summary = str(summary or clean_orig).strip()[:120]
        kw_list = list(dict.fromkeys(str(k).strip() for k in bust_keywords if str(k).strip()))[:12]
        cap = max(1, int(max_active_per_user))

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    dup = conn.execute(
                        """
                        SELECT * FROM flags
                        WHERE group_id = ? AND user_id = ? AND status = 'active'
                          AND created_at >= ?
                          AND (original_text = ? OR summary = ?)
                        ORDER BY id DESC LIMIT 1
                        """,
                        (group_id, user_id, now - 600.0, clean_orig, clean_summary),
                    ).fetchone()
                    if dup is not None:
                        return self._row_to_record(dup), False

                    active_rows = conn.execute(
                        """
                        SELECT id FROM flags
                        WHERE group_id = ? AND user_id = ? AND status = 'active'
                        ORDER BY created_at ASC, id ASC
                        """,
                        (group_id, user_id),
                    ).fetchall()
                    overflow = len(active_rows) - (cap - 1)
                    if overflow > 0:
                        expire_ids = [(int(r["id"]),) for r in active_rows[:overflow]]
                        conn.executemany(
                            "UPDATE flags SET status = 'expired' WHERE id = ?",
                            expire_ids,
                        )

                    cur = conn.execute(
                        """
                        INSERT INTO flags (
                            group_id, session_id, user_id, user_name, message_id,
                            original_text, summary, category, created_at,
                            deadline_ts, sleep_cutoff_ts, sleep_window_end_ts,
                            bust_keywords, status, confidence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                        """,
                        (
                            group_id,
                            session_id,
                            user_id,
                            user_name or user_id,
                            str(message_id or ""),
                            clean_orig,
                            clean_summary,
                            category,
                            now,
                            deadline_ts,
                            sleep_cutoff_ts,
                            sleep_window_end_ts,
                            json.dumps(kw_list, ensure_ascii=False),
                            float(confidence),
                        ),
                    )
                    row = conn.execute("SELECT * FROM flags WHERE id = ?", (cur.lastrowid,)).fetchone()
                    assert row is not None
                    return self._row_to_record(row), True
            finally:
                conn.close()

    def get_active_for_user(self, group_id: str, user_id: str) -> list[FlagRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM flags
                    WHERE group_id = ? AND user_id = ? AND status = 'active'
                    ORDER BY created_at DESC, id DESC
                    """,
                    (group_id, user_id),
                ).fetchall()
                return [self._row_to_record(r) for r in rows]
            finally:
                conn.close()

    def get_pending_for_user(self, group_id: str, user_id: str) -> list[FlagRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM flags
                    WHERE group_id = ? AND user_id = ? AND status IN ('pending_roast', 'due_wait')
                    ORDER BY created_at DESC, id DESC
                    """,
                    (group_id, user_id),
                ).fetchall()
                return [self._row_to_record(r) for r in rows]
            finally:
                conn.close()

    def list_flags(
        self,
        group_id: str,
        *,
        status_filter: str = "active",
        user_name: str = "",
        keyword: str = "",
        limit: int = 8,
    ) -> list[FlagRecord]:
        lim = max(1, min(15, int(limit)))
        clauses = ["group_id = ?"]
        params: list[object] = [group_id]

        sf = str(status_filter or "active").strip().lower()
        if sf == "active":
            clauses.append("status IN ('active', 'pending_roast', 'due_wait')")
        elif sf in ("busted", "fulfilled", "reminded", "expired"):
            clauses.append("status = ?")
            params.append(sf)
        elif sf == "all":
            clauses.append("status != 'expired'")
        else:
            clauses.append("status IN ('active', 'pending_roast', 'due_wait')")

        if user_name.strip():
            clauses.append("(user_name LIKE ? OR user_id = ?)")
            like_u = f"%{user_name.strip()}%"
            params.extend([like_u, user_name.strip()])

        if keyword.strip():
            clauses.append("(summary LIKE ? OR original_text LIKE ?)")
            like_k = f"%{keyword.strip()}%"
            params.extend([like_k, like_k])

        sql = f"SELECT * FROM flags WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(lim)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [self._row_to_record(r) for r in rows]
            finally:
                conn.close()

    def find_due_flags(self, now_ts: float | None = None) -> list[FlagRecord]:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM flags
                    WHERE status = 'active' AND deadline_ts IS NOT NULL AND deadline_ts <= ?
                    ORDER BY deadline_ts ASC, id ASC
                    """,
                    (now,),
                ).fetchall()
                return [self._row_to_record(r) for r in rows]
            finally:
                conn.close()

    def mark_busted(
        self,
        flag_id: int,
        *,
        busted_at: float | None = None,
        bust_text: str = "",
        bust_message_id: str = "",
    ) -> bool:
        now = float(busted_at if busted_at is not None else time.time())
        return self._transition_bust(flag_id, "busted", now, bust_text, bust_message_id)

    def mark_pending_roast(
        self,
        flag_id: int,
        *,
        busted_at: float | None = None,
        bust_text: str = "",
        bust_message_id: str = "",
    ) -> bool:
        now = float(busted_at if busted_at is not None else time.time())
        return self._transition_bust(flag_id, "pending_roast", now, bust_text, bust_message_id)

    def _transition_bust(
        self,
        flag_id: int,
        target_status: str,
        busted_at: float,
        bust_text: str,
        bust_message_id: str,
    ) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        UPDATE flags
                        SET status = ?, busted_at = ?, bust_text = ?, bust_message_id = ?
                        WHERE id = ? AND status IN ('active', 'pending_roast')
                        """,
                        (
                            target_status,
                            float(busted_at),
                            str(bust_text or "")[:300],
                            str(bust_message_id or ""),
                            int(flag_id),
                        ),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def mark_due_wait(self, flag_id: int) -> bool:
        return self._simple_transition(flag_id, from_statuses=("active",), to_status="due_wait")

    def mark_reminded(self, flag_id: int) -> bool:
        return self._simple_transition(flag_id, from_statuses=("active", "due_wait"), to_status="reminded")

    def _simple_transition(self, flag_id: int, *, from_statuses: tuple[str, ...], to_status: str) -> bool:
        placeholders = ",".join("?" for _ in from_statuses)
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        f"UPDATE flags SET status = ? WHERE id = ? AND status IN ({placeholders})",
                        (to_status, int(flag_id), *from_statuses),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def find_flag_by_id(self, group_id: str, flag_id: int) -> FlagRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM flags WHERE id = ? AND group_id = ?",
                    (int(flag_id), group_id),
                ).fetchone()
                return self._row_to_record(row) if row is not None else None
            finally:
                conn.close()

    def update_status(self, group_id: str, flag_id: int, new_status: str) -> FlagRecord | None:
        st = str(new_status or "").strip()
        if st not in _VALID_STATUSES:
            return None
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE flags SET status = ? WHERE id = ? AND group_id = ?",
                        (st, int(flag_id), group_id),
                    )
                    if cur.rowcount <= 0:
                        return None
                    row = conn.execute(
                        "SELECT * FROM flags WHERE id = ? AND group_id = ?",
                        (int(flag_id), group_id),
                    ).fetchone()
                    return self._row_to_record(row) if row is not None else None
            finally:
                conn.close()

    def mark_memory_saved(self, flag_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE flags SET memory_saved = 1 WHERE id = ?",
                        (int(flag_id),),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()
