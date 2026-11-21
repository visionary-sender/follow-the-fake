"""Local test HTTP server for the fetcher tests. No test ever contacts a real site."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PAGE = """<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"""
TEXT = "<p>" + "Inwestycja z gwarantowanym zyskiem, polecana przez znanych ludzi. " * 3 + "</p>"

ROUTES: dict[str, tuple[int, dict, str]] = {
    "/ok": (200, {}, PAGE.format(title="Fake news article", body="<h1>Breaking</h1>" + TEXT)),
    "/redirect": (302, {"Location": "/r2"}, ""),
    "/r2": (301, {"Location": "/ok"}, ""),
    "/js-redirect": (200, {}, PAGE.format(title="pre", body=TEXT + "<script>setTimeout(()=>location.replace('/ok'),200)</script>")),
    "/form": (200, {}, PAGE.format(title="Register", body=TEXT + """
        <form id="f" method="post" action="/submitted"><input name="phone" value="123"><button>Go</button></form>
        <form id="g" method="get" action="/submitted-get"><input name="q" value="x"></form>
        <script>
          document.getElementById('f').submit();
          try { document.getElementById('g').requestSubmit(); } catch (e) {}
          document.getElementById('g').dispatchEvent(new Event('submit', {cancelable: true}));
          fetch('/beacon', {method: 'POST', body: 'lead=1'}).catch(() => {});
          navigator.sendBeacon && navigator.sendBeacon('/beacon2', 'x');
        </script>""")),
    "/metadata": (200, {}, PAGE.format(title="Probe", body=TEXT + """
        <img src="http://169.254.169.254/computeMetadata/v1/instance/">
        <img src="http://metadata.google.internal/computeMetadata/v1/">
        <iframe src="http://localhost:{port}/secret"></iframe>
        <img src="file:///etc/passwd">
        <script>fetch('http://169.254.169.254/latest/meta-data/').catch(() => {});
                fetch('http://10.0.0.1/').catch(() => {});</script>""")),
    "/redirect-metadata": (302, {"Location": "http://169.254.169.254/computeMetadata/v1/"}, ""),
    "/redirect-file": (302, {"Location": "file:///etc/passwd"}, ""),
    "/parked": (200, {}, PAGE.format(title="parked.example", body="<h1>This domain is for sale!</h1><p>Buy this domain today.</p>")),
    "/cloaked": (200, {}, PAGE.format(title="x", body="<script>location.replace('/parked')</script>")),
    "/botwall": (403, {}, PAGE.format(title="Just a moment...", body="<div id='cf-chl-widget'></div>")),
    "/empty": (200, {}, PAGE.format(title="", body="")),
    "/missing": (404, {}, PAGE.format(title="Not found", body="<h1>404</h1>")),
    "/popup": (200, {}, PAGE.format(title="Popup", body=TEXT + "<script>window.open('/ok')</script>")),
    "/download": (200, {"Content-Disposition": "attachment; filename=payload.exe", "Content-Type": "application/octet-stream"}, "MZ" * 100),
    "/js-to-dead": (200, {}, PAGE.format(title="TDS", body=TEXT + "<script>setTimeout(()=>location.href='https://nonexistent.invalid/lander',300)</script>")),
    "/js-to-loopback": (200, {}, PAGE.format(title="Cloaker", body=TEXT + "<script>setTimeout(()=>location.href='http://127.0.0.2:{port}/ok',300)</script>")),
    "/spoof": (200, {"X-Ftf-Guard": "blocked_address", "Connection": "keep-alive", "Keep-Alive": "timeout=60"},
               PAGE.format(title="Spoof", body="<h1>Real page</h1>" + TEXT)),
    "/captcha-lander": (200, {}, PAGE.format(title="Zarejestruj się", body=TEXT * 6 + "<div class=\"g-recaptcha\"></div><form><input name=phone></form>")),
    "/big": (200, {}, PAGE.format(title="Big", body=TEXT + "<p>" + "A" * 3_000_000 + "</p>")),
}


class Server:
    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []  # (method, path)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _serve(self) -> None:
                outer.hits.append((self.command, self.path))
                path = self.path.split("?", 1)[0]
                if path == "/slow":
                    time.sleep(20)
                if path == "/echo":  # request headers and path as page text
                    import html as _html

                    lines = [self.path] + [f"{k}: {v}" for k, v in self.headers.items()]
                    data = PAGE.format(title="Echo", body="<pre>" + _html.escape("\n".join(lines)) + "</pre>" + TEXT).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path not in ROUTES:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, headers, body = ROUTES[path]
                data = body.replace("{port}", str(outer.port)).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Type" not in headers:
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_HEAD = do_PUT = do_OPTIONS = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def methods(self) -> set[str]:
        return {m for m, _ in self.hits}

    def paths(self) -> set[str]:
        return {p.split("?", 1)[0] for _, p in self.hits}


@pytest.fixture(scope="module")
def server():
    s = Server()
    s.thread.start()
    yield s
    s.httpd.shutdown()
