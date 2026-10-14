"""Graph file contracts and durable snapshot recovery.

Architecture: isolated pinned/native probes -> public binding and decoding ->
real journals, artifacts, replay and process recovery. Main callers: SDK suite
and optional framework CI. No environments or hosted resources are created.
"""
import copy,importlib.metadata,importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.apis.public.replay import reduce_events
from backprop.transport import sync
ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}

def make_graph():
    graph=w.Graph();a=graph.add_node(id='a',name='original');b=graph.add_node(id='b',name='output');graph.add_edge(a,b)
    return graph


def paired_probe(case):
    """Run actual public APIs under each installed package, retaining CI evidence.

    Main callers: file matrix and Keras matrix tests. Only output is compared;
    each subprocess imports its own SDK and optional framework independently.
    """
    try:reference=importlib.metadata.distribution('wandb')
    except importlib.metadata.PackageNotFoundError:raise unittest.SkipTest('Pinned W&B wheel unavailable')
    if reference.version!='0.30.0':raise AssertionError(reference.version)
    values=[]
    for mode in ['reference','native']:
        result=subprocess.run([sys.executable,'-B',str(ROOT/f'tests/{case}_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=120,
            env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
        if result.returncode:raise AssertionError(result.stderr)
        values.append(json.loads(result.stdout))
        if os.environ.get('CI'):
            out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'{case}-{mode}.json').write_text(result.stdout)
    return values

class GraphFileContracts(unittest.TestCase):
    def setUp(self):
        downloads=tempfile.TemporaryDirectory();self.addCleanup(downloads.cleanup)
        destination=patch.object(w.Artifact,'_default_root',lambda artifact,**kwargs:str(Path(downloads.name)/artifact._id))
        destination.start();self.addCleanup(destination.stop)

    def test_pinned_file_matrix_and_explicit_repairs(self):
        reference,native=paired_probe('graph_file')
        self.assertEqual(reference['portable'],native['portable'])
        for module,observed in reference['repairs'].items():
            self.assertEqual(observed['pprint'],{'error':'AttributeError'})
            self.assertEqual(observed['rebound'],{'published':1,'same_path':False,'inside_owner':False})
            self.assertEqual(observed['decoded'],{'format_is_filename':True,'state':[False,False],'nodes':0,'edges':0})
            self.assertEqual(native['repairs'][module]['decoded'],{'format_is_filename':False,'state':[True,False],'nodes':2,'edges':1})
            self.assertIsNone(native['repairs'][module]['pprint']['value']['return'])
            self.assertEqual(native['repairs'][module]['rebound'],{'published':2,'same_path':True,'inside_owner':True})

    def test_rebind_failed_publication_late_nodes_and_owner_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();run=w.Run(id='graph-files',entity='e',project='graphs',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
            self.addCleanup(run.finish);graph=make_graph()
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):graph.bind_to_run(run,'network',0,id_='model')
            graph.bind_to_run(run,'network',0,id_='model');first_path=graph._path
            first=copy.deepcopy(graph._to_graph_json());run.log({'graph':graph});run.flush()
            other=w.Run(id='secondary',entity='e',project='graphs',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
            self.addCleanup(other.finish)
            graph.add_node(id='late');second=copy.deepcopy(graph._to_graph_json())
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):graph.bind_to_run(other,'ignored',1)
            graph.bind_to_run(other,'ignored',1)
            self.assertEqual(graph._path,first_path);self.assertIs(graph._run,run)
            self.assertEqual(json.loads(Path(first_path).read_bytes()),second)
            with self.assertRaises(AssertionError):graph.to_json(other)
            # Native logging must snapshot new values even without a public bind.
            graph.add_node(id='unbound-change');third=copy.deepcopy(graph._to_graph_json());run.log({'graph':graph})
            artifact=w.Artifact('networks','model');artifact.add(graph,'architecture');run.log_artifact(artifact)
            publish=sink.publish
            def lost_ack(events):publish(events);raise ConnectionError('lost acknowledgement')
            with patch.object(sink,'publish',side_effect=lost_ack):
                with self.assertRaises(w.Error):run.flush()
            run.flush();run.finish();other.finish()
            self.assertEqual(reduce_events(sink.events),reduce_events(list(reversed(sink.events))+sink.events))
            api=w.Api._from_store(sink,entity='e',project='graphs');reader=api.run(run.id);rows=list(reader.scan_history())
            self.assertEqual([json.loads(sink.download_file(row['graph'])) for row in rows],[first,third])
            self.assertEqual(reader.file(graph.to_json(run)['path']).size,len(Path(first_path).read_bytes()))
            saved=api.artifact('networks:latest')
            with patch.object(sink,'download_file',side_effect=FileNotFoundError('not yet visible')):
                with self.assertRaises(FileNotFoundError):saved.get('architecture')
            restored=saved.get('architecture');self.assertEqual(restored._to_graph_json(),third)
            self.assertTrue(restored.file_is_set());self.assertFalse(restored.is_bound())
            copied=w.Artifact('copy','model');descriptor=restored.to_json(copied)
            self.assertEqual(w.Graph.from_json(descriptor,copied)._to_graph_json(),third)

    def test_decoder_duplicate_ids_and_invalid_topology(self):
        from backprop.compatibility import install_wandb_aliases
        import importlib
        install_wandb_aliases()
        for module in ['wandb','wandb.data_types','wandb.sdk.data_types.graph']:
            cls=importlib.import_module(module).Graph
            value={'format':'custom','nodes':[{'id':'a','name':'old'},{'id':'a','name':'new'}],'edges':[['a','a']]}
            graph=cls.from_json(value,None)
            self.assertEqual(graph._to_graph_json(),value);self.assertEqual(graph.edges[0].from_node.name,'new')
            self.assertTrue(graph.file_is_set());self.assertFalse(graph.is_bound())
            for invalid,error in [({},KeyError),({'format':'x','nodes':[],'edges':[['missing','other']]},KeyError),({'format':'x','nodes':[None],'edges':[]},TypeError),({'format':'x','nodes':[{'id':[]}],'edges':[]},TypeError),({'format':'x','nodes':[],'edges':[[1]]},ValueError)]:
                with self.assertRaises(error):cls.from_json(invalid,None)

    def test_independent_process_journal_retains_each_graph_snapshot(self):
        script="""
import json,sys
from pathlib import Path
import backprop as w
from test_optional_graph_files import make_graph,SETTINGS
run=w.Run(id='recovered-graph',entity='e',project='graphs',dir=sys.argv[1],mode='offline',settings=SETTINGS)
graph=make_graph();graph.bind_to_run(run,'network',0,id_='model');run.log({'graph':graph})
graph.add_node(id='late');graph.bind_to_run(run,'network',1);run.log({'graph':graph});run.finish()
print(json.dumps({'journal':run.sync_dir,'public':graph.to_json(run)}))
"""
        with tempfile.TemporaryDirectory() as directory:
            p=subprocess.run([sys.executable,'-B','-c',script,directory],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
            self.assertEqual(p.returncode,0,p.stderr);record=json.loads(p.stdout);sink=w.MemorySink()
            with patch.object(sink,'publish',side_effect=ConnectionError('disconnected')):
                with self.assertRaises(w.Error):sync(record['journal'],sink=sink)
            sync(record['journal'],sink=sink);sync(record['journal'],sink=sink)
            reader=w.Api._from_store(sink,entity='e',project='graphs').run('recovered-graph')
            self.assertEqual(reader.state,'finished')
            graphs=[json.loads(sink.download_file(row['graph'])) for row in reader.scan_history()]
            self.assertEqual([len(graph['nodes']) for graph in graphs],[2,3])

@unittest.skipUnless(importlib.util.find_spec('tensorflow'),'Optional TensorFlow runtime is required')
class KerasGraphMatrix(unittest.TestCase):
    def test_pinned_real_model_matrix(self):
        reference,native=paired_probe('keras_graph');self.assertEqual(reference,native)
        self.assertEqual(len(native),10)
        # Pinned Keras 3 rejects functional models; record that actual boundary.
        import tensorflow as tf
        if int(getattr(tf.keras,'__version__','2').split('.')[0])<3:
            for value in native['functional'].values():self.assertIn('value',value)

@unittest.skipUnless(importlib.util.find_spec('tensorflow'),'Optional TensorFlow runtime is required')
class KerasNodeMatrix(unittest.TestCase):
    def test_pinned_layer_conversion_and_subclass_contract(self):
        reference,native=paired_probe('keras_node')
        self.assertEqual(reference['imports'],native['imports'])
        self.assertEqual(reference['versions'],native['versions'])
        for prefix,group in native['imports'].items():
            self.assertEqual(group['signature'],[['layer','POSITIONAL_OR_KEYWORD','required']])
            self.assertEqual(len(group['cases']),16)
            for name in ['dense','frozen','input','conv','normalization','embedding','nested','shared_shapes','multiple_outputs','derived']:
                observed=group['cases'][name]
                self.assertIn('value',observed,(prefix,name))
                self.assertTrue(all(observed[key] for key in ['class_preserved','fresh','empty_edges','input_unchanged']))
            self.assertEqual(group['cases']['dense']['value']['num_parameters'],8)
            self.assertEqual(group['cases']['conv']['value']['num_parameters'],40)
            self.assertEqual(group['cases']['normalization']['value']['num_parameters'],12)
            self.assertEqual(group['cases']['embedding']['value']['num_parameters'],10)
            self.assertEqual(group['cases']['shape_error']['error'],'ValueError')
            self.assertEqual(group['cases']['unbuilt']['error'],'ValueError')

if __name__=='__main__':unittest.main()
