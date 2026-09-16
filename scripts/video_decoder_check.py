"""Exercise real bounded CPU child termination/cleanup in the deployed API image."""
import argparse
import base64
import json
import subprocess
from pathlib import Path

import httpx

if __package__:
    from .deployment_target import validate_target
else:
    from deployment_target import validate_target

ROOT = Path(__file__).resolve().parents[1]
CHECK = '''
import asyncio,json,time,os,sys,subprocess
from pathlib import Path
from selfhost_models import video
encoded=sys.stdin.read()
def payload():
 return {'messages':[{'role':'user','content':[{'type':'video_url','video_url':{'url':video.PREFIX+encoded}}]}]}
async def main():
 report={}
 report['os_limits']=json.loads(subprocess.check_output([sys.executable,'-c',
  "import resource,json; from selfhost_models.video_decoder import resource_limits; resource_limits(); print(json.dumps({k:resource.getrlimit(getattr(resource,k)) for k in ('RLIMIT_AS','RLIMIT_CPU','RLIMIT_FSIZE','RLIMIT_NOFILE')}))"],text=True,timeout=5))
 assert not list(Path('/tmp').glob('selfhost-video-*'))
 t=time.perf_counter(); p=payload(); m=await video.prepare_video(p)
 report['maximum_mp4']={'seconds':time.perf_counter()-t,'metadata':m}
 assert m['sampled_frames']==120 and m['source_frames']==3600
 assert not list(Path('/tmp').glob('selfhost-video-*'))
 video.DECODE_SECONDS=.001
 try: await video.prepare_video(payload())
 except video.VideoError as e: report['timeout']=e.code; assert e.code=='video_decode_timeout'
 else: raise AssertionError('expected timeout')
 assert not list(Path('/tmp').glob('selfhost-video-*'))
 video.DECODE_SECONDS=15
 task=asyncio.create_task(video.prepare_video(payload())); await asyncio.sleep(.1); task.cancel()
 try: await task
 except asyncio.CancelledError: report['cancelled']=True
 else: raise AssertionError('expected cancellation')
 assert not list(Path('/tmp').glob('selfhost-video-*'))
 try: os.waitpid(-1,os.WNOHANG)
 except ChildProcessError: report['all_children_reaped']=True
 else: raise AssertionError('unexpected child')
 report['temp_clean']=True
 print(json.dumps(report,indent=2))
asyncio.run(main())
'''


def main(args):
    _, _, containers = validate_target(args.state, args.url)
    health = httpx.get(args.url + "/health/ready", trust_env=False, timeout=5).json()
    assert health["ready"] and not health["inflight"], "requires exclusive idle maintenance window"
    raw = args.fixture.read_bytes()
    assert len(raw) <= 16 * 1024**2
    result = subprocess.run(["docker", "exec", "-i", containers["api"]["Id"], "python", "-c", CHECK],
                            input=base64.b64encode(raw).decode(), capture_output=True, text=True, timeout=45, check=True)
    report = json.loads(result.stdout)
    report["api_image_id"] = containers["api"]["Image"]
    report["container_memory_limit"] = containers["api"]["HostConfig"]["Memory"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "maximum_mp4"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--fixture", type=Path, default=ROOT / ".state/video-fixtures/synthetic-60s-720p60.mp4")
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
