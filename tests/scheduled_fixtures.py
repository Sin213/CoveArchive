"""Import-only subprocess doubles; never imported by the production launcher."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import threading
import time

DATA=b'scheduled preservation fixture\n'
BOOT="import sys;sys.path[:0]=sys.argv[1:3];from scheduled_fixtures import worker;raise SystemExit(worker(sys.argv[3],sys.argv[4],sys.argv[5]))"


def event(kind,**fields):
    path=Path(os.environ['XDG_STATE_HOME'])/'events.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
    try: os.write(fd,(json.dumps(dict(kind=kind,pid=os.getpid(),**fields))+'\n').encode())
    finally: os.close(fd)


def fake_catalog(ids,extra=False):
    from test_scan_worker import catalog,release,asset
    result=catalog(*(release(i,assets=[asset(i,size=len(DATA),browser_download_url='https://github.com/Owner/Repo/releases/download/v'+str(i)+'/app.zip')]) for i in map(int,ids.split(','))))
    if extra:
        from dataclasses import replace
        old=result.releases[0]; new=replace(old.assets[0],id=2,name='second.zip')
        result=replace(result,releases=(replace(old,assets=(*old.assets,new)),*result.releases[1:]))
    return result


def worker(mode,behavior,ids):
    block_qt()
    from covearchive import scheduled as s
    from covearchive.github.client import GitHubReleaseClient
    from covearchive.archive import download
    from covearchive.transfer.resolve import DownloadError
    event('worker',mode=mode)
    def discover(self,target):
        event('scan')
        if behavior=='scan_hang':
            while True: time.sleep(1)
        return fake_catalog(ids,extra=behavior=='new_asset')
    GitHubReleaseClient.discover_releases=discover
    @contextmanager
    def stream(repo,asset,*,cancel=None,**kw):
        event('download',asset=asset.id)
        def chunks():
            if behavior in ('slow','hung'):
                while behavior=='hung' or not cancel(): time.sleep(0.02)
                raise DownloadError('cancelled')
            yield DATA
        yield chunks()
    download.open_asset_stream=stream
    if mode=='--scheduled-scan-worker':
        # Match the launcher's os._exit: its stdin watchdog must not outlive this worker.
        os._exit(s.scan_worker())
    os._exit(s.queue_worker())


def parent(behavior,ids,now,deadline='30'):
    block_qt()
    from covearchive import scheduled as s
    import runpy
    s.GRACE_SECONDS=0.5
    source=str(Path(__file__).resolve().parents[1]/'src'); tests=str(Path(__file__).resolve().parent)
    s.command=lambda launcher,mode:[sys.executable,'-I','-B','-c',BOOT,source,tests,mode,behavior,ids]
    original=s.run
    def run(launcher=None,**kw):
        return original(launcher,clock=lambda:float(now),deadline_seconds=float(deadline),
            scan_timeout=0.3 if behavior=='scan_hang' else 10,**kw)
    s.run=run
    sys.argv=[str(Path(source).parent/'run_covearchive.py'),'--scheduled-run']
    try: runpy.run_path(sys.argv[0],run_name='__main__')
    except SystemExit as exc: return exc.code


def block_qt():
    class BlockQt:
        def find_spec(self,name,*args):
            if name.startswith(('PySide6','PyQt')): raise AssertionError(name)
    sys.meta_path.insert(0,BlockQt())
