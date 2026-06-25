"""Unit tests for the walk-forward splitter and out-of-fold prediction.

The leakage-safety of every model result in the project rests on two structural
guarantees: train rows strictly precede test rows in every fold, and the initial
training block is never scored. These tests pin both.
"""

import numpy as np

from ml.alpha.model import logistic_pipeline, walk_forward_oof
from ml.alpha.walkforward import walk_forward_splits


class TestWalkForwardSplits:
    def test_train_strictly_before_test(self) -> None:
        for train_idx, test_idx in walk_forward_splits(1000, n_splits=8):
            assert train_idx.max() < test_idx.min()

    def test_test_blocks_tile_the_post_warmup_rows(self) -> None:
        n, frac = 997, 0.4  # deliberately not divisible
        covered: list[np.ndarray] = []
        for _, test_idx in walk_forward_splits(n, n_splits=8, min_train_frac=frac):
            covered.append(test_idx)
        all_test = np.concatenate(covered)
        # Contiguous, non-overlapping, and exactly the rows after the warmup block.
        assert len(all_test) == len(set(all_test.tolist()))
        np.testing.assert_array_equal(np.sort(all_test), np.arange(int(n * frac), n))

    def test_window_expands(self) -> None:
        sizes = [len(tr) for tr, _ in walk_forward_splits(500, n_splits=5)]
        assert sizes == sorted(sizes)
        assert sizes[0] >= int(500 * 0.4)

    def test_embargo_gap(self) -> None:
        for train_idx, test_idx in walk_forward_splits(300, n_splits=3, embargo=10):
            assert test_idx.min() - train_idx.max() > 10


class TestWalkForwardOof:
    def test_warmup_block_is_nan_rest_is_scored(self) -> None:
        rng = np.random.default_rng(0)
        x = rng.normal(size=(400, 3))
        y = rng.integers(0, 2, 400).astype(np.intp)
        oof = walk_forward_oof(x, y, build_estimator=logistic_pipeline, n_splits=4)
        warmup = int(400 * 0.4)
        assert np.isnan(oof[:warmup]).all()
        assert not np.isnan(oof[warmup:]).any()
        assert ((oof[warmup:] >= 0) & (oof[warmup:] <= 1)).all()

    def test_cannot_see_the_future(self) -> None:
        """Plant signal ONLY in the second half's feature-target relation; the
        early folds (trained on the unrelated first half) must stay near 0.5 on
        their test rows, proving each fold ignores later rows entirely."""
        rng = np.random.default_rng(1)
        n = 600
        x = rng.normal(size=(n, 1))
        y = rng.integers(0, 2, n).astype(np.intp)
        # Second half: x perfectly encodes y.
        x[n // 2 :, 0] = y[n // 2 :] * 2.0 - 1.0
        oof = walk_forward_oof(x, y, build_estimator=logistic_pipeline, n_splits=6)
        warmup = int(n * 0.4)
        first_scored_block = oof[warmup : n // 2]
        # Trained only on noise -> predictions hug the base rate, not the answer.
        assert np.nanstd(first_scored_block) < 0.15
