"""
Web interface for the badminton tournament planner.

    uvicorn webapp.server:app --host 0.0.0.0 --port 8000

Step 1 (setup) runs in-process: it takes well under a second.
Step 2 (scheduler) runs scheduler.py in a subprocess, one run at a time from a queue,
so the optimizer can use every core and a run can be cancelled.
Every run is kept under $DATA_DIR/runs/<id>/ (meta.json + inputs + outputs).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import random
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scheduler as SCH  # noqa: E402
import tournament_setup as TS  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
RUNS = DATA / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"
OUTPUT_FILES = ["schedule.html", "statistics.txt", "statistics.json", "schedule.csv", "convocations.csv",
                "team_stats.csv", "plan.json", "tournament.json", "settings.json", "log.txt",
                "config.toml", "report.txt"]

app = FastAPI(title="Badminton tournament planner")
_lock = threading.RLock()


# --------------------------------------------------------------------------- password protection
# Set APP_PASSWORD to require one shared password (no user accounts). Unset = open (trusted LAN).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
COOKIE = "bp_auth"
OPEN_PATHS = {"/login", "/logout", "/api/health", "/api/session"}


def _secret() -> bytes:
    """Random key kept in the data folder, so sessions survive restarts; changing the password logs everyone out."""
    p = DATA / "secret.key"
    if not p.exists():
        p.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p.read_text(encoding="utf-8").strip().encode()


def _token() -> str:
    return hmac.new(_secret(), ("planner:" + APP_PASSWORD).encode(), hashlib.sha256).hexdigest()


def _logged_in(request: Request) -> bool:
    return not APP_PASSWORD or hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


@app.middleware("http")
async def _auth(request: Request, call_next):
    if _logged_in(request) or request.url.path in OPEN_PATHS:
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Badminton tournament planner</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f4;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1d2421}
form{background:#fff;border:1px solid #dfe4e1;border-radius:12px;padding:28px 30px;width:320px;box-shadow:0 2px 8px rgba(0,0,0,.05)}
h1{font-size:18px;color:#176a4f;margin:0 0 18px}
label{display:block;font-size:12px;color:#66706b;margin-bottom:6px}
input{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #dfe4e1;border-radius:7px;font:inherit}
button{margin-top:14px;width:100%;padding:9px;border:0;border-radius:7px;background:#176a4f;color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{background:#fde8e8;color:#a32020;border-radius:7px;padding:8px 10px;margin-bottom:12px;font-size:13px}
</style></head><body>
<form method="post" action="/login">
<h1>Badminton tournament planner</h1>
{error}
<label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Log in</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not APP_PASSWORD or _logged_in(request):
        return RedirectResponse("/", status_code=303)
    return LOGIN_PAGE.replace("{error}", "")


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    if APP_PASSWORD and hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        resp = RedirectResponse("/", status_code=303)
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
        resp.set_cookie(COOKIE, _token(), max_age=14 * 24 * 3600, httponly=True, samesite="lax", secure=https)
        return resp
    await asyncio.sleep(1.0)                       # slows down password guessing
    return HTMLResponse(LOGIN_PAGE.replace("{error}", '<div class="err">Wrong password.</div>'), status_code=401)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login" if APP_PASSWORD else "/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/session")
def session(request: Request):
    return {"auth": bool(APP_PASSWORD), "logged_in": _logged_in(request)}


# --------------------------------------------------------------------------- run store
def _new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + "".join(random.choices(string.ascii_lowercase, k=4))


def _run_dir(run_id: str) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise HTTPException(400, "Bad run id")
    d = RUNS / run_id
    if not (d / "meta.json").exists():
        raise HTTPException(404, "Run not found")
    return d


def _read_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


def _write_meta(d: Path, meta: dict):
    with _lock:
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "meta.json")


def _update_meta(d: Path, **kv) -> dict:
    with _lock:
        meta = _read_meta(d)
        meta.update(kv)
        _write_meta(d, meta)
        return meta


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _create_run(kind: str, name: str, **extra) -> tuple[str, Path]:
    run_id = _new_id()
    d = RUNS / run_id
    d.mkdir(parents=True)
    meta = {"id": run_id, "type": kind, "name": name or run_id, "created": _now(), "status": "new", **extra}
    _write_meta(d, meta)
    return run_id, d


# --------------------------------------------------------------------------- scheduler settings
SETTINGS_DEFAULTS = {
    "event": {},                     # overrides of start_time, end_time, courts, match_duration, min_rest,
                                     # convocation_lead, convocation_rounding (filled from the tournament)
    "solver": "auto", "time_limit": 60, "jobs": None, "workers": None, "seed": 1, "free_round_order": False,
    "makespan_weight": None, "overtime_weight": None, "ko_wait_weight": 1.0,
    "free_wait": 60, "wait_tiers": [[b, w] for b, w in SCH.DEFAULT_TIERS], "wait_steps": [],
    "pool_spread_weight": 2.0, "series_spread_weight": 5.0,
    "simulate": 300, "duration_cv": 0.25, "max_advance": None, "csv_sep": ",",
}
EVENT_FLAGS = {"start_time": "--start", "end_time": "--end", "courts": "--courts", "match_duration": "--duration",
               "min_rest": "--rest", "convocation_lead": "--lead", "convocation_rounding": "--rounding"}


def merge_settings(s: dict | None) -> dict:
    s = dict(s or {})
    if "settings" in s and isinstance(s["settings"], dict):     # exported file wrapper
        s = s["settings"]
    out = json.loads(json.dumps(SETTINGS_DEFAULTS))
    for k, v in s.items():
        if k in out:
            out[k] = v
    out["event"] = {k: v for k, v in (s.get("event") or {}).items() if k in EVENT_FLAGS}
    return out


def _pairs(v) -> str:
    if isinstance(v, str):
        return v
    pairs = [(int(float(b)), float(w)) for b, w in (v or []) if str(b) != "" and str(w) != ""]
    return ",".join(f"{b}:{w:g}" for b, w in sorted(pairs)) or "none"


def settings_to_args(s: dict) -> list[str]:
    a: list[str] = []
    for k, flag in EVENT_FLAGS.items():
        v = s["event"].get(k)
        if v is None:
            continue
        a += [flag, str(v)]
    a += ["--solver", str(s["solver"]), "--time-limit", str(float(s["time_limit"]))]
    for key, flag in (("jobs", "--jobs"), ("workers", "--workers"), ("makespan_weight", "--makespan-weight"),
                      ("overtime_weight", "--overtime-weight"), ("max_advance", "--max-advance")):
        if s.get(key) not in (None, ""):
            a += [flag, str(s[key])]
    a += ["--seed", str(int(s["seed"])), "--ko-wait-weight", str(float(s["ko_wait_weight"])),
          "--free-wait", str(int(s["free_wait"])), "--wait-tiers", _pairs(s["wait_tiers"]),
          "--wait-steps", _pairs(s["wait_steps"]),
          "--pool-spread-weight", str(float(s["pool_spread_weight"])),
          "--series-spread-weight", str(float(s["series_spread_weight"])),
          "--simulate", str(int(s["simulate"] or 0)), "--duration-cv", str(float(s["duration_cv"])),
          "--csv-sep", str(s["csv_sep"] or ",")]
    if s.get("free_round_order"):
        a.append("--free-round-order")
    return a


def tournament_summary(doc: dict) -> dict:
    """Validate a tournament document and describe it."""
    try:
        P = SCH.Problem(doc, {})
    except SystemExit as e:
        raise HTTPException(400, f"Invalid tournament file: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid tournament file: {type(e).__name__}: {e}") from e
    series = []
    for s in doc["series"]:
        n = sum(1 for m in doc["matches"] if m["series"] == s["name"])
        series.append({"name": s["name"], "teams": s.get("n_teams", len(s.get("teams", []))),
                       "format": (s.get("format") or {}).get("description", ""), "matches": n,
                       "match_duration": s.get("match_duration")})
    return {"event": doc["event"], "series": series, "matches": P.M, "teams": P.T}


# --------------------------------------------------------------------------- job queue
class Runner:
    def __init__(self):
        self.queue: list[str] = []
        self.current: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cv = threading.Condition()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, run_id: str):
        with self.cv:
            self.queue.append(run_id)
            self.cv.notify()

    def cancel(self, run_id: str) -> bool:
        with self.cv:
            if run_id in self.queue:
                self.queue.remove(run_id)
                _update_meta(RUNS / run_id, status="cancelled", finished=_now())
                return True
            if run_id == self.current and self.proc and self.proc.poll() is None:
                _update_meta(RUNS / run_id, cancel_requested=True)
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, AttributeError):
                    self.proc.terminate()
                return True
        return False

    def position(self, run_id: str) -> int | None:
        with self.cv:
            return self.queue.index(run_id) + 1 if run_id in self.queue else None

    def _loop(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                run_id = self.queue.pop(0)
                self.current = run_id
            try:
                self._run(run_id)
            except Exception as e:  # noqa: BLE001
                _update_meta(RUNS / run_id, status="failed", error=str(e), finished=_now())
            finally:
                with self.cv:
                    self.current, self.proc = None, None

    def _run(self, run_id: str):
        d = RUNS / run_id
        settings = json.loads((d / "settings.json").read_text(encoding="utf-8"))
        cmd = [sys.executable, str(ROOT / "scheduler.py"), "tournament.json", *settings_to_args(settings), "-o", "."]
        _update_meta(d, status="running", started=_now(), command=" ".join(cmd[1:]))
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        with open(d / "log.txt", "w", encoding="utf-8") as log:
            log.write("$ scheduler.py " + " ".join(cmd[3:]) + "\n\n")
            log.flush()
            kw = {"start_new_session": True} if os.name == "posix" else {}
            with self.cv:
                self.proc = subprocess.Popen(cmd, cwd=d, stdout=log, stderr=subprocess.STDOUT, env=env, **kw)
            code = self.proc.wait()
        meta = _read_meta(d)
        if meta.get("cancel_requested"):
            _update_meta(d, status="cancelled", finished=_now())
        elif code == 0 and (d / "statistics.json").exists():
            _update_meta(d, status="done", finished=_now(), summary=_summary(d))
        else:
            tail = (d / "log.txt").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
            _update_meta(d, status="failed", finished=_now(), error="\n".join(tail))


def _summary(d: Path) -> dict:
    st = json.loads((d / "statistics.json").read_text(encoding="utf-8"))
    g, info = st["plan"], st.get("info", {})
    th = [str(b) for b in g.get("thresholds", [])]
    top = th[min(2, len(th) - 1)] if th else None
    return {"day_end": g.get("day_end"), "end_limit": info.get("end_limit"), "grade": g.get("grade"),
            "wait_index": g.get("wait_index"), "mean_gap": (g.get("all") or {}).get("mean"),
            "worst_gap": g.get("worst_any"), "over_top": g.get("over", {}).get(top) if top else None,
            "over_threshold": top, "court_use": g.get("court_use"),
            "worst_ko": (g.get("entry_wait") or {}).get("max"), "series_stretch": g.get("series_stretch_rel"),
            "solver": info.get("solver"), "matches": info.get("matches"),
            "sim_end_mean": ((st.get("simulation") or {}).get("makespan") or {}).get("mean"), "t0": info.get("t0")}


runner = Runner()


def _recover():
    """After a restart, runs that were queued or running are lost."""
    for d in RUNS.iterdir():
        if (d / "meta.json").exists():
            meta = _read_meta(d)
            if meta.get("status") in ("queued", "running"):
                _update_meta(d, status="interrupted", finished=_now())


_recover()


# --------------------------------------------------------------------------- helpers
def _load_upload(name: str, data: bytes) -> dict:
    suffix = Path(name or "").suffix.lower()
    text = data.decode("utf-8-sig")
    try:
        if suffix == ".toml":
            import tomllib
            return tomllib.loads(text)
        if suffix in (".yaml", ".yml"):
            import yaml
            return yaml.safe_load(text) or {}
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read {name}: {e}") from e


class SetupRequest(BaseModel):
    config: dict
    picks: dict = {}
    no_fit: bool = False
    name: str = ""


class ScheduleRequest(BaseModel):
    settings: dict = {}
    tournament: dict | None = None
    source_run: str | None = None
    name: str = ""


class RenameRequest(BaseModel):
    name: str


# --------------------------------------------------------------------------- API: defaults
@app.get("/api/defaults")
def defaults():
    return {"setup": TS.normalize_config({"series": [{"name": "A", "teams": 16}]}),
            "settings": SETTINGS_DEFAULTS, "cpu_count": os.cpu_count()}


# --------------------------------------------------------------------------- API: step 1
@app.post("/api/setup/parse")
async def setup_parse(file: UploadFile = File(...)):
    cfg = _load_upload(file.filename, await file.read())
    if not isinstance(cfg, dict) or "series" not in cfg:
        raise HTTPException(400, "This does not look like an event config (no 'series').")
    return TS.normalize_config(cfg)


@app.post("/api/setup/propose")
def setup_propose(req: SetupRequest):
    try:
        res = TS.run_setup(req.config, req.picks, req.no_fit)
    except TS.SetupError as e:
        raise HTTPException(400, str(e)) from e
    res.pop("doc")
    res["report"] = TS.report_text(res, top=99)
    return res


@app.post("/api/setup/generate")
def setup_generate(req: SetupRequest):
    try:
        res = TS.run_setup(req.config, req.picks, req.no_fit)
    except TS.SetupError as e:
        raise HTTPException(400, str(e)) from e
    doc = res.pop("doc")
    name = req.name or res["config"]["event"].get("name") or "setup"
    run_id, d = _create_run("setup", name, picks=req.picks, no_fit=req.no_fit)
    (d / "tournament.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    (d / "config.toml").write_text(TS.config_to_toml(res["config"]), encoding="utf-8")
    (d / "report.txt").write_text(TS.report_text(res, top=99), encoding="utf-8")
    (d / "result.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    est = res["estimate"]
    _update_meta(d, status="done", finished=_now(),
                 summary={"matches": est["matches"], "estimated_end": est["estimated_end"], "fits": est["fits"],
                          "series": [f"{s['name']}: {s['options'][s['chosen'] - 1]['description']}"
                                     for s in res["series"]]})
    return {"run_id": run_id, "name": name, "summary": tournament_summary(doc)}


class ExampleRequest(BaseModel):
    disciplines: list[str] = ["SH", "SD", "DH", "DD", "MX"]
    best: str = "R4"
    worst: str = "NC"
    ranks_per_series: int = 2
    n_series: int | None = None
    min_teams: int = 5
    max_teams: int = 20
    names: bool = True
    seed: int | None = None


@app.get("/api/setup/example-options")
def example_options():
    return {"ranks": TS.RANKS, "disciplines": TS.DISCIPLINES}


@app.post("/api/setup/example")
def setup_example(req: ExampleRequest):
    if req.best not in TS.RANKS or req.worst not in TS.RANKS:
        raise HTTPException(400, "Unknown ranking.")
    ex = TS.generate_example(req.disciplines, req.best, req.worst, req.ranks_per_series, req.n_series,
                             req.min_teams, req.max_teams, req.names, req.seed)
    teams = sum(len(s["teams"]) if isinstance(s["teams"], list) else int(s["teams"]) for s in ex["series"])
    return {**ex, "teams": teams}


@app.post("/api/setup/toml")
def setup_toml(req: SetupRequest):
    return PlainTextResponse(TS.config_to_toml(req.config), media_type="application/toml")


# --------------------------------------------------------------------------- API: step 2
@app.post("/api/schedule/parse-tournament")
async def parse_tournament(file: UploadFile = File(...)):
    doc = _load_upload(file.filename, await file.read())
    if not isinstance(doc, dict) or "matches" not in doc:
        raise HTTPException(400, "This does not look like a tournament file (no 'matches'). "
                                 "Generate one in step 1.")
    return {"doc": doc, "summary": tournament_summary(doc)}


@app.post("/api/schedule/parse-settings")
async def parse_settings(file: UploadFile = File(...)):
    data = _load_upload(file.filename, await file.read())
    if not isinstance(data, dict):
        raise HTTPException(400, "Settings file must be a JSON object.")
    return merge_settings(data)


@app.get("/api/runs/{run_id}/tournament")
def run_tournament(run_id: str):
    d = _run_dir(run_id)
    p = d / "tournament.json"
    if not p.exists():
        raise HTTPException(404, "This run has no tournament file.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    return {"doc": doc, "summary": tournament_summary(doc)}


@app.post("/api/schedule/run")
def schedule_run(req: ScheduleRequest):
    if req.tournament is not None:
        doc, source = req.tournament, "upload"
    elif req.source_run:
        doc = json.loads((_run_dir(req.source_run) / "tournament.json").read_text(encoding="utf-8"))
        source = req.source_run
    else:
        raise HTTPException(400, "No tournament given.")
    tournament_summary(doc)                                   # validation
    settings = merge_settings(req.settings)
    try:
        settings_to_args(settings)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Invalid settings: {e}") from e
    name = req.name or doc.get("event", {}).get("name") or "schedule"
    run_id, d = _create_run("schedule", name, source=source)
    (d / "tournament.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    (d / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    _update_meta(d, status="queued", time_limit=float(settings["time_limit"]),
                 simulate=int(settings["simulate"] or 0))
    runner.submit(run_id)
    return {"run_id": run_id}


# --------------------------------------------------------------------------- API: runs
@app.get("/api/runs")
def list_runs():
    out = []
    for d in sorted(RUNS.iterdir(), reverse=True):
        if (d / "meta.json").exists():
            meta = _read_meta(d)
            meta["queue_position"] = runner.position(meta["id"])
            out.append(meta)
    return out


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    d = _run_dir(run_id)
    meta = _read_meta(d)
    meta["queue_position"] = runner.position(run_id)
    meta["files"] = [f for f in OUTPUT_FILES if (d / f).exists()]
    if meta["type"] == "setup":
        res = json.loads((d / "result.json").read_text(encoding="utf-8"))
        res["report"] = (d / "report.txt").read_text(encoding="utf-8")
        meta["result"] = res
    else:
        meta["settings"] = json.loads((d / "settings.json").read_text(encoding="utf-8"))
        if (d / "statistics.json").exists():
            meta["statistics"] = json.loads((d / "statistics.json").read_text(encoding="utf-8"))
    return meta


@app.get("/api/runs/{run_id}/log")
def run_log(run_id: str, offset: int = 0):
    p = _run_dir(run_id) / "log.txt"
    if not p.exists():
        return {"text": "", "offset": 0}
    data = p.read_bytes()
    return {"text": data[offset:].decode("utf-8", errors="replace"), "offset": len(data)}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    _run_dir(run_id)
    if not runner.cancel(run_id):
        raise HTTPException(409, "This run is not queued or running.")
    return {"ok": True}


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str):
    """Ask a running optimization to finish now with the best plan found so far."""
    d = _run_dir(run_id)
    if _read_meta(d).get("status") != "running":
        raise HTTPException(409, "Only a running run can be stopped (cancel a queued one).")
    (d / "STOP").touch()
    _update_meta(d, stop_requested=_now())
    return {"ok": True}


@app.patch("/api/runs/{run_id}")
def rename_run(run_id: str, req: RenameRequest):
    return _update_meta(_run_dir(run_id), name=req.name.strip() or run_id)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    d = _run_dir(run_id)
    if _read_meta(d).get("status") in ("queued", "running"):
        raise HTTPException(409, "Cancel the run before deleting it.")
    shutil.rmtree(d)
    return {"ok": True}


@app.get("/api/runs/{run_id}/files/{name}")
def run_file(run_id: str, name: str, download: bool = False):
    d = _run_dir(run_id)
    if name not in OUTPUT_FILES or not (d / name).exists():
        raise HTTPException(404, "File not found")
    media = {".html": "text/html", ".csv": "text/csv", ".json": "application/json",
             ".txt": "text/plain", ".toml": "text/plain"}.get(Path(name).suffix, "application/octet-stream")
    headers = {"Content-Disposition": f'attachment; filename="{run_id}-{name}"'} if download else None
    return FileResponse(d / name, media_type=media + ("; charset=utf-8" if media.startswith("text") else ""),
                        headers=headers)


@app.get("/api/runs/{run_id}/zip")
def run_zip(run_id: str):
    d = _run_dir(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in OUTPUT_FILES + ["meta.json"]:
            if (d / f).exists():
                z.write(d / f, f)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.zip"'})


@app.get("/api/health")
def health():
    return {"ok": True, "time": time.time()}


@app.exception_handler(TS.SetupError)
def _setup_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
