"""3D constant-velocity Kalman filter with EKF range updates.

State: [x, y, z, vx, vy, vz]. Household items mostly sit still, so process
noise is small; a carried item is handled by the velocity states plus
measurement-driven correction.
"""

from __future__ import annotations

import math

from apartment_tracker import linalg as la


class KalmanFilter3D:
    def __init__(
        self,
        position: tuple[float, float, float],
        sigma0_m: float = 2.0,
        process_accel: float = 0.5,  # m/s^2 white-noise accel driving the CV model
    ):
        self.x: la.Vec = [*position, 0.0, 0.0, 0.0]
        self.P: la.Mat = la.eye(6)
        for i in range(3):
            self.P[i][i] = sigma0_m**2
        for i in range(3, 6):
            self.P[i][i] = 1.0
        self.q = process_accel

    @property
    def position(self) -> tuple[float, float, float]:
        return (self.x[0], self.x[1], self.x[2])

    @property
    def position_sigma(self) -> float:
        return math.sqrt(max(self.P[0][0] + self.P[1][1] + self.P[2][2], 0.0) / 3.0)

    def predict(self, dt: float) -> None:
        if dt <= 0:
            return
        F = la.eye(6)
        for i in range(3):
            F[i][i + 3] = dt
        # discrete white-noise acceleration Q
        q2 = self.q**2
        dt2, dt3, dt4 = dt * dt, dt**3, dt**4
        Q = la.zeros(6, 6)
        for i in range(3):
            Q[i][i] = q2 * dt4 / 4.0
            Q[i][i + 3] = Q[i + 3][i] = q2 * dt3 / 2.0
            Q[i + 3][i + 3] = q2 * dt2
        self.x = la.mat_vec(F, self.x)
        self.P = la.mat_add(la.mat_mul(la.mat_mul(F, self.P), la.transpose(F)), Q)

    def _update(self, H: la.Mat, residual: la.Vec, R: la.Mat) -> None:
        Ht = la.transpose(H)
        S = la.mat_add(la.mat_mul(la.mat_mul(H, self.P), Ht), R)
        K = la.mat_mul(la.mat_mul(self.P, Ht), la.inverse(S))
        self.x = la.vec_add(self.x, la.mat_vec(K, residual))
        ikh = la.mat_sub(la.eye(6), la.mat_mul(K, H))
        self.P = la.mat_mul(ikh, self.P)

    def update_position(self, z: tuple[float, float, float], sigma_m: float) -> None:
        H = la.zeros(3, 6)
        for i in range(3):
            H[i][i] = 1.0
        residual = la.vec_sub(list(z), list(self.position))
        self._update(H, residual, la.eye(3, sigma_m**2))

    def update_range(
        self, anchor: tuple[float, float, float], range_m: float, sigma_m: float
    ) -> None:
        """EKF update on h(x) = || p - anchor ||."""
        d = la.vec_sub(list(self.position), list(anchor))
        pred = la.norm(d)
        if pred < 1e-6:
            return  # gradient undefined at the anchor itself
        H = la.zeros(1, 6)
        for i in range(3):
            H[0][i] = d[i] / pred
        self._update(H, [range_m - pred], [[sigma_m**2]])

    def mahalanobis_sq(self, z: tuple[float, float, float], sigma_m: float) -> float:
        """Squared gating distance of a position measurement against this track."""
        S = [[self.P[i][j] for j in range(3)] for i in range(3)]
        for i in range(3):
            S[i][i] += sigma_m**2
        r = la.vec_sub(list(z), list(self.position))
        return sum(ri * vi for ri, vi in zip(r, la.mat_vec(la.inverse(S), r)))
