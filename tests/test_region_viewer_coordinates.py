import unittest
from unittest.mock import patch

from config import GAME_LOGIC_H, GAME_LOGIC_W
from tools.region_viewer import (
    parse_logic_coordinate_inputs,
    point_probe_region,
    runtime_scan_rgb_for_region,
)


class RegionViewerCoordinateTests(unittest.TestCase):
    def test_valid_coordinate(self) -> None:
        point, error = parse_logic_coordinate_inputs("268", "510")

        self.assertEqual(point, (268, 510))
        self.assertEqual(error, "")

    def test_boundary_coordinate(self) -> None:
        point, error = parse_logic_coordinate_inputs(
            str(GAME_LOGIC_W),
            str(GAME_LOGIC_H),
        )

        self.assertEqual(point, (GAME_LOGIC_W, GAME_LOGIC_H))
        self.assertEqual(error, "")

    def test_incomplete_or_non_integer_coordinate_is_rejected(self) -> None:
        self.assertIsNone(parse_logic_coordinate_inputs("", "510")[0])
        self.assertIsNone(parse_logic_coordinate_inputs("268.5", "510")[0])

    def test_out_of_range_coordinate_is_rejected(self) -> None:
        self.assertIsNone(parse_logic_coordinate_inputs("-1", "510")[0])
        self.assertIsNone(
            parse_logic_coordinate_inputs(str(GAME_LOGIC_W + 1), "510")[0]
        )
        self.assertIsNone(
            parse_logic_coordinate_inputs("268", str(GAME_LOGIC_H + 1))[0]
        )

    def test_point_probe_matches_saved_single_point_region(self) -> None:
        region = point_probe_region(765, 505)

        self.assertEqual(
            region.points,
            [(764, 504), (766, 504), (766, 506), (764, 506)],
        )

    def test_runtime_rgb_delegates_to_shared_scanner(self) -> None:
        region = point_probe_region(765, 505, key="测试.探针")

        with patch(
            "tools.region_viewer.mean_rgb_for_region_key",
            return_value=(192, 165, 165),
        ) as scanner:
            result = runtime_scan_rgb_for_region(region)

        self.assertEqual(result, (192, 165, 165))
        lookup, key = scanner.call_args.args
        self.assertIs(lookup[key], region)


if __name__ == "__main__":
    unittest.main()
