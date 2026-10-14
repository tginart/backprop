"""Compare real Keras layer-to-Node conversion at each public import.

Architecture: installed TensorFlow layers -> pinned/native classmethod -> node
fields, errors and input immutability. Main caller: optional graph CI. Every
case constructs a fresh real layer, and derived Node classes verify that the
classmethod respects cls. No training service or simulated framework is used.
"""
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import tensorflow as tf

ROOT=Path(__file__).resolve().parents[1]
REFERENCE=sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def layer(kind):
    """Build deterministic layer shapes while leaving unbuilt cases unbuilt."""
    tf.keras.backend.clear_session()
    if kind=='none':return None
    if kind=='invalid':return object()
    if kind=='input':return tf.keras.layers.InputLayer(input_shape=(3,),name='input')
    if kind=='nested':
        inputs=tf.keras.Input((3,),name='input')
        return tf.keras.Model(inputs,tf.keras.layers.Dense(2,name='dense')(inputs),name='nested')
    if kind=='shared_shapes':
        value=tf.keras.layers.Dense(2,name='shared')
        value(tf.keras.Input((3,),name='vector'));value(tf.keras.Input((None,3),name='sequence'))
        return value
    if kind=='multiple_outputs':
        value=tf.keras.layers.Lambda(lambda x:(x,x[:,:1]),name='outputs');value(tf.keras.Input((3,),name='input'));return value
    if kind=='shape_error':
        class BrokenShape(tf.keras.layers.Layer):
            @property
            def output_shape(self):raise ValueError('shape unavailable')
        value=BrokenShape(name='broken');value.build((None,3));return value
    if kind=='conv':
        value=tf.keras.layers.Conv2D(4,3,name='conv');value(tf.zeros((1,8,8,1)));return value
    if kind=='normalization':
        value=tf.keras.layers.BatchNormalization(name='normalization');value(tf.zeros((1,3)));return value
    if kind=='embedding':
        value=tf.keras.layers.Embedding(5,2,name='embedding');value(tf.zeros((1,3),dtype='int32'));return value
    value=tf.keras.layers.Dense(2,name='dense',trainable=kind!='frozen')
    if kind!='unbuilt':value(tf.keras.Input((3,),name='input'))
    return value


def observe(cls,kind):
    """Capture the public result and ensure the supplied layer is untouched.

    Main caller: the per-import matrix. Error messages are compared inside the
    same TensorFlow runtime; framework-specific shape behavior remains visible.
    """
    value=layer(kind)
    before=[(id(v),v.shape) for v in getattr(value,'weights',[])]
    try:
        if kind=='missing_argument':node=cls.from_keras()
        elif kind=='unknown_argument':node=cls.from_keras(value,unknown=True)
        else:node=cls.from_keras(value)
        result={'value':node.to_json(),'class_preserved':type(node) is cls,
                'fresh':cls.from_keras(value) is not node,'empty_edges':not node.in_edges and not node.out_edges,
                'input_unchanged':before==[(id(v),v.shape) for v in getattr(value,'weights',[])]}
    except Exception as error:result={'error':type(error).__name__,'message':str(error)}
    return result


result={'versions':{'tensorflow':tf.__version__,'keras':getattr(tf.keras,'__version__','2')},'imports':{}}
for module in ['wandb.data_types','wandb.sdk.data_types.graph']:
    cls=importlib.import_module(module).Node
    class DerivedNode(cls):
        pass
    signature=[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.from_keras).parameters.values()]
    result['imports'][module]={'signature':signature,'cases':{}}
    for kind in ['dense','unbuilt','frozen','input','conv','normalization','embedding','nested','shared_shapes','multiple_outputs','shape_error','none','invalid','missing_argument','unknown_argument']:
        result['imports'][module]['cases'][kind]=observe(cls,kind)
    result['imports'][module]['cases']['derived']=observe(DerivedNode,'dense')
if REFERENCE:
    source=Path(importlib.import_module('wandb.sdk.data_types.graph').__file__)
    expected=ROOT/'.reference/wandb/source/wandb/sdk/data_types/graph.py'
    assert source.read_bytes()==expected.read_bytes()
    result['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
print(json.dumps(result,sort_keys=True,default=str))
