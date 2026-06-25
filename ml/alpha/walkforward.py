"""Walk-forward (expanding-window) splitter for time-ordered rows.

Train on the past, test on the immediate future, step forward — never shuffle.
This is the only split that respects causality for a time series, and respecting
it is the whole reason the PIT feature store exists. A shuffled split would let
the model peek at the future and report a fantasy score. Folds are integer
positions over a frame already sorted chronologically (load_training_frame does
that), so any model can iterate them the same way.
"""

from collections.abc import Iterator

import numpy as np
import numpy.typing as npt


def walk_forward_splits(
    n_rows: int,
    n_splits: int = 8,
    min_train_frac: float = 0.4,
    embargo: int = 0,
) -> Iterator[tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]]]:
    """Yield (train_idx, test_idx) expanding-window folds.

    The first `min_train_frac` of the rows is the initial training block; the rest
    is cut into `n_splits` contiguous test blocks. Each fold trains on EVERY row
    before its test block (the window expands as we step forward), minus an
    optional `embargo` gap of rows immediately before the test block — use it only
    if train and test windows could overlap in time. KXBTC15M windows are
    back-to-back and the label settles before the next window's decision, so the
    default embargo of 0 is correct here.
    """
    start = int(n_rows * min_train_frac)
    bounds = np.linspace(start, n_rows, n_splits + 1).astype(int)
    for i in range(n_splits):
        test_lo, test_hi = int(bounds[i]), int(bounds[i + 1])
        train_hi = max(0, test_lo - embargo)
        train_idx = np.arange(0, train_hi, dtype=np.intp)
        test_idx = np.arange(test_lo, test_hi, dtype=np.intp)
        if train_idx.size == 0 or test_idx.size == 0:
            continue
        yield train_idx, test_idx
