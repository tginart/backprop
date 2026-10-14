"""Public Table descriptors alongside Backprop's durable native snapshots.

Architecture: explicit public calls -> pinned run/file or artifact descriptor ->
shared Media/Artifact storage. Reads recognize native schema lists separately
from the public typed-dictionary schema. Run logging keeps its existing native
codec and atomic journal cursors; explicit serialization never advances them.
Main callers: Table.to_json/from_json and typed public artifact files.
Why this exists: public callers depend on keyword names, owner errors, array
files and schema mutations even though Foundry stores richer native snapshots.
The portable transformation follows pinned W&B 0.30.0 (THIRD_PARTY_NOTICES.md).
"""
from __future__ import annotations

from datetime import date,datetime,timezone
from decimal import Decimal
import enum
import io
import json
import logging
from pathlib import Path
import sys

from .media_base import Media
from .values import WBValue
from . import table_types as types


def _cell(value, artifact):
    """Apply the public recursive value conversion without consuming run state.

    Main callers: artifact serialization. Unknown ordinary objects stay ordinary
    until JSON storage validates them, matching the pinned helper's boundary.
    """
    if isinstance(value,WBValue):return value.to_json(artifact)
    if type(value) is dict:return {key:_cell(item,artifact) for key,item in value.items()}
    if hasattr(value,'tolist'):
        converted=value.tolist()
        if type(value).__name__=='datetime64' and isinstance(converted,int):converted/=1_000_000
        return _cell(converted,artifact)
    if hasattr(value,'item'):return _cell(value.item(),artifact)
    if isinstance(value,datetime):
        return int((value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).timestamp()*1000)
    if isinstance(value,date):return int(datetime(value.year,value.month,value.day,tzinfo=timezone.utc).timestamp()*1000)
    if isinstance(value,(list,tuple)):return [_cell(item,artifact) for item in value]
    if isinstance(value,Decimal):return float(value)
    from .plotly_spec import _tensor_value
    value=_tensor_value(value)
    np=sys.modules.get('numpy')
    if np is not None and isinstance(value,np.ndarray):
        if value.size==1:return value.flatten()[0]
        return value.tolist() if value.size<=32 else value
    if np is not None and isinstance(value,np.generic):return _numpy_scalar(value,np)
    if isinstance(value,bytes):return value.decode('utf-8')
    if callable(value):return f'{value.__module__}.{value.__qualname__}' if hasattr(value,'__module__') and hasattr(value,'__qualname__') else str(value)
    if isinstance(value,float) and value!=value:return None
    if isinstance(value,dict) and np is not None:return _numpy_keys(value,np,{})
    if isinstance(value,set):return tuple(value)
    if isinstance(value,enum.Enum):return value.name
    return value


def _numpy_scalar(value,np):
    value=value.item()
    if isinstance(value,float) and value!=value:return None
    if isinstance(value,np.generic) and (value.dtype.kind=='f' or value.dtype=='bfloat16'):return float(value)
    return value


def _numpy_keys(value,np,visited):
    """Match the fallback conversion for dictionary subclasses, including cycles.

    Main caller: _cell after exact built-in dictionaries took the recursive
    value path. Here only keys and nested mappings are converted, as upstream.
    """
    if id(value) in visited:return visited[id(value)]
    result={};visited[id(value)]=result
    for key,item in value.items():
        if isinstance(item,dict):item=_numpy_keys(item,np,visited)
        result[_numpy_scalar(key,np) if isinstance(key,np.generic) else key]=item
    return result


def _array_type(dtype):
    candidates=dtype.params['allowed_types'] if isinstance(dtype,types.UnionType) else [dtype]
    return next((item for item in reversed(candidates) if isinstance(item,types.NDArrayType)),None)


def _set_types(table, schema):
    """Keep public schema mutations in the same actual native dtype instances.

    Main callers: serialization's embedding conversion and deserialization.
    This swaps ColumnType views without invoking user-facing cast restrictions.
    """
    from .table_dtype_bridge import from_upstream_dtype
    table._types=[from_upstream_dtype(schema.params['type_map'][name],False) for name in table.columns]
    table._pk_col=next((name for name,dtype in zip(table.columns,table._types) if dtype.kind=='primaryKey'),None)
    table._fk_cols={name for name,dtype in zip(table.columns,table._types) if dtype.kind in {'foreignKey','foreignIndex'}}
    table.data=[table._wrap_keys(row,table._types) for row in table.data]


