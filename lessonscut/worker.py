import argparse
import fcntl
import threading
import time
from .config import get_settings
from .store import Store,Job
from .pipeline import Pipeline


def run_once(settings,store,pipeline=None):
    claim=store.claim()
    if not claim:
        return False
    jid,rev,token=claim
    stopped=threading.Event()
    def heartbeat():
        while not stopped.wait(10):
            if not store.heartbeat(jid,rev,token):
                return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        (pipeline or Pipeline(settings,store)).execute(jid,rev,token)
    except Exception as exc:
        # Credentials and response bodies are never part of exception output.
        message=str(exc)[:1200]
        with store.session() as s:
            job=s.get(Job,jid)
            valid=job and job.revision==rev and job.token==token
        if valid:
            store.transition(jid,rev,token,"NEEDS_REVIEW",reason=message)
    finally:
        stopped.set();thread.join(timeout=1)
    return True


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--once",action="store_true");args=parser.parse_args()
    settings=get_settings();store=Store(settings.database_url)
    lock=(settings.data_dir/"worker.lock").open("w")
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Only one worker is supported in v0.1; another worker holds this data directory") from None
    # Never assume an expired worker released GPU or stopped an unknown API call.
    if store.stale_claims():
        raise SystemExit("Stale worker claim detected; inspect process/provider state before recovery")
    while True:
        worked=run_once(settings,store)
        if args.once:return
        if not worked:time.sleep(2)


if __name__=="__main__":main()
