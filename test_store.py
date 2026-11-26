"""Unit tests for flag-boomerang SQLite store."""

import sqlite3
from pathlib import Path
import tempfile
import unittest

from store import FlagStore


class StoreTests(unittest.TestCase):
    def test_crud_dedup_cap_and_group_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st = FlagStore(Path(td))
            r1, ins1 = st.add_flag(
                group_id="10001",
                session_id="s1",
                user_id="u1",
                user_name="阿强",
                original_text="今晚11点前必睡",
                summary="今晚11点前早睡",
                category="sleep_vow",
                created_at=1000.0,
                sleep_cutoff_ts=1200.0,
                sleep_window_end_ts=2000.0,
                max_active_per_user=2,
            )
            self.assertTrue(ins1)

            # 10 分钟内重复立同一句应去重
            r_dup, ins_dup = st.add_flag(
                group_id="10001",
                session_id="s1",
                user_id="u1",
                user_name="阿强",
                original_text="今晚11点前必睡",
                summary="今晚11点前早睡",
                category="sleep_vow",
                created_at=1050.0,
                max_active_per_user=2,
            )
            self.assertFalse(ins_dup)
            self.assertEqual(r_dup.id, r1.id)

            # 超过单人上限时自动归档最旧记录
            st.add_flag(
                group_id="10001",
                session_id="s1",
                user_id="u1",
                user_name="阿强",
                original_text="下个池子绝不抽卡",
                summary="不抽下个池子",
                category="spend_gacha_vow",
                created_at=1100.0,
                max_active_per_user=2,
            )
            st.add_flag(
                group_id="10001",
                session_id="s1",
                user_id="u1",
                user_name="阿强",
                original_text="三天内写完作业",
                summary="三天写完作业",
                category="deadline_goal",
                created_at=1200.0,
                deadline_ts=1500.0,
                max_active_per_user=2,
            )
            active_u1 = st.get_active_for_user("10001", "u1")
            self.assertEqual(len(active_u1), 2)
            self.assertEqual(st.find_flag_by_id("10001", r1.id).status, "expired")  # type: ignore[union-attr]

            # B 群完全隔离：查不到 A 群记录，也不能跨群改 A 群状态
            self.assertEqual(st.list_flags("20002", status_filter="all"), [])
            self.assertIsNone(st.update_status("20002", active_u1[0].id, "fulfilled"))

            # pending_roast -> busted 与到期检查
            ok_pending = st.mark_pending_roast(active_u1[1].id, busted_at=1600.0, bust_text="还没睡")
            self.assertTrue(ok_pending)
            self.assertEqual(len(st.get_pending_for_user("10001", "u1")), 1)
            self.assertTrue(st.mark_busted(active_u1[1].id, busted_at=1700.0, bust_text="破戒"))

            due = st.find_due_flags(now_ts=1600.0)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0].id, active_u1[0].id)
            self.assertTrue(st.mark_due_wait(due[0].id))

    def test_schema_migration_and_mark_memory_saved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "flag_boomerang.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE flags (
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
                    confidence REAL NOT NULL DEFAULT 0.8
                )
                """
            )
            conn.commit()
            conn.close()

            st = FlagStore(Path(td))
            rec, ins = st.add_flag(
                group_id="10001",
                session_id="s1",
                user_id="u1",
                user_name="阿强",
                original_text="再氪648我是狗",
                summary="不氪648",
                category="spend_gacha_vow",
                created_at=2000.0,
            )
            self.assertTrue(ins)
            self.assertFalse(rec.memory_saved)
            self.assertTrue(st.mark_memory_saved(rec.id))
            reloaded = st.find_flag_by_id("10001", rec.id)
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            self.assertTrue(reloaded.memory_saved)


if __name__ == "__main__":
    unittest.main()
