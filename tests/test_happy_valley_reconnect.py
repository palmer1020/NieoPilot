import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.daily_runner import DailyRunner


class HappyValleyReconnectTests(unittest.TestCase):
    @staticmethod
    def _runner(dar_route_runner):
        bot = SimpleNamespace(
            dar_route_runner=dar_route_runner,
            regions={},
            user_stop_requested=False,
            stop_current=False,
            _stop_event=threading.Event(),
            emit_and_log=lambda *_args: None,
        )
        return DailyRunner(bot)

    def test_collection_entry_failure_reconnects_locally_without_restarting_preamble(self):
        runner = self._runner(SimpleNamespace())
        stop_event = threading.Event()

        with (
            patch.object(
                runner,
                "_happy_valley_enter_map_once",
                return_value=False,
            ) as enter_once,
            patch.object(
                runner,
                "_happy_valley_refresh_reenter_phase",
                side_effect=[False, True],
            ) as reconnect,
            patch.object(
                runner,
                "_run_happy_valley_phases_with_reconnect",
                return_value=True,
            ) as phases,
        ):
            result = runner._collection_daily_run_happy_valley_without_pre(
                False,
                stop_event,
                log_tag="test",
            )

        self.assertTrue(result)
        enter_once.assert_called_once()
        self.assertEqual(reconnect.call_count, 2)
        self.assertEqual(reconnect.call_args_list[0].args[0], "water")
        phases.assert_called_once()

    def test_refresh_reentry_restores_companion_for_each_phase(self):
        drr = SimpleNamespace(
            run_refresh_login_until_map=Mock(return_value=True),
            _pre_daily_follow_pet_one_after_daily_six_pets=Mock(return_value=True),
            set_follow_color_from_closed_bag=Mock(return_value=True),
        )
        runner = self._runner(drr)

        with patch.object(runner, "_happy_valley_enter_map_once", return_value=True):
            for phase in ("water", "fire", "grass"):
                self.assertTrue(
                    runner._happy_valley_refresh_reenter_phase(
                        phase,
                        False,
                        threading.Event(),
                        log_tag=f"test-{phase}",
                    )
                )

        self.assertEqual(
            drr._pre_daily_follow_pet_one_after_daily_six_pets.call_count,
            1,
        )
        self.assertEqual(
            [item.args[0] for item in drr.set_follow_color_from_closed_bag.call_args_list],
            ["purple", "cyan"],
        )

    def test_collection_daily_after_happy_valley_continues_through_daily_chain(self):
        drr = SimpleNamespace(
            run_pre_daily_ocean_energy_handoff=Mock(return_value=True),
        )
        runner = self._runner(drr)

        with (
            patch.object(
                runner,
                "_collection_daily_follow_first_orange_1_to_6",
                return_value=True,
            ) as follow_orange,
            patch.object(
                runner,
                "_collection_daily_run_to_daily_1_wait_map1",
                return_value=True,
            ) as to_daily_1,
            patch.object(
                runner,
                "run_new_daily_chain_1_to_9",
                return_value=True,
            ) as daily_chain,
        ):
            result = runner.run_collection_daily_after_happy_valley(
                False,
                log_tag="test-tail",
            )

        self.assertTrue(result)
        follow_orange.assert_called_once()
        to_daily_1.assert_called_once()
        drr.run_pre_daily_ocean_energy_handoff.assert_called_once()
        daily_chain.assert_called_once_with(
            False,
            skip_hero_tower=False,
            from_daily_chain=True,
            start_variant="1",
            start_step=1,
        )


if __name__ == "__main__":
    unittest.main()
