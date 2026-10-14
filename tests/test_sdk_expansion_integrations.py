"""Real optional-library execution and public import/serialization contracts.

Architecture: real training/data objects -> public adapters -> MemorySink ->
public readback. Missing optional libraries are explicit skips, not parity proof.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import backprop
from backprop.apis.public import Api
from backprop.transport import MemorySink


class Integrations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BACKPROP_DIR": self.tmp.name})
        self.env.start()
        self.store = MemorySink()
        self.run = backprop.init(project="p", entity="e", _sink=self.store, dir=self.tmp.name,
                                settings={"console": "off", "silent": True, "x_disable_stats": True}, reinit=True)
        self.api = Api._from_store(self.store, entity="e", project="p")

    def tearDown(self):
        self.run.finish()
        self.env.stop()
        self.tmp.cleanup()

    def test_joined_table_artifact_roundtrip_and_run_display(self):
        left = backprop.Table(columns=['id', 'a'], data=[[1, 2], [2, 3]])
        right = backprop.Table(columns=['id', 'b'], data=[[1, 4], [3, 5]])
        joined = backprop.JoinedTable(left, right, 'id')
        with self.assertRaises(ValueError): self.run.log({'joined': joined})
        artifact = backprop.Artifact('tables', 'dataset')
        artifact.add(joined, 'join')
        self.run.log_artifact(artifact).wait()
        self.run.log({'joined': joined})
        self.run.flush()
        result = self.api.artifact(artifact.id).get('join')
        self.assertIsInstance(result, backprop.JoinedTable)
        self.assertEqual(result.materialize().data, [[1, 2, 1, 4], [2, 3, None, None], [None, None, 3, 5]])
        self.assertEqual(self.api.run(self.run.id).summary['joined']['columns'], ['left.id', 'left.a', 'right.id', 'right.b'])

    def test_eval_roles_remain_in_logged_descriptor(self):
        table = backprop.EvalTable(input_columns=['prompt'], output_columns=['answer'], score_columns=['score'],
                                  data=[['hi', 'hello', 1.]])
        self.run.log({'eval': table})
        self.run.flush()
        value = self.api.run(self.run.id).summary['eval']
        self.assertEqual(value['evaluation_roles']['score_columns'], ['score'])
        self.run.log({'eval_again': table})
        self.run.flush()
        self.api.flush()
        self.assertEqual(self.api.run(self.run.id).summary['eval_again']['evaluation_roles'], value['evaluation_roles'])
        invalid = backprop.EvalTable(columns=['x'], input_columns=['missing'])
        with self.assertRaises(ValueError): self.run.log({'invalid': invalid})

    def test_custom_chart_registration_embeds_spec_and_table(self):
        identity = self.api.create_custom_chart('e', 'loss', 'Loss', 'vega2', 'private', {
            'data': {'name': 'wandb'}, 'mark': 'line', 'encoding': {
                'x': {'field': '${field:step}', 'type':'quantitative'},
                'y': {'field': '${field:loss}', 'type':'quantitative'}}})
        chart = backprop.plot_table(identity, backprop.Table(columns=['epoch','loss'], data=[[0,3],[1,2]]),
                                    {'step':'epoch', 'loss':'loss'})
        self.run.log({'chart': chart})
        self.run.flush()
        value = self.api.run(self.run.id).summary['chart']
        self.assertEqual(value['spec']['encoding']['x']['field'], 'epoch')
        self.assertEqual(value['table']['data'], [[0,3],[1,2]])
        with self.assertRaises(ValueError): backprop.plot_table('e/unknown', chart.table, {})
        with self.assertRaises(NotImplementedError): self.api.create_custom_chart('e','public','Public','vega2','public',{})

    @unittest.skipUnless(importlib.util.find_spec('sklearn'), 'optional scikit-learn not installed')
    def test_real_sklearn_classification_and_regression_plots(self):
        from sklearn.datasets import make_classification, make_regression
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.linear_model import LinearRegression
        from backprop.integration import sklearn as plots
        x,y = make_classification(n_samples=80, n_features=5, random_state=42)
        model = RandomForestClassifier(n_estimators=3, random_state=42).fit(x,y)
        plots.plot_confusion_matrix(y, model.predict(x))
        plots.plot_roc(y, model.predict_proba(x))
        plots.plot_precision_recall(y, model.predict_proba(x))
        plots.plot_feature_importances(model)
        plots.plot_class_proportions(y[:40],y[40:])
        plots.plot_learning_curve(model,x,y)
        plots.plot_summary_metrics(model,x,y,x,y)
        xr,yr = make_regression(n_samples=40,n_features=3,random_state=42)
        regressor = LinearRegression().fit(xr,yr)
        plots.plot_residuals(regressor,xr,yr)
        plots.plot_outlier_candidates(regressor,xr,yr)
        self.run.flush()
        state = self.api.run(self.run.id)
        self.assertGreaterEqual(len(list(state.scan_history())), 9)
        self.assertEqual(state.summary['feature_importances']['_type'], 'vega')

    def test_eval_media_mode_and_score_statistics(self):
        with self.assertRaises(TypeError):
            backprop.EvalTable(columns=['x'],data=[[backprop.Html('<p>x</p>')]],unsupported_media_mode='raise')
        table=backprop.EvalTable(columns=['x'],data=[[backprop.Html('<p>x</p>')]])
        self.assertIsInstance(table.data[0][0], backprop.Html)
        self.run.log({'stubbed': table})
        self.assertIn('not yet supported', self.run.summary['stubbed']['data'][0][0])
        value=backprop.EvalTable(score_columns=['score'],data=[[1],[3]])
        self.assertEqual(value.evaluation_metadata()['evaluation_scores']['score']['mean'],2)
        self.assertEqual(value.evaluation_metadata()['evaluation_row_identity']['start'],1)

    def test_supported_import_aliases_share_identity(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        import wandb
        from wandb.data_types import JoinedTable, EvalTable
        self.assertIs(JoinedTable,backprop.JoinedTable)
        self.assertIs(EvalTable,backprop.EvalTable)
        from wandb.integration import tensorboard
        from backprop.integration import tensorboard as native
        self.assertIs(tensorboard,native)
        self.assertIs(wandb.run,self.run)
