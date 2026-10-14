# Adapted from W&B 0.30.0 (MIT); see THIRD_PARTY_NOTICES.md.
"""Silhouette diagnostics with explicit class identity and display names.

Architecture: sklearn computes each sample's score -> encode arbitrary cluster
IDs -> align centers and display labels -> build the existing two-panel table.
Main caller: integration.sklearn.plot_silhouette, directly or via plot_clusterer.
Invariants: display names never index centers; both panels retain the same
cluster meaning; original point order and coefficient-sorted order are distinct.
"""
from collections.abc import Mapping
import numpy as np
from sklearn.metrics import silhouette_samples, silhouette_score
from sklearn.preprocessing import LabelEncoder

import backprop as wandb
from backprop.integration.sklearn import utils


def _display_names(classes, labels):
    """Resolve one name per cluster without altering its numerical identity.

    Main caller: silhouette. A mapping keys by original cluster IDs; a sequence
    indexes nonnegative integer IDs when it covers them; otherwise it follows
    sorted observed IDs, including string and nonconsecutive IDs.
    """
    if labels is None:
        return classes.tolist()
    if isinstance(labels, Mapping):
        if any(value not in labels for value in classes):
            raise ValueError("labels must name every observed cluster")
        return [labels[value] for value in classes]
    if isinstance(labels, (str, bytes)):
        raise TypeError("labels must be a sequence or mapping of cluster names")
    names = list(labels)
    if all(isinstance(value, (int, np.integer)) and 0 <= value < len(names) for value in classes):
        return [names[int(value)] for value in classes]
    if len(names) != len(classes):
        raise ValueError("labels must contain one name per observed cluster")
    return names


def _centers(clusterer, classes, encoded, count):
    """Align model centers with original IDs, including renamed cluster labels.

    Main caller: silhouette in KMeans mode. Ordinary integer IDs index model
    centers directly. Renamed labels use the fitted model's sample assignments;
    an inconsistent assignment fails explicitly rather than drawing wrong centers.
    """
    centers = np.asarray(clusterer.cluster_centers_)
    if all(isinstance(value, (int, np.integer)) and 0 <= value < len(centers) for value in classes):
        return centers[classes.astype(int), :2]
    assignments = np.asarray(clusterer.labels_)
    if assignments.shape != (count,):
        raise ValueError("Renamed cluster labels require aligned fitted model assignments")
    indices = []
    for index in range(len(classes)):
        original = np.unique(assignments[encoded == index])
        if len(original) != 1 or not isinstance(original[0], (int, np.integer)) or not 0 <= original[0] < len(centers):
            raise ValueError("cluster_labels do not identify a single fitted model center")
        indices.append(int(original[0]))
    return centers[indices, :2]


def silhouette(clusterer, X, cluster_labels, labels, metric, kmeans):
    """Build scatter and ordered coefficient data using the requested metric.

    Main caller: the public plotting wrapper. sklearn owns score validation;
    this function owns only the two-dimensional presentation and label mapping.
    """
    from ..plot.controls import boolean
    kmeans = boolean(kmeans, "kmeans")
    from scipy import sparse
    X = X.toarray() if sparse.issparse(X) else np.asarray(X)
    cluster_labels = np.asarray(cluster_labels)
    encoder = LabelEncoder()
    encoded = encoder.fit_transform(cluster_labels)
    names = _display_names(encoder.classes_, labels)
    average = silhouette_score(X, cluster_labels, metric=metric)
    samples = silhouette_samples(X, cluster_labels, metric=metric)
    if X.ndim != 2 or X.shape[1] < 2:
        raise ValueError("Silhouette scatter plots require at least two feature columns")
    centers = _centers(clusterer, encoder.classes_, encoded, len(X)) if kmeans else None
    positions, values, coefficient_names = [], [], []
    lower = 10
    for index, name in enumerate(names):
        ordered = np.sort(samples[encoded == index])
        positions.extend(range(lower, lower + len(ordered)))
        values.extend(ordered)
        coefficient_names.extend([name] * len(ordered))
        lower += len(ordered) + 10
    retained = min(len(X), utils.chart_limit)
    if retained < len(X):
        utils.warn_chart_limit(utils.chart_limit, "silhouette")
    data = []
    for row in range(retained):
        center = centers[encoded[row]] if centers is not None else (None, None)
        data.append([X[row, 0], X[row, 1], names[encoded[row]], center[0], center[1],
                     positions[row], 0, values[row], coefficient_names[row], average])
    table = wandb.Table(columns=["x", "y", "colors", "centerx", "centery", "y_sil", "x1", "x2", "color_sil", "silhouette_avg"], data=data)
    return wandb.visualize("wandb/silhouette_/v1", table)
