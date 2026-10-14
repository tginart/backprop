"""Compare EvalTable with the genuine pinned SDK in an isolated process.

Architecture: unchanged EvalTable -> external Weave service fixture -> observed
examples and lifecycle. Only dependency discovery, service initialization and
EvaluationLogger storage are replaced. Native execution uses real Run, journals,
artifacts and public readback. Main caller: test_optional_eval_table.
"""
from contextlib import ExitStack
from datetime import date, datetime, timezone
import enum
import hashlib
import importlib
import inspect
import importlib.util
import io
import wave
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

reference = sys.argv[1] == 'reference'
if reference:
    import wandb as w
    assert w.__version__ == '0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
import numpy as np
from PIL import Image as PILImage
from eval_weave_audio_fixture import Audio as WeaveAudio
try:
    from moviepy.video.io.VideoFileClip import VideoFileClip
except ImportError:
    VideoFileClip=None


class EvaluationLogger:
    evaluations = []
    @classmethod
    def _create_with_meta(cls, marker, *, name):
        result = cls(); result.rows = []; result.summary_count = 0
        result._evaluate_call = SimpleNamespace(id='fixture-evaluation')
        cls.evaluations.append(result)
        return result
    def log_example(self, **values): self.rows.append(values)
    def log_summary(self): self.summary_count += 1


def canonical(value):
    if isinstance(value, (WeaveAudio, w.Audio)):
        data=value.data if isinstance(value,WeaveAudio) else value._bytes()
        return {'audio_bytes':hashlib.sha256(data).hexdigest()}
    if isinstance(value,w.Video):
        if VideoFileClip is None:raise ImportError('MoviePy unavailable')
        value=VideoFileClip(value._path)
    if VideoFileClip is not None and isinstance(value,VideoFileClip):
        try:return {'video':{'size':list(value.size),'duration':value.duration,'fps':value.fps,
            'first_frame':hashlib.sha256(value.get_frame(0).tobytes()).hexdigest()}}
        finally:value.close()
    if isinstance(value, w.Image): value = value.image
    if isinstance(value, PILImage.Image):
        return {'image': [value.mode, list(value.size), hashlib.sha256(value.tobytes()).hexdigest()]}
    if isinstance(value, (date, datetime)): return {'date': value.isoformat()}
    if isinstance(value, dict): return {str(key): canonical(item) for key,item in value.items()}
    if isinstance(value, (list, tuple)): return [canonical(item) for item in value]
    if isinstance(value, np.ndarray): return canonical(value.tolist())
    if isinstance(value, np.generic): return canonical(value.item())
    if isinstance(value, enum.Enum): return value.name
    return value


def outcome(call):
    try: return {'value': canonical(call())}
    except Exception as error:
        # The documented contract is TypeError; W&B's private Weave adapter
        # subtype is implementation machinery outside the portable surface.
        name=type(error).__name__
        return {'error':'TypeError' if name=='_UnsupportedMediaVariantError' and isinstance(error,TypeError) else name}


def state(table):
    return [table.is_bound(), table.file_is_set(), table.has_been_logged()]


def signature(call):
    return [(p.name, p.kind.name, 'required' if p.default is p.empty else repr(p.default))
            for p in inspect.signature(call).parameters.values()]


class Choice(enum.Enum):
    ONE = 1


