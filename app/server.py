"""Advent calendar server: serves the page, the calendar config, and optional shared progress.

Stdlib only. Config is re-read on every request, so editing the JSON needs no restart
(except switching "progress" mode, which is also picked up live).

AI-generated: written entirely by Claude (Anthropic). The project owner has not reviewed
every line by hand. See "How this was made" in README.md.
"""
import json
import os
import re
import sys
import tempfile
import threading
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUBLIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CALENDAR_FILE = os.environ.get("CALENDAR_FILE", "calendar.json")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 1024
LOCK = threading.Lock()


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_config():
    """Return (config, errors). config is None if it can't be parsed at all."""
    path = os.path.join(CONFIG_DIR, os.path.basename(CALENDAR_FILE))
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None, [f"config file not found: {path}"]
    except json.JSONDecodeError as e:
        return None, [f"{path} is not valid JSON: line {e.lineno} col {e.colno}: {e.msg}"]
    return cfg, validate(cfg)


def validate(cfg):
    errs = []
    if not isinstance(cfg, dict):
        return ["top level must be a JSON object"]
    if not isinstance(cfg.get("title"), str) or not cfg["title"].strip():
        errs.append('"title" is required')
    try:
        date.fromisoformat(str(cfg.get("startDate")))
    except ValueError:
        errs.append('"startDate" must be a date like "2026-12-01"')
    days = cfg.get("days")
    if not isinstance(days, int) or not 1 <= days <= 366:
        errs.append('"days" must be a whole number from 1 to 366')
    entries = cfg.get("entries")
    if not isinstance(entries, list):
        errs.append('"entries" must be a list')
    else:
        if isinstance(days, int) and len(entries) != days:
            errs.append(f'"days" is {days} but there are {len(entries)} entries')
        for i, e in enumerate(entries, 1):
            if not isinstance(e, dict) or not e.get("title"):
                errs.append(f"entry {i} needs a \"title\"")
            elif not re.match(r"^https?://", str(e.get("url", ""))):
                errs.append(f"entry {i} needs a \"url\" starting with http:// or https://")
    if cfg.get("progress", "browser") not in ("browser", "server"):
        errs.append('"progress" must be "browser" or "server"')
    if "id" in cfg and not re.match(r"^[A-Za-z0-9_-]{1,64}$", str(cfg["id"])):
        errs.append('"id" may only contain letters, numbers, - and _')
    return errs


def cal_id(cfg):
    return str(cfg.get("id") or "calendar")


def progress_path(cfg):
    return os.path.join(DATA_DIR, f"progress-{cal_id(cfg)}.json")


def read_progress(cfg):
    try:
        with open(progress_path(cfg), encoding="utf-8") as f:
            data = json.load(f)
        return sorted({int(d) for d in data.get("finished", []) if 1 <= int(d) <= cfg["days"]})
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        return []


def write_progress(cfg, finished):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".progress-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"finished": sorted(finished)}, f)
    os.replace(tmp, progress_path(cfg))


class Handler(BaseHTTPRequestHandler):
    server_version = "advent-calendar"
    sys_version = ""

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def send(self, status, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def route_path(self):
        return self.path.split("?", 1)[0].split("#", 1)[0]

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.route_path()
        if p in ("/", "/index.html"):
            with open(os.path.join(PUBLIC, "index.html"), "rb") as f:
                return self.send(HTTPStatus.OK, f.read(), "text/html; charset=utf-8")
        if p == "/calendar.json":
            cfg, errs = load_config()
            if cfg is None:
                return self.send(HTTPStatus.INTERNAL_SERVER_ERROR, {"errors": errs})
            if errs:
                return self.send(HTTPStatus.OK, {"errors": errs})
            return self.send(HTTPStatus.OK, cfg)
        if p == "/healthz":
            cfg, errs = load_config()
            return self.send(HTTPStatus.OK if not errs else HTTPStatus.INTERNAL_SERVER_ERROR,
                             {"ok": not errs, "errors": errs})
        if p == "/api/progress":
            cfg = self.server_cfg()
            if cfg is None:
                return
            return self.send(HTTPStatus.OK, {"finished": read_progress(cfg)})
        self.send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self):
        if self.route_path() != "/api/progress":
            return self.send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        cfg = self.server_cfg()
        if cfg is None:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": "bad body size"})
        try:
            body = json.loads(self.rfile.read(length))
            day, done = body["day"], body["done"]
            if not isinstance(day, int) or isinstance(day, bool) or not isinstance(done, bool):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self.send(HTTPStatus.BAD_REQUEST, {"error": 'expected {"day": <int>, "done": <bool>}'})
        if not 1 <= day <= cfg["days"]:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": f"day must be 1..{cfg['days']}"})
        with LOCK:
            finished = set(read_progress(cfg))
            (finished.add if done else finished.discard)(day)
            write_progress(cfg, finished)
        self.send(HTTPStatus.OK, {"finished": sorted(finished)})

    def server_cfg(self):
        """Config if valid and in server-progress mode; otherwise sends a response and returns None."""
        cfg, errs = load_config()
        if cfg is None or errs:
            self.send(HTTPStatus.INTERNAL_SERVER_ERROR, {"errors": errs})
            return None
        if cfg.get("progress", "browser") != "server":
            self.send(HTTPStatus.NOT_FOUND, {"error": "server progress is off"})
            return None
        return cfg


def main():
    cfg, errs = load_config()
    if errs:
        log("CONFIG PROBLEMS (the page will show these too):")
        for e in errs:
            log("  - " + e)
    else:
        log(f'Loaded "{cfg["title"]}": {cfg["days"]} days from {cfg["startDate"]}, '
            f'progress={cfg.get("progress", "browser")}')
    log(f"Listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
