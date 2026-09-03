import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.bot_thread import BotWorker


class NonblockingRuntimeLogTests(unittest.TestCase):
    def test_warning_is_emitted_to_dashboard_without_stdlib_logging(self) -> None:
        emitted = []
        worker = SimpleNamespace(
            _localize_log_text=lambda text: str(text),
            log_signal=SimpleNamespace(
                emit=lambda text, level: emitted.append((text, level))
            ),
        )

        # A stdlib warning used to reach logging.lastResort -> stderr ->
        # WriteConsoleW on the automation thread.  No logging method may be
        # touched by the runtime Dashboard path anymore.
        with patch.object(
            logging.Logger,
            "warning",
            side_effect=AssertionError("runtime log reached stdlib logging"),
        ):
            BotWorker.emit_and_log(worker, "[双塔] OCR组合不合理", "WARN")

        self.assertEqual(emitted, [("[双塔] OCR组合不合理", "WARN")])


if __name__ == "__main__":
    unittest.main()
