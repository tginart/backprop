"""Produce real annotation events and PNG bytes for Python/browser replay.

Architecture: stdlib PNG inputs -> public annotation helpers -> existing run
files/config/history -> both readers and the actual image viewer. Main callers:
test_annotations_replay.mjs and browser_annotations_qa.mjs. No optional packages.
"""
import base64
import json
from pathlib import Path
import struct
import tempfile
import zlib

import backprop
from backprop.media import BoundingBoxes2D,ImageMask
from backprop.apis.public.replay import reduce_events


def png(mask=False):
    width,height=64,32
    def chunk(name,data):
        return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data))
    rows=[]
    for y in range(height):
        row=bytearray([0])
        for x in range(width):
            foreground=16<=x<48 and 8<=y<24
            row.extend([255 if foreground else 0] if mask else ([65,120,165] if foreground else [24,31,42]))
        rows.append(bytes(row))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,0 if mask else 2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')


def main():
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'labels.png';path.write_bytes(png(True))
        mask=ImageMask({'path':path,'class_labels':{0:'Background',255:'Foreground 255'}},'prediction')
        box=BoundingBoxes2D({'box_data':[{'position':{'minX':.25,'minY':.25,'maxX':.75,'maxY':.75},'class_id':7,'scores':{'confidence':.9}}],'class_labels':{7:'Target box'}},'truth')
        image=backprop.Image(png(),boxes={'truth':box},masks={'prediction':mask},caption='SDK annotation verification')
        sink=backprop.MemorySink()
        with backprop.Run(entity='e',project='annotations',id='fixture',dir=directory,_sink=sink,_deployment={},
                settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}) as run:
            image.bind_to_run(run,'image',0,id_='image')
            run.log({'image':image,'step':0})
        state=reduce_events(sink.events)
        print(json.dumps({'events':sink.events,'expected':{key:state[key] for key in ['config','summary','state']},
            'image':state['history'][0]['image'],
            'files':{key:base64.b64encode(value).decode() for key,value in sink.files.items()},
            'history':[{k:v for k,v in row.items() if k not in ['_timestamp','_runtime']} for row in state['history']]}))


if __name__=='__main__':main()
