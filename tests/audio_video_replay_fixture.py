"""Build actual Audio/Video events for Python/browser replay verification.

Architecture: SDK file binding -> durable journal -> Python public history and
browser events. Main caller: test_audio_video_replay.mjs and release checks.
"""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import backprop as w
from backprop.apis.public.replay import reduce_events

GIF=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')


def fixture():
    with tempfile.TemporaryDirectory() as directory:
        sink=w.MemorySink()
        with w.Run(id='audio-video',entity='e',project='media',dir=directory,_sink=sink,_deployment={},settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}) as run:
            audio=w.Audio([0,.5,-.5],8000,caption='clip');video=w.Video(io.BytesIO(GIF),format='gif',caption='animation')
            audio.bind_to_run(run,'audio',0,id_='fixed');video.bind_to_run(run,'video',0,id_='fixed')
            run.log({'audio':audio,'video':video})
        api=w.Api._from_store(sink,entity='e',project='media');rows=list(api.run(run.id).scan_history())
        return {'events':sink.events,'expected':reduce_events(sink.events),
                'history':[{k:v for k,v in row.items() if k not in {'_timestamp','_runtime'}} for row in rows],
                'files':{key:base64.b64encode(sink.download_file(rows[0][key])).decode() for key in ['audio','video']}}


if __name__=='__main__':print(json.dumps(fixture()))
