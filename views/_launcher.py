#!/usr/bin/env python3
"""Generic HTML view launcher for cl.

Run by `cl view <name>` under the SYSTEM python (which has pywebview + GTK),
not the clife venv. Each view lives in views/<name>/ and provides:

  scan.py    writes a JSON snapshot to the path given as argv[1]
  view.html  fetches ./data.json and renders it

This launcher copies view.html to a runtime dir, runs scan.py -> data.json,
serves the runtime dir on a local port, and opens a native pywebview window.
A POST to /rescan re-runs the scanner so the in-window refresh button works.

Usage: _launcher.py <view-name> [port]
"""
import os
import shutil
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer

import webview

VIEWS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 19848


def view_paths(name):
    view_dir = os.path.join(VIEWS_DIR, name)
    return {
        "dir": view_dir,
        "scan": os.path.join(view_dir, "scan.py"),
        "html": os.path.join(view_dir, "view.html"),
        "runtime": os.path.join("/tmp", "cl-view", name),
    }


def run_scan(paths):
    subprocess.run(
        [sys.executable, paths["scan"], os.path.join(paths["runtime"], "data.json")],
        check=True,
    )


def setup(paths):
    os.makedirs(paths["runtime"], exist_ok=True)
    shutil.copy2(paths["html"], os.path.join(paths["runtime"], "view.html"))
    run_scan(paths)


def make_handler(paths):
    runtime = paths["runtime"]

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=runtime, **kwargs)

        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path == "/rescan":
                run_scan(paths)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def main():
    if len(sys.argv) < 2:
        print("usage: _launcher.py <view-name> [port]", file=sys.stderr)
        sys.exit(2)

    name = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    paths = view_paths(name)

    if not os.path.isfile(paths["html"]) or not os.path.isfile(paths["scan"]):
        print(f"view '{name}' is missing scan.py or view.html in {paths['dir']}",
              file=sys.stderr)
        sys.exit(1)

    setup(paths)

    httpd = TCPServer(("127.0.0.1", port), make_handler(paths))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    webview.create_window(
        f"cl view — {name}",
        url=f"http://127.0.0.1:{port}/view.html",
        width=900,
        height=800,
        resizable=True,
    )
    webview.start()


if __name__ == "__main__":
    main()
