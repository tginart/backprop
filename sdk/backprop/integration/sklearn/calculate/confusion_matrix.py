# Adapted from W&B 0.30.0 (MIT); see THIRD_PARTY_NOTICES.md.
# Architecture: framework callbacks -> Backprop public logging -> Foundry.
# Main callers: the corresponding optional training framework.
import itertools
from warnings import simplefilter

import numpy as np
from sklearn import metrics
from sklearn.utils.multiclass import unique_labels

import backprop as wandb

from .. import utils

# ignore all future warnings
simplefilter(action="ignore", category=FutureWarning)


def validate_labels(classes, selected, name):
    """Require selected display labels to belong to the plotted class names.

    Main caller: confusion_matrix, before filtering either table axis.
    Why this exists: the pinned helper contains an unconditional assertion,
    making both declared label-filter arguments unusable.
    """
    if isinstance(selected, (str, bytes)) or not hasattr(selected, "__iter__"):
        raise TypeError(f"{name} must be a sequence of class labels")
    selected = list(selected)
    unknown = [label for label in selected if label not in classes]
    if unknown:
        raise ValueError(f"{name} contains unknown labels: {unknown}")
    return selected


def confusion_matrix(
    y_true=None,
    y_pred=None,
    labels=None,
    true_labels=None,
    pred_labels=None,
    normalize=False,
):
    """Compute the confusion matrix to evaluate the performance of a classification.

    Called by plot_confusion_matrix to visualize roc curves. Please use the function
    plot_confusion_matrix() if you wish to visualize your confusion matrix.
    """
    cm = metrics.confusion_matrix(y_true, y_pred)

    if labels is None:
        classes = unique_labels(y_true, y_pred)
    else:
        classes = np.asarray(labels)
        if classes.ndim != 1 or len(classes) != cm.shape[0]:
            raise ValueError("labels must contain one display name per class")

    if normalize:
        cm = cm.astype("float") / cm.sum(axis=1)[:, np.newaxis]
        cm = np.around(cm, decimals=2)
        cm[np.isnan(cm)] = 0.0

    if true_labels is None:
        true_classes = classes
    else:
        true_labels = validate_labels(classes, true_labels, "true_labels")

        true_label_indexes = np.isin(classes, true_labels)

        true_classes = classes[true_label_indexes]
        cm = cm[true_label_indexes]

    if pred_labels is None:
        pred_classes = classes
    else:
        pred_labels = validate_labels(classes, pred_labels, "pred_labels")

        pred_label_indexes = np.isin(classes, pred_labels)

        pred_classes = classes[pred_label_indexes]
        cm = cm[:, pred_label_indexes]

    table = make_table(cm, pred_classes, true_classes, labels)
    chart = wandb.visualize("wandb/confusion_matrix/v1", table)

    return chart


def make_table(cm, pred_classes, true_classes, labels):
    """Keep the matrix's true-label rows and predicted-label columns aligned.

    Main caller: confusion_matrix after optional axis filtering. Each cell uses
    cm[actual_index, predicted_index], including rectangular filtered matrices.
    The pinned code swaps these names and fails on rectangular selections.
    """
    data = []
    for actual_index, predicted_index in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        data.append([pred_classes[predicted_index], true_classes[actual_index], cm[actual_index, predicted_index]])
        if utils.check_against_limit(len(data), "confusion_matrix", utils.chart_limit):
            break
    return wandb.Table(columns=["Predicted", "Actual", "Count"], data=data)
