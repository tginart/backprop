"""Local equivalents of the pinned scikit-learn chart presets.

Architecture: public dataclasses -> receiving run resolves a preset/registered
definition -> existing Table and native chart encoder -> browser renderer.
Unknown IDs fail explicitly during logging. Main callers: direct Visualize
construction, integration.sklearn calculations, Encoder and Run.log.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from .plot import CustomChart, CustomChartSpec
from .tables import Table

_PRESETS = {
    'wandb/feature_importances/v1': ('bar', 'feature_names', 'importances', None, 'nominal'),
    'wandb/metrics/v1': ('bar', 'metric_name', 'metric_value', 'model_name', 'nominal'),
    'wandb/elbow/v1': ('line', 'cluster_ranges', 'errors', None, 'quantitative'),
    'wandb/residuals_plot/v1': ('point', 'y_pred', 'residuals', 'dataset', 'quantitative'),
    'wandb/outliers/v1': ('point', 'instance_indicies', 'distance', None, 'quantitative'),
    'wandb/calibration/v1': ('line', 'mean_predicted_value', 'fraction_of_positives', 'model', 'quantitative'),
    'wandb/class_proportions/v1': ('bar', 'class', 'count', 'dataset', 'nominal'),
    'wandb/learning_curve/v1': ('line', 'train_size', 'score', 'dataset', 'quantitative'),
    'wandb/decision_boundaries/v1': ('point', 'x', 'y', 'color', 'quantitative'),
    'wandb/silhouette_/v1': ('point', 'x', 'y', 'colors', 'quantitative'),
}


@dataclass
class VisualizeSpec:
    """Retain the pinned two-field mutable chart specification.

    Main callers: Visualize and user-authored local chart values. Rendering
    fields live in a separate CustomChartSpec created only when logging, so
    constructing, copying or inspecting this value never resolves a chart ID.
    """
    name: str
    key: str = ""

    @property
    def config_key(self) -> tuple[str, str, str]: return ('_wandb', 'viz', self.key)

    @property
    def config_value(self) -> dict[str, Any]:
        return {'id': self.name, 'historyFieldSettings': {'x-axis': '_step', 'key': self.key}}


@dataclass
class Visualize:
    """Keep the caller's table/spec identities with the pinned constructor.

    Main callers: direct Python construction, visualize and selected sklearn
    helpers. The dataclass intentionally does not validate its annotated fields;
    the reference has the same ordinary Python value and mutation behavior.
    """
    table: Table
    spec: VisualizeSpec
    _backprop_plot = True

    def set_key(self, key: str) -> None:
        self.spec.key = key


def visualize(id: str, value: Table) -> Visualize:
    if not isinstance(value, Table):
        from .errors import Error
        raise Error(f"Expected `value` to be `wandb.Table` type, instead got {type(value).__name__}")
    return Visualize(table=value, spec=VisualizeSpec(name=id))


def rendering_chart(value, run):
    """Resolve one public value into an immutable native rendering snapshot.

    Main caller: Encoder.encode. Built-in presets need no run or network;
    registered definitions are read from the receiving run's own Foundry store.
    Why this exists: public dataclass state must not become stale or acquire
    private render fields when users mutate name, key or table between logs.
    """
    identity, table = value.spec.name, value.table
    if identity == 'wandb/confusion_matrix/v1':
        from .plot import plot_table
        chart = plot_table(identity, table, {'Predicted': 'Predicted', 'Actual': 'Actual', 'nPredictions': 'Count'})
        chart.spec.key = value.spec.key
        return chart
    if identity in _PRESETS:
        mark, x, y, color, xtype = _PRESETS[identity]
        spec = {'data': {'name': 'wandb'}, 'mark': mark, 'encoding': {
            'x': {'field': x, 'type': xtype}, 'y': {'field': y, 'type': 'quantitative'},
            **({'color': {'field': color, 'type': 'nominal'}} if color else {})}}
        options = getattr(value, '_backprop_render_options', {})
        spec = enrich_preset(identity, spec, show_cluster_time=options.get('show_cluster_time', True))
        if options.get('title') is not None:
            spec['title'] = options['title']
    else:
        from .custom_charts import resolve
        if run is None:
            raise ValueError("Custom chart lookup requires a receiving run")
        spec = resolve(run, identity)
    if not isinstance(table, Table):
        raise TypeError('visualize requires a Table')
    return CustomChart(table, CustomChartSpec(identity, {}, {}, key=value.spec.key, custom_spec=spec))


def chart_bindings(data, prefix=""):
    """Yield charts with the pinned dotted key for nested logging dictionaries.

    Main caller: Run.log, after the whole row has passed encoding validation.
    Keeping mutation at that boundary lets failed rows leave chart keys intact.
    """
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if hasattr(value, '_backprop_plot') and hasattr(value, 'set_key'):
            yield name, value
        elif isinstance(value, dict):
            yield from chart_bindings(value, name)


def enrich_preset(identity, spec, *, show_cluster_time=True):
    """Retain the diagnostic panels carried by the pinned sklearn table schema.

    Main caller: rendering_chart. Silhouette coefficients, residual distributions,
    calibration distributions and elbow timings are meaningful measurements,
    not decoration that can be replaced by a generic scatter plot.
    """
    if identity == 'wandb/feature_importances/v1':
        spec = {**spec, 'width': 260, 'height': 220}
        spec['encoding']['x']['sort'] = None
        return spec
    if identity == 'wandb/silhouette_/v1':
        silhouette={'title':'Silhouette coefficients','width':260,'height':220,'layer':[
            {'mark':'bar','encoding':{'x':{'field':'x1','type':'quantitative'},'x2':{'field':'x2'},
             'y':{'field':'y_sil','type':'quantitative','axis':None},'color':{'field':'color_sil','type':'nominal'}}},
            {'mark':{'type':'rule','strokeDash':[4,4],'color':'#fbb360'},'encoding':{
                'x':{'aggregate':'mean','field':'silhouette_avg','type':'quantitative'}}}]}
        scatter={k:v for k,v in spec.items() if k!='data'}
        scatter.update(width=260,height=220)
        return {'data':{'name':'wandb'},'vconcat':[silhouette,scatter]}
    if identity == 'wandb/residuals_plot/v1':
        panel={'title':'Residual distribution','mark':{'type':'bar','opacity':0.6},'encoding':{
            'x':{'bin':True,'field':'residuals','type':'quantitative'},
            'y':{'aggregate':'count','type':'quantitative'},'color':{'field':'dataset','type':'nominal'}}}
    elif identity == 'wandb/calibration/v1':
        panel={'title':'Prediction distribution','mark':'bar','encoding':{
            'x':{'field':'edge_dict','type':'quantitative'},'y':{'field':'hist_dict','type':'quantitative'},
            'color':{'field':'model','type':'nominal'}}}
    elif identity == 'wandb/elbow/v1':
        if not show_cluster_time:
            return spec
        panel={'title':'Clustering time','mark':'line','encoding':{
            'x':{'field':'cluster_ranges','type':'quantitative'},'y':{'field':'clustering_time','type':'quantitative'}}}
    else: return spec
    return {'data':{'name':'wandb'},'vconcat':[{k:v for k,v in spec.items() if k!='data'},panel]}
