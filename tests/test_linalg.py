import math

import pytest

from apartment_tracker import linalg as la


def test_mat_mul_identity():
    a = [[1.0, 2.0], [3.0, 4.0]]
    assert la.mat_mul(a, la.eye(2)) == a


def test_inverse_roundtrip():
    a = [[4.0, 7.0, 2.0], [3.0, 6.0, 1.0], [2.0, 5.0, 3.0]]
    prod = la.mat_mul(a, la.inverse(a))
    for i in range(3):
        for j in range(3):
            assert prod[i][j] == pytest.approx(1.0 if i == j else 0.0, abs=1e-9)


def test_inverse_singular_raises():
    with pytest.raises(ValueError):
        la.inverse([[1.0, 2.0], [2.0, 4.0]])


def test_norm_dist():
    assert la.norm([3.0, 4.0]) == pytest.approx(5.0)
    assert la.dist([1.0, 1.0, 1.0], [1.0, 1.0, 2.0]) == pytest.approx(1.0)
    assert math.isclose(la.dist([0, 0, 0], [1, 2, 2]), 3.0)
