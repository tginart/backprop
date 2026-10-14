"""Compare real artifact construction without a W&B or Foundry service.

Architecture: fresh inputs -> each actual public import -> constructor results,
errors and terminal warnings. Main caller: artifact constructor contract tests.
Why this exists: constructor validation differs from restoring a saved artifact;
no server-state fixtures are needed to check this boundary.
"""
import contextlib
import dataclasses
import datetime
import enum
import hashlib
import importlib
import inspect
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = sys.argv[1] == 'reference'
if REFERENCE:
    import wandb as w
    assert w.__version__ == '0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()

class Label(enum.Enum):
    FIRST = 'first-value'

@dataclasses.dataclass
class Record:
    count: int = 2
    when: object = datetime.date(2026, 1, 2)

class Display:
    def __str__(self):
        return 'display-value'


def portable_cases():
    """Produce fresh mutable inputs for independent constructor invocations."""
    result = {'default': (('dataset', 'data'), {})}
    for name, value in {
        'empty': '', 'space': 'a b', 'colon': 'a:v1', 'unicode': 'café',
        'slash': 'a/b', 'backslash': 'a\\b', 'null': 'a\0b', 'newline': 'a\nb',
        'trailing_newline': 'a\n', 'max': 'a' * 128, 'too_long': 'a' * 129,
        'dot': '.', 'dots': '..', 'valid': 'Model_A-1.2', 'int': 42,
        'none': None, 'bytes': b'data', 'path': Path('data'),
    }.items():
        result['name_' + name] = ((value, 'data'), {})
    for name, value in {'empty': '', 'slash': 'data/custom', 'colon': 'data:custom',
            'long': 'a' * 129, 'reserved': 'wandb-test', 'job': 'job',
            'int': 1, 'none': None, 'list': [], 'bytes': b'data'}.items():
        result['type_' + name] = (('dataset', value), {})
    for name, typ in [('run-table', 'run_table'), ('source-code', 'code'), ('user', 'run_table'), ('user-code', 'code')]:
        result['reserved_' + name] = ((name, typ), {})
    values = {
        'none': None, 'dict': {'a': 1}, 'json': '{"a":1}', 'empty_json': '',
        'bad_json': '{', 'json_list': '[1]', 'json_null': 'null', 'json_nan': '{"a":NaN}',
        'empty_list': [], 'int': 1, 'bool': False, 'hundred': {str(i):i for i in range(100)},
        'over_limit': {str(i):i for i in range(101)}, 'nan': {'a': float('nan')},
        'inf': {'a': float('inf')}, 'neg_inf': {'a': -float('inf')},
        'tuple': {'a': (1, 'b')}, 'set': {'a': {1, 2, 3}}, 'frozen': {'a': frozenset([1, 2])},
        'slice': {'a': slice(1, 7, 2)}, 'range': {'a': range(3)}, 'bytes': {'a': b'h\xc3\xa9'},
        'bad_bytes': {'a': b'\xff'}, 'decimal': {'a': Decimal('1.25')}, 'enum': {'a': Label.FIRST},
        'date': {'a': datetime.date(2026, 1, 2)}, 'datetime': {'a': datetime.datetime(2026, 1, 2, 3, 4, tzinfo=datetime.timezone.utc)},
        'path': {'a': Path('weights.bin')}, 'dataclass': {'a': Record()}, 'object': {'a': Display()},
        'callable': {'a': portable_cases}, 'class': {'a': Record}, 'complex': {'a': 1 + 2j},
        'keys': {1: 'int', None: 'none', False: 'false', 2.5: 'float'}, 'bad_key': {(1, 2): 'tuple'},
        'nested': {'a': [{'b': (datetime.date(2026, 1, 2), Label.FIRST)}]},
        'tag_like': {'number': {'_type': 'number', 'value': 'Infinity'}, 'timestamp': {'_type': 'timestamp', 'value': 12}},
    }
    for name, value in values.items():
        result['metadata_' + name] = (('dataset', 'data'), {'metadata': value})
    for name, value in {'true': True, 'false': False, 'string': 'yes', 'zero': 0, 'none': None}.items():
        result['incremental_' + name] = (('dataset', 'data'), {'incremental': value})
    for name, value in {'text': 'input', 'empty': '', 'false': False}.items():
        result['use_as_' + name] = (('dataset', 'data'), {'use_as': value})
    for name, value in {'md5': 'MD5', 'lowercase': 'xxh128', 'unknown': 'other', 'none': None, 'invalid': []}.items():
        result['digest_' + name] = (('dataset', 'data'), {'digest_algorithm': value})
    for name, value in {'text': 'description', 'empty': '', 'int': 42, 'dict': {'a': 1}}.items():
        result['description_' + name] = (('dataset', 'data'), {'description': value})
    result.update({
        'all_positional': (('dataset', 'data', 'description', {'a': 1}, True, 'input', None, 'MD5'), {}),
        'all_keyword': ((), {'name':'dataset', 'type':'data', 'description':'description', 'metadata':{'a':1}, 'incremental':True, 'use_as':'input', 'storage_region':None, 'digest_algorithm':'MD5'}),
        'missing_name': ((), {'type':'data'}), 'missing_type': (('data',), {}),
        'extra': (('dataset', 'data', None, None, False, None, None, 'MD5', 1), {}),
        'unknown': (('dataset', 'data'), {'unknown': True}),
        'duplicate': (('dataset', 'data'), {'name': 'again'}),
        'error_order': (('dataset', 'data'), {'metadata': [], 'digest_algorithm': []}),
    })
    return result


