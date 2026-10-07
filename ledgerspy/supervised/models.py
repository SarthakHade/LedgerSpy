"""Supervised models in pure Python.

No scikit-learn / numpy is installed, and adding them needs a network install.

LogisticRegression: L2-regularised logistic regression with an unpenalised intercept, fitted by Newton's
method (IRLS) on standardised inputs. Deterministic; converges to the unique optimum of the convex objective
    Σ w_i·[y_i log p_i + (1 − y_i) log(1 − p_i)] − ½·l2·‖β‖².

GradientBoosting: a histogram gradient-boosted decision-tree classifier, the algorithm of scikit-learn's
HistGradientBoostingClassifier: logistic loss; inputs binned once on the training rows (≤ max_bins quantile
bins); trees grown depth-wise to max_depth with second-order (Newton) gain
    gain = G_L²/(H_L + λ) + G_R²/(H_R + λ) − G²/(H + λ),   leaf value = −G/(H + λ) · learning_rate,
each leaf holding ≥ min_samples_leaf rows; histograms of a node's larger child are its parent's minus its
smaller child's. No randomness: features and bins are scanned in order and ties keep the first split.

Both accept per-row weights (class weights for imbalance); neither ever sees validation or test rows.
"""

import math
from bisect import bisect_right
from operator import mul
from typing import Any

from ledgerspy.hashing import canonical_json, sha256_hex


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve a·x = b for a symmetric positive-definite a (Gaussian elimination with partial pivoting)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        for r in range(c + 1, n):
            f = m[r][c] / piv
            if f:
                row_r, row_c = m[r], m[c]
                for k in range(c, n + 1):
                    row_r[k] -= f * row_c[k]
    x = [0.0] * n
    for c in range(n - 1, -1, -1):
        x[c] = (m[c][n] - sum(m[c][k] * x[k] for k in range(c + 1, n))) / m[c][c]
    return x


class LogisticRegression:
    name = "logistic_regression"

    def __init__(self, l2: float, max_iter: int, tol: float) -> None:
        self.l2, self.max_iter, self.tol = l2, max_iter, tol
        self.coef: list[float] = []
        self.intercept = 0.0
        self.iterations = 0
        self.converged = False

    def fit(self, cols: list[list[float]], y: list[int], w: list[float]) -> "LogisticRegression":
        n, d = len(y), len(cols)
        xs = [[1.0] * n, *cols]  # intercept first
        beta = [0.0] * (d + 1)
        for it in range(1, self.max_iter + 1):
            eta = [beta[0]] * n
            for j in range(d):
                if beta[j + 1]:
                    bj = beta[j + 1]
                    eta = [e + bj * x for e, x in zip(eta, cols[j], strict=True)]
            p = [sigmoid(e) for e in eta]
            r = [wi * (yi - pi) for wi, yi, pi in zip(w, y, p, strict=True)]
            ww = [wi * pi * (1 - pi) for wi, pi in zip(w, p, strict=True)]
            grad = [sum(map(mul, r, xs[j])) - (self.l2 * beta[j] if j else 0.0) for j in range(d + 1)]
            wx = [list(map(mul, ww, xs[j])) for j in range(d + 1)]
            hess = [[0.0] * (d + 1) for _ in range(d + 1)]
            for j in range(d + 1):
                for k in range(j, d + 1):
                    v = sum(map(mul, wx[j], xs[k]))
                    hess[j][k] = hess[k][j] = v
                if j:
                    hess[j][j] += self.l2
            step = _solve(hess, grad)
            beta = [b + s for b, s in zip(beta, step, strict=True)]
            self.iterations = it
            if max(abs(s) for s in step) < self.tol:
                self.converged = True
                break
        self.intercept, self.coef = beta[0], beta[1:]
        return self

    def decision(self, cols: list[list[float]]) -> list[float]:
        n = len(cols[0]) if cols else 0
        out = [self.intercept] * n
        for c, b in zip(cols, self.coef, strict=True):
            if b:
                out = [o + b * x for o, x in zip(out, c, strict=True)]
        return out

    def score(self, cols: list[list[float]]) -> list[float]:
        return [sigmoid(v) for v in self.decision(cols)]

    def describe(self) -> dict[str, Any]:
        return {
            "iterations": self.iterations,
            "converged": self.converged,
            "intercept": round(self.intercept, 8),
        }

    def sha256(self) -> str:
        return sha256_hex(
            canonical_json({"intercept": repr(self.intercept), "coef": [repr(c) for c in self.coef]})
        )


