"""Weighted least-squares multilateration: many ranges -> one fix.

The fusion engine can absorb ranges one at a time, but a single EKF scalar
update per anchor throws away the joint geometry: the filter never sees that
three circles intersect at one point, and it cannot tell a tight intersection
from a grazing one. Solving all ranges together recovers both the position and
an honest covariance, which is what makes 3+ BLE anchors beat 1.

Practical details that matter more than the algebra:
- Coplanar anchors (all on the ceiling, all on the desk) leave z unobservable.
  A z-prior pseudo-measurement regularizes the normal equations instead of
  letting the solve blow up or mirror through the anchor plane.
- One NLOS anchor (body, fridge, wall) biases every range it reports. With a
  spare anchor we drop the worst outlier and re-solve.
- Geometry quality is reported as GDOP so callers can refuse a fix taken from
  nearly-collinear anchors rather than publish a confident wrong answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hometwin import linalg as la

MAX_ITERS = 12
CONVERGE_M = 1e-4
LM_DAMPING = 1e-6
OUTLIER_SIGMAS = 3.0
OUTLIER_CHI2_RATIO = 0.35  # excluding a liar must collapse the fit, not nudge it
MAX_GDOP = 12.0


@dataclass
class Fix:
    position: tuple[float, float, float]
    sigma_m: float
    gdop: float
    residual_rms_m: float
    used: list[int] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)

    @property
    def anchors_used(self) -> int:
        return len(self.used)


def _solve_once(
    anchors: list[tuple[float, float, float]],
    ranges: list[float],
    sigmas: list[float],
    seed: tuple[float, float, float],
    z_prior: float | None,
    z_sigma: float,
) -> tuple[tuple[float, float, float], la.Mat, float] | None:
    """Gauss-Newton with LM damping. Returns (position, covariance, chi2)."""
    x = [float(seed[0]), float(seed[1]), float(seed[2])]
    for _ in range(MAX_ITERS):
        jtwj = la.zeros(3, 3)
        jtwf = [0.0, 0.0, 0.0]
        chi2 = 0.0
        for a, r, s in zip(anchors, ranges, sigmas):
            dx, dy, dz = x[0] - a[0], x[1] - a[1], x[2] - a[2]
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            if d < 1e-6:  # sitting on the anchor: gradient undefined, nudge off
                d, dx = 1e-6, 1e-6
            w = 1.0 / max(s, 1e-3) ** 2
            jac = (dx / d, dy / d, dz / d)
            resid = d - r
            chi2 += w * resid * resid
            for i in range(3):
                jtwf[i] += w * jac[i] * resid
                for j in range(3):
                    jtwj[i][j] += w * jac[i] * jac[j]
        if z_prior is not None:
            w = 1.0 / max(z_sigma, 1e-3) ** 2
            resid = x[2] - z_prior
            chi2 += w * resid * resid
            jtwf[2] += w * resid
            jtwj[2][2] += w

        damped = [[jtwj[i][j] for j in range(3)] for i in range(3)]
        for i in range(3):
            damped[i][i] += LM_DAMPING * max(damped[i][i], 1.0)
        try:
            step = la.mat_vec(la.inverse(damped), jtwf)
        except (ZeroDivisionError, ValueError):
            return None
        if any(math.isnan(s) or math.isinf(s) for s in step):
            return None
        for i in range(3):
            x[i] -= step[i]
        if la.norm(step) < CONVERGE_M:
            break

    try:
        cov = la.inverse(damped)
    except (ZeroDivisionError, ValueError):
        return None
    if any(math.isnan(cov[i][i]) or cov[i][i] < 0 for i in range(3)):
        return None
    return (x[0], x[1], x[2]), cov, chi2


def _gdop(
    anchors: list[tuple[float, float, float]],
    x: tuple[float, float, float],
    z_prior: float | None,
) -> float:
    """Unit-weight dilution of precision — geometry quality, independent of
    how good the individual ranges are."""
    jtj = la.zeros(3, 3)
    for a in anchors:
        dx, dy, dz = x[0] - a[0], x[1] - a[1], x[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz) or 1e-6
        jac = (dx / d, dy / d, dz / d)
        for i in range(3):
            for j in range(3):
                jtj[i][j] += jac[i] * jac[j]
    if z_prior is not None:
        jtj[2][2] += 1.0
    for i in range(3):
        jtj[i][i] += LM_DAMPING
    try:
        cov = la.inverse(jtj)
    except (ZeroDivisionError, ValueError):
        return float("inf")
    tr = sum(cov[i][i] for i in range(3))
    return math.sqrt(tr) if tr > 0 else float("inf")


def multilaterate(
    anchors: list[tuple[float, float, float]],
    ranges: list[float],
    sigmas: list[float] | None = None,
    seed: tuple[float, float, float] | None = None,
    z_prior: float | None = None,
    z_sigma: float = 0.6,
    reject_outliers: bool = True,
    max_gdop: float = MAX_GDOP,
) -> Fix | None:
    """Least-squares fix from >=2 ranges (>=3 without a z prior).

    Returns None when the geometry cannot support a fix — callers should fall
    back to feeding the ranges in individually rather than trust a bad solve.
    """
    n = len(anchors)
    if n != len(ranges) or n < 2:
        return None
    if sigmas is None:
        sigmas = [1.0] * n
    if n < 3 and z_prior is None:
        return None

    idx = list(range(n))
    dropped: list[int] = []
    # keep enough anchors after a drop to still overdetermine the solve
    min_after = 4 if z_prior is None else 3
    while True:
        a = [anchors[i] for i in idx]
        r = [ranges[i] for i in idx]
        s = [sigmas[i] for i in idx]
        start = seed or _centroid_seed(a, r, z_prior)
        out = _solve_once(a, r, s, start, z_prior, z_sigma)
        if out is None:
            return None
        pos, cov, chi2 = out
        dof = len(idx) + (1 if z_prior is not None else 0) - 3

        # Least squares hides a single NLOS anchor by spreading its error across
        # every residual, so thresholding residuals misses it. Leave-one-out
        # exposes it: excluding the liar collapses chi-square, excluding an
        # honest anchor barely moves it.
        if reject_outliers and len(idx) > min_after and dof > 0:
            base = chi2 / dof
            best_k, best_score, best_sol = None, base, None
            for k in range(len(idx)):
                sub = [j for j in range(len(idx)) if j != k]
                sa = [a[j] for j in sub]
                sr = [r[j] for j in sub]
                ss = [s[j] for j in sub]
                sub_out = _solve_once(
                    sa, sr, ss, _centroid_seed(sa, sr, z_prior), z_prior, z_sigma
                )
                if sub_out is None:
                    continue
                sub_dof = len(sub) + (1 if z_prior is not None else 0) - 3
                if sub_dof <= 0:
                    continue
                score = sub_out[2] / sub_dof
                if score < best_score:
                    best_k, best_score, best_sol = k, score, sub_out
            if (
                best_k is not None
                and best_score < OUTLIER_CHI2_RATIO * base
                and abs(la.dist(list(best_sol[0]), list(a[best_k])) - r[best_k])
                > OUTLIER_SIGMAS * max(s[best_k], 1e-3)
            ):
                dropped.append(idx.pop(best_k))
                continue

        resid = [la.dist(list(pos), list(ai)) - ri for ai, ri in zip(a, r)]
        scale = max(chi2 / dof, 1.0) if dof > 0 else 1.0
        var = sum(cov[i][i] for i in range(3)) * scale / 3.0
        gdop = _gdop(a, pos, z_prior)
        if gdop > max_gdop or math.isinf(gdop):
            return None
        rms = math.sqrt(sum(v * v for v in resid) / len(resid))
        return Fix(
            position=pos,
            sigma_m=math.sqrt(max(var, 1e-6)),
            gdop=gdop,
            residual_rms_m=rms,
            used=list(idx),
            dropped=dropped,
        )


def _centroid_seed(
    anchors: list[tuple[float, float, float]],
    ranges: list[float],
    z_prior: float | None,
) -> tuple[float, float, float]:
    """Range-weighted centroid: nearest anchor dominates, which is a far better
    starting point than the plain centroid when anchors are spread out."""
    wsum = 0.0
    acc = [0.0, 0.0, 0.0]
    for a, r in zip(anchors, ranges):
        w = 1.0 / max(r, 0.3) ** 2
        wsum += w
        for i in range(3):
            acc[i] += w * a[i]
    if wsum <= 0:
        return (0.0, 0.0, z_prior if z_prior is not None else 0.0)
    pos = [acc[i] / wsum for i in range(3)]
    if z_prior is not None:
        pos[2] = z_prior
    return (pos[0], pos[1], pos[2])
