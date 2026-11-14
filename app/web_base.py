"""A tiny local web server for this project's browser interface (standard library only).

Safety rules shared by every FA Software web interface:
- listens on 127.0.0.1 only, so nothing on the network can reach it
- the Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding)
- every POST must come from this page (Origin check) and carry JSON (blocks CSRF)
- request bodies are size-limited; responses carry a strict Content-Security-Policy
- static files are served only from static/, never from anywhere else
"""
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".mp3": "audio/mpeg", ".srt": "text/plain; charset=utf-8", ".json": "application/json"}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    """Routes: app.get("/api/x")(fn) / app.post(...)(fn). A route may contain one (?P<name>...) group.

    GET handlers get (query: dict, **groups); POST handlers get (body, **groups). They return
    a JSON-able value, or (bytes, content_type, extra_headers) for a file download."""

    def __init__(self, static_dir, max_body=64 * 1024):
        self.static_dir = Path(static_dir).resolve()
        self.max_body = max_body
        self.routes = {"GET": [], "POST": []}

    def _add(self, method, pattern):
        def deco(fn):
            self.routes[method].append((re.compile(pattern + r"\Z"), fn))
            return fn
        return deco

    def get(self, pattern):
        return self._add("GET", pattern)

    def post(self, pattern):
        return self._add("POST", pattern)

    def handler(self, port):
        app = self
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origins = {f"http://{h}" for h in hosts}

        class Handler(BaseHTTPRequestHandler):
            server_version = "FA-Web"
            sys_version = ""
            timeout = 30

            def log_message(self, fmt, *args):
                pass

            def send(self, status, body, ctype="application/json", headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def dispatch(self, method, arg):
                path = urlsplit(self.path).path
                for regex, fn in app.routes[method]:
                    m = regex.match(path)
                    if not m:
                        continue
                    try:
                        result = fn(arg, **m.groupdict())
                    except HttpError as exc:
                        return self.send(exc.status, {"error": str(exc)})
                    if isinstance(result, tuple):
                        data, ctype, headers = result
                        return self.send(200, data, ctype, headers)
                    return self.send(200, result)
                self.send(404, {"error": "not found"})

            def do_GET(self):
                if self.headers.get("Host") not in hosts:
                    return self.send(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                parts = urlsplit(self.path)
                query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
                if parts.path.startswith("/api/"):
                    return self.dispatch("GET", query)
                name = "index.html" if parts.path == "/" else parts.path.removeprefix("/static/")
                file = (app.static_dir / name).resolve()
                if (parts.path == "/" or parts.path.startswith("/static/")) and \
                        file.parent == app.static_dir and file.is_file() and file.suffix in TYPES:
                    return self.send(200, file.read_bytes(), TYPES[file.suffix])
                self.send(404, {"error": "not found"})

            def do_POST(self):
                if self.headers.get("Host") not in hosts:
                    return self.send(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                if self.headers.get("Origin") not in origins:
                    return self.send(HTTPStatus.FORBIDDEN, {"error": "cross-site request refused"})
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    return self.send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send JSON"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 <= length <= app.max_body:
                    return self.send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self.send(400, {"error": "invalid JSON"})
                self.dispatch("POST", body)

        return Handler

    def serve(self, port, banner):
        server = ThreadingHTTPServer(("127.0.0.1", port), self.handler(port))
        print(f"{banner} on http://127.0.0.1:{port}  (Ctrl+C to quit)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


def field(body, name, kind=str, required=True, max_len=500, default=None):
    """Read one field from a JSON body with a type check."""
    if not isinstance(body, dict):
        raise HttpError(400, "expected a JSON object")
    value = body.get(name, default)
    if value is None:
        if required:
            raise HttpError(400, f"{name} is required")
        return default
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise HttpError(400, f"{name} has the wrong type")
    if kind is str:
        value = value.strip()
        if len(value) > max_len:
            raise HttpError(400, f"{name} is too long (max {max_len})")
        if required and not value:
            raise HttpError(400, f"{name} is required")
    return value
