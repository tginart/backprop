"""Compare real Keras graph conversion across model shapes and public aliases.

Architecture: installed TensorFlow/Keras -> actual model construction/build ->
unchanged pinned/native Graph.from_keras -> values or public errors. Main caller:
optional Graph file CI. Keras 3 outcomes are compared, including upstream errors;
there is no simulated framework or hosted W&B boundary in this probe.
"""
import importlib,json,sys
import tensorflow as tf
if sys.argv[1]=='reference':
 import wandb as w
 assert w.__version__=='0.30.0'
else:
 import backprop as w
 from backprop.compatibility import install_wandb_aliases
 install_wandb_aliases()
def model(kind):
 tf.keras.backend.clear_session()
 if kind=='none':return None
 if kind=='invalid':return object()
 if kind.startswith('subclass'):
  class Custom(tf.keras.Model):
   def __init__(self):super().__init__(name='custom');self.layer=tf.keras.layers.Dense(2,name='subdense')
   def call(self,x):return self.layer(x)
  value=Custom()
  if kind=='subclass_built':value(tf.zeros((1,3)))
  return value
 if kind.startswith('sequential'):
  value=tf.keras.Sequential([tf.keras.layers.Dense(2,name='dense'),tf.keras.layers.Dense(1,name='out')],name='sequential')
  if kind=='sequential_built':value(tf.zeros((1,3)))
  return value
 left=tf.keras.Input((3,),name='left');right=tf.keras.Input((3,),name='right')
 if kind=='nested':
  inside=tf.keras.Input((3,),name='inside');nested=tf.keras.Model(inside,tf.keras.layers.Dense(2,name='nested_out')(inside),name='nested')
  return tf.keras.Model(left,nested(left))
 if kind=='shared':
  dense=tf.keras.layers.Dense(2,name='shared');a=dense(left);b=dense(right);return tf.keras.Model([left,right],tf.keras.layers.Add(name='merge')([a,b]))
 if kind=='multi_output':return tf.keras.Model(left,[tf.keras.layers.Dense(1,name='one')(left),tf.keras.layers.Dense(2,name='two')(left)])
 merged=tf.keras.layers.Concatenate(name='merge')([left,right]);return tf.keras.Model([left,right],tf.keras.layers.Dense(1,name='out')(merged))
out={}
for kind in ['functional','sequential_built','sequential_unbuilt','nested','shared','multi_output','subclass_built','subclass_unbuilt','none','invalid']:
 value=model(kind);out[kind]={}
 for module in ['wandb','wandb.data_types','wandb.sdk.data_types.graph']:
  cls=importlib.import_module(module).Graph
  try:
   graph=cls.from_keras(value);out[kind][module]={'value':graph._to_graph_json(),'format':graph.format,'loaded':graph.loaded}
  except Exception as error:out[kind][module]={'error':type(error).__name__}
print(json.dumps(out,sort_keys=True))
