"""Tiny file-backed background job system.

Why not Celery/Redis? Zero setup, free, and a reviewer can run it in 1 minute.
Each job lives in data/jobs/<id>/ with meta.json (status/progress), inputs and outputs, so
users can close the tab and come back to /jobs/<id> (or the Recent jobs list) later,
even after a server restart (finished jobs survive; in-flight jobs are marked 'interrupted').
"""
import json, threading, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
_pool = ThreadPoolExecutor(max_workers=2)
_lock = threading.Lock()


class UserError(Exception):
    """Bad input that should be shown to the user as a clear message (not a crash)."""


def _meta_path(jid): return JOBS_DIR / jid / "meta.json"


def _write(jid, **kw):
    with _lock:
        p = _meta_path(jid)
        meta = json.loads(p.read_text()) if p.exists() else {}
        meta.update(kw)
        tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(meta)); tmp.replace(p)


def get(jid):
    p = _meta_path(jid)
    if not p.exists(): return None
    try: return json.loads(p.read_text())
    except Exception: return None


def list_jobs(limit=30):
    out = []
    for d in JOBS_DIR.iterdir():
        m = get(d.name)
        if m: out.append(m)
    return sorted(out, key=lambda m: m.get("created", 0), reverse=True)[:limit]


def new_job_dir(kind, title):
    jid = uuid.uuid4().hex[:10]
    d = JOBS_DIR / jid; (d / "in").mkdir(parents=True); (d / "out").mkdir()
    _write(jid, id=jid, kind=kind, title=title, status="queued", done=0, total=0,
           message="Queued", created=time.time(), files=[], result=None)
    return jid, d


def submit(jid, fn, *args):
    """fn(job_dir, progress(done,total,msg), *args) -> {'result': {...}, 'files': [names]}"""
    d = JOBS_DIR / jid

    def progress(done, total, msg=""):
        _write(jid, done=done, total=total, message=msg)

    def run():
        _write(jid, status="running", message="Starting")
        try:
            res = fn(d, progress, *args)
            _write(jid, status="done", result=res.get("result"), files=res.get("files", []),
                   message="Finished")
        except UserError as e:
            _write(jid, status="error", message=str(e), user_error=True)
        except Exception as e:  # never crash silently; show something useful
            traceback.print_exc()
            _write(jid, status="error", message=f"Unexpected problem: {e}. The input may be corrupt or in an unsupported format.")
    _pool.submit(run)


def mark_interrupted():
    for m in list_jobs(1000):
        if m["status"] in ("queued", "running"):
            _write(m["id"], status="error", message="Server restarted while this job was running. Please re-upload.")
