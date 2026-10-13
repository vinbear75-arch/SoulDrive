import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.rag_engine import build_learning_context
from core.temporal_memory import resolve_memory_time_scope


TIMEZONE = ZoneInfo("Asia/Shanghai")


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class TemporalMemoryTests(unittest.TestCase):
    def test_previous_month_resolves_to_previous_calendar_month_end(self):
        scope = resolve_memory_time_scope(
            "请回顾上个月的实验结论",
            reference_timestamp_ms=_timestamp_ms(datetime(2026, 9, 15, 12, tzinfo=TIMEZONE)),
        )

        resolved = datetime.fromtimestamp(scope["as_of"], tz=TIMEZONE)
        self.assertTrue(scope["applied"])
        self.assertEqual(scope["expression"], "上个月")
        self.assertEqual(scope["resolution"], "previous_calendar_month_end")
        self.assertEqual(resolved.year, 2026)
        self.assertEqual(resolved.month, 8)
        self.assertEqual(resolved.day, 31)
        self.assertEqual(resolved.hour, 23)
        self.assertEqual(resolved.minute, 59)

        prompt_context = build_learning_context({"memory_time_scope": scope})
        self.assertIn("不要用当前事实替代该历史时点", prompt_context)

    def test_explicit_date_resolves_to_end_of_local_day(self):
        scope = resolve_memory_time_scope("截至 2026年8月12日 的记忆", reference_timestamp_ms=0)

        resolved = datetime.fromtimestamp(scope["as_of"], tz=TIMEZONE)
        self.assertTrue(scope["applied"])
        self.assertEqual(scope["expression"], "2026年8月12日")
        self.assertEqual(scope["resolution"], "explicit_date_end")
        self.assertEqual((resolved.year, resolved.month, resolved.day), (2026, 8, 12))
        self.assertEqual((resolved.hour, resolved.minute, resolved.second), (23, 59, 59))

    def test_multiple_explicit_dates_are_left_for_explicit_comparison_context(self):
        scope = resolve_memory_time_scope(
            "比较 2026-08-01 和 2026-09-01 的记忆",
            reference_timestamp_ms=_timestamp_ms(datetime(2026, 9, 15, tzinfo=TIMEZONE)),
        )

        self.assertFalse(scope["applied"])
        self.assertIsNone(scope["as_of"])
        self.assertEqual(scope["resolution"], "ambiguous_multiple_dates")

    def test_yesterday_resolves_to_previous_local_calendar_day_end(self):
        scope = resolve_memory_time_scope(
            "请回顾昨天的实验结论",
            reference_timestamp_ms=_timestamp_ms(datetime(2026, 9, 15, 12, tzinfo=TIMEZONE)),
        )

        resolved = datetime.fromtimestamp(scope["as_of"], tz=TIMEZONE)
        self.assertTrue(scope["applied"])
        self.assertEqual(scope["expression"], "昨天")
        self.assertEqual(scope["resolution"], "previous_calendar_day_end")
        self.assertEqual((resolved.year, resolved.month, resolved.day), (2026, 9, 14))
        self.assertEqual((resolved.hour, resolved.minute), (23, 59))

    def test_multiple_relative_time_expressions_are_left_unresolved(self):
        scope = resolve_memory_time_scope(
            "比较昨天和上个月的记忆",
            reference_timestamp_ms=_timestamp_ms(datetime(2026, 9, 15, 12, tzinfo=TIMEZONE)),
        )

        self.assertFalse(scope["applied"])
        self.assertIsNone(scope["as_of"])
        self.assertEqual(scope["resolution"], "ambiguous_multiple_dates")


if __name__ == "__main__":
    unittest.main()
