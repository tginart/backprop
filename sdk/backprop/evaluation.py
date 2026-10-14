"""Prepare evaluation rows without changing the caller's raw Table.

Architecture: validate top-level additions -> normalize at publication -> freeze
column roles, examples and score summaries -> existing Table/Artifact encoder.
Main callers: EvalTable and Encoder. Foundry media retain their original files
and captions instead of requiring Weave's separate media objects or service.
The validation and normalization order follows W&B 0.30.0 eval_table.py and
integration/weave/media_adapters.py (see THIRD_PARTY_NOTICES.md).
"""
from __future__ import annotations
from datetime import date,datetime,timezone
import copy
import enum
import math
import sys
from .tables import Table
from .values import WBValue
from .media import Image,Audio,Video


def validate_value(value,column,mode):
    """Check additions at the same top-level boundary as the pinned EvalTable.

    Main callers: add_data and add_column. Nested containers are checked during
    logging, so rejected publication cannot modify the original Python cells.
    """
    if isinstance(value,Table):raise TypeError(f'Column {column!r} contains a {type(value).__name__}; EvalTable does not support nested Tables (or EvalTables) as cell values.')
    if isinstance(value,WBValue) and not isinstance(value,(Image,Audio,Video)) and mode=='raise':
        raise TypeError(f'Column {column!r} contains unsupported wandb value type {type(value).__name__!r}.')


def _stub(value,variant=''):
    import backprop
    suffix=f' {variant}' if variant else ''
    backprop.termwarn(f'wandb.{type(value).__name__}{suffix} values are not yet supported by EvalTable. They will be logged as placeholder strings.',repeat=False)
    return f'[wandb.{type(value).__name__}{suffix} not yet supported]'


def normalize(value,column,mode,*,nested=False,inside_sequence=False):
    """Apply pinned cell normalization, keeping native media as the file adapter.

    Main callers: capture. Normalizing before schema inference keeps stub strings
    and normalized dates consistent with the snapshot's actual declared types.
    """
    if isinstance(value,Table):raise TypeError(f'Column {column!r} contains a nested {type(value).__name__}; EvalTable does not support nested Tables.')
    if isinstance(value,WBValue):
        if nested and inside_sequence:
            if mode=='raise':raise TypeError(f'Column {column!r} contains nested wandb value type {type(value).__name__!r}.')
            return _stub(value,'nested value')
        validate_value(value,column,mode)
        if isinstance(value,(Image,Audio,Video)):
            if getattr(value,'_reference_uri',None) or value.path_is_reference(getattr(value,'_path',None)):
                if mode=='raise':raise TypeError(f'Unsupported external media reference in column {column!r}.')
                return _stub(value,'external reference')
            from .evaluation_media import local_value
            return local_value(value, column)
        return _stub(value)
    if isinstance(value,dict):
        np=sys.modules.get('numpy')
        return {(key.item() if np is not None and isinstance(key,np.generic) else key):normalize(item,column,mode,nested=True,inside_sequence=inside_sequence) for key,item in value.items()}
    if isinstance(value,(list,tuple)):return [normalize(item,column,mode,nested=True,inside_sequence=True) for item in value]
    if isinstance(value,datetime):return value
    if isinstance(value,date):return datetime(value.year,value.month,value.day,tzinfo=timezone.utc)
    np=sys.modules.get('numpy')
    if np is not None and isinstance(value,np.datetime64):
        result=value.astype('datetime64[us]').tolist()
        return None if result is None else result.replace(tzinfo=timezone.utc)
    from .plotly_spec import _arrays_to_lists,_tensor_value
    value=_arrays_to_lists(value)
    value=_tensor_value(value)
    if np is not None and isinstance(value,np.ndarray):
        if value.size==1:value=value.flatten()[0]
        elif value.size<=32:value=value.tolist()
    elif np is not None and isinstance(value,np.generic):
        value=value.item()
        if isinstance(value,np.generic) and (value.dtype.kind=='f' or value.dtype=='bfloat16'):value=float(value)
    if isinstance(value,(list,tuple,dict)):return normalize(value,column,mode,nested=nested,inside_sequence=inside_sequence)
    if isinstance(value,set):return normalize(tuple(value),column,mode,nested=nested,inside_sequence=inside_sequence)
    if isinstance(value,enum.Enum):return value.name
    if isinstance(value,bytes):return value.decode('utf-8')
    if callable(value):return f'{value.__module__}.{value.__qualname__}' if hasattr(value,'__module__') and hasattr(value,'__qualname__') else str(value)
    if isinstance(value,float) and math.isnan(value):return None
    return value


