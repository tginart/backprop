"""Built-in charts for native Foundry rendering.

Architecture: helpers build official-shaped Tables and CustomChartSpecs;
CustomChart supplies a common renderer mapping; Encoder stores both. ROC/PR
statistics run locally. Arbitrary Vega execution is explicitly unsupported.
Main callers: training scripts and Run.log.
"""
from __future__ import annotations

from dataclasses import dataclass

from .tables import Table


@dataclass
class CustomChartSpec:
    spec_name: str
    fields: dict
    string_fields: dict
    key: str = ""
    panel_type: str = "Vega2"
    split_table: bool = False
    custom_spec: dict | None = None

    @property
    def table_key(self):
        if not self.key:
            raise ValueError("Key for the custom chart spec is not set")
        return ("Custom Chart Tables/" if self.split_table else "") + self.key + "_table"

    @property
    def config_key(self):
        return "_wandb", "visualize", self.key

    @property
    def config_value(self):
        return {"panel_type": self.panel_type, "panel_config": {
            "panelDefId": self.spec_name, "fieldSettings": dict(self.fields),
            "stringSettings": dict(self.string_fields), "transform": {"name": "tableWithLeafColNames"},
            "userQuery": {"queryFields": [{"name": "runSets", "args": [{"name": "runSets", "value": "$" + "{runSets}"}],
                "fields": [{"name": "id", "fields": []}, {"name": "name", "fields": []}, {"name": "_defaultColorIndex", "fields": []},
                    {"name": "summaryTable", "args": [{"name": "tableKey", "value": self.table_key}], "fields": []}]}]}}}


@dataclass
class CustomChart:
    table: Table
    spec: CustomChartSpec
    _backprop_plot = True

    def set_key(self, key):
        self.spec.key = key

    @property
    def title(self):
        return self.spec.string_fields.get("title", "")

    @property
    def kind(self):
        if self.spec.custom_spec is not None: return "vega"
        types = {"wandb/line/v0": "line", "wandb/scatter/v0": "scatter", "wandb/bar/v0": "bar",
                 "wandb/histogram/v0": "histogram", "wandb/lineseries/v0": "line_series",
                 "wandb/confusion_matrix/v1": "confusion_matrix"}
        if self.spec.spec_name == "wandb/area-under-curve/v0":
            return "pr_curve" if self.spec.fields.get("x") == "recall" else "roc_curve"
        return types.get(self.spec.spec_name, "vega")

    @property
    def fields(self):
        """Map the public official spec into the browser's common dimensions."""
        fields = self.spec.fields
        if self.kind == "line_series":
            return {"x": fields["step"], "y": fields["lineVal"], "series": fields["lineKey"]}
        if self.kind == "confusion_matrix":
            return {"x": fields["Predicted"], "y": fields["Actual"], "value": fields["nPredictions"]}
        if self.kind == "bar":
            return {"x": fields["label"], "y": fields["value"]}
        result = {key: value for key, value in fields.items() if key not in {"stroke", "class"} and value is not None}
        if fields.get("stroke") is not None:
            result["series"] = fields["stroke"]
        if fields.get("class") is not None:
            result["series"] = fields["class"]
        return result


_SUPPORTED_SPECS = {"wandb/line/v0", "wandb/scatter/v0", "wandb/bar/v0", "wandb/histogram/v0",
                    "wandb/lineseries/v0", "wandb/confusion_matrix/v1", "wandb/area-under-curve/v0"}


def plot_table(vega_spec_name, data_table, fields, string_fields=None, split_table=False):
    custom = None
    if vega_spec_name not in _SUPPORTED_SPECS:
        import backprop
        from .custom_charts import resolve
        custom = resolve(backprop._active(), vega_spec_name)
    if not isinstance(data_table, Table):
        raise TypeError("data_table must be a Table")
    for column in fields.values():
        if column is not None and column not in data_table.columns:
            raise ValueError(f"Plot column {column!r} is absent from table")
    return CustomChart(data_table, CustomChartSpec(vega_spec_name, dict(fields), dict(string_fields or {}), split_table=split_table, custom_spec=custom))


