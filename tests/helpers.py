"""Shared test plumbing: a scratch /data dir per test and a scripted fake for urlopen.

Every module here reads its file paths and credentials from the environment at import
time, so each test points the env at a fresh temp dir and reloads the modules it uses.
Nothing in this suite touches the network: urlopen is replaced for the whole test.
"""
import importlib
import io
import json
import os
import tempfile
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def respond(obj):
    return Response(json.dumps(obj).encode())


class FakeHTTP:
    """Route requests by URL substring to handler(request, json_body) -> object or Response.
    Records every call's last path segment in .calls; unrouted URLs fail the test."""

    def __init__(self):
        self.routes, self.calls = [], []

    def route(self, needle, handler):
        self.routes.append((needle, handler))
        return self

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        body = None
        data = getattr(req, "data", None)
        if data:
            try:
                body = json.loads(data)
            except ValueError:
                body = data.decode()
        self.calls.append(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
        for needle, handler in self.routes:
            if needle in url:
                out = handler(req, body)
                if isinstance(out, BaseException):
                    raise out
                return out if isinstance(out, Response) else respond(out)
        raise AssertionError(f"unexpected request: {url}")


class EnvTestCase(unittest.TestCase):
    """Temp data dir + env vars + a FakeHTTP installed as urlopen, all undone afterwards."""

    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._old_env = dict(os.environ)
        os.environ.update({
            "ENERGY_DATA_DIR": self.dir,
            "STATE_FILE": os.path.join(self.dir, "state.json"),
            "HISTORY_FILE": os.path.join(self.dir, "history.csv"),
            "CALIBRATION_FILE": os.path.join(self.dir, "calibration.json"),
            "WINDOW_FILE": os.path.join(self.dir, "window_report.json"),
            "BLIND_FILE": os.path.join(self.dir, "blind_report.json"),
            "NTFY_TOPIC": "test-topic",
            "GITHUB_TOKEN": "",
            "GIST_TOKEN": "",
            **self.env,
        })
        for k in ("GITHUB_TOKEN", "GIST_TOKEN"):
            os.environ.pop(k, None)
        self.http = FakeHTTP()
        self._old_urlopen = urllib.request.urlopen
        urllib.request.urlopen = self.http

    def tearDown(self):
        urllib.request.urlopen = self._old_urlopen
        os.environ.clear()
        os.environ.update(self._old_env)
        self.tmp.cleanup()

    def load(self, name):
        """Import (or re-import) a module so it picks up this test's env."""
        mod = importlib.import_module(name)
        return importlib.reload(mod)

    def path(self, *parts):
        return os.path.join(self.dir, *parts)