def role_columns(table):
    """Validate canonical column names only when an evaluation is published.

    Main callers: capture and explicit evaluation_metadata. Role overlap remains
    valid and emits a warning; unassigned columns become output columns.
    """
    import backprop
    names=[str(column) for column in table.columns]
    duplicates=sorted({name for name in names if names.count(name)>1})
    if duplicates:raise ValueError(f'EvalTable column names must be unique after converting to strings. Duplicate column name(s): {duplicates}.')
    inputs,outputs,scores=table._input_columns,table._output_columns,table._score_columns
    assigned=set(inputs)|set(outputs)|set(scores);unknown=assigned-set(names)
    if unknown:raise ValueError(f'Column(s) {sorted(unknown)} listed in input/output/score_columns do not exist in the table.')
    seen=set()
    for column in inputs+outputs+scores:
        if column in seen:backprop.termwarn(f'Column {column!r} appears in more than one role list; it will be included in all matching dicts.',repeat=False)
        seen.add(column)
    return names,{'input_columns':list(inputs),'output_columns':[*outputs,*(name for name in names if name not in assigned)],'score_columns':list(scores),'backend':'backprop-tables'}


def capture(table):
    """Create one immutable normalized snapshot, leaving raw rows untouched.

    Main callers: Encoder and public artifact serialization. No run binding,
    upload cursor or logged flag changes until the caller commits its journal.
    """
    names,roles=role_columns(table)
    data=[[normalize(value,column,table._unsupported_media_mode) for column,value in zip(table.columns,row)] for row in table.data]
    candidate=copy.copy(table)
    typed=Table(columns=list(table.columns),data=data,allow_mixed_types=True)
    candidate.data,candidate._types,candidate._optional=typed.data,typed._types,typed._optional
    candidate._declared_columns=set(typed._declared_columns)
    candidate._native_evaluation_snapshot=True
    candidate._snapshot=None;candidate._implicit_artifacts={}
    candidate.MAX_ARTIFACT_ROWS=max(Table.MAX_ARTIFACT_ROWS,len(data))
    scores={}
    for name in roles['score_columns']:
        numbers=[float(row[names.index(name)]) for row in data if isinstance(row[names.index(name)],(int,float)) and math.isfinite(row[names.index(name)])]
        if numbers:scores[name]={'mean':sum(numbers)/len(numbers),'count':len(numbers)}
    candidate._evaluation_frozen_metadata={'evaluation_roles':roles,'evaluation_scores':scores,
        'evaluation_row_identity':None if roles['input_columns'] else {'field':'row','start':1}}
    return candidate


def encode(encoder,table):
    """Stage normalized data through the ordinary atomic Table artifact path.

    Main callers: Encoder's EvalTable dispatch. Pending repeated values share
    one capture. Only successful publication changes the original object's state.
    """
    from .errors import UsageError
    at_run=encoder.artifact_factory is not None and encoder.context in {'history','value'}
    if at_run and table.is_bound() and table._run is not encoder.run:raise UsageError('EvalTable cannot be serialized for a different run than it was bound to.')
    if at_run and table._evaluation_record is not None:
        table._warn_immutable_already_logged()
        return copy.deepcopy(table._evaluation_record)
    key=(id(table),0)
    if at_run and key in encoder.table_cache:
        cached=encoder.table_cache[key];encoder.commits.extend(cached[1]);encoder.artifacts.extend(cached[2]);encoder.prepared_tables[key]=cached
        return cached[0]
    start_commits,start_artifacts=len(encoder.commits),len(encoder.artifacts)
    candidate=capture(table);result=encoder.encode(candidate)
    if at_run:
        manifests=encoder.artifacts[start_artifacts:]
        if len(manifests)!=1:raise RuntimeError('An evaluation snapshot must own exactly one artifact manifest')
        identity=manifests[0]['artifact_id'];result={**result,'evaluate_call_id':identity}
        public={'_type':'eval-table','ncols':len(table.columns),'nrows':len(table.data),'log_mode':table.log_mode,'evaluate_call_id':identity}
        generation=table._generation
        run,log_key=encoder.run,str(encoder.key)
        def commit():
            table._run,table._run_log_key=run,log_key
            table._evaluation_record=copy.deepcopy(result);table._immutable_logged_json=dict(public)
            table._immutable_evaluate_call_id=identity
            table._snapshot=copy.deepcopy(result);table._offset=len(candidate.data);table._snapshot_generation=generation
            table._artifact_target=candidate._artifact_target;table._artifact_target_name=candidate._artifact_target_name
        encoder.commits.append(commit)
        cached=(result,encoder.commits[start_commits:],encoder.artifacts[start_artifacts:]);encoder.table_cache[key]=cached;encoder.prepared_tables[key]=cached
    return result