def optional_cases():
    """Exercise installed array/tensor packages only when explicitly requested."""
    result = {}
    if '--xxhash' in sys.argv:
        result['digest_xxh128'] = (('dataset','data'), {'digest_algorithm':'XXH128'})
    if '--numpy' in sys.argv:
        import numpy as np
        for name, value in {
            'scalar_int': np.int64(3), 'scalar_float': np.float32(1.25), 'scalar_nan': np.float32('nan'),
            'scalar_inf': np.float64('inf'), 'scalar_long': np.longdouble('1.25'),
            'scalar_complex': np.complex128(1+2j), 'scalar_bytes': np.bytes_('a'),
            'scalar_date': np.datetime64('2026-01-02'), 'empty': np.array([]),
            'one': np.array([1]), 'one_float': np.array([1.5]), 'one_nan': np.array([np.nan]),
            'matrix': np.arange(6).reshape(2,3), 'max_small': np.arange(32), 'large': np.arange(33),
        }.items(): result['numpy_' + name] = (('dataset','data'), {'metadata':{'a':value}})
        result['numpy_key'] = (('dataset','data'), {'metadata':{np.int64(1):'key'}})
    if '--torch' in sys.argv:
        import torch
        for name, value in {'scalar':torch.tensor(2.5), 'gradient':torch.tensor([1.,2.],requires_grad=True),
                'one':torch.tensor([1.]), 'matrix':torch.arange(6).reshape(2,3), 'large':torch.arange(33),
                'scalar_nan':torch.tensor(float('nan'))}.items():
            result['torch_' + name] = (('dataset','data'), {'metadata':{'a':value}})
    if '--jax' in sys.argv:
        import jax.numpy as jnp
        for name, value in {'scalar':jnp.array(2.5), 'one':jnp.array([1.]),
                'matrix':jnp.arange(6).reshape(2,3), 'large':jnp.arange(33),
                'scalar_nan':jnp.array(float('nan'))}.items():
            result['jax_' + name] = (('dataset','data'), {'metadata':{'a':value}})
    if '--tensorflow' in sys.argv:
        import tensorflow as tf
        for name, value in {'scalar':tf.constant(2.5), 'one':tf.constant([1.]), 'matrix':tf.reshape(tf.range(6),(2,3)),
                'variable':tf.Variable([1.,2.]), 'large':tf.range(33)}.items():
            result['tensorflow_' + name] = (('dataset','data'), {'metadata':{'a':value}})
    return result


def observe(cls, args, kwargs):
    """Record warnings at their output boundary without replacing validation."""
    messages = []
    def warn(message, **options): messages.append({'text':str(message), **options})
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(w, 'termwarn', warn))
        if REFERENCE:
            stack.enter_context(patch.object(importlib.import_module('wandb.sdk.artifacts.artifact'), 'termwarn', warn))
        try:
            value = cls(*args, **kwargs)
            result = {'value': {'name':value.name, 'type':value.type, 'description':value.description,
                'metadata':value.metadata, 'incremental':value.incremental, 'state':value.state,
                'digest_algorithm':value.digest_algorithm.value, 'is_draft':value.is_draft(), 'use_as':value.use_as}}
        except Exception as error:
            result = {'error':type(error).__name__, 'message':str(error)}
    return {**result, 'warnings':messages}

def observe_assignment(cls, field, replacement, stage):
    """Check assignment validation in genuine draft and finalized objects.

    Main caller: the constructor matrix. Committed fixtures inject only the
    server state needed for these setters; metadata conversion is never mocked.
    """
    value = cls('dataset', 'data', description='before', metadata={'before':1})
    if stage != 'draft':
        value.finalize()
    if stage == 'committed':
        if REFERENCE:
            from wandb.sdk.artifacts.artifact import ArtifactState
            value._state = ArtifactState.COMMITTED
        else:
            value._logged = value._committed = True
    try:
        setattr(value, field, replacement)
        result = {'value':getattr(value, field)}
    except Exception as error:
        result = {'error':type(error).__name__, 'message':str(error), 'unchanged':getattr(value, field)}
    return result


out = {'imports':{}}
for prefix in ['wandb', 'wandb.sdk', 'wandb.sdk.artifacts.artifact']:
    cls = importlib.import_module(prefix).Artifact
    cases = portable_cases() | optional_cases()
    out['imports'][prefix] = {name:observe(cls,*case) for name,case in cases.items()}
    for stage in ['draft','finalized','committed']:
        for name, (_, kwargs) in cases.items():
            if name.startswith(('metadata_', 'numpy_', 'torch_', 'tensorflow_', 'jax_', 'description_')):
                field = 'metadata' if 'metadata' in kwargs else 'description'
                out['imports'][prefix][f'assignment_{stage}_{name}'] = observe_assignment(cls, field, kwargs[field], stage)
    out['imports'][prefix]['signature'] = [(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls).parameters.values()]
if REFERENCE:
    out['source_hashes'] = {}
    for module in ['wandb.sdk.artifacts.artifact','wandb.sdk.artifacts._validators','wandb.util']:
        actual = Path(importlib.import_module(module).__file__)
        expected = ROOT/'.reference/wandb/source'/Path(*module.split('.')).with_suffix('.py')
        assert actual.read_bytes() == expected.read_bytes(), module
        out['source_hashes'][module] = hashlib.sha256(actual.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True))
