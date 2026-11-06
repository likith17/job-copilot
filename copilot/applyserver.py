"""Local API for the autofill extension: http://127.0.0.1:8765

    python -m copilot serve          (also starts with the app)

It only listens on this computer, and every request must carry your token
(data/extension_token.txt; paste it into the extension's options once), so a
website you visit can't read your profile.

    GET  /api/ping
    GET  /api/profile
    GET  /api/job?url=<application page URL>      the job + its tailored documents
    GET  /api/file?job=<id>&kind=resume|cover_letter
    POST /api/answer   {question, job_id?, kind?, options?}
    POST /api/answers  {job_id?, answers: [{question, answer}]}     save approved answers
    POST /api/applied  {job_id, url, ats, mode, outcome, filled, unfilled, answers}
"""
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from copilot import apply, db, inputs, llm

PORT = 8765


def token_path(cfg):
    return cfg.data_dir / "extension_token.txt"


def get_token(cfg):
    path = token_path(cfg)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def make_handler(cfg):
    token = get_token(cfg)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json", filename=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Copilot-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.end_headers()

        def _authorized(self):
            origin = self.headers.get("Origin", "")
            if origin and not origin.startswith("chrome-extension://"):
                return False      # a web page, not the extension
            return secrets.compare_digest(self.headers.get("X-Copilot-Token", ""), token)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            if not self._authorized():
                return self._send(401, {"error": "bad or missing token (see the extension's options)"})
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            conn = db.connect(cfg.db_path)
            try:
                handler = ROUTES.get((method, url.path))
                if not handler:
                    return self._send(404, {"error": f"no route {method} {url.path}"})
                result = handler(inputs.apply(cfg), conn, q, body)      # always the current resume
                if isinstance(result, tuple):      # (bytes, ctype, filename)
                    return self._send(200, *result)
                return self._send(200, result)
            except llm.LLMError as exc:
                return self._send(503, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                return self._send(400, {"error": str(exc)})
            finally:
                conn.close()

    return Handler


def _job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown job {job_id}")
    return db.decode(row)


def r_ping(cfg, conn, q, body):
    return {"ok": True, "app": "job-copilot"}


def r_profile(cfg, conn, q, body):
    return apply.load_profile(cfg)


def r_job(cfg, conn, q, body):
    job = apply.job_for_url(conn, q["url"])
    return {"job": apply.job_summary(job) if job else None}


def r_file(cfg, conn, q, body):
    path = apply.documents(_job(conn, q["job"])).get(q["kind"])
    if not path:
        raise ValueError(f"no {q['kind']} generated for this job yet")
    return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.name


def r_answer(cfg, conn, q, body):
    job = _job(conn, body["job_id"]) if body.get("job_id") else None
    return apply.answer_question(cfg, conn, body["question"], job, body.get("kind", "text"), body.get("options"),
                                 draft=body.get("draft", True), required=body.get("required"))


def r_answers(cfg, conn, q, body):
    for a in body.get("answers", []):
        if a.get("question") and a.get("answer"):
            db.save_answer(conn, a["question"], a["answer"], a.get("source", "you"), body.get("job_id"))
    return {"saved": len(body.get("answers", []))}


def r_applied(cfg, conn, q, body):
    job_id = body.get("job_id")
    outcome = body.get("outcome", "submitted")
    if job_id and outcome == "submitted":
        db.set_status(conn, job_id, "Applied")
    db.log_application(conn, job_id, body.get("url", ""), body.get("ats", ""), body.get("mode", "extension"),
                       outcome, body.get("filled", []), body.get("unfilled", []), body.get("note", ""))
    r_answers(cfg, conn, q, {"job_id": job_id, "answers": body.get("answers", [])})
    return {"ok": True}


ROUTES = {
    ("GET", "/api/ping"): r_ping, ("GET", "/api/profile"): r_profile, ("GET", "/api/job"): r_job,
    ("GET", "/api/file"): r_file, ("POST", "/api/answer"): r_answer, ("POST", "/api/answers"): r_answers,
    ("POST", "/api/applied"): r_applied,
}


def serve(cfg, port=PORT, background=False):
    """Start the API. Returns the server, or None if the port is taken (already running)."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    except OSError:
        return None
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    else:
        server.serve_forever()
    return server