def line(table, x, y, stroke=None, title="", split_table=False):
    return plot_table("wandb/line/v0", table, {"x": x, "y": y, "stroke": stroke}, {"title": title}, split_table)


def scatter(table, x, y, title="", split_table=False):
    return plot_table("wandb/scatter/v0", table, {"x": x, "y": y}, {"title": title}, split_table)


def bar(table, label, value, title="", split_table=False):
    return plot_table("wandb/bar/v0", table, {"label": label, "value": value}, {"title": title}, split_table)


def histogram(table, value, title="", split_table=False):
    return plot_table("wandb/histogram/v0", table, {"value": value}, {"title": title}, split_table)


def line_series(xs, ys, keys=None, title="", xname="x", split_table=False):
    xs, ys = list(xs), [list(values) for values in ys]
    keys = list(keys) if keys is not None else [f"line_{index}" for index in range(len(ys))]
    nested = bool(xs) and hasattr(xs[0], "__iter__") and not isinstance(xs[0], (str, bytes))
    series = [list(values) for values in xs] if nested else [xs] * len(ys)
    if len(series) != len(ys) or len(keys) != len(ys):
        raise ValueError("The numbers of x series, y series, and keys must match")
    # Upstream 0.30.0 uses zip(strict=False) within each series pair.
    rows = [[x, keys[index], y] for index, (xx, yy) in enumerate(zip(series, ys)) for x, y in zip(xx, yy)]
    return plot_table("wandb/lineseries/v0", Table(columns=["step", "lineKey", "lineVal"], data=rows),
                      {"step": "step", "lineKey": "lineKey", "lineVal": "lineVal"},
                      {"title": title, "xname": xname}, split_table)


def confusion_matrix(probs=None, y_true=None, preds=None, class_names=None, title="Confusion Matrix Curve", split_table=False):
    if y_true is None or (probs is None) == (preds is None):
        raise ValueError("Provide y_true and exactly one of probs or preds")
    if probs is not None:
        probs = probs.tolist() if hasattr(probs, "tolist") else list(probs)
        preds = [max(range(len(row)), key=row.__getitem__) for row in probs]
    y_true, preds = list(y_true), list(preds)
    if len(y_true) != len(preds):
        raise ValueError("The number of predictions and true labels must match")
    classes = list(range(len(class_names))) if class_names is not None else sorted(set(y_true) | set(preds))
    labels = list(class_names) if class_names is not None else [f"Class_{index + 1}" for index in range(len(classes))]
    indices = {value: index for index, value in enumerate(classes)}
    counts = [[0 for _ in labels] for _ in labels]
    for actual, predicted in zip(y_true, preds):
        if actual not in indices or predicted not in indices:
            raise ValueError("Class index is outside class_names")
        counts[indices[actual]][indices[predicted]] += 1
    rows = [[labels[i], labels[j], count] for i, row in enumerate(counts) for j, count in enumerate(row)]
    return plot_table("wandb/confusion_matrix/v1", Table(columns=["Actual", "Predicted", "nPredictions"], data=rows),
                      {"Actual": "Actual", "Predicted": "Predicted", "nPredictions": "nPredictions"}, {"title": title}, split_table)


def roc_curve(y_true, y_probas=None, labels=None, classes_to_plot=None, title="ROC Curve", split_table=False):
    from .statistical_curves import curve_chart
    return curve_chart("roc", y_true, y_probas, labels, classes_to_plot, title, split_table)


def pr_curve(y_true=None, y_probas=None, labels=None, classes_to_plot=None, interp_size=21, title="Precision-Recall Curve", split_table=False):
    from .statistical_curves import curve_chart
    return curve_chart("pr", y_true, y_probas, labels, classes_to_plot, title, split_table, interp_size)

