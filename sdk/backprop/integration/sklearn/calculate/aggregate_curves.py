"""ROC/PR aggregate controls over the existing numerical chart implementation.

Architecture: build the verified per-class chart -> compute requested averages
using sklearn -> append named series -> apply the existing table-size limit.
Main callers: integration.sklearn.plot_roc and plot_precision_recall. No service
or browser feature is added. Class selection hides per-class curves; aggregates
use every observed class, as their names describe an overall measurement.
"""
from collections import deque
import numpy as np
import pandas as pd
from sklearn import metrics

from ..plot.controls import boolean


def _series_name(base, used):
    """Keep an aggregate distinct from a user-provided class display name."""
    name = base
    while name in used:
        name += " (aggregate)"
    used.add(name)
    return name


def _bounded_frame(frame, kind, limit):
    """Retain each requested curve when a pooled series has many thresholds.

    Main caller: curve_chart after all averages are computed. Allocate points
    evenly among curves, then sample each sorted curve at evenly spaced indices.
    This prevents a large pooled series from erasing smaller class curves.
    """
    groups = [group.sort_values(["fpr", "tpr"] if kind == "roc" else ["recall", "precision"], kind="stable")
              for _, group in frame.groupby("class", sort=False, dropna=False)]
    if limit < len(groups):
        raise ValueError("Table.MAX_ROWS must allow at least one point per requested curve")
    quotas = [1] * len(groups)
    available = deque(index for index, group in enumerate(groups) if len(group) > 1)
    for _ in range(limit - len(groups)):
        if not available:
            break
        index = available.popleft()
        quotas[index] += 1
        if quotas[index] < len(groups[index]):
            available.append(index)
    return pd.concat([group.iloc[np.linspace(0, len(group) - 1, quota, dtype=int)]
                      for group, quota in zip(groups, quotas, strict=True)], ignore_index=True)


def curve_chart(kind, truth, probabilities, labels, selected, micro, macro=False):
    """Add pooled and equal-weight curves without changing per-class values.

    Main callers: the two public sklearn curve wrappers. Micro pools one-vs-rest
    decisions; macro ROC averages interpolated true-positive rates on the union
    of false-positive rates. PR retains the raw pooled threshold curve. Existing
    sklearn validation handles invalid targets/probability shapes first.
    """
    import backprop as wandb
    micro = boolean(micro, "plot_micro")
    macro = boolean(macro, "plot_macro")
    empty_selection = selected is not None and np.asarray(selected).size == 0
    from backprop.statistical_curves import curve_chart as per_class_chart
    chart = per_class_chart(kind, truth, probabilities, labels,
        None if empty_selection else selected,
        "ROC Curve" if kind == "roc" else "Precision-Recall Curve", False, resample=False)
    rows = [] if empty_selection else list(chart.table.data)
    columns = list(chart.table.columns)
    target, scores = np.asarray(truth), np.asarray(probabilities)
    classes = np.unique(target)
    # The existing per-class implementation uses probability columns in sorted
    # observed-class order. Extra columns retain that same upstream behavior.
    indicators = target[:, None] == classes[None, :]
    scores = scores[:, :len(classes)]
    used = {str(row[columns.index("class")]) for row in rows}

    def append_series(name, first, second):
        x, y = ("fpr", "tpr") if kind == "roc" else ("recall", "precision")
        for left, right in zip(first, second, strict=True):
            values = {"class": name, x: left, y: right}
            rows.append([values[column] for column in columns])

    if micro:
        name = _series_name("micro-average", used)
        if kind == "roc":
            first, second, _ = metrics.roc_curve(indicators.ravel(), scores.ravel())
        else:
            second, first, _ = metrics.precision_recall_curve(indicators.ravel(), scores.ravel())
        append_series(name, first, second)
    if macro:
        curves = [metrics.roc_curve(indicators[:, index], scores[:, index])[:2] for index in range(len(classes))]
        grid = np.unique(np.concatenate([curve[0] for curve in curves]))
        mean = np.mean([np.interp(grid, false_positive, true_positive) for false_positive, true_positive in curves], axis=0)
        append_series(_series_name("macro-average", used), grid, mean)
    frame = pd.DataFrame(rows, columns=columns).round(3)
    if len(frame) > wandb.Table.MAX_ROWS:
        wandb.termwarn(f"wandb uses only {wandb.Table.MAX_ROWS} data points to create the plots.")
        frame = _bounded_frame(frame, kind, wandb.Table.MAX_ROWS)
    # Preserve each column's scalar types instead of letting DataFrame.values
    # coerce numeric class IDs to floats alongside floating-point coordinates.
    rows = [list(row) for row in frame.itertuples(index=False, name=None)]
    chart.table = wandb.Table(columns=columns, data=rows, allow_mixed_types=True)
    return chart