def serialize(table, owner):
    """Return the complete owner-specific descriptor and stage its dependencies.

    Main callers: Table.to_json. Explicit calls do not create implicit artifacts
    or consume journal cursors. Artifact array/type mutations follow the public
    reference; native logging uses its independent, richer snapshot encoder.
    """
    from .run import Run
    from .artifacts import Artifact
    from .tables import Table,_optional_module
    result=Media.to_json(table,owner)
    if table.log_mode=='INCREMENTAL':
        if getattr(table,'_previous_increments_paths',None) is None:table._previous_increments_paths=[]
        if getattr(table,'_increment_num',None) is None:table._increment_num=0
        result.update(increment_num=table._increment_num,previous_increments_paths=table._previous_increments_paths)
    if isinstance(owner,Run):
        result.update(_type='incremental-table-file' if table.log_mode=='INCREMENTAL' else 'table-file',
                      ncols=len(table.columns),nrows=len(table.data),log_mode=table.log_mode)
        return result
    if not isinstance(owner,Artifact):raise TypeError('to_json accepts wandb_run.Run or wandb_artifact.Artifact')
    if len(table.data)>Table.MAX_ARTIFACT_ROWS:
        import backprop
        settings=getattr(backprop.run,'settings',None)
        if getattr(settings,'table_raise_on_max_row_limit_exceeded',False) or getattr(settings,'strict',False):
            raise ValueError(f'Table row limit exceeded: table has {len(table.data)} rows, limit is {Table.MAX_ARTIFACT_ROWS}.')
        logging.warning('Truncating wandb.Table object to %s rows.',Table.MAX_ARTIFACT_ROWS)
    offset=table._offset if table.log_mode=='INCREMENTAL' else 0
    data=table.data[offset:offset+Table.MAX_ARTIFACT_ROWS]
    schema=table._column_types;array_columns=set()
    for index,name in enumerate(table.columns):
        array=_array_type(schema.params['type_map'][name])
        if array is None:continue
        shape=array.params.get('shape')
        if isinstance(shape,list) and len(shape)==1 and shape[0]<=table._MAX_EMBEDDING_DIMENSIONS:
            schema.params['type_map'][name]=types.ListType(types.NumberType,shape[0])
        else:
            np=_optional_module('numpy','Serializing NumPy')
            output=io.BytesIO();np.savez_compressed(output,**{str(name):table.get_column(name,convert_to='numpy')})
            from .media_files import freeze_file
            path=freeze_file(output.getvalue(),'.npz')
            entry=owner.add_file(path,'media/serialized_data/'+str(name)+'_'+Path(path).name,is_tmp=True)
            array._set_serialization_path(entry.path,str(name));array_columns.add(index)
        _set_types(table,schema)
    mapped=[[None if index in array_columns else _cell(value,owner) for index,value in enumerate(row)] for row in data]
    result.update(_type='table',columns=table.columns,data=mapped,ncols=len(table.columns),nrows=len(mapped),column_types=schema.to_json(owner),log_mode=table.log_mode)
    return result


def _manifest_rows(value,artifact):
    """Read the pinned manifest-local increment order through verified entries.

    Main callers: deserialize for public incremental files. Native descriptors
    with explicit immutable artifact links use their existing chain reader.
    """
    import backprop
    if 'latest' not in artifact.aliases:backprop.termwarn('It is recommended to use the latest version of the incremental table artifact for ordering guarantees.',repeat=False)
    if value.get('increment_num') is None:return []
    def order(path):
        try:
            parts=path.split('.')[0].split('-');return int(parts[0]),int(parts[1]) if len(parts)>1 else 0
        except (ValueError,IndexError):
            backprop.termwarn(f'Could not parse artifact entry for increment {path}. The data in the table will be out of order.',repeat=False);return 0,0
    data=[]
    for path in sorted((path for path in artifact.manifest.entries if path.endswith('.table.json')),key=order):
        try:
            with open(artifact.manifest.entries[path].download(),encoding='utf-8') as stream:data.extend(json.load(stream)['data'])
        except (json.JSONDecodeError,KeyError) as error:raise backprop.Error(f'Invalid table file {path}') from error
    return data


def deserialize(cls, value, artifact):
    """Reconstruct typed rows using only locally registered value/type handlers.

    Main callers: Table.from_json and Artifact.get for explicitly saved public
    JSON. Array files use NumPy's safe default (no pickle). Source references
    remain scoped to the supplied artifact; no logged module is imported.
    """
    from .tables import _optional_module
    if isinstance(value.get('column_types'),list):
        from .object_codec import decode_object
        return decode_object({**value,'_type':'table'},artifact)
    schema=None;arrays={};timestamps=set();mode=value.get('log_mode','IMMUTABLE')
    if value.get('column_types') is not None:
        schema=types.TypeRegistry.type_from_dict(value['column_types'],artifact)
        for name,dtype in schema.params['type_map'].items():
            candidates=dtype.params['allowed_types'] if isinstance(dtype,types.UnionType) else [dtype]
            if any(isinstance(item,types.TimestampType) for item in candidates):timestamps.add(value['columns'].index(name))
            array=_array_type(dtype)
            if array is not None and array._get_serialization_path() is not None:
                saved=array._get_serialization_path();np=_optional_module('numpy','Deserializing NumPy columns')
                with np.load(artifact.get_entry(saved['path']).download()) as encoded:arrays[value['columns'].index(name)]=encoded[saved['key']]
                array._clear_serialization_path()
    rows=_manifest_rows(value,artifact) if mode=='INCREMENTAL' else value['data']
    data=[]
    for row_index,row in enumerate(rows):
        decoded=[]
        for index,item in enumerate(row):
            if index in timestamps and isinstance(item,(int,float)):cell=datetime.fromtimestamp(item/1000,tz=timezone.utc)
            elif index in arrays:cell=arrays[index][row_index]
            elif isinstance(item,dict) and '_type' in item:
                obj=WBValue.init_from_json(item,artifact);cell=obj if obj else item
            else:cell=item
            decoded.append(cell)
        data.append(decoded)
    dtypes=[schema.params['type_map'][str(name)] for name in value['columns']] if schema is not None else None
    roles=value.get('evaluation_roles')
    arguments={}
    if roles is not None:
        from .extended_tables import EvalTable
        cls=EvalTable
        arguments={key:roles[key] for key in ('input_columns','output_columns','score_columns')}
    result=cls(columns=value['columns'],data=data,dtype=dtypes,log_mode=mode,**arguments)
    if schema is not None:_set_types(result,schema)
    return result
