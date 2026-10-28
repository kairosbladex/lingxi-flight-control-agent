"""到位回读：新的位置样本对上落点才算到，平台回执状态不算。"""
import unittest

from lingxi_flight_control.arrival import arrival_observed
from lingxi_flight_control.geometry import offset_target, relative_target


def pose(**changes):
    base = {"longitude": 113.0, "latitude": 23.0, "altitude_m": 50.0, "height_m": 30.0,
            "heading_deg": 90.0, "yaw_deg": 90.0, "revision": 1}
    base.update(changes)
    return base


class ArrivalObservationTests(unittest.TestCase):
    def test_horizontal_move_requires_a_new_sample_at_the_expected_point(self):
        before = pose()
        longitude, latitude = relative_target(113.0, 23.0, 90.0, "forward", 10)
        after = pose(longitude=longitude, latitude=latitude, revision=2)
        self.assertTrue(arrival_observed("move_forward", {"distance_m": 10}, before, after))
        self.assertFalse(arrival_observed("move_forward", {"distance_m": 10}, before, pose()))
        stale = pose(longitude=longitude, latitude=latitude, revision=1)
        self.assertFalse(arrival_observed("move_forward", {"distance_m": 10}, before, stale))

    def test_vertical_move_checks_altitude_and_relative_height(self):
        before = pose()
        after = pose(altitude_m=60.0, height_m=40.0, revision=2)
        self.assertTrue(arrival_observed("move_up", {"distance_m": 10}, before, after))
        self.assertFalse(arrival_observed("move_up", {"distance_m": 10}, before,
                                          pose(altitude_m=60.0, revision=2)))

    def test_point_flight_matches_the_target_not_the_receipt(self):
        after = pose(longitude=113.1, latitude=23.2, altitude_m=80.0, revision=2)
        parameters = {"longitude": 113.1, "latitude": 23.2, "altitude_m": 80.0}
        self.assertTrue(arrival_observed("point_flight", parameters, pose(), after))
        self.assertFalse(arrival_observed("point_flight", parameters, pose(),
                                          pose(revision=2)))

    def test_orbit_matches_the_radius_on_a_new_sample(self):
        longitude, latitude = offset_target(114.0, 24.0, 0.0, 30.0)
        parameters = {"longitude": 114.0, "latitude": 24.0, "radius_m": 30}
        after = pose(longitude=longitude, latitude=latitude, revision=2)
        self.assertTrue(arrival_observed("point_orbit", parameters, pose(), after))
        self.assertFalse(arrival_observed("point_orbit", parameters, pose(),
                                          pose(longitude=114.0, latitude=24.0, revision=2)))

    def test_payload_does_not_need_a_position_sample(self):
        self.assertTrue(arrival_observed("camera_photo", {}, pose(), pose()))
