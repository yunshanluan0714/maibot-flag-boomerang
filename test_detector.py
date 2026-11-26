"""Unit tests for flag-boomerang detector."""

from datetime import datetime, timedelta, timezone
import unittest

from detector import (
    build_extract_prompt,
    compute_sleep_window,
    evaluate_outrageous_bust,
    fallback_parse_candidate,
    in_quiet_hours,
    match_keyword_bust,
    parse_chinese_deadline,
    parse_llm_bust_detail,
    parse_llm_bust_response,
    parse_llm_extract_response,
    prefilter_flag_candidate,
)
from store import FlagRecord


class DetectorTests(unittest.TestCase):
    def _dt(self, year: int = 2026, month: int = 11, day: int = 25, hour: int = 20, minute: int = 0) -> datetime:
        tz = timezone(timedelta(hours=8))
        return datetime(year, month, day, hour, minute, 0, tzinfo=tz)

    def test_prefilter_four_categories_and_ignores_chatter(self) -> None:
        now_dt = self._dt(hour=20, minute=0)

        c1 = prefilter_flag_candidate("今晚11点前必睡，再熬夜我是狗", now_dt)
        self.assertIsNotNone(c1)
        assert c1 is not None
        self.assertEqual(c1.category, "sleep_vow")
        self.assertEqual(c1.sleep_cutoff_hhmm, "23:00")

        c2 = prefilter_flag_candidate("下个池子绝对不抽，再抽剁手", now_dt)
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2.category, "spend_gacha_vow")
        self.assertIn("真香", c2.bust_keywords)

        c3 = prefilter_flag_candidate("月底前一定把论文写完", now_dt)
        self.assertIsNotNone(c3)
        assert c3 is not None
        self.assertEqual(c3.category, "deadline_goal")
        self.assertIsNotNone(c3.deadline_ts)

        c4 = prefilter_flag_candidate("立贴为证，这个新游明天开服必炸服", now_dt)
        self.assertIsNotNone(c4)
        assert c4 is not None
        self.assertEqual(c4.category, "bold_prediction")

        self.assertIsNone(prefilter_flag_candidate("你们今天晚上准备几点睡呀？", now_dt))
        self.assertIsNone(prefilter_flag_candidate("好困", now_dt))
        self.assertIsNone(prefilter_flag_candidate("【防撤回提醒】张三撤回了一条消息", now_dt))

    def test_sleep_window_across_midnight_and_quiet_hours(self) -> None:
        evening = self._dt(day=25, hour=21, minute=0)
        bust_start, win_end = compute_sleep_window("23:00", evening)
        self.assertEqual(datetime.fromtimestamp(bust_start, tz=evening.tzinfo).strftime("%d %H:%M"), "25 23:15")
        self.assertEqual(datetime.fromtimestamp(win_end, tz=evening.tzinfo).strftime("%d %H:%M"), "26 05:30")

        bust_00, win_end_00 = compute_sleep_window("00:30", evening)
        self.assertEqual(datetime.fromtimestamp(bust_00, tz=evening.tzinfo).strftime("%d %H:%M"), "26 00:45")
        self.assertEqual(datetime.fromtimestamp(win_end_00, tz=evening.tzinfo).strftime("%d %H:%M"), "26 05:30")

        self.assertTrue(in_quiet_hours(self._dt(hour=2, minute=30), "01:00", "08:00"))
        self.assertFalse(in_quiet_hours(self._dt(hour=23, minute=40), "01:00", "08:00"))

    def test_chinese_deadline_and_llm_parsers(self) -> None:
        now_dt = self._dt(day=25, hour=14, minute=0)
        ts_tomorrow = parse_chinese_deadline("明天一定交稿", now_dt)
        self.assertIsNotNone(ts_tomorrow)
        assert ts_tomorrow is not None
        self.assertEqual(datetime.fromtimestamp(ts_tomorrow, tz=now_dt.tzinfo).day, 26)

        ts_3d = parse_chinese_deadline("三天内搞定这个bug", now_dt)
        self.assertIsNotNone(ts_3d)
        assert ts_3d is not None
        self.assertEqual(datetime.fromtimestamp(ts_3d, tz=now_dt.tzinfo).day, 28)

        cand = prefilter_flag_candidate("再氪648我是狗", now_dt)
        assert cand is not None
        self.assertIn("再氪648", build_extract_prompt("阿明", "再氪648我是狗", now_dt))

        parsed = parse_llm_extract_response(
            '```json\n{"is_flag": true, "category": "spend_gacha_vow", "summary": "发誓不氪648", "bust_keywords": ["真香", "氪了"], "confidence": 0.92}\n```',
            cand,
            now_dt,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.is_flag)
        self.assertGreater(parsed.confidence, 0.9)

        fb = fallback_parse_candidate(cand, now_dt)
        self.assertTrue(fb.is_flag)

        flag = FlagRecord(
            id=1,
            group_id="1001",
            session_id="s1",
            user_id="u1",
            user_name="阿明",
            message_id="m1",
            original_text="再抽卡剁手",
            summary="发誓不再抽卡",
            category="spend_gacha_vow",
            created_at=now_dt.timestamp(),
            deadline_ts=None,
            sleep_cutoff_ts=None,
            sleep_window_end_ts=None,
            bust_keywords=("真香", "抽了", "出金"),
            status="active",
            busted_at=None,
            bust_text="",
            bust_message_id="",
            confidence=0.9,
        )
        self.assertEqual(match_keyword_bust("草还是真香了刚抽了十连", flag), ["真香", "抽了"])
        self.assertEqual(match_keyword_bust("我朋友刚才出金了", flag), [])

        ok_bust, reason = parse_llm_bust_response('{"busted": true, "reason": "自己承认十连真香了"}')
        self.assertTrue(ok_bust)
        self.assertIn("真香", reason)

        ok2, reason2, out2 = parse_llm_bust_detail(
            '{"busted": true, "outrageous": true, "reason": "刚发毒誓不到一小时就氪了648"}'
        )
        self.assertTrue(ok2)
        self.assertTrue(out2)
        self.assertIn("648", reason2)

    def test_outrageous_scoring_thresholds(self) -> None:
        base_dt = self._dt(day=25, hour=21, minute=0)
        normal_sleep = FlagRecord(
            id=1,
            group_id="1001",
            session_id="s1",
            user_id="u1",
            user_name="阿强",
            message_id="m1",
            original_text="今晚11点前早睡",
            summary="11点前早睡",
            category="sleep_vow",
            created_at=base_dt.timestamp(),
            deadline_ts=None,
            sleep_cutoff_ts=self._dt(day=25, hour=23, minute=15).timestamp(),
            sleep_window_end_ts=self._dt(day=26, hour=5, minute=30).timestamp(),
            bust_keywords=(),
            status="active",
            busted_at=None,
            bust_text="",
            bust_message_id="",
            confidence=0.88,
        )
        ev_normal = evaluate_outrageous_bust(normal_sleep, "刷会儿手机", self._dt(day=25, hour=23, minute=25).timestamp())
        self.assertFalse(ev_normal.is_outrageous)
        self.assertLess(ev_normal.score, 4)

        extreme_gacha = FlagRecord(
            id=2,
            group_id="1001",
            session_id="s1",
            user_id="u2",
            user_name="小林",
            message_id="m2",
            original_text="下个池子绝不抽卡，再氪648我倒立吃键盘",
            summary="绝不抽卡再氪倒立吃键盘",
            category="spend_gacha_vow",
            created_at=base_dt.timestamp(),
            deadline_ts=None,
            sleep_cutoff_ts=None,
            sleep_window_end_ts=None,
            bust_keywords=("648", "真香"),
            status="active",
            busted_at=None,
            bust_text="",
            bust_message_id="",
            confidence=0.95,
        )
        ev_extreme = evaluate_outrageous_bust(
            extreme_gacha,
            "草真香了刚充完648吃大保底",
            base_dt.timestamp() + 1800,
            llm_outrageous=True,
        )
        self.assertTrue(ev_extreme.is_outrageous)
        self.assertGreaterEqual(ev_extreme.score, 7)


if __name__ == "__main__":
    unittest.main()
