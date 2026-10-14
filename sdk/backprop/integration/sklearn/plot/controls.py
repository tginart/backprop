"""Meaningful rendering controls for legacy sklearn plotting signatures.

Architecture: validate public options -> keep ordinary table/chart values ->
record small rendering overrides for the existing native chart encoder.
Main callers: sklearn classifier and elbow helpers. These options are carried
by the chart itself, so journals and browser readers observe the same settings.
Why this exists: pinned W&B exposes these arguments but never applies them.
"""
from numbers import Integral
import numpy as np


def boolean(value, name):
    """Validate a plotting switch without treating arbitrary containers as true."""
    if not isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must be a boolean")
    return bool(value)


def binary_mode(value, y_train, y_test):
    """Enable binary diagnostics only for an explicitly declared binary problem.

    Main caller: plot_classifier before it writes any chart. False leaves the
    general classification charts enabled; True also enables calibration.
    """
    enabled = boolean(value, "is_binary")
    if enabled and len(np.unique(np.concatenate([np.asarray(y_train), np.asarray(y_test)]))) != 2:
        raise ValueError("is_binary=True requires exactly two target classes")
    return enabled


def feature_chart(chart, title, maximum):
    """Apply feature names already computed by sklearn, then cap the ranked rows.

    Main caller: plot_feature_importances. None means no row limit; zero means
    an intentionally empty chart. The original estimator is never changed.
    """
    import backprop as wandb
    if title is not None and not isinstance(title, str):
        raise TypeError("title must be a string or None")
    if maximum is not None:
        if isinstance(maximum, (bool, np.bool_)) or not isinstance(maximum, Integral):
            raise TypeError("max_num_features must be an integer or None")
        if maximum < 0:
            raise ValueError("max_num_features must be nonnegative")
    if chart is None:
        return None
    if maximum is not None and len(chart.table.data) > maximum:
        chart.table = wandb.Table(columns=list(chart.table.columns), data=chart.table.data[:maximum])
    chart._backprop_render_options = {"title": title}
    return chart


def elbow_chart(chart, show_time):
    """Retain measured timings while letting the caller hide the timing panel.

    Main caller: calculate.elbow_curve. Both values and the chosen presentation
    remain in the ordinary chart snapshot; no browser preference is required.
    """
    chart._backprop_render_options = {"show_cluster_time": boolean(show_time, "show_cluster_time")}
    return chart
