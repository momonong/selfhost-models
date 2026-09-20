import json
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

parser=argparse.ArgumentParser(description='CPU-only documented CLI management workflow')
parser.add_argument('--scratch',type=Path,required=True)
args=parser.parse_args()
root=args.scratch.resolve(strict=True)
if sys.platform!='linux' or not (root/'.cpu-validation-snapshot.json').is_file():
    parser.error('--scratch must be a Linux native snapshot created by snapshot.py')
os.chdir(root)
sys.path.insert(0,str(root))
from selfhost_models.scheduler_store import Store
uv=shutil.which('uv')
if not uv:
    parser.error('uv must be on PATH')
case=Path(tempfile.mkdtemp(prefix='doc-management-',dir=root))
state=case/'state'
asset=case/'synthetic-model'
asset.mkdir()
(asset/'config.json').write_text('{}')
(asset/'model.safetensors').write_bytes(b'synthetic-weights-not-a-real-model')
records=[]
def cli(*arguments,json_result=True):
    command=[uv,'run','--locked','modelctl','--state',str(state),'scheduler',*map(str,arguments)]
    process=subprocess.run(command,capture_output=True,text=True,timeout=20)
    if process.returncode:
        raise AssertionError({'arguments':arguments,'status':process.returncode,'stderr':process.stderr})
    value=json.loads(process.stdout) if json_result else process.stdout
    records.append({'command':arguments[0],'arguments':list(map(str,arguments[1:])), 'returncode':process.returncode,'result':value if json_result else 'argparse help success'})
    return value
assert cli('init')['initialized']
asset_ref=cli('asset-register','--path',asset)['asset_ref']
assert cli('asset-verify',asset_ref)['verified']
examples=[json.loads(body) for body in re.findall(r'```json\s*\n(.*?)\n```',(root/'docs/scheduler.md').read_text(),re.S)]
deployments=[]
for example in examples:
    if 'runtime' not in example:
        continue
    example['asset_ref']=asset_ref
    example['image']='sha256:'+('a' if example['runtime']=='vllm' else 'b')*64
    path=case/(example['runtime']+'.json')
    path.write_text(json.dumps(example))
    deployments.append(cli('register','--file',path)['deployment_id'])
assert len(deployments)==2
assert cli('status')['phase']=='unloaded'
cli('events','--after','0','--limit','100')
cli('collect')
store=Store(state)
epoch=store.acquire_controller()
store.lifecycle_failed(epoch,deployments[0])
assert cli('resume-deployment',deployments[0])['resumed']==deployments[0]
with store.connect() as db:
    assert db.execute('SELECT COUNT(*) FROM blocked_deployments').fetchone()[0]==0
assert any(event['kind']=='operator_resumed_deployment' for event in cli('events','--after','0','--limit','100'))
for command in ('controller','api','unload'):
    cli(command,'--help',json_result=False)
evidence={'result':'PASS','source':'docs/scheduler.md JSON examples','native_state':str(state),'commands':records,'docker_commands_executed':False,'gpu':False,'synthetic_asset':True,'public_key_logged':False}
(root/'management-evidence-final.json').write_text(json.dumps(evidence,indent=2))
print(json.dumps({'result':'PASS','commands':len(records),'deployment_ids':deployments,'evidence':str(root/'management-evidence-final.json')}))
