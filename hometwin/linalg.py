"""Minimal dense linear algebra so the fusion core has zero binary deps.

Matrices are lists of row lists of floats. Sizes here are tiny (<= 6x6),
so clarity beats vectorization.
"""

from __future__ import annotations

import math

Mat = list[list[float]]
Vec = list[float]


def zeros(r: int, c: int) -> Mat:
    return [[0.0] * c for _ in range(r)]


def eye(n: int, s: float = 1.0) -> Mat:
    m = zeros(n, n)
    for i in range(n):
        m[i][i] = s
    return m


def mat_mul(a: Mat, b: Mat) -> Mat:
    rows, inner, cols = len(a), len(b), len(b[0])
    out = zeros(rows, cols)
    for i in range(rows):
        ai = a[i]
        for k in range(inner):
            aik = ai[k]
            if aik == 0.0:
                continue
            bk = b[k]
            oi = out[i]
            for j in range(cols):
                oi[j] += aik * bk[j]
    return out


def mat_vec(a: Mat, v: Vec) -> Vec:
    return [sum(ai[j] * v[j] for j in range(len(v))) for ai in a]


def transpose(a: Mat) -> Mat:
    return [list(col) for col in zip(*a)]


def mat_add(a: Mat, b: Mat) -> Mat:
    return [[x + y for x, y in zip(ra, rb)] for ra, rb in zip(a, b)]


def mat_sub(a: Mat, b: Mat) -> Mat:
    return [[x - y for x, y in zip(ra, rb)] for ra, rb in zip(a, b)]


def mat_scale(a: Mat, s: float) -> Mat:
    return [[x * s for x in row] for row in a]


def inverse(a: Mat) -> Mat:
    """Gauss-Jordan with partial pivoting. Raises ValueError on singular input."""
    n = len(a)
    aug = [row[:] + ident_row for row, ident_row in zip(a, eye(n))]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        p = aug[col][col]
        aug[col] = [x / p for x in aug[col]]
        for r in range(n):
            if r == col:
                continue
            f = aug[r][col]
            if f != 0.0:
                aug[r] = [x - f * y for x, y in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def vec_sub(a: Vec, b: Vec) -> Vec:
    return [x - y for x, y in zip(a, b)]


def vec_add(a: Vec, b: Vec) -> Vec:
    return [x + y for x, y in zip(a, b)]


def norm(v: Vec) -> float:
    return math.sqrt(sum(x * x for x in v))


def dist(a: Vec, b: Vec) -> float:
    return norm(vec_sub(a, b))
