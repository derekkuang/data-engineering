"""Reusable walk-forward out-of-fold prediction.

Both the baseline report and the cost-aware backtest need the exact same thing:
for every window, a probability produced by a model trained ONLY on earlier
windows. Centralising that loop here means the report and the backtest can never
disagree about how the predictions were made — they call the same function.

The default estimator is a logistic pipeline (impute → standardise → logistic).
Swapping in LightGBM later is just a different `build_estimator` argument, so the
walk-forward machinery never changes.
"""

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ml.alpha.walkforward import walk_forward_splits

EstimatorFactory = Callable[[], Pipeline]


def logistic_pipeline() -> Pipeline:
    """Impute (median) → standardise → logistic. Every step is fit per fold on the
    train block only, so no test-fold statistics leak backward into training."""
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, C=1.0)),
        ]
    )


def lightgbm_pipeline() -> Pipeline:
    """Gradient-boosted trees. LightGBM handles NaNs natively and needs no scaling,
    so the pipeline is just the classifier. Params are deliberately CONSERVATIVE —
    a ~50/50 target on a few thousand rows overfits trivially, so shallow trees +
    strong regularisation, to give the fancier model a fair (not rigged) shot."""
    return Pipeline(
        [
            (
                "clf",
                LGBMClassifier(
                    n_estimators=300,
                    learning_rate=0.03,
                    num_leaves=15,
                    max_depth=4,
                    min_child_samples=50,
                    subsample=0.8,
                    subsample_freq=1,
                    colsample_bytree=0.8,
                    reg_lambda=1.0,
                    random_state=0,
                    n_jobs=-1,
                    verbosity=-1,
                ),
            ),
        ]
    )


def walk_forward_oof(
    x: npt.NDArray[np.float64],
    y: npt.NDArray[np.intp],
    build_estimator: EstimatorFactory = logistic_pipeline,
    n_splits: int = 8,
) -> npt.NDArray[np.float64]:
    """Out-of-fold P(up): each row is predicted by a model that never saw it and
    was trained only on earlier rows. Rows not reached by any test fold (the
    initial training block) stay NaN, so callers can mask them out."""
    oof = np.full(len(y), np.nan)
    for train_idx, test_idx in walk_forward_splits(len(y), n_splits=n_splits):
        estimator = build_estimator()
        estimator.fit(x[train_idx], y[train_idx])
        oof[test_idx] = estimator.predict_proba(x[test_idx])[:, 1]
    return oof
