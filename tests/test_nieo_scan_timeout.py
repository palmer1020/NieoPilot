import unittest

from core.dar_route_runner import DarRouteRunner


class NieoScanTimeoutTests(unittest.TestCase):
    def test_builtin_nieo_waits_fifteen_seconds_before_reconnect(self):
        self.assertEqual(
            DarRouteRunner.NIEO_SCAN_NO_CHANGE_RECONNECT_SEC,
            15.0,
        )

    def test_pure_energy_scan_timeout_is_unchanged(self):
        self.assertEqual(
            DarRouteRunner.PURE_ENERGY_SCAN_NO_CHANGE_RECONNECT_SEC,
            10.0,
        )


if __name__ == "__main__":
    unittest.main()
