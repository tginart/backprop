"""Check exact public contents, local doc links, private values and archive bytes.

Findings disclose paths/categories only, never matched values. ZIP/tar members
are screened recursively. No token or deployment configuration is required.
"""
from pathlib import Path
import argparse, hashlib, io, json, re, tarfile, zipfile

PUBLIC_DOCUMENTATION = frozenset({
 'README.md', 'backend/README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'RELEASE_NOTES.md',
 'THIRD_PARTY_NOTICES.md', 'docs/README.md', 'docs/compatibility.md',
 'docs/dependency-security.md', 'docs/distributed-logging.md',
 'docs/foundry-setup.md', 'docs/interface.md', 'docs/python-api-support.md',
 'docs/local-backend.md', 'docs/backend-verification.md', 'docs/gcp-setup.md',
 'docs/updates.md', 'examples/README.md', 'tests/fixtures/MAP-FIXTURES.md',
})

def public_documentation(name):
    """Only reviewed user, operator, contributor and attribution guides ship."""
    return not (name.startswith('docs/') or Path(name).suffix in {'.md', '.rst', '.adoc'}) or name in PUBLIC_DOCUMENTATION

PATTERNS = {
 'internal_documentation_reference':rb'docs/(?:implementation|sdk-audit|ui-audit)/|tools/(?:public_release)/',
 'personal_path':rb'/Users/[A-Za-z0-9_.-]+/|/home/(?!example)[\w.-]+/',
 'private_enrollment':rb'[\w.-]+\.(?:palantirfoundry|palantircloud|foundryapps)\.',
 'credential':rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}',
}
SYNTHETIC_TLS = {'tests/framework_init_tls_cert.pem': '59931f7536e56b013c5dc985bc2bb4251f0a66eeb3fdee5b6a8886c242fc8ccf', 'tests/framework_init_tls_key.pem': 'bd27bf9f05821444b5b76d25f7397851df248a368d57ab412c260efa9918000e', 'tests/tensorboard_gap_tls_wrong_cert.pem': 'd59c4706c579c69cf82f0a34861ac8c4d63e72c94e63a77b0733303d0b313b87', 'tests/tensorboard_gap_tls_wrong_key.pem': '9daa9320bccf71ca2db1f31cde6b78d6764c9bc961178fee7ae12d31e59269c8'}
# Public loopback TLS test fixtures, never deployment credentials.
def screen(data, name, findings):
    for fixture, digest in SYNTHETIC_TLS.items():
        if name.endswith(fixture):
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Unreviewed TLS fixture')
            return
    for category, pattern in PATTERNS.items():
        if re.search(pattern,data):findings.append({'path':name,'category':category})
    if name.endswith(('.whl','.zip')):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if '..' in Path(n).parts or n.startswith('/'):raise ValueError('Unsafe archive member')
                screen(z.read(n),name+'!'+n,findings)
    elif name.endswith(('.tar.gz','.tgz')):
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for member in t:
                if member.uid or member.gid or member.uname or member.gname:raise ValueError('Private archive ownership metadata')
                if member.issym() or member.islnk() or '..' in Path(member.name).parts:raise ValueError('Unsafe source archive')
                if member.isfile():screen(t.extractfile(member).read(),name+'!'+member.name,findings)


def check(root, artifacts=()):
    manifest=json.loads((root/'PUBLIC_MANIFEST.json').read_text())
    expected={r['path']:r for r in manifest['files']}
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    if '.git' in {p.name for p in root.iterdir()}:raise ValueError('Public candidate must contain no Git metadata')
    if actual != set(expected)|{'PUBLIC_MANIFEST.json'}:raise ValueError('Candidate manifest mismatch: '+json.dumps({'unexpected':sorted(actual-set(expected)-{'PUBLIC_MANIFEST.json'}),'missing':sorted(set(expected)-actual)}))
    findings=[]
    for name,row in expected.items():
        if not public_documentation(name):raise ValueError('Unreviewed public documentation file: '+name)
        p=root/name
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Candidate hash mismatch: '+name)
        # Synthetic keys enable optional loopback TLS tests, never public production credentials.
        if p.suffix=='.pem':
            if SYNTHETIC_TLS.get(name) != row['sha256']:raise ValueError('Unreviewed TLS fixture')
            continue
        screen(p.read_bytes(),name,findings)
        if p.suffix=='.md':
            for target in re.findall(r'\]\(([^\s)]+)',p.read_text()):
                if not target.startswith(('http:', 'https:', '#', 'mailto:')) and not (p.parent/target.split('#')[0]).is_file():
                    findings.append({'path':name,'category':'broken_local_link'})
    for artifact in artifacts:screen(artifact.read_bytes(),artifact.name,findings)
    if findings:raise ValueError(json.dumps(findings,indent=2))
    return {'source_files':len(actual),'artifacts_screened':len(artifacts),'privacy_patterns':'passed','manifest':'passed','local_links':'passed'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('artifacts',type=Path,nargs='*');a=p.parse_args()
    print(json.dumps(check(a.root,a.artifacts),indent=2))
