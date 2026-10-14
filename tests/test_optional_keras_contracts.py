"""Real Keras callback contracts plus native persistence and model recovery.

Architecture: isolated pinned/native callback observations -> real CPU training
and checkpoint load -> Foundry-shaped journals/artifacts -> retries and replay.
Main caller: the existing optional-framework CI matrix. TensorFlow is optional;
the user's local environment is never installed or modified by these tests.
"""
import copy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch

import backprop as w

ROOT=Path(__file__).resolve().parents[1]
os.environ.setdefault('TF_NUM_INTEROP_THREADS','1')
os.environ.setdefault('TF_NUM_INTRAOP_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','1')
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}

@unittest.skipUnless(importlib.util.find_spec('tensorflow'),'optional TensorFlow is not installed')
class KerasContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'BACKPROP_DIR':self.temp.name});env.start();self.addCleanup(env.stop)
        self.entity,self.project='e','keras-contracts'
        self.store=w.MemorySink();self.api=w.Api._from_store(self.store,entity=self.entity,project=self.project)
        import tensorflow as tf
        self.addCleanup(tf.keras.backend.clear_session)

    def run_context(self,identity='keras',**kwargs):
        return w.init(id=identity,entity=self.entity,project=self.project,dir=self.temp.name,
            _sink=self.store,_deployment={},settings=SETTINGS,reinit=True,**kwargs)

    def model(self):
        import tensorflow as tf
        tf.keras.backend.clear_session();tf.keras.utils.set_random_seed(7)
        model=tf.keras.Sequential([tf.keras.layers.Input(shape=(2,)),tf.keras.layers.Dense(1,name='prediction')])
        model.compile(optimizer=tf.keras.optimizers.SGD(.125),loss='mse')
        return model

    def test_pinned_callback_parameters_methods_errors_outputs_and_every_alias(self):
        reference=importlib.metadata.distribution('wandb');self.assertEqual(reference.version,'0.30.0')
        observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/keras_callbacks_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=240,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                output=ROOT/'test-results';output.mkdir(exist_ok=True)
                (output/f'keras-callbacks-{mode}.json').write_text(result.stdout)
        self.assertEqual(observations[0]['portable'],observations[1]['portable'])
        self.assertEqual(len(observations[0]['portable']),4)
        for prefix in observations[0]['repairs']:
            original=observations[0]['repairs'][prefix];native=observations[1]['repairs'][prefix]
            for case in ['best','threshold']:
                self.assertTrue(any(item.get('error')=='FileNotFoundError' for item in original[case]['result']['value']['hooks']))
                self.assertTrue(all(item=={'value':None} for item in native[case]['result']['value']['hooks']))
                self.assertEqual(original[case]['result']['value']['files'],native[case]['result']['value']['files'])
            self.assertIn('value',native['path']['result'])
            if 'error' in original['path']['result']:
                self.assertEqual(original['path']['result']['error'],'AttributeError')
            else:
                self.assertEqual(original['path'],native['path'])
            self.assertEqual(original['storage_error']['result'],{'value':None})
            self.assertEqual(native['storage_error']['result']['error'],'ValueError')
            self.assertEqual(native['storage_error']['result']['message'],'storage rejected artifact')

    def test_real_training_repeated_sessions_checkpoint_reload_and_table_relationships(self):
        import numpy as np
        import tensorflow as tf
        from backprop.integration.keras import WandbMetricsLogger,WandbModelCheckpoint,WandbEvalCallback
        data=np.ones((4,2),dtype=np.float32);labels=np.ones((4,1),dtype=np.float32)
        class Evaluation(WandbEvalCallback):
            def add_ground_truth(self,logs=None):
                for index in range(4):self.data_table.add_data(index,float(labels[index,0]))
            def add_model_predictions(self,epoch,logs=None):
                predictions=self.model(data,training=False).numpy()
                for index in range(4):self.pred_table.add_data(epoch,self.data_table_ref.index_ref(index),float(predictions[index,0]))
        dataset=tf.data.Dataset.from_tensor_slices((data,labels)).batch(2)
        options=tf.data.Options();options.threading.private_threadpool_size=1;dataset=dataset.with_options(options)
        self.entity,self.project='backprop-tests','sdk-expansion-validation'
        self.api=w.Api._from_store(self.store,entity=self.entity,project=self.project)
        with self.run_context('keras-ci-'+uuid.uuid4().hex[:10]) as run:
            model=self.model();metrics=WandbMetricsLogger(log_freq=2,initial_global_step=10)
            checkpoint=WandbModelCheckpoint(Path(self.temp.name)/'epoch-{epoch:02d}.keras',monitor='loss',save_best_only=True,mode='min')
            evaluation=Evaluation(['id','truth'],['epoch','source','prediction'])
            model.fit(dataset,epochs=2,verbose=0,callbacks=[metrics,checkpoint,evaluation])
            self.assertEqual(metrics.global_step,14)
            model.fit(dataset,initial_epoch=2,epochs=3,verbose=0,callbacks=[metrics,checkpoint,evaluation])
            self.assertEqual(metrics.global_step,16)
            run.flush();expected=model(data,training=False).numpy()
            run_id=run.id
        rows=self.api.run(run_id).history(pandas=False)
        self.assertTrue(any('batch/loss' in row for row in rows));self.assertTrue(any('epoch/loss' in row for row in rows))
        saved=self.api.artifact('run_'+run_id+'_model:latest')
        filename=saved.file(root=str(Path(self.temp.name)/'reload'))
        restored=tf.keras.models.load_model(filename)
        np.testing.assert_allclose(restored(data,training=False).numpy(),expected,rtol=1e-5,atol=1e-6)
        predictions=self.api.artifact('run_'+run_id+'_pred:latest').get('eval_data')
        self.assertEqual(len(predictions.data),4)
        self.assertEqual([row[0] for row in predictions.data],[2]*4)
        self.assertEqual([int(row[1]) for row in predictions.data],list(range(4)))
        self.assertGreaterEqual(len(list(self.api.run(run_id).used_artifacts())),1)
        if os.environ.get('CI'):
            output=ROOT/'test-results';output.mkdir(exist_ok=True)
            with zipfile.ZipFile(output/'keras-training-journal.zip','w',zipfile.ZIP_DEFLATED) as archive:
                archive.write(run._journal.event_path,'events.jsonl')
                for path in sorted((run._journal.directory/'files').rglob('*')):
                    if path.is_file():archive.write(path,path.relative_to(run._journal.directory).as_posix())
            artifacts=list(self.api.run(run_id).logged_artifacts())
            expected={'entity':self.entity,'project':self.project,'run_id':run_id,'state':'finished',
                'history':[{k:v for k,v in row.items() if k not in ['_timestamp','_runtime']} for row in rows],
                'artifacts':[{'id':item.id,'files':[{'name':entry.path,'sha256':entry.sha256} for entry in item.manifest.entries.values()]} for item in artifacts],
                'checkpoint_id':saved.id,'checkpoint_file':next(iter(saved.manifest.entries)),
                'checkpoint_sha256':next(iter(saved.manifest.entries.values())).sha256,
                'evaluation_rows':4,'evaluation_epoch':2,'global_step':metrics.global_step,
                'used_artifacts':len(list(self.api.run(run_id).used_artifacts()))}
            (output/'keras-training-replay.json').write_text(json.dumps({'events':self.store.events,'expected':expected},indent=2)+'\n')


    def test_weights_only_batch_checkpoint_and_custom_evaluation_names(self):
        import numpy as np
        from backprop.integration.keras import WandbModelCheckpoint,WandbEvalCallback
        class Evaluation(WandbEvalCallback):
            def add_ground_truth(self,logs=None):pass
            def add_model_predictions(self,epoch,logs=None):pass
        with self.run_context('options') as run:
            model=self.model();callback=WandbModelCheckpoint(str(Path(self.temp.name)/'batch-{batch:02d}.weights.h5'),save_weights_only=True,save_freq=2)
            callback.set_model(model);callback.on_epoch_begin(0)
            for batch in range(3):callback.on_train_batch_end(batch,{'loss':1})
            evaluation=Evaluation([],[]);evaluation.init_data_table(['id']);evaluation.data_table.add_data(1)
            evaluation.log_data_table(name='custom-data',type='custom-dataset',table_name='ground-truth')
            evaluation.init_pred_table(['prediction']);evaluation.pred_table.add_data(.75)
            evaluation.log_pred_table(type='custom-evaluation',table_name='predictions',aliases=['best','reviewed'])
            run.flush()
            weights=self.api.artifact('run_'+run.id+'_model:latest').file(root=str(Path(self.temp.name)/'weights'))
            restored=self.model();restored.load_weights(weights)
            np.testing.assert_allclose(restored(np.ones((1,2))).numpy(),model(np.ones((1,2))).numpy())
            self.assertEqual(self.api.artifact('custom-data:latest').get('ground-truth').data,[[1]])
            self.assertEqual(self.api.artifact('run_'+run.id+'_pred:best').get('predictions').data,[[.75]])

    def test_checkpoint_storage_error_propagates_then_retry_and_replay_recover(self):
        from backprop.integration.keras import WandbModelCheckpoint
        with self.run_context('retry') as run:
            callback=WandbModelCheckpoint(str(Path(self.temp.name)/'retry.keras'),monitor='loss');callback.set_model(self.model())
            with patch.object(w,'log_artifact',side_effect=ValueError('storage rejected artifact')):
                with self.assertRaisesRegex(ValueError,'storage rejected artifact'):callback.on_epoch_end(0,{'loss':1})
            callback.on_epoch_end(0,{'loss':1});before=copy.deepcopy(self.store.events)
            publish=self.store.publish
            def lose_ack(events):publish(events);raise OSError('lost acknowledgement')
            with patch.object(self.store,'publish',side_effect=lose_ack):
                with self.assertRaises(w.Error):run.flush()
            run.flush();identity=run.id
        final=copy.deepcopy(self.store.events);self.store.events[:]=before
        self.assertEqual(list(self.api.run(identity).logged_artifacts()),[])
        self.store.events[:]=list(reversed(final))+final
        artifacts=list(self.api.run(identity).logged_artifacts());self.assertEqual(len(artifacts),1)
        filename=artifacts[0].file(root=str(Path(self.temp.name)/'recovered'))
        self.assertTrue(Path(filename).is_file())
        with self.run_context(identity,resume='must') as run:run.log({'resumed':True})
        self.assertEqual(len(list(self.api.run(identity).logged_artifacts())),1)

    def test_subprocess_crash_preserves_callback_checkpoint_before_publication(self):
        script='''
import json,os,sys
from pathlib import Path
import tensorflow as tf
import backprop as w
from backprop.integration.keras import WandbModelCheckpoint
run=w.init(id='crashed',entity='e',project='keras-contracts',dir=sys.argv[1],_sink=w.MemorySink(),_deployment={},settings=json.loads(sys.argv[2]))
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(2,)),tf.keras.layers.Dense(1)])
model.compile(optimizer='sgd',loss='mse')
callback=WandbModelCheckpoint(str(Path(sys.argv[1])/'crash.keras'),monitor='loss');callback.set_model(model);callback.on_epoch_end(0,{'loss':1})
print(str(run._journal.directory),flush=True);os._exit(0)
'''
        result=subprocess.run([sys.executable,'-B','-c',script,self.temp.name,json.dumps(SETTINGS)],
            cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
        self.assertEqual(result.returncode,0,result.stderr)
        journal=result.stdout.strip().splitlines()[-1];count=w.sync(journal,sink=self.store)
        self.assertGreater(count,0);self.assertEqual(w.sync(journal,sink=self.store),count)
        artifact=self.api.artifact('run_crashed_model:latest')
        import tensorflow as tf
        self.assertEqual(tf.keras.models.load_model(artifact.file(root=str(Path(self.temp.name)/'crash-read'))).output_shape,(None,1))

if __name__=='__main__':unittest.main()
