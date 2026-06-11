import math
import random

import pytest

from hometwin.fusion.kalman import KalmanFilter3D


def test_position_updates_converge():
    rng = random.Random(42)
    true_pos = (2.0, 3.0, 0.9)
    kf = KalmanFilter3D((0.0, 0.0, 0.0), sigma0_m=5.0)
    for _ in range(30):
        kf.predict(0.5)
        z = tuple(p + rng.gauss(0, 0.2) for p in true_pos)
        kf.update_position(z, sigma_m=0.2)
    assert math.dist(kf.position, true_pos) < 0.15
    assert kf.position_sigma < 0.2


def test_range_updates_trilaterate():
    rng = random.Random(7)
    true_pos = (3.0, 2.0, 1.0)
    anchors = [(0.0, 0.0, 2.2), (8.0, 0.0, 2.2), (0.0, 4.0, 2.2), (8.0, 4.0, 2.2)]
    kf = KalmanFilter3D((4.0, 2.0, 1.0), sigma0_m=4.0, process_accel=0.05)
    for _ in range(60):
        kf.predict(0.5)
        for a in anchors:
            r = math.dist(true_pos, a) + rng.gauss(0, 0.3)
            kf.update_range(a, r, sigma_m=0.5)
    assert math.dist(kf.position, true_pos) < 0.6


def test_uncertainty_grows_without_measurements():
    kf = KalmanFilter3D((1.0, 1.0, 1.0))
    kf.update_position((1.0, 1.0, 1.0), sigma_m=0.1)
    s0 = kf.position_sigma
    for _ in range(20):
        kf.predict(1.0)
    assert kf.position_sigma > s0


def test_mahalanobis_gates_far_measurements():
    kf = KalmanFilter3D((0.0, 0.0, 0.0), sigma0_m=0.5)
    kf.update_position((0.0, 0.0, 0.0), sigma_m=0.1)
    near = kf.mahalanobis_sq((0.1, 0.0, 0.0), 0.3)
    far = kf.mahalanobis_sq((5.0, 5.0, 0.0), 0.3)
    assert near < 1.0
    assert far > 100.0


def test_range_update_at_anchor_is_noop():
    kf = KalmanFilter3D((1.0, 1.0, 1.0))
    before = kf.position
    kf.update_range((1.0, 1.0, 1.0), 3.0, 0.5)
    assert kf.position == pytest.approx(before)
