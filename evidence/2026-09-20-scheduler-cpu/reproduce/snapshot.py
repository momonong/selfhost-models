"""Copy only CPU source/test inputs into a new native Linux scratch directory."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--scratch-parent',type=Path,required=True)
args=parser.parse_args()
repo=args.repo.resolve(strict=True)
parent=args.scratch_parent.resolve(strict=True)
if sys.platform!='linux':
    parser.error('snapshot/SQLite/decoder execution requires Linux; Windows is client only')
if parent==repo or repo in parent.parents:
    parser.error('scratch-parent must be outside the repository')
fs=subprocess.run(['stat','-f','-c','%T',str(parent)],check=True,capture_output=True,text=True).stdout.strip()
if fs not in ('ext2/ext3','xfs','btrfs'):
    parser.error('scratch-parent must use local Linux filesystem; not DrvFs/9p/NAS')
root=Path(tempfile.mkdtemp(prefix='selfhost-scheduler-cpu-',dir=parent))
root.chmod(0o700)
manifest=[]
for folder in ('selfhost_models','tests','worker'):
    for source in sorted((repo/folder).rglob('*')):
        if not source.is_file() or '__pycache__' in source.parts or source.suffix=='.pyc':
            continue
        if source.is_symlink():
            parser.error('source symlinks are not allowed')
        relative=source.relative_to(repo)
        target=root/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
        manifest.append((str(relative),hashlib.sha256(target.read_bytes()).hexdigest()))
for relative in ('pyproject.toml','uv.lock','README.md','docs/scheduler.md'):
    target=root/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(repo/relative,target)
    manifest.append((relative,hashlib.sha256(target.read_bytes()).hexdigest()))
bundle=Path(__file__).resolve().parent
for source in bundle.iterdir():
    if source.is_file() and source.suffix in ('.py','.md'):
        target=root/'reproduce'/source.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source,target)
text=''.join(f'{sha}  {name}\n' for name,sha in sorted(manifest))
(root/'source-sha256-final.txt').write_text(text)
record={'scratch':str(root),'filesystem':fs,'manifest_sha256':hashlib.sha256(text.encode()).hexdigest(),'files':len(manifest),'cpu_only':True}
(root/'.cpu-validation-snapshot.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record))
