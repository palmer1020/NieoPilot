import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.dar_route_runner import DarRouteRunner


class PetSelectionTimeoutTests(unittest.TestCase):
    @staticmethod
    def _runner():
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner.bot = SimpleNamespace(stop_current=False)
        runner._emit = lambda *_args, **_kwargs: None
        return runner

    def test_generic_selection_keeps_retrying_for_at_least_three_seconds(self):
        runner = self._runner()
        clock = [0.0]
        checks = []
        runner._click_region = lambda *_args, **_kwargs: None
        runner._sleep_abortable = (
            lambda _event, seconds, **_kwargs: clock.__setitem__(
                0, clock[0] + float(seconds)
            )
        )
        runner._check_selected_pet_color = (
            lambda *_args, **_kwargs: checks.append(clock[0]) or 0
        )

        with patch(
            "core.dar_route_runner.time.time",
            side_effect=lambda: clock[0],
        ):
            ok = runner._click_pet_with_selection_check(
                "六",
                False,
                threading.Event(),
            )

        self.assertFalse(ok)
        self.assertGreaterEqual(clock[0], 3.0)
        self.assertGreaterEqual(checks[-1], 3.0)
        self.assertGreater(len(checks), 5)

    def test_generic_selection_still_returns_immediately_after_success(self):
        runner = self._runner()
        clock = [0.0]
        results = iter([0, 1])
        runner._click_region = lambda *_args, **_kwargs: None
        runner._sleep_abortable = (
            lambda _event, seconds, **_kwargs: clock.__setitem__(
                0, clock[0] + float(seconds)
            )
        )
        runner._check_selected_pet_color = (
            lambda *_args, **_kwargs: next(results)
        )

        with patch(
            "core.dar_route_runner.time.time",
            side_effect=lambda: clock[0],
        ):
            ok = runner._click_pet_with_selection_check(
                "六",
                False,
                threading.Event(),
            )

        self.assertTrue(ok)
        self.assertLess(clock[0], 3.0)


if __name__ == "__main__":
    unittest.main()
