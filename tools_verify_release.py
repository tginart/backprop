"""Verify a Backprop source snapshot and its distribution artifacts.

Builds and tests run in disposable copies without changing the source manifest.
Installed-package checks run outside the source checkout.
"""
from pathlib import Path
from email.parser import BytesParser
import argparse, gzip, hashlib, io, json, os, shutil, subprocess, sys, tarfile, tempfile, zipfile
sys.dont_write_bytecode = True
from tools_check_release import check


def normalize_sdist(path):
    """Remove builder account metadata and make source archives reproducible."""
    with tarfile.open(path) as source:
        entries=[]
        for member in source:
            if member.issym() or member.islnk() or '..' in Path(member.name).parts:
                raise ValueError('Unsafe source distribution member')
            entries.append((member.name,member.isdir(),source.extractfile(member).read() if member.isfile() else b''))
    replacement=path.with_suffix('.normalized')
    with replacement.open('wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=1767225600) as compressed, tarfile.open(fileobj=compressed,mode='w') as bundle:
        for name,directory,data in sorted(entries):
            member=tarfile.TarInfo(name);member.mtime=1767225600
            member.type=tarfile.DIRTYPE if directory else tarfile.REGTYPE
            member.mode=0o755 if directory else 0o644;member.size=0 if directory else len(data)
            bundle.addfile(member,None if directory else io.BytesIO(data))
    replacement.replace(path)


def verify(root, output, browser=False):
    output.mkdir(parents=True,exist_ok=True)
    check(root)
    env={**os.environ,'SOURCE_DATE_EPOCH':'1767225600','PYTHONDONTWRITEBYTECODE':'1'};env.pop('PYTHONPATH',None)
    report={'python':sys.version,'checks':[],'skips':[
        'New enrollment setup, permission isolation and cross-enrollment access are outside this local verifier.',
        'Hosted cloud IAM, multi-device CUDA, legacy Windows consoles and external Plotly configurations need environment-specific checks.',
        'Live Foundry round trips are excluded from this credential-free verifier.',
        'Marketplace/AIP distribution is outside the supported installation path.']}
    def run(label,args,cwd,extra=None):
        log=output/(label+'.log')
        with log.open('w') as handle:
            result=subprocess.run(args,cwd=cwd,env={**env,**(extra or {})},stdout=handle,stderr=subprocess.STDOUT,timeout=1200)
        report['checks'].append({'name':label,'exit_code':result.returncode})
        (output/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
        if result.returncode:raise RuntimeError(label+' failed; see '+str(log))
    with tempfile.TemporaryDirectory(prefix='backprop-public-verification-') as temp:
        work=Path(temp)/'source';shutil.copytree(root,work)
        # Keep synthetic TLS fixtures, bytecode and journal output outside the reviewed tree.
        reference=Path(os.environ.get('BACKPROP_REFERENCE_SOURCE', root/'.reference/wandb/source'))
        if reference.is_dir():shutil.copytree(reference,work/'.reference/wandb/source',dirs_exist_ok=True)
        else:
            from urllib.request import urlopen
            with urlopen('https://codeload.github.com/wandb/wandb/tar.gz/e5ec9dc9236d1a4fbb9fe7c0795bbccdff1e0e77',timeout=60) as response:
                archive=response.read(60_000_001)
            if hashlib.sha256(archive).hexdigest()!='0be0f4f840f0df49d558bc63987a90a2c2bcf812cee7d9db746591855eebaeb5':
                raise ValueError('Public reference source digest mismatch')
            with tarfile.open(fileobj=io.BytesIO(archive)) as t:t.extractall(Path(temp)/'reference',filter='data')
            shutil.move(str(next((Path(temp)/'reference').iterdir())),work/'.reference/wandb/source')
        runtime={'BACKPROP_DIR':str(Path(temp)/'sdk-state'), 'PYTHONPATH':str(work/'sdk'),'BACKPROP_PYTHON':sys.executable,'BACKPROP_TEST_PYTHON':sys.executable,
                 'PATH':str(Path(sys.executable).parent)+os.pathsep+env.get('PATH','')}
        run('python-contracts',[sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_*.py'],work,runtime)
        run('node-replay',['node','--test',*map(str,sorted((work/'tests').glob('test_*.mjs')))],work,runtime)
        dist=output/'dist';dist.mkdir(exist_ok=True)
        run('build',[sys.executable,'-m','build','--no-isolation','--outdir',str(dist)],work)
        wheel=next(dist.glob('*.whl'));sdist=next(dist.glob('*.tar.gz'));normalize_sdist(sdist)
        with zipfile.ZipFile(wheel) as z:
            names=z.namelist();assert 'backprop/integration/upstream-sources.json' in names
            metadata_name=next(n for n in names if n.endswith('.dist-info/METADATA'))
            package_version=BytesParser().parsebytes(z.read(metadata_name),headersonly=True)['Version']
            assert package_version, 'Built wheel has no package version'
            report['version']=package_version
            for required in ['LICENSE','NOTICE','THIRD_PARTY_NOTICES.md']:
                assert any(n.endswith('/'+required) for n in names),required
            assert not any(n.startswith(('tests/','web/','deploy/','docs/')) for n in names)
        with tarfile.open(sdist) as t:t.extractall(Path(temp)/'sdist',filter='data')
        unpacked=next((Path(temp)/'sdist').iterdir());rebuilt=output/'sdist-wheel';rebuilt.mkdir(exist_ok=True)
        run('build-from-sdist',[sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(rebuilt)],unpacked)
        for label,artifact in [('wheel',wheel),('sdist-wheel',next(rebuilt.glob('*.whl')))]:
            venv=Path(temp)/label;run(label+'-venv',[sys.executable,'-m','venv',str(venv)],Path(temp))
            python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            run(label+'-install',[str(python),'-m','pip','install',str(artifact)+'[media,video,gym]'],Path(temp))
            run(label+'-dependencies',[str(python),'-m','pip','check'],Path(temp))
            run(label+'-versions',[str(python),'-m','pip','freeze'],Path(temp))
            run(label+'-smoke',[str(python),str(root/'tools_smoke_release.py')],Path(temp))
            run(label+'-cli',[str(python),'-m','backprop','--help'],Path(temp))
            console=venv/('Scripts/backprop.exe' if os.name=='nt' else 'bin/backprop')
            run(label+'-console',[str(console),'--help'],Path(temp))
            program=Path(temp)/'launcher.py';program.write_text('import wandb; assert wandb.__name__ == "backprop"\n')
            run(label+'-launcher',[str(python),'-m','backprop','run','--',str(program)],Path(temp))
        sys.path.insert(0,str(work/'deploy'));from deploy import build_archive
        state={'host':'https://foundry.example.com','client_id':'synthetic-public-client','app_url':'https://backprop.example.com/'}
        resources={'host':state['host'],'branch':'master','stream_dataset_rid':'ri.foundry.main.dataset.synthetic-events','media_dataset_rid':'ri.foundry.main.dataset.synthetic-media'}
        archive=build_archive(state,resources);(output/'website.zip').write_bytes(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            assert len(z.namelist())<=1000;assert sum(r.file_size for r in z.infolist())<=20_000_000
            for n in ['config.json','LICENSE','NOTICE','THIRD_PARTY_NOTICES.md','vega-provenance.json']:assert n in z.namelist()
            asset_manifest=[{'path':n,'bytes':len(z.read(n)),'sha256':hashlib.sha256(z.read(n)).hexdigest()} for n in sorted(z.namelist())]
            (output/'website-manifest.json').write_text(json.dumps(asset_manifest,indent=2)+'\n')
            report['website']={'files':len(z.namelist()),'compressed_bytes':len(archive),'uncompressed_bytes':sum(r.file_size for r in z.infolist())}
        if browser:
            for name in ['browser_request_recovery.mjs','browser_expansion_qa.mjs','browser_plotly_qa.mjs','browser_maps_qa.mjs','ui_workflow_reference_qa.mjs']:
                run(name.removesuffix('.mjs'),['node',str(work/'tests'/name)],work,{**runtime,'PYTHON':sys.executable})
            if (work/'test-results').exists():shutil.copytree(work/'test-results',output/'browser-evidence',dirs_exist_ok=True)
        else:report['skips'].append('Actual browser checks not requested for this run.')
        source_archive=output/f'backprop-public-source-{package_version}.tar.gz'
        with source_archive.open('wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=1767225600) as compressed, tarfile.open(fileobj=compressed,mode='w') as bundle:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    data=path.read_bytes();info=tarfile.TarInfo('backprop/'+str(path.relative_to(root)))
                    info.size=len(data);info.mode=0o644;info.mtime=1767225600;bundle.addfile(info,io.BytesIO(data))
        artifacts=[wheel,sdist,*rebuilt.glob('*.whl'),output/'website.zip',source_archive]
        report['privacy']=check(root,artifacts)
        report['artifacts']=[{'name':str(a.relative_to(output)),'sha256':hashlib.sha256(a.read_bytes()).hexdigest(),'bytes':a.stat().st_size} for a in artifacts]
        run('resolved-versions',[sys.executable,'-m','pip','freeze'],Path(temp))
    report['status']='passed';(output/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent);p.add_argument('--output',type=Path,required=True);p.add_argument('--browser',action='store_true');a=p.parse_args()
    print(json.dumps(verify(a.root.resolve(),a.output.resolve(),a.browser),indent=2))
