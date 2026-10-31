#!/usr/bin/env python3
"""Legacy Nuvyyo Tablo -> M3U live TV bridge.

Exposes:
  /playlist.m3u        M3U tuner playlist (one entry per Tablo channel)
  /stream/<oid>.ts     live MPEG-TS relay (ffmpeg remux of Tablo HLS)
  /guide.xml           XMLTV (from /data/guide.xml when present)
  /healthz             bridge + Tablo liveness
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TABLO = os.environ.get("TABLO_HOST", "").strip()
GROUP = os.environ.get("GROUP_NAME", "Live TV")
DEBUG = os.environ.get("DEBUG") == "1"
PORT = int(os.environ.get("PORT", "8050"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
GUIDE_XML = os.environ.get("GUIDE_XML", "/data/guide.xml")  # optional XMLTV
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")  # host:port used in playlist URLs


def tget(path, timeout=15):
    with urllib.request.urlopen("http://%s:8885%s" % (TABLO, path), timeout=timeout) as r:
        return json.load(r)


def tpost(path, body=None, timeout=25):
    data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(
        "http://%s:8885%s" % (TABLO, path), data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def channels():
    paths = tget("/guide/channels")
    if not paths:
        return []
    data = tpost("/batch", paths)
    out = []
    for p in paths:
        ch = (data.get(p) or {}).get("channel") or {}
        out.append({
            "oid": p.rsplit("/", 1)[-1],
            "call": ch.get("call_sign") or "CH",
            "major": ch.get("major") or 0,
            "minor": ch.get("minor") or 0,
            "network": ch.get("network") or "",
        })
    out.sort(key=lambda c: (c["major"], c["minor"]))
    return out


def playlist(public_host):
    lines = ["#EXTM3U"]
    for c in channels():
        num = "%s.%s" % (c["major"], c["minor"])
        label = "%s %s" % (num, c["call"])
        if c["network"]:
            label += " (%s)" % c["network"]
        # tvg-id is the channel number, which is also what guide.xml uses,
        # so M3U and XMLTV line up without any manual mapping.
        lines.append(
            '#EXTINF:-1 tvg-id="%s" tvg-chno="%s" tvg-name="%s" group-title="%s",%s'
            % (num, num, c["call"], GROUP, label))
        lines.append("http://%s/stream/%s.ts" % (public_host, c["oid"]))
    return "\n".join(lines) + "\n"


def empty_xmltv():
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<tv generator-info-name="tablo-bridge">']
    for c in channels():
        num = "%s.%s" % (c["major"], c["minor"])
        parts.append('  <channel id="%s"><display-name>%s %s</display-name>'
                     '<display-name>%s</display-name><display-name>%s</display-name></channel>'
                     % (c["call"], num, c["call"], c["call"], num))
    parts.append("</tv>")
    return "\n".join(parts)


def start_stream(oid):
    """Open a Tablo watch session and spawn ffmpeg remuxing to MPEG-TS on stdout."""
    started = time.time()
    w = tpost("/guide/channels/%s/watch" % oid)
    if not w.get("playlist_url"):
        raise RuntimeError("tablo returned no playlist_url: %s" % str(w)[:120])
    if DEBUG:
        sys.stderr.write("watch %s: session after %.2fs\n" % (oid, time.time() - started))
    return subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-analyzeduration", "0", "-probesize", "32768",
         "-fflags", "+genpts+nobuffer", "-flags", "low_delay",
         "-i", w["playlist_url"],
         "-c", "copy", "-muxdelay", "0", "-muxpreload", "0",
         "-flush_packets", "1", "-f", "mpegts", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tablo-bridge/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Dispatcharr probes with HEAD; answering 200 with an empty body makes
        # its stream manager mark the channel dead, so refuse the method and let
        # it fall back to GET (which works).
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        host = PUBLIC_HOST or self.headers.get("Host") or "127.0.0.1:%d" % PORT
        try:
            if path in ("/", "/index.html"):
                self._send(200, "Tablo bridge OK. Endpoints: /playlist.m3u /guide.xml /healthz\n")
            elif path == "/healthz":
                self._send(200, json.dumps({"tablo": TABLO, "channels": len(channels())}),
                           "application/json")
            elif path.endswith(".m3u"):
                self._send(200, playlist(host), "audio/x-mpegurl")
            elif path.endswith(".xml") or path.endswith(".xmltv"):
                if os.path.exists(GUIDE_XML):
                    with open(GUIDE_XML, "rb") as f:
                        self._send(200, f.read(), "application/xml")
                else:
                    self._send(200, empty_xmltv(), "application/xml")
            elif path.startswith("/stream/"):
                oid = path[len("/stream/"):].split(".")[0]
                self.stream(oid)
            else:
                self._send(404, "not found\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._send(500, "error: %s\n" % exc)
            except Exception:
                pass

    def stream(self, oid):
        """Open a Tablo watch session and relay it as MPEG-TS."""
        proc = None
        started = time.time()
        try:
            for attempt in range(3):
                try:
                    proc = start_stream(oid)
                except urllib.error.HTTPError as exc:
                    # Tablo answers 503 itself when no tuner can be allocated
                    self._send(503, "tablo refused a tuner (HTTP %s)\n" % exc.code)
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send(502, "tablo watch failed: %s\n" % exc)
                    return
                first = proc.stdout.read(4096)
                if first:
                    if DEBUG:
                        sys.stderr.write("stream %s: first bytes after %.2fs\n"
                                         % (oid, time.time() - started))
                    break
                proc.kill()
                proc = None
                time.sleep(1)
            if proc is None:
                self._send(503, "tablo returned no stream (all tuners busy?)\n")
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            chunk = first
            while chunk:
                self.wfile.write(chunk)
                chunk = proc.stdout.read(65536)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass


def main():
    if not TABLO:
        sys.exit("TABLO_HOST is required (hostname or IP of the Tablo)")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("tablo-bridge listening on :%d (tablo %s)\n" % (PORT, TABLO))
    srv.serve_forever()


if __name__ == "__main__":
    main()
