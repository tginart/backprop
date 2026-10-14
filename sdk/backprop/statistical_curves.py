"""Pinned W&B statistical chart behavior using its documented dependencies.

Architecture: NumPy coerces/selects classes -> sklearn computes threshold
statistics -> PR smooths and interpolates -> pandas rounds -> sklearn samples
oversized tables -> the ordinary built-in chart wrapper renders the result.
Main callers: plot.roc_curve and plot.pr_curve. No packages are imported until
one of these statistical APIs is used; ordinary logging stays dependency free.
Why this exists: hand-written approximations diverge on warnings, degenerate
input, interpolation, duplicate display labels and floating-point rounding.
Algorithm reference: W&B v0.30.0 wandb/plot/{roc_curve,pr_curve}.py (MIT).
"""
from __future__ import annotations

import importlib


def _dependencies():
    """Require the same numerical libraries as the pinned public curve APIs."""
    from .errors import Error
    modules = []
    for name in ("numpy", "pandas", "sklearn.metrics", "sklearn.utils"):
        try:
            modules.append(importlib.import_module(name))
        except Exception as error:
            raise Error(f"Statistical curves require the optional {name} library in your Python environment") from error
    return modules


def _precision_at_recalls(np, precision, recall, interpolation):
    """Apply pinned monotone precision smoothing and left-sided lookup.

    Main callers: curve_chart's PR branch. Compute linspace before reversing,
    preserving its exact threshold-tie behavior and accepted integer types.
    """
    smoothed = np.array(precision)
    for index in range(1, len(smoothed)):
        smoothed[index] = max(smoothed[index], smoothed[index - 1])
    positions = np.searchsorted(recall[::-1], interpolation, side="left")
    return smoothed[::-1][positions]


def curve_chart(kind, y_true, y_probas, labels, classes_to_plot, title, split_table,
                interp_size=21, *, resample=True):
    """Build an official-shaped ROC or PR chart without altering inputs.

    Main callers: public plot helpers. The upstream missing/type checks run
    after np.array coercion and accept all these ndarray values; sklearn/NumPy
    therefore define shape/value errors, including None and empty inputs.
    Dictionaries intentionally key by display label: duplicate labels replace
    earlier class curves, matching the pinned wrapper instead of duplicating
    visually indistinguishable series. The sklearn integration defers sampling
    until its additional aggregate curves exist, so one shared row budget
    preserves every series and its endpoints.
    """
    import backprop
    from .plot import plot_table
    from .tables import Table
    np, pd, metrics, utils = _dependencies()
    truth, probabilities = np.array(y_true), np.array(y_probas)
    classes = np.unique(truth)
    selected = classes if classes_to_plot is None else classes_to_plot
    first, second = {}, {}
    interpolation = np.linspace(0, 1, interp_size)[::-1] if kind == "pr" else None
    for index in np.where(np.isin(classes, selected))[0]:
        label = (labels[classes[index]] if labels is not None
                 and (isinstance(classes[index], int) or isinstance(classes[0], np.integer))
                 else classes[index])
        if kind == "roc":
            first[label], second[label], _ = metrics.roc_curve(
                truth, probabilities[..., index], pos_label=classes[index])
        else:
            precision, recall, _ = metrics.precision_recall_curve(
                truth, probabilities[:, index], pos_label=classes[index])
            first[label] = _precision_at_recalls(np, precision, recall, interpolation)

    x, y = ("fpr", "tpr") if kind == "roc" else ("recall", "precision")
    if kind == "roc":
        values = {"class": np.hstack([[label] * len(value) for label, value in first.items()]),
                  "fpr": np.hstack(list(first.values())), "tpr": np.hstack(list(second.values()))}
    else:
        values = {"class": np.hstack([[label] * len(value) for label, value in first.items()]),
                  "precision": np.hstack(list(first.values())),
                  "recall": np.tile(interpolation, len(first))}
    frame = pd.DataFrame(values).round(3)
    if resample and len(frame) > Table.MAX_ROWS:
        backprop.termwarn(f"wandb uses only {Table.MAX_ROWS} data points to create the plots."
                         if kind == "roc" else f"Table has a limit of {Table.MAX_ROWS} rows. Resampling to fit.")
        frame = utils.resample(frame, replace=False, n_samples=Table.MAX_ROWS,
                               random_state=42, stratify=frame["class"])
        frame = frame.sort_values(["fpr", "tpr", "class"] if kind == "roc"
                                  else ["precision", "recall", "class"])
    return plot_table("wandb/area-under-curve/v0", Table(dataframe=frame),
                      {"x": x, "y": y, "class": "class"},
                      {"title": title, "x-axis-title": "False positive rate" if kind == "roc" else "Recall",
                       "y-axis-title": "True positive rate" if kind == "roc" else "Precision"}, split_table)
