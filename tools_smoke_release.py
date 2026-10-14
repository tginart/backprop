"""Run away from source against an installed distribution, with synthetic data."""
from pathlib import Path
import importlib.metadata, json, os, tempfile
import backprop as w
import numpy as np
from PIL import Image
import gymnasium as gym

assert 'site-packages' in str(Path(w.__file__).resolve()), w.__file__
metadata=importlib.metadata.metadata('backprop-foundry')
assert w.__version__==metadata['Version'], 'SDK and installed distribution versions differ'
assert metadata['License-Expression']=='Apache-2.0'
assert metadata.get('Home-page') or metadata.get_all('Project-URL')
provenance=Path(w.__file__).parent/'integration/upstream-sources.json'
assert json.loads(provenance.read_text())
settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}
with tempfile.TemporaryDirectory() as d:
    os.environ['BACKPROP_DIR']=d
    with w.init(project='synthetic-release',mode='offline',dir=d,settings=settings) as run:
        run.log({'loss':0.5,'table':w.Table(columns=['x'],data=[[1]]),'image':w.Image(np.zeros((2,2,3),dtype=np.uint8)), 'video':w.Video(np.zeros((2,3,4,4),dtype=np.uint8),fps=2,format='gif')})
        run.summary['result']='finished'
        try:run.alert('unsupported')
        except w.UnsupportedError:pass
        else:raise AssertionError('Unsupported service API accepted')
    assert list(Path(d).rglob('events.jsonl'))
    try:w.Artifact('synthetic','model',storage_region='synthetic-region')
    except NotImplementedError:pass
    else:raise AssertionError('Unsupported placement accepted')
print(json.dumps({'installed_sdk':metadata['Version'],'offline_logging':'passed','unsupported_errors':'passed','provenance':'passed','numpy_pillow_integration':'passed','moviepy_gif_encoding':'passed','gymnasium_version':gym.__version__}))
