"""Pure HTTP real CLI reproduction; forbid SQLite in every client child."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave

import httpx

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--endpoint-file',type=Path,required=True)
parser.add_argument('--api-key-file',type=Path,required=True)
parser.add_argument('--scratch-parent',type=Path,required=True)
parser.add_argument('--stop-file',type=Path,help='optional explicitly mapped fixture stop file (e.g. WSL UNC)')
args=parser.parse_args()
repo=args.repo.resolve(strict=True)
parent=args.scratch_parent.resolve(strict=True)
if parent==repo or repo in parent.parents:
    parser.error('client scratch-parent must be outside the repository')
root=Path(tempfile.mkdtemp(prefix='selfhost-scheduler-client-',dir=parent))
endpoint=json.loads(args.endpoint_file.read_bytes())
url='http://127.0.0.1:'+str(endpoint['port'])
key_file=args.api_key_file.resolve(strict=True)
env={**os.environ,'PYTHONPATH':str(repo),'PYTHONDONTWRITEBYTECODE':'1'}
entry=Path(__file__).with_name('client_entry.py')
client_state=root/'must-not-exist'
records=[]
def client(*arguments):
    command=[sys.executable,str(entry),'--state',str(client_state),'scheduler',*map(str,arguments),'--url',url,'--api-key-file',str(key_file)]
    result=subprocess.run(command,capture_output=True,text=True,env=env,timeout=20)
    if result.returncode:
        raise AssertionError({'command':arguments[0],'exit':result.returncode,'stderr':result.stderr})
    data=json.loads(result.stdout)
    records.append(arguments[0])
    return data
catalog=client('catalog')
body={'deployment_id':endpoint['deployment_id'],'operation':'chat','input':{'model':endpoint['model'],'messages':[{'role':'user','content':'Synthetic portable CPU client request'}]}}
payload=root/'job.json'
payload.write_text(json.dumps(body))
key='portable-cpu-'+root.name
job=client('submit','--file',payload,'--idempotency-key',key,'--urgent')
for _ in range(20):
    current=client('get',job['id'])
    if current['result_state']=='available':
        break
    time.sleep(.05)
result=client('result',job['id'])
duplicate=client('submit','--file',payload,'--idempotency-key',key,'--urgent')
wav=root/'synthetic.wav'
with wave.open(str(wav),'wb') as audio:
    audio.setparams((1,2,16000,0,'NONE','not compressed'))
    audio.writeframes(bytes(32000))
uploaded=client('upload','--file',wav)
public_key=key_file.read_text().strip()
with httpx.Client(trust_env=False,timeout=5) as http:
    ready=http.get(url+'/health/ready').json()
    models=http.get(url+'/v1/models',headers={'Authorization':'Bearer '+public_key}).json()
    worker='http://127.0.0.1:'+str(endpoint['worker_port'])
    bare=http.get(worker+'/health').status_code
    public_worker=http.get(worker+'/health',headers={'x-selfhost-worker-key':public_key}).status_code
assert current['state']=='succeeded' and duplicate['id']==job['id']
assert not client_state.exists() and bare==401 and public_worker==401
assert result['_fixture']['urgent_absent'] and uploaded['artifact_ref']
evidence={'result':'PASS','commands':records,'job':job['id'],'sqlite_forbidden':True,'client_state_created':False,'worker_bare_status':bare,'worker_public_key_status':public_worker,'ready':ready,'models':models,'upload':uploaded,'compute_result':result,'transport':'real loopback TCP','gpu':False}
output=root/'windows-client-evidence.json'
output.write_text(json.dumps(evidence,indent=2))
if args.stop_file:
    args.stop_file.touch(exist_ok=False)
print(json.dumps({'result':'PASS','evidence':str(output),'sqlite_forbidden':True}))