class GradientBoosting:
    name = "gradient_boosting"

    def __init__(
        self,
        n_estimators: int,
        learning_rate: float,
        max_depth: int,
        max_bins: int,
        min_samples_leaf: int,
        l2: float,
    ) -> None:
        self.n_estimators, self.lr, self.max_depth = n_estimators, learning_rate, max_depth
        self.max_bins, self.min_leaf, self.l2 = max_bins, min_samples_leaf, l2
        self.cuts: list[list[float]] = []
        self.init = 0.0
        self.trees: list[
            list[tuple]
        ] = []  # node list: ("leaf", value) | ("split", feature, bin, left, right, gain)
        self.gain: list[float] = []

    def _bin_edges(self, col: list[float]) -> list[float]:
        vals = sorted(set(col))
        if len(vals) <= self.max_bins:
            return [(a + b) / 2 for a, b in zip(vals, vals[1:], strict=False)]
        s = sorted(col)
        edges = []
        for q in range(1, self.max_bins):
            v = s[min(len(s) - 1, (q * len(s)) // self.max_bins)]
            if not edges or v > edges[-1]:
                edges.append(v)
        return edges

    def fit(self, cols: list[list[float]], y: list[int], w: list[float]) -> "GradientBoosting":
        n, d = len(y), len(cols)
        self.cuts = [self._bin_edges(c) for c in cols]
        binned = [[bisect_right(cut, v) for v in c] for c, cut in zip(cols, self.cuts, strict=True)]
        nbins = [len(c) + 1 for c in self.cuts]
        wp = sum(wi * yi for wi, yi in zip(w, y, strict=True))
        p0 = min(max(wp / sum(w), 1e-6), 1 - 1e-6)
        self.init = math.log(p0 / (1 - p0))
        raw = [self.init] * n
        self.gain = [0.0] * d
        self.trees = []
        for _ in range(self.n_estimators):
            p = [sigmoid(v) for v in raw]
            g = [wi * (pi - yi) for wi, pi, yi in zip(w, p, y, strict=True)]
            h = [max(wi * pi * (1 - pi), 1e-12) for wi, pi in zip(w, p, strict=True)]
            nodes: list[tuple] = []
            leaf_of = [0] * n
            self._grow(nodes, list(range(n)), 0, binned, nbins, g, h, None, leaf_of)
            for i in range(n):
                raw[i] += nodes[leaf_of[i]][1]
            self.trees.append(nodes)
        return self

    def _hist(self, rows, binned, nbins, g, h):
        out = []
        for f, col in enumerate(binned):
            G = [0.0] * nbins[f]
            H = [0.0] * nbins[f]
            C = [0] * nbins[f]
            for i in rows:
                b = col[i]
                G[b] += g[i]
                H[b] += h[i]
                C[b] += 1
            out.append((G, H, C))
        return out

    def _grow(self, nodes, rows, depth, binned, nbins, g, h, hist, leaf_of) -> int:
        me = len(nodes)
        nodes.append(None)
        Gt = sum(g[i] for i in rows)
        Ht = sum(h[i] for i in rows)
        leaf = ("leaf", -Gt / (Ht + self.l2) * self.lr)
        if depth >= self.max_depth or len(rows) < 2 * self.min_leaf:
            nodes[me] = leaf
            for i in rows:
                leaf_of[i] = me
            return me
        if hist is None:
            hist = self._hist(rows, binned, nbins, g, h)
        parent = Gt * Gt / (Ht + self.l2)
        best = (0.0, -1, -1)
        for f, (G, H, C) in enumerate(hist):
            gl = hl = 0.0
            cl = 0
            for b in range(len(G) - 1):
                gl += G[b]
                hl += H[b]
                cl += C[b]
                if cl < self.min_leaf:
                    continue
                if len(rows) - cl < self.min_leaf:
                    break
                gr, hr = Gt - gl, Ht - hl
                gain = gl * gl / (hl + self.l2) + gr * gr / (hr + self.l2) - parent
                if gain > best[0] + 1e-12:
                    best = (gain, f, b)
        gain, f, b = best
        if f < 0:
            nodes[me] = leaf
            for i in rows:
                leaf_of[i] = me
            return me
        col = binned[f]
        left = [i for i in rows if col[i] <= b]
        right = [i for i in rows if col[i] > b]
        self.gain[f] += gain
        small, large = (left, right) if len(left) <= len(right) else (right, left)
        hs = self._hist(small, binned, nbins, g, h)
        hl_ = [
            (
                [a - c for a, c in zip(P[0], S[0], strict=True)],
                [a - c for a, c in zip(P[1], S[1], strict=True)],
                [a - c for a, c in zip(P[2], S[2], strict=True)],
            )
            for P, S in zip(hist, hs, strict=True)
        ]
        hists = {id(small): hs, id(large): hl_}
        li = self._grow(nodes, left, depth + 1, binned, nbins, g, h, hists[id(left)], leaf_of)
        ri = self._grow(nodes, right, depth + 1, binned, nbins, g, h, hists[id(right)], leaf_of)
        nodes[me] = ("split", f, b, li, ri, gain)
        return me

    def decision(self, cols: list[list[float]]) -> list[float]:
        n = len(cols[0]) if cols else 0
        binned = [[bisect_right(cut, v) for v in c] for c, cut in zip(cols, self.cuts, strict=True)]
        out = [self.init] * n
        for nodes in self.trees:
            for i in range(n):
                k = 0
                node = nodes[0]
                while node[0] == "split":
                    k = node[3] if binned[node[1]][i] <= node[2] else node[4]
                    node = nodes[k]
                out[i] += node[1]
        return out

    def score(self, cols: list[list[float]]) -> list[float]:
        return [sigmoid(v) for v in self.decision(cols)]

    def describe(self) -> dict[str, Any]:
        return {"trees": len(self.trees), "leaves": sum(1 for t in self.trees for nd in t if nd[0] == "leaf")}

    def sha256(self) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "init": repr(self.init),
                    "cuts": [[repr(c) for c in cut] for cut in self.cuts],
                    "trees": [
                        [[repr(x) if isinstance(x, float) else x for x in nd] for nd in t] for t in self.trees
                    ],
                }
            )
        )


def make(name: str, cfg) -> LogisticRegression | GradientBoosting:
    if name == "logistic_regression":
        c = cfg.logistic_regression
        return LogisticRegression(c.l2, c.max_iter, c.tol)
    c = cfg.gradient_boosting
    return GradientBoosting(
        c.n_estimators, c.learning_rate, c.max_depth, c.max_bins, c.min_samples_leaf, c.l2
    )
