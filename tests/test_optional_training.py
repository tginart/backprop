"""Exercise real frameworks in CI; missing local extras are explicit skips.

Architecture: tiny CPU training jobs -> selected public callbacks -> normal
MemorySink events -> public API readback. These verify integration wiring rather
than pretending that a callback import proves a training contract.
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


os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "1")


def available(name):
    return importlib.util.find_spec(name) is not None


class OptionalTraining(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'BACKPROP_DIR':self.tmp.name, 'OMP_NUM_THREADS':'1'})
        self.env.start()
        self.store = MemorySink()
        self.run = backprop.init(entity='e',project='p',_sink=self.store,dir=self.tmp.name,
            settings={'silent':True,'console':'off','x_disable_stats':True},reinit=True)
        self.api = Api._from_store(self.store,entity='e',project='p')

    def tearDown(self):
        self.run.finish(); self.env.stop(); self.tmp.cleanup()

    def assert_logged(self, key):
        self.run.flush()
        self.assertIn(key, self.api.run(self.run.id).summary)

    @unittest.skipUnless(available('tensorboard'), 'optional tensorboard is not installed')
    def test_tensorboard_real_event_file_and_duplicate_import(self):
        from tensorboard.summary.writer.event_file_writer import EventFileWriter
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.compat.proto.summary_pb2 import Summary
        from backprop.integration import tensorboard as tb
        directory = str(Path(self.tmp.name)/'tb')
        tb.patch(root_logdir=directory)
        try:
            writer = EventFileWriter(directory)
            writer.add_event(Event(step=12,summary=Summary(value=[Summary.Value(tag='loss',simple_value=0.25)])))
            writer.flush(); writer.close()
            self.assertIn(directory,tb.directories())
            # Closing a writer does not close its live watched stream. Import
            # this completed historical source explicitly, then deduplicate it.
            self.assertEqual(tb.sync(self.run,directory,final=False),0)
            self.run.flush()
            self.assertNotIn('loss',self.api.run(self.run.id).summary)
            self.assertEqual(tb.sync(self.run,directory,final=True),1)
            self.assertEqual(tb.sync(self.run,directory,final=True),0)
            self.run.flush()
            self.assertNotIn('loss',self.api.run(self.run.id).summary)
            # Producer flush publishes the import; the earlier public snapshot
            # stays empty until its API cache is explicitly refreshed.
            self.api.flush()
            self.assert_logged('loss')
            self.assertEqual(self.api.run(self.run.id).summary['global_step'],12)
        finally: tb.unpatch()

    @unittest.skipUnless(available('xgboost'), 'optional xgboost is not installed')
    def test_xgboost_training_and_checkpoint(self):
        import numpy as np
        import xgboost as xgb
        from backprop.integration.xgboost import WandbCallback
        x=np.arange(80).reshape(20,4); y=np.arange(20)%2
        data=xgb.DMatrix(x,label=y)
        xgb.train({'objective':'binary:logistic','nthread':1},data,num_boost_round=2,
                  evals=[(data,'train')],callbacks=[WandbCallback(log_model=True)],verbose_eval=False)
        self.assert_logged('train-logloss')
        self.assertTrue(list(self.api.run(self.run.id).logged_artifacts()))

    @unittest.skipUnless(available('lightgbm'), 'optional lightgbm is not installed')
    def test_lightgbm_training_and_checkpoint(self):
        import numpy as np
        import lightgbm as lgb
        from backprop.integration.lightgbm import wandb_callback, log_summary
        x=np.arange(80).reshape(20,4); y=np.arange(20)%2
        data=lgb.Dataset(x,label=y)
        model=lgb.train({'objective':'binary','verbosity':-1,'num_threads':1,'min_data_in_leaf':1},
            data,num_boost_round=2,valid_sets=[data],valid_names=['train'],callbacks=[wandb_callback()])
        log_summary(model,save_model_checkpoint=True)
        self.assert_logged('train_binary_logloss')
        self.assertTrue(list(self.api.run(self.run.id).logged_artifacts()))

    @unittest.skipUnless(available('catboost'), 'optional catboost is not installed')
    def test_catboost_training_and_checkpoint(self):
        import numpy as np
        from catboost import CatBoostClassifier
        from backprop.integration.catboost import WandbCallback, log_summary
        model=CatBoostClassifier(iterations=2,thread_count=1,verbose=False,allow_writing_files=False)
        model.fit(np.arange(80).reshape(20,4),np.arange(20)%2,callbacks=[WandbCallback()])
        log_summary(model,save_model_checkpoint=True)
        self.assert_logged('learn-Logloss')
        self.assertTrue(list(self.api.run(self.run.id).logged_artifacts()))

    @unittest.skipUnless(available('lightning'), 'optional lightning is not installed')
    def test_lightning_public_logger_and_graph_noop(self):
        from backprop.integration.lightning.fabric import WandbLogger
        logger=WandbLogger(experiment=self.run,save_dir=self.tmp.name)
        logger.log_hyperparams({'width':32})
        logger.log_metrics({'loss':0.5},step=3)
        logger.log_table('predictions',columns=['x'],data=[[1]])
        self.assert_logged('loss')
        self.assertEqual(self.api.run(self.run.id).config['width'],32)
        self.assertIsNone(logger.log_graph(None))
        self.assertIsNone(logger.log_graph(None, input_array=object()))

    @unittest.skipUnless(available('tensorflow'), 'optional tensorflow is not installed')
    def test_keras_training_metrics_and_checkpoint(self):
        import numpy as np
        import tensorflow as tf
        from backprop.integration.keras import WandbMetricsLogger, WandbModelCheckpoint
        model=tf.keras.Sequential([tf.keras.layers.Input((2,)),tf.keras.layers.Dense(1)])
        model.compile(optimizer='sgd',loss='mse')
        checkpoint=WandbModelCheckpoint(str(Path(self.tmp.name)/'model.keras'))
        model.fit(np.ones((4,2)),np.ones((4,1)),epochs=1,batch_size=4,verbose=0,
                  callbacks=[WandbMetricsLogger(),checkpoint])
        self.assert_logged('epoch/loss')
        self.assertTrue(list(self.api.run(self.run.id).logged_artifacts()))

    @unittest.skipUnless(available('stable_baselines3'), 'optional stable_baselines3 is not installed')
    def test_sb3_training_and_checkpoint(self):
        from stable_baselines3 import PPO
        from backprop.integration.sb3 import WandbCallback
        model=PPO('MlpPolicy','CartPole-v1',n_steps=8,batch_size=4,n_epochs=1,verbose=0,device='cpu')
        model.learn(total_timesteps=8,callback=WandbCallback(model_save_path=self.tmp.name,model_save_freq=4))
        self.run.flush()
        self.assertTrue(any(file.name.endswith('.zip') for file in self.api.run(self.run.id).files()))

    @unittest.skipUnless(available('tensorflow'), 'optional tensorflow is not installed')
    def test_keras_evaluation_callback_artifact_tables(self):
        from backprop.integration.keras import WandbEvalCallback
        class Evaluation(WandbEvalCallback):
            def add_ground_truth(self, logs=None): self.data_table.add_data(0,'hello')
            def add_model_predictions(self, epoch, logs=None):
                self.pred_table.add_data(epoch,self.data_table_ref.index_ref(0),'world')
        callback=Evaluation(data_table_columns=['id','input'],pred_table_columns=['epoch','source','prediction'])
        callback.on_train_begin(); callback.on_epoch_end(0)
        self.run.flush()
        artifacts=list(self.api.run(self.run.id).logged_artifacts())
        self.assertEqual({a.type for a in artifacts},{'dataset','evaluation'})
        self.assertEqual(len(list(self.api.run(self.run.id).used_artifacts())),1)

    @unittest.skipUnless(available('fastai'), 'optional fastai 1 is not installed')
    def test_fastai_legacy_training_and_checkpoint(self):
        import torch
        from torch.utils.data import TensorDataset,DataLoader
        from fastai.basic_data import DataBunch
        from fastai.basic_train import Learner
        from backprop.integration.fastai import WandbCallback
        dataset=TensorDataset(torch.ones(8,2),torch.ones(8,1))
        data=DataBunch(DataLoader(dataset,batch_size=4),DataLoader(dataset,batch_size=4),device=torch.device('cpu'),path=self.tmp.name)
        learner=Learner(data,torch.nn.Linear(2,1),loss_func=torch.nn.MSELoss())
        callback=WandbCallback(learner,log="all",input_type=None,save_model=True)
        learner.fit(1,callbacks=[callback])
        self.assertFalse(callback._backprop_watched)
        learner.fit(1,callbacks=[callback])
        self.assertFalse(callback._backprop_watched)
        self.assert_logged('train_loss')
        self.assertTrue(any(file.name.endswith('bestmodel.pth') for file in self.api.run(self.run.id).files()))

    @unittest.skipUnless(available('tensorboard'), 'optional tensorboard is not installed')
    def test_tensorboard_media_errors_and_restart_deduplication(self):
        import base64
        from tensorboard.compat.proto.summary_pb2 import Summary, SummaryMetadata
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.summary.writer.event_file_writer import EventFileWriter
        from tensorboard.util.tensor_util import make_tensor_proto
        from backprop.integration import tensorboard as tb
        text=Summary.Value(tag='text',tensor=make_tensor_proto(['hello']),metadata=SummaryMetadata(plugin_data=SummaryMetadata.PluginData(plugin_name='text')))
        self.assertEqual(tb.values(Summary(value=[text]))['text'],['hello'])
        unknown=Summary.Value(tag='unknown',tensor=make_tensor_proto(1),metadata=SummaryMetadata(plugin_data=SummaryMetadata.PluginData(plugin_name='unknown')))
        with self.assertRaises(NotImplementedError):tb.values(Summary(value=[unknown]))
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4aMAAAAASUVORK5CYII=')
        values=tb.values(Summary(value=[Summary.Value(tag='image',image=Summary.Image(encoded_image_string=png,width=1,height=1))]))
        self.run.log(values)
        directory=str(Path(self.tmp.name)/'events')
        writer=EventFileWriter(directory)
        writer.add_event(Event(step=2,summary=Summary(value=[Summary.Value(tag='metric',simple_value=3)])))
        writer.close()
        self.assertEqual(tb.sync(self.run,directory),1)
        # Simulate reconstructing the importer after process-local state is lost.
        del self.run._tensorboard_seen
        self.assertEqual(tb.sync(self.run,directory),0)
        original=EventFileWriter.__init__
        tb.patch(save=False,root_logdir=directory)
        try:
            with self.assertRaises(ValueError):tb.patch()
        finally:tb.unpatch()
        self.assertIs(EventFileWriter.__init__,original)
        self.assertEqual(tb.directories(),[])

    @unittest.skipUnless(available('lightning'), 'optional lightning is not installed')
    def test_lightning_media_argument_validation_and_properties(self):
        import numpy as np
        from backprop.integration.lightning.fabric import WandbLogger
        logger=WandbLogger(experiment=self.run,save_dir=self.tmp.name)
        self.assertIs(logger.experiment,self.run)
        for method in (logger.log_html,logger.log_image,logger.log_audio,logger.log_video):
            with self.assertRaises(TypeError):method('bad','not-a-list')
            with self.assertRaises(ValueError):method('bad',[None],caption=[])
        self.assertIsNone(logger.log_text('text',columns=['text'],data=[['hello']],step=1))
        logger.log_html('html',['<p>Hello</p>'],step=2)
        logger.log_image('images',[np.zeros((2,2,3),dtype=np.uint8)],caption=['sample'],step=3)
        logger.log_audio('audio',[np.zeros(16)],sample_rate=[8000],step=4)
        self.run.flush()
        summary=self.api.run(self.run.id).summary
        for key in ('text','html','images','audio'):self.assertIn(key,summary)

    @unittest.skipUnless(available('tensorflow'), 'optional tensorflow is not installed')
    def test_keras_metrics_callback_batch_and_epoch_contracts(self):
        import tensorflow as tf
        from backprop.integration.keras import WandbMetricsLogger
        model=tf.keras.Sequential([tf.keras.layers.Input((2,)),tf.keras.layers.Dense(1)])
        model.compile(optimizer='sgd',loss='mse')
        callback=WandbMetricsLogger(log_freq=2,initial_global_step=10)
        callback.set_model(model)
        self.assertIsNone(callback.on_train_batch_end(0,{'loss':2}))
        callback.on_train_batch_end(1,{'loss':1})
        callback.on_epoch_end(0,{'loss':1})
        self.run.flush()
        rows=list(self.api.run(self.run.id).scan_history())
        self.assertTrue(any('batch/loss' in row for row in rows))
        self.assertTrue(any('epoch/loss' in row for row in rows))
