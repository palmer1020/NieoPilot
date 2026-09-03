import unittest

from core.dar_route_runner import DarRouteRunner


class NieFamilyRoundLimitScopeTests(unittest.TestCase):
    def test_pure_nieo_1459_never_uses_round_limit_escape(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._check_fear_probe_pure_red = lambda _foreground: self.fail(
            "尼奥模式不应读取野外第7回合害怕探针"
        )

        action = runner._nie_family_round_limit_action(
            7,
            False,
            allow_escape=False,
        )

        self.assertIsNone(action)

    def test_wild_mode_keeps_round_limit_escape(self) -> None:
        runner = DarRouteRunner.__new__(DarRouteRunner)
        runner._emit = lambda *_args, **_kwargs: None
        runner._check_fear_probe_pure_red = lambda _foreground: False

        action = runner._nie_family_round_limit_action(
            7,
            False,
            allow_escape=True,
        )

        self.assertEqual(action, "escape")


if __name__ == "__main__":
    unittest.main()
