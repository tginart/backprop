"""Public table/value operations and serialization across supported imports.

Architecture: real public values -> ordinary Run/Artifact -> read API -> compare
roles, rows and immutable plot specifications. Main callers: contract CI.
"""
import tempfile
import unittest
import backprop as w
from backprop.transport import MemorySink
from backprop.apis.public import Api
from backprop.compatibility import install_wandb_aliases


class Values(unittest.TestCase):
    def test_eval_inherited_data_operations_and_alias_identity(self):
        install_wandb_aliases()
        from wandb import EvalTable
        from wandb.data_types import EvalTable as Alias
        self.assertIs(EvalTable,Alias)
        t=EvalTable(input_columns=['id'],score_columns=['score'],data=[['a',1],['b',3]])
        t.add_data('c',5);t.add_row('d',7)
        t.add_column('prediction',[1,2,3,4])
        t.add_computed_columns(lambda ndx,row:{'double':row['score']*2})
        self.assertEqual(t.get_column('double'),[2,6,10,14])
        self.assertEqual(len(list(t.iterrows())),4)
        self.assertEqual(len(t.get_index()),4)
        self.assertEqual(int(t.index_ref(1)),1)
        self.assertEqual(t.cast('score',float).name,'number')
        t.set_pk('id')
        with self.assertRaises((TypeError,AssertionError)):t.add_data('e','wrong',5,10)
        self.assertEqual(len(t.data),4)
        self.assertEqual(t.evaluation_metadata()['evaluation_scores']['score']['mean'],4)
        self.assertIn('prediction',t.evaluation_roles['output_columns'])
        clone=EvalTable.from_json({'columns':t.columns,'data':t.data,**t.evaluation_metadata()}, None)
        self.assertEqual(clone.evaluation_roles,t.evaluation_roles)

    def test_eval_and_plotly_artifact_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=MemorySink()
            run=w.Run(entity='e',project='p',dir=directory,_sink=sink,
                      settings={'console':'off','silent':True,'x_disable_stats':True})
            with run:
                table=w.EvalTable(input_columns=['id'],score_columns=['score'],data=[['a',True]])
                chart=w.Plotly({'data':[{'type':'scatter3d','x':[1],'y':[2],'z':[3]}]})
                artifact=w.Artifact('values','dataset')
                artifact.add(table,'evaluation');artifact.add(w.Table(columns=['plot'],data=[[chart]]),'trajectory')
                run.log_artifact(artifact)
            loaded=Api._from_store(sink,entity='e',project='p').artifact(artifact.id)
            recovered=loaded.get('evaluation')
            self.assertIsInstance(recovered,w.EvalTable)
            self.assertEqual(recovered.evaluation_roles,table.evaluation_roles)
            self.assertEqual(recovered.data,table.data)
            self.assertEqual(loaded.get('trajectory').data[0][0].spec,chart.spec)

    def test_joined_serialization_registry_and_duplicate_keys(self):
        from backprop.values import WBValue
        left=w.Table(columns=['id','x'],data=[['a',1],['a',2],['b',3]])
        right=w.Table(columns=['key','y'],data=[['a',4],['a',5],['c',6]])
        joined=w.JoinedTable(left,right,['id','key'])
        self.assertEqual(len(joined.materialize().data),6)
        self.assertIs(WBValue.type_mapping()['joined-table'],w.JoinedTable)
        self.assertEqual(joined.with_suffix('x'),'x.joined-table.json')
        self.assertFalse(joined.is_bound())
        with self.assertRaises(NotImplementedError):joined.get_media_subdir()
        with self.assertRaises(ValueError):w.JoinedTable(left,right,['id'])

    def test_plot_public_import_paths_and_keywords(self):
        install_wandb_aliases()
        from wandb.plot.custom_chart import CustomChartSpec
        from wandb.plot import visualize
        self.assertIs(CustomChartSpec,w.plot.CustomChartSpec)
        chart=visualize('wandb/feature_importances/v1',w.Table(columns=['feature_names','importances'],data=[['x',1]]))
        self.assertEqual(chart.table.data,[['x',1]])
        from wandb.plot.viz import VisualizeSpec
        self.assertIsInstance(chart.spec,VisualizeSpec)
        chart.set_key('importance')
        self.assertEqual(chart.spec.config_key,('_wandb','viz','importance'))
        self.assertEqual(chart.spec.config_value,{'id':'wandb/feature_importances/v1','historyFieldSettings':{'x-axis':'_step','key':'importance'}})
        spec=VisualizeSpec(name='local',key='k')
        self.assertEqual(spec.name,'local')
        with self.assertRaises(IndexError):w.Plotly.captions(media_items=[])
        table=w.EvalTable.from_json(json_obj={'columns':['a'],'data':[[1]]}, source_artifact=None)
        self.assertEqual(table.data,[[1]])

    def test_settings_login_timeout_bounds_foundry_verification(self):
        from unittest.mock import Mock,patch
        from backprop.login_session import authenticate_candidate
        settings=w.Settings(login_timeout=2)
        store=Mock()
        with patch('backprop.login_session.bounded_call') as bounded:
            result=authenticate_candidate(key='fixture-token',host=None,prompt=False,verify=True,timeout=None,
                settings=settings,load_config=lambda:{'host':'https://foundry.invalid'},native_store=lambda *args,**kwargs:store)
        bounded.assert_called_once_with(store.verify_credentials,2,'login')
        self.assertEqual(result.host,'https://foundry.invalid')
