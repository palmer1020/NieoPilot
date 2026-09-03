import unittest

from core.daily_runner import DailyRunner


class CollectionDailySkipExpTests(unittest.TestCase):
    def test_exp_day_skip_only_skips_input_and_keeps_putback(self):
        for signin_count in (3, 6, 27, 28):
            with self.subTest(signin_count=signin_count):
                run_exp, putback = DailyRunner._collection_daily_exp_plan(
                    signin_count,
                    skip_exp_input=True,
                )
                self.assertFalse(run_exp)
                self.assertTrue(putback)

    def test_exp_day_without_skip_runs_input_and_putback(self):
        run_exp, putback = DailyRunner._collection_daily_exp_plan(
            3,
            skip_exp_input=False,
        )

        self.assertTrue(run_exp)
        self.assertTrue(putback)

    def test_non_exp_day_does_neither(self):
        for skip_exp_input in (False, True):
            with self.subTest(skip_exp_input=skip_exp_input):
                run_exp, putback = DailyRunner._collection_daily_exp_plan(
                    2,
                    skip_exp_input=skip_exp_input,
                )
                self.assertFalse(run_exp)
                self.assertFalse(putback)


if __name__ == "__main__":
    unittest.main()