out = {'aliases': {}, 'cases': {}, 'invalid': {}, 'normalization': {}}
with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
    if reference:
        import wandb.integration.weave as integration
        stack.enter_context(patch.object(integration, 'ensure_version'))
        stack.enter_context(patch.object(integration, 'init_weave'))
        weave = ModuleType('weave'); weave.Audio = WeaveAudio
        stack.enter_context(patch.dict(sys.modules, {'weave':weave}))
        if VideoFileClip is not None:
            # MoviePy 2 moved the import. Supply its genuine reader under the
            # legacy dependency path; no W&B media adapter is replaced.
            editor=ModuleType('moviepy.editor');editor.VideoFileClip=VideoFileClip
            stack.enter_context(patch.dict(sys.modules,{'moviepy.editor':editor}))
        module = ModuleType('weave.evaluation.eval_imperative'); module.EvaluationLogger = EvaluationLogger
        stack.enter_context(patch.dict(sys.modules, {'weave.evaluation.eval_imperative': module}))
        run = w.init(project='eval-probe', entity='local', mode='offline', dir=directory,
                     settings=w.Settings(console='off', silent=True, x_disable_stats=True))
        stack.callback(run.finish)
        # Weave rejects offline mode. Use a real offline Run, replacing only this
        # external-service gate while exercising bind/to_json/telemetry unchanged.
        stack.enter_context(patch.object(type(run), 'offline', new=property(lambda self: False)))
    else:
        from backprop.transport import MemorySink
        from backprop.apis.public import Api
        sink = MemorySink()
        run = w.Run(project='eval-probe', entity='local', dir=directory, _sink=sink,
                    settings={'console':'off', 'silent':True, 'x_disable_stats':True})
        stack.callback(run.finish)
        api = Api._from_store(sink, entity='local', project='eval-probe')

    def publish(table, key):
        before = len(EvaluationLogger.evaluations)
        result = table.to_json(run)
        if reference:
            stored = EvaluationLogger.evaluations[-1]
            examples = stored.rows
        else:
            run.flush(); api.flush()
            artifact = api.artifact(result['evaluate_call_id'])
            captured = artifact.get(key)
            roles = captured.evaluation_roles
            columns = [str(col) for col in captured.columns]
            examples = []
            for index, row in enumerate(captured.data):
                values = dict(zip(columns,row))
                examples.append({'inputs': {col:values[col] for col in roles['input_columns']} if roles['input_columns'] else {'row':index+1},
                    'output': {col:values[col] for col in roles['output_columns']} if roles['output_columns'] else None,
                    'scores': {col:values[col] for col in roles['score_columns']}})
        return {**result, 'evaluate_call_id': '<stored-evaluation>'}, canonical(examples)

    for module_name in ['wandb', 'wandb.data_types', 'wandb.sdk.data_types.eval_table']:
        cls = getattr(importlib.import_module(module_name), 'EvalTable')
        out['aliases'][module_name] = {'same_class': cls is w.EvalTable,
            'signatures': {name:signature(getattr(cls,name)) for name in ['__init__','bind_to_run','to_json','from_json','add_data','add_column','has_been_logged','file_is_set','is_bound']}}

        table=cls(columns=['alias'],data=[[module_name]])
        table.bind_to_run(run,module_name,0)
        descriptor,examples=publish(table,module_name)
        restored=cls.from_json(json_obj={'columns':['alias'],'data':[[module_name]]},source_artifact=None)
        out['aliases'][module_name].update(json=descriptor,examples=examples,restored=restored.data,state=state(table))

    cases = {
        'roles': dict(input_columns=['prompt'], score_columns=['score'], columns=['prompt','answer','score'], data=[['hi','hello',1.],['bye','goodbye',.5]]),
        'row_identity': dict(columns=['answer','score'], score_columns=['score'], data=[['yes',True],['no',False]]),
        'overlap': dict(columns=['x'], input_columns=['x'], output_columns=['x'], score_columns=['x'], data=[[2]]),
        'all_inputs': dict(columns=['x'], input_columns=['x'], data=[[2]]),
        'int_columns': dict(columns=[1,2], input_columns=['1'], data=[['a','b']]),
        'empty': dict(columns=[]),
        'numpy': dict(columns=['a','b'], data=np.array([[1,2],[3,4]])),
        'rows': dict(columns=['a'], rows=[[1],[2]], dtype=int, optional=False),
    }
    for name, args in cases.items():
        table = w.EvalTable(**args); lifecycle = [state(table)]
        table.bind_to_run(run=run, key=name, step='step', id_='chosen', ignore_copy_err=True); lifecycle.append(state(table))
        result, examples = publish(table,name); lifecycle.append(state(table))
        again = table.to_json(run)
        out['cases'][name] = {'lifecycle':lifecycle,'json':result,'examples':examples,'repeat':again==table.to_json(run)}

    cells = {'scalar': np.float32(1.5), 'array':np.array([1,2]), 'scalar_array':np.array(2),
        'date':date(2024,1,2), 'datetime':datetime(2024,1,2,3,4,tzinfo=timezone.utc),
        'datetime64':np.datetime64('2024-01-02T03:04:05.123456789'), 'nat':np.datetime64('NaT'),
        'nan':float('nan'), 'bytes':b'abc', 'enum':Choice.ONE, 'set':{1,2},
        'dictionary':{np.int64(2):{'x':(np.int32(3), date(2024,1,2))}},
        'html': w.Html('<p>x</p>'), 'image':w.Image(PILImage.new('RGB',(2,2),'red')),
        'dict_image':{'x':w.Image(PILImage.new('RGB',(2,2),'red'))},
        'list_image':[w.Image(PILImage.new('RGB',(2,2),'red'))],
        'nested_html':{'x':[{'h':w.Html('<p>x</p>')}]}}
    audio_path=Path(directory,'input.wav')
    with wave.open(str(audio_path),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000);stream.writeframes(b'\x00\x00\x00\x20\x00\xe0')
    audio=w.Audio(str(audio_path),caption='preserved natively')
    cells.update(audio=audio,dict_audio={'nested':audio},list_audio=[audio],
        external_audio=w.Audio('https://example.com/audio.wav'),external_image=w.Image('https://example.com/image.png'))
    if importlib.util.find_spec('soundfile'):
        cells['raw_audio']=w.Audio(np.array([0.,.25,-.25]),sample_rate=8000)
        cells['stereo_audio']=w.Audio(np.array([[0.,.25],[-.25,0.]]),sample_rate=8000)
    if VideoFileClip is not None:
        video_path=Path(directory,'input.gif');PILImage.new('RGB',(4,4),'red').save(video_path,save_all=True,append_images=[PILImage.new('RGB',(4,4),'blue')],duration=250,loop=0)
        video=w.Video(str(video_path),caption='preserved natively')
        cells.update(video=video,dict_video={'nested':video},list_video=[video],video_bytes=w.Video(io.BytesIO(video_path.read_bytes()),format='gif'))
    if importlib.util.find_spec('torch'):
        import torch
        cells.update(torch_scalar=torch.tensor(2.),torch_vector=torch.arange(4.),
            torch_grad=torch.arange(4.,requires_grad=True),torch_large=torch.arange(40.).reshape(2,20),
            torch_nested={'value':torch.tensor([1.,2.])},torch_empty=torch.empty(0),
            torch_bool=torch.tensor([True,False]),torch_bfloat16=torch.ones(2,dtype=torch.bfloat16))
    if importlib.util.find_spec('torch') and torch.backends.mps.is_available():
        cells.update(torch_mps_scalar=torch.tensor(2.,device='mps'),
            torch_mps_grad=torch.arange(4.,device='mps',requires_grad=True),
            torch_mps_large=torch.arange(40.,device='mps').reshape(2,20))
    if importlib.util.find_spec('tensorflow'):
        import tensorflow as tf
        cells.update(tf_scalar=tf.constant(2.),tf_vector=tf.range(4.),tf_large=tf.reshape(tf.range(40.),(2,20)),
            tf_variable=tf.Variable([1.,2.]),tf_nested={'value':tf.constant([1.,2.])},tf_empty=tf.constant([]),
            tf_bool=tf.constant([True,False]),tf_bfloat16=tf.ones(2,dtype=tf.bfloat16))
    for name, cell in cells.items():
        def perform():
            table = w.EvalTable(columns=['value'], data=[[cell]], allow_mixed_types=True)
            unchanged = table.data[0][0] is cell
            table.bind_to_run(run,name,0)
            result, examples = publish(table,name)
            return {'examples':examples,'raw_unchanged':unchanged and table.data[0][0] is cell}
        out['normalization'][name] = outcome(perform)

    for name,args in {'mode_mutable':{'log_mode':'MUTABLE'},'bad_mode':{'unsupported_media_mode':'invalid'},
        'tuple_columns':{'columns':('x',)},'bool_columns':{'columns':[True]},
        'duplicate_columns':{'columns':['x','x'],'data':[[1,2]]},
        'derived_duplicates':{'input_columns':['x'],'score_columns':['x'],'data':[[1,2]]},
        'colliding_columns':{'columns':[1,'1'],'data':[[1,2]]},
        'unknown_role':{'columns':['x'],'input_columns':['missing']},
        'integer_role':{'columns':[1],'input_columns':[1]},
        'table_cell':{'columns':['x'],'data':[[w.Table(columns=['a'])]]},
        'nested_table':{'columns':['x'],'data':[[{'x':w.Table(columns=['a'])}]],'allow_mixed_types':True},
        'raise_html':{'columns':['x'],'data':[[w.Html('hello')]],'unsupported_media_mode':'raise'},
        'raise_nested_html':{'columns':['x'],'data':[[[w.Html('hello')]]],'unsupported_media_mode':'raise','allow_mixed_types':True}}.items():
        def invalid_case():
            try: table=w.EvalTable(**args)
            except Exception as error: return {'stage':'constructor','error':type(error).__name__}
            table.bind_to_run(run,name,0)
            try: table.to_json(run)
            except Exception as error: return {'stage':'publication','error':type(error).__name__,'state':state(table)}
            return {'stage':'accepted'}
        out['invalid'][name] = invalid_case()

    for mode in ['stub','raise']:
        for kind,media in [('audio',audio),('image',cells['image']),('external_audio',cells['external_audio']),('external_image',cells['external_image'])]+([('video',video)] if VideoFileClip is not None else []):
            def nested_value(mode=mode,media=media):
                # The source Audio equality bug rejects list construction, but
                # raw public data can still reach the evaluation serializer.
                table=w.EvalTable(columns=['x'],data=[['placeholder']],allow_mixed_types=True,unsupported_media_mode=mode)
                table.data[0][0]={'nested':[media]};table.bind_to_run(run,'nested-mode',0)
                return publish(table,'nested-mode')[1]
            out['invalid'][kind+':nested:'+mode]=outcome(nested_value)
            if kind.startswith('external'):
                def external_value(mode=mode,media=media):
                    table=w.EvalTable(columns=['x'],data=[[media]],allow_mixed_types=True,unsupported_media_mode=mode)
                    table.bind_to_run(run,'external-mode',0);return publish(table,'external-mode')[1]
                out['invalid'][kind+':external:'+mode]=outcome(external_value)
    for kind in ['audio','video']:
        if kind=='video' and VideoFileClip is None:continue
        for mode in ['stub','raise']:
            def missing_path(kind=kind,mode=mode):
                value=object.__new__(w.Audio if kind=='audio' else w.Video);value._path=None
                table=w.EvalTable(columns=['x'],data=[[value]],allow_mixed_types=True,unsupported_media_mode=mode)
                table.bind_to_run(run,'missing-path',0);return table.to_json(run)
            out['invalid'][kind+':missing_path:'+mode]=outcome(missing_path)
    for suffix,data in [('mp3',b'not-base64!'),('mp3',b'YWJj'),('flac',b'FLACtest'),('wav',b'')]:
        path=Path(directory,'audio-check.'+suffix);path.write_bytes(data)
        def audio_check():
            table=w.EvalTable(columns=['x'],data=[[w.Audio(str(path))]],allow_mixed_types=True)
            table.bind_to_run(run,'audio-check',0);return publish(table,'audio-check')[1]
        out['invalid']['audio_file:'+suffix+':'+str(data)]=outcome(audio_check)
    if VideoFileClip is not None:
        path=Path(directory,'broken.mp4');path.write_bytes(b'broken video')
        def corrupt_video():
            table=w.EvalTable(columns=['x'],data=[[w.Video(str(path))]],allow_mixed_types=True)
            table.bind_to_run(run,'corrupt',0);return table.to_json(run)
        out['invalid']['corrupt_video']=outcome(corrupt_video)
    table=w.EvalTable(columns=['x'],data=[[1]])
    out['unbound']=outcome(lambda:table.to_json(run))
    out['wrong_owner']=outcome(lambda:table.to_json(None))
    table.bind_to_run(run,'immutable',0)
    first,_=publish(table,'immutable'); table.add_data(2)
    again=table.to_json(run);again['evaluate_call_id']='<stored-evaluation>'
    out['immutable']={'first':first,'second':again,'raw_rows':table.data,'state':state(table)}
    out['from_json_required']=outcome(lambda:w.EvalTable.from_json({'columns':['x'],'data':[[1]]}))
    out['from_json']=outcome(lambda:w.EvalTable.from_json(json_obj={'columns':['x'],'data':[[1]]},source_artifact=None).data)

    out['constructor_edges']={}
    for name,args in {'short_dtype':{'columns':['a','b'],'dtype':[int]},'long_dtype':{'columns':['a'],'dtype':[int,str]},
                      'short_optional':{'columns':['a','b'],'optional':[True]},'tuple_data':{'columns':['a'],'data':((1,),)}}.items():
        out['constructor_edges'][name]=outcome(lambda args=args:w.Table(**args).data)
    out['independent_publication']={}
    disabled=w.init(mode='disabled') if reference else w.Run(mode='disabled',dir=directory,_sink=sink,settings={'silent':True,'console':'off'})
    run.finish();disabled.finish()
    for module_name in ['wandb','wandb.data_types','wandb.sdk.data_types.eval_table']:
        cls=getattr(importlib.import_module(module_name),'EvalTable')
        for context,owner in [('disabled',disabled),('finished',run)]:
            table=cls(columns=['x'],data=[[1]]);table.bind_to_run(owner,context,0)
            result=outcome(lambda:table.to_json(owner))
            if 'value' in result:result['value']['evaluate_call_id']='<stored-evaluation>'
            out['independent_publication'][module_name+':'+context]={'result':result,'state':state(table),'repeat':table.to_json(owner)==table.to_json(owner)}

print(json.dumps(out, sort_keys=True, allow_nan=False))
