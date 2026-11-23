#!/usr/bin/env python3
"""Legacy Nuvyyo Tablo -> M3U / HLS bridge.

Endpoints
  /playlist.m3u          M3U playlist of raw-TS URLs (for a proxy like Dispatcharr)
  /playlist.m3u?format=hls
                         the same playlist advertising HLS (for a media server
                         that probes the tuner itself, e.g. Jellyfin directly)
  /hls/<oid>.m3u8        HLS for one channel, proxied from the Tablo session
  /hls/seg?oid=&u=       segment proxy (forwards Range, preserves byteranges)
  /stream/<oid>.ts       raw MPEG-TS relay of the same session
  /cc/<oid>.ts           the same relay with CEA-608 captions burned in
  /playlist.m3u?cc=1     a captioned variant of every channel (TS only)
  /guide.xml             XMLTV (served from GUIDE_XML when present)
  /healthz               bridge + device liveness

A Tablo has no API to release a tuner, allocating one takes 6-11 s, and a stale
session blocks the next allocate. So sessions are cached per channel and fanned
out: one Tablo session serves the probe, the playback and every other reader,
and is released only after IDLE_TTL seconds with nobody attached.

Sessions tune in a BACKGROUND thread. A request never blocks the registry, so
while one client is waiting for the device the next request can attach to the
same warming session instead of queueing behind a global lock (that serialisation
was what made a 3-retry proxy give up).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TABLO = os.environ.get("TABLO_HOST", "").strip()
PORT = int(os.environ.get("PORT", "8050"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# Input probe window for the raw-TS remux. With -c copy the output stream set is
# fixed once ffmpeg has probed, so if the first programme map it sees carries only
# the video PID the whole session is video-only — and, because sessions are cached
# and fanned out, so is every reader that attaches to it. A wider window reaches
# past that first map to a later one that names the audio PID, but it must stay
# bounded: clients ffprobe the endless TS and give up if the first bytes are slow
# (see "Probing behaviour" in docs/TABLO-API.md). ANALYZEDURATION is
# microseconds, PROBESIZE bytes; at OTA bitrates the byte limit usually binds
# first, roughly half a second at 15 Mbps.
ANALYZEDURATION = int(os.environ.get("ANALYZEDURATION", "3000000"))
PROBESIZE = int(os.environ.get("PROBESIZE", "1000000"))
PROBE_TRIES = max(1, int(os.environ.get("PROBE_TRIES", "2")))
PROBE_INFO_TIMEOUT = float(os.environ.get("PROBE_INFO_TIMEOUT", "12"))
# A retry widens the window instead of repeating it. The device's media playlist
# declares ~1.37 MB per one-second segment, so the default 1 MB does not span a
# whole segment; the retry reads several. Only a probe that came back video-only
# pays that latency.
RETRY_WIDEN = 4
RETRY_MAX_ANALYZE = 6000000     # µs; ~6 s, enough for four one-second segments
# A retry must read *different* media to stand any chance of a different
# programme map; the device's segments are one second, so wait for the playlist
# to roll past the bad one before reopening it.
RETRY_DELAY = 1.5
GUIDE_XML = os.environ.get("GUIDE_XML", "/data/guide.xml")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
GROUP = os.environ.get("GROUP_NAME", "Live TV")
DEBUG = os.environ.get("DEBUG") == "1"

IDLE_TTL = float(os.environ.get("IDLE_TTL", "120"))     # keep a session warm this long
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "2"))  # physical tuners
RING = int(os.environ.get("RING_BYTES", str(8 * 1024 * 1024)))   # buffered per session
BURST = int(os.environ.get("START_BURST", str(1536 * 1024)))     # backfill a new reader
STREAM_WAIT = float(os.environ.get("STREAM_WAIT", "60"))  # /stream waits for the tuner
HLS_WAIT = float(os.environ.get("HLS_WAIT", "30"))        # /hls waits for the tuner
TUNE_TIMEOUT = float(os.environ.get("TUNE_TIMEOUT", "45"))

# ---- closed captions -------------------------------------------------------
# Broadcast captions (CEA-608/708) travel inside the H.264 SEI, not as a
# subtitle stream, so nothing downstream can select them: Jellyfin neither
# lists them nor keeps them through a transcode, and MPEG-TS has no text
# subtitle type ffmpeg can encode (dvb_subtitle and dvdsub are bitmap-only and
# refuse text). The one form every client renders is captions burned into the
# picture, which is what a cc=1 request serves.
#
# The text comes from a second ffmpeg decoding the same device session. The
# lavfi `movie` source cannot be given a URL - the graph parser splits the
# value at the colons - so the stream is handed to it through a FIFO.
CC_FONT = os.environ.get("CC_FONT",
                         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
CC_FONTSIZE = os.environ.get("CC_FONTSIZE", "h/16")   # expression, scales with the frame
CC_PRESET = os.environ.get("CC_PRESET", "ultrafast")
CC_CRF = os.environ.get("CC_CRF", "23")
CC_HOLD = float(os.environ.get("CC_HOLD", "1.2"))     # keep a finished cue up this long
CC_LINES = int(os.environ.get("CC_LINES", "3"))
CC_COLS = int(os.environ.get("CC_COLS", "42"))
CC_MAX = int(os.environ.get("CC_MAX", "2"))           # concurrent caption sessions (CPU)
# WebVTT writes MM:SS.mmm until an hour has passed, then HH:MM:SS.mmm.
CUE_RE = re.compile(r"^\s*((?:\d+:)?\d+:\d+)[.,](\d+)\s*-->\s*"
                    r"((?:\d+:)?\d+:\d+)[.,](\d+)")
TAG_RE = re.compile(r"\{[^}]*\}|<[^>]*>")


def log(fmt, *args):
    if DEBUG:
        sys.stderr.write((fmt % args) + "\n")


def note(fmt, *args):
    """For the few lines worth having in production logs without DEBUG.

    What ffmpeg actually probed is the only evidence that a tune came up with
    audio, and it is otherwise thrown away (stderr used to go to /dev/null).
    """
    sys.stderr.write((fmt % args) + "\n")


# "  Stream #0:1[0x101](eng): Audio: ac3 ([129][0][0][0] / 0x0081), 48 kHz, ..."
# search(), not match(): ffmpeg indents the block by two spaces.
STREAM_RE = re.compile(r"Stream #(\d+):(\d+)[^:]*?: (Video|Audio|Subtitle|Data): ([\w0-9]+)")


def tget(path, timeout=15):
    with urllib.request.urlopen("http://%s:8885%s" % (TABLO, path), timeout=timeout) as r:
        return json.load(r)


def tpost(path, body=None, timeout=25):
    data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request("http://%s:8885%s" % (TABLO, path), data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_open(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def channels():
    paths = tget("/guide/channels")
    if not paths:
        return []
    data = tpost("/batch", paths)
    out = []
    for p in paths:
        ch = (data.get(p) or {}).get("channel") or {}
        out.append({"oid": p.rsplit("/", 1)[-1], "call": ch.get("call_sign") or "CH",
                    "major": ch.get("major") or 0, "minor": ch.get("minor") or 0,
                    "network": ch.get("network") or ""})
    out.sort(key=lambda c: (c["major"], c["minor"]))
    return out


def playlist(public_host, fmt="ts", cc=False):
    """Raw TS by default, because a proxy (Dispatcharr) reads bytes and turns a
    .m3u8 URL into a needless ffmpeg transcode; HLS on request, because a media
    server probing an endless TS never finishes its analyze window.

    cc=1 publishes a second entry per channel whose stream carries burned-in
    captions. It is TS-only: HLS is proxied straight from the device, which
    cannot draw anything, so there is no caption form to add there.
    """
    hls = str(fmt).lower() == "hls"
    lines = ["#EXTM3U"]
    for c in channels():
        num = "%s.%s" % (c["major"], c["minor"])
        label = "%s %s" % (num, c["call"])
        if c["network"]:
            label += " (%s)" % c["network"]
        # tvg-id is the call sign, matching the XMLTV channel ids; tvg-chno is the
        # dial position. Dispatcharr hashes a stream on url+tvg-id, so changing
        # either one re-creates every stream on the next refresh.
        if cc and not hls:
            # Dispatcharr identifies a stream by tvg-id alone, so the captioned
            # variant needs its own id or the two collapse onto one stream row.
            lines.append('#EXTINF:-1 tvg-id="%s-CC" tvg-chno="%s" tvg-name="%s CC" '
                         'group-title="%s",%s CC'
                         % (c["call"], num, c["call"], GROUP, label))
            lines.append("http://%s/cc/%s.ts" % (public_host, c["oid"]))
            continue
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-chno="%s" tvg-name="%s" group-title="%s",%s'
                     % (c["call"], num, c["call"], GROUP, label))
        lines.append(("http://%s/hls/%s.m3u8" if hls else "http://%s/stream/%s.ts")
                     % (public_host, c["oid"]))
    return "\n".join(lines) + "\n"


def empty_xmltv():
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-name="tablo-bridge">']
    for c in channels():
        num = "%s.%s" % (c["major"], c["minor"])
        parts.append('  <channel id="%s"><display-name>%s %s</display-name></channel>'
                     % (num, num, c["call"]))
    parts.append("</tv>")
    return "\n".join(parts)


class Busy(Exception):
    pass


class CCWorker:
    """Keep one channel's current caption text in a file ffmpeg can draw.

    Extraction needs the lavfi `movie` source: the Debian ffmpeg in this image
    has no `subcc` filter, but `movie=<path>[out0+subcc]` still decodes CC1. It
    cannot be handed a URL (the graph parser splits the value at the colons),
    and it cannot be handed a Tablo watch URL either way - that answers with a
    playlist, not media - so a feeder ffmpeg turns the device's HLS into a plain
    TS on stdout and the extractor reads it as /dev/stdin. The feeder also keeps
    the extractor's clock honest: it starts at the live edge and re-muxes, so
    the bytes are continuous even where the device changes segment tokens.

    The clock is the extractor's, not wall time. It starts counting when it
    opens its input, a fraction of a second before or after the burn-in encoder
    opens the playlist, so cue write times are compared against cue start times
    to learn that difference and keep the two processes in step.
    """

    def __init__(self, oid):
        self.dir = tempfile.mkdtemp(prefix="cc-%s-" % oid)
        self.vtt = os.path.join(self.dir, "cc.vtt")
        self.now = os.path.join(self.dir, "now.txt")
        self.err = os.path.join(self.dir, "cc.err")
        self.feed_err = os.path.join(self.dir, "feed.err")
        self.proc = None
        self.feeder = None
        self.stopping = False
        self.cues = []                  # [(start, end, text)]
        self.pending = None
        self.buf = ""
        self.samples = []
        self.offset = None              # wall clock minus extractor clock
        self.text = ""
        self._write("")

    @property
    def filter(self):
        """drawtext expression handed to the burn-in encoder."""
        return ("drawtext=textfile='%s':reload=1:fontfile='%s':fontsize=%s:"
                "fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=12:"
                "line_spacing=8:x=(w-text_w)/2:y=h-th-h/18"
                % (self.now, CC_FONT, CC_FONTSIZE))

    def start(self, url):
        self.offset = time.time()
        self.feeder = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", url, "-c", "copy", "-f", "mpegts", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=open(self.feed_err, "wb"), bufsize=0)
        self.proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostats", "-nostdin",
             "-f", "lavfi", "-i", "movie=/dev/stdin[out0+subcc]",
             "-map", "0:s", "-c:s", "webvtt", "-flush_packets", "1",
             "-f", "webvtt", self.vtt],
            stdin=self.feeder.stdout, stdout=subprocess.DEVNULL,
            stderr=open(self.err, "wb"))
        # Only the extractor may hold the read end, or EOF never propagates.
        self.feeder.stdout.close()
        threading.Thread(target=self._run, daemon=True).start()
        note("captions: extracting cc1 in %s", os.path.basename(self.dir))

    def stop(self):
        self.stopping = True
        for proc in (self.proc, self.feeder):
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:       # noqa: BLE001
                    pass
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- webvtt -> current line ------------------------------------------
    def _run(self):
        pos = 0
        while not self.stopping:
            time.sleep(0.2)
            try:
                with open(self.vtt, "rb") as f:
                    f.seek(pos)
                    data = f.read().decode("utf-8", "replace")
                    pos = f.tell()
            except (FileNotFoundError, OSError):
                continue
            if data:
                self._parse(data)
            self._tick()

    def _parse(self, data):
        self.buf += data
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip("\r")
            m = CUE_RE.match(line)
            if m:
                self._flush()
                start = self._secs(m.group(1), m.group(2))
                end = self._secs(m.group(3), m.group(4))
                self.pending = {"start": start, "end": end, "lines": []}
                self.samples.append(time.time() - start)
                del self.samples[:-8]
                if self.samples:
                    self.offset = sorted(self.samples)[len(self.samples) // 2]
                continue
            if not line.strip():
                self._flush()
            elif self.pending is not None:
                self.pending["lines"].append(line)

    @staticmethod
    def _secs(stamp, frac):
        parts = [int(x) for x in stamp.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return (parts[0] * 3600 + parts[1] * 60 + parts[2]
                + int(frac) / (10 ** len(frac)))

    def _flush(self):
        p, self.pending = self.pending, None
        if not p:
            return
        text = self._clean(p["lines"])
        if not text:
            return
        self.cues.append((p["start"], p["end"], text))
        del self.cues[:-12]

    @staticmethod
    def _clean(lines):
        out = []
        for line in lines:
            line = TAG_RE.sub("", line).replace("\\h", " ").replace("\\N", " ")
            line = " ".join(line.split())
            if not line or set(line) <= set(".-_* "):
                continue
            out.append(line[:CC_COLS])
        return "\n".join(out[:CC_LINES])

    def _tick(self):
        if self.offset is None:
            return
        rel = time.time() - self.offset
        text = ""
        for start, end, cue in reversed(self.cues):
            if rel > end + CC_HOLD:
                break
            if rel >= start:
                text = cue
                break
        if text != self.text:
            self.text = text
            self._write(text)

    def _write(self, text):
        tmp = self.now + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, self.now)
        except Exception as exc:        # noqa: BLE001
            log("captions: text write failed (%s)", exc)


class Session:
    """One Tablo tuner, shared by every reader of that channel."""

    def __init__(self, oid, cc=False):
        self.oid = oid
        self.cc = cc
        self.key = "%s|cc" % oid if cc else oid
        self.captions = None
        self.lock = threading.Condition()
        self.chunks = []            # [(abs_offset, bytes)]
        self.base = 0               # absolute offset of chunks[0]
        self.end = 0                # absolute write position
        self.readers = 0
        self.proc = None
        self.probe = {}
        self.stopping = False
        self.dead = False
        self.ready = threading.Event()
        self.error = None
        self.last_used = time.time()
        self.master_url = None
        self.media_url = None
        self.media_at = 0.0
        self.hls_lock = threading.Lock()

    # ---- tuner -----------------------------------------------------------
    def start_async(self):
        """Tune off the request thread so waiters attach instead of queueing."""
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        try:
            self._tune()
        except Exception as exc:            # noqa: BLE001
            self.error = exc
            self.dead = True
            log("session %s: tune failed (%s)", self.oid, exc)
        finally:
            self.ready.set()

    def _tune(self):
        started = time.time()
        w = tpost("/guide/channels/%s/watch" % self.oid, timeout=TUNE_TIMEOUT)
        if not w.get("playlist_url"):
            raise Busy("tablo returned no playlist_url")
        self.master_url = w["playlist_url"]
        if self.cc:
            # Start the extractor first: it needs a couple of seconds of stream
            # before the first cue, and the encoder only has to pick up text.
            self.captions = CCWorker(self.oid)
            self.captions.start(w["playlist_url"])
        log("session %s: tuner allocated in %.2fs", self.key, time.time() - started)
        for attempt in range(1, PROBE_TRIES + 1):
            widen = RETRY_WIDEN ** (attempt - 1)
            self.proc, self.probe = self._spawn(
                w["playlist_url"],
                PROBESIZE * widen,
                min(ANALYZEDURATION * widen, RETRY_MAX_ANALYZE))
            known = self.probe["done"].wait(PROBE_INFO_TIMEOUT)
            streams = list(self.probe["streams"])
            if streams:
                note("session %s: probed streams: %s", self.oid,
                     ", ".join("%s/%s" % s for s in streams))
            kinds = {kind for kind, _ in streams}
            if not known:
                # ffmpeg said nothing we could parse (it may have died already);
                # hand the process on rather than retune on a guess.
                note("session %s: no stream list from ffmpeg within %.0fs, "
                     "continuing unchecked", self.oid, PROBE_INFO_TIMEOUT)
                break
            if not streams:
                note("session %s: could not read ffmpeg's stream list, "
                     "continuing unchecked", self.oid)
                break
            if "audio" in kinds:
                break
            if attempt >= PROBE_TRIES:
                note("session %s: %s after %d probes; serving video only",
                     self.oid, "/".join(sorted(kinds)), attempt)
                break
            note("session %s: probe %d/%d saw %s but no audio, retuning",
                 self.oid, attempt, PROBE_TRIES, "/".join(sorted(kinds)))
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:               # noqa: BLE001
                pass
            time.sleep(RETRY_DELAY)
        threading.Thread(target=self._pump, daemon=True).start()
        log("session %s: ffmpeg started", self.oid)

    def _spawn(self, url, probesize, analyzeduration):
        """Start the remux; stderr is parsed for the stream list, not discarded.

        With captions on, the same process re-encodes the video with the
        current cue drawn on it, because a burned-in caption is the only form
        of CC every client renders. Audio is still copied.
        """
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin",
               "-analyzeduration", str(analyzeduration),
               "-probesize", str(probesize),
               "-fflags", "+genpts+nobuffer", "-flags", "low_delay",
               "-i", url]
        if self.cc and self.captions is not None:
            cmd += ["-map", "0:v:0", "-map", "0:a:0", "-vf", self.captions.filter,
                    "-c:v", "libx264", "-preset", CC_PRESET, "-tune", "zerolatency",
                    "-crf", CC_CRF, "-pix_fmt", "yuv420p", "-g", "60",
                    "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-muxdelay", "0", "-muxpreload", "0",
                "-flush_packets", "1", "-f", "mpegts", "-"]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        probe = {"streams": [], "tail": [], "done": threading.Event()}
        threading.Thread(target=self._read_stderr, args=(proc, probe),
                         daemon=True).start()
        return proc, probe

    def _read_stderr(self, proc, probe):
        """Drain ffmpeg's stderr for the life of the process.

        The input stream block is printed once probing finishes and before
        "Stream mapping:", so that line is the signal the layout is known.
        """
        in_output = False
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            probe["tail"].append(line)
            del probe["tail"][:-20]
            if in_output:
                continue
            if line.startswith("Stream mapping:") or line.startswith("Output #"):
                in_output = True
                probe["done"].set()
                continue
            m = STREAM_RE.search(line)
            if m:
                probe["streams"].append((m.group(3).lower(), m.group(4)))
        probe["done"].set()

    def wait_ready(self, timeout):
        if not self.ready.wait(max(0.0, timeout)):
            raise Busy("tuning %s" % self.oid)
        if self.error is not None:
            raise Busy("tune failed: %s" % self.error)
        if self.dead:
            raise Busy("session ended")

    def _pump(self):
        proc = self.proc
        while True:
            try:
                data = proc.stdout.read(65536)
            except Exception:               # noqa: BLE001
                break
            if not data:
                break
            with self.lock:
                self.chunks.append((self.end, data))
                self.end += len(data)
                while self.chunks and self.end - self.chunks[0][0] > RING:
                    start, buf = self.chunks.pop(0)
                    self.base = start + len(buf)
                self.lock.notify_all()
        with self.lock:
            self.dead = True
            self.lock.notify_all()
        if not self.stopping:
            tail = [l for l in (self.probe.get("tail") or []) if l.strip()][-2:]
            note("session %s: ffmpeg exited%s", self.oid,
                 (": " + " | ".join(tail)) if tail else "")
        log("session %s: ffmpeg ended", self.oid)

    def stop(self):
        self.stopping = True
        self.dead = True
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:               # noqa: BLE001
                pass
        if self.captions is not None:
            self.captions.stop()

    def idle_for(self):
        return time.time() - self.last_used

    # ---- fan-out ---------------------------------------------------------
    def attach(self):
        with self.lock:
            self.readers += 1
            self.last_used = time.time()
            return max(self.base, self.end - BURST)

    def detach(self):
        with self.lock:
            self.readers = max(0, self.readers - 1)
            self.last_used = time.time()

    def read_from(self, pos, want=65536, timeout=5.0):
        """Return (bytes, new_pos); b"" on timeout; None when the session ends."""
        with self.lock:
            if pos >= self.end:
                if self.dead:
                    return None, pos
                self.lock.wait(timeout)
            if pos < self.base:
                log("session %s: reader fell behind, resyncing", self.oid)
                pos = self.base
            if pos >= self.end:
                return b"", pos
            out = bytearray()
            for start, buf in self.chunks:
                stop = start + len(buf)
                if stop <= pos:
                    continue
                if start <= pos:
                    out += buf[pos - start:]
                else:
                    out += buf
                pos = stop
                if len(out) >= want:
                    break
            self.last_used = time.time()
            return bytes(out), pos

    # ---- HLS -------------------------------------------------------------
    def media_playlist_url(self):
        """Resolve (and cache) the media playlist behind the master playlist."""
        with self.hls_lock:
            if self.media_url and time.time() - self.media_at < 120:
                return self.media_url
            try:
                with http_open(self.master_url) as r:
                    body = r.read().decode("utf-8", "replace")
                for line in body.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.media_url = urllib.parse.urljoin(self.master_url, line)
                        self.media_at = time.time()
                        return self.media_url
            except Exception as exc:        # noqa: BLE001
                log("session %s: master playlist failed (%s)", self.oid, exc)
            raise Busy("no media playlist")

    def media_playlist(self):
        media = self.media_playlist_url()
        with http_open(media) as r:
            body = r.read().decode("utf-8", "replace")
        return media, body


class Registry:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def _make_room(self, oid):
        """Called with self.lock held: free a tuner slot, preferring idle ones."""
        alive = [s for s in self.sessions.values() if not s.dead]
        if len(alive) < MAX_SESSIONS:
            return
        idle = [s for s in alive if s.readers == 0 and s.ready.is_set()]
        if not idle:
            raise Busy("all %d tuners are in use" % MAX_SESSIONS)
        victim = min(idle, key=lambda s: s.last_used)
        log("evicting idle session %s for %s", victim.key, oid)
        victim.stop()
        self.sessions.pop(victim.key, None)

    def acquire(self, oid, wait=0.0, cc=False):
        key = "%s|cc" % oid if cc else oid
        with self.lock:
            s = self.sessions.get(key)
            if s is not None and s.dead and s.ready.is_set():
                self.sessions.pop(key, None)
                s = None
            fresh = s is None
            if fresh:
                if cc and sum(1 for x in self.sessions.values()
                              if x.cc and not x.dead) >= CC_MAX:
                    raise Busy("captions already running on %d channels" % CC_MAX)
                self._make_room(key)
                s = Session(oid, cc)
                self.sessions[key] = s
                s.start_async()
                log("session %s: tuning", key)
            s.last_used = time.time()
        s.wait_ready(wait)
        return s

    def snapshot(self):
        with self.lock:
            return {key: {"readers": s.readers, "ready": s.ready.is_set(),
                          "dead": s.dead, "cc": s.cc, "buf": s.end - s.base,
                          "text": (s.captions.text[-60:] if s.captions else "")}
                    for key, s in self.sessions.items()}

    def reap(self):
        while True:
            time.sleep(5)
            with self.lock:
                for key, s in list(self.sessions.items()):
                    if s.dead and s.ready.is_set():
                        self.sessions.pop(key, None)
                    elif s.readers == 0 and s.idle_for() > IDLE_TTL:
                        log("closing idle session %s", key)
                        s.stop()
                        self.sessions.pop(key, None)


REG = Registry()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tablo-bridge/2.2"

    def log_message(self, fmt, *args):
        if DEBUG:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        path = self.path.split("?")[0]
        if (path.startswith("/stream/") or path.startswith("/cc/")
                or (path.startswith("/hls/") and path.endswith(".m3u8"))):
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl"
                             if path.endswith(".m3u8") else "video/mp2t")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        host = PUBLIC_HOST or self.headers.get("Host") or "127.0.0.1:%d" % PORT
        try:
            if path in ("/", "/index.html"):
                self._send(200, "tablo-bridge: /playlist.m3u /hls/<id>.m3u8 /stream/<id>.ts "
                                "/cc/<id>.ts /playlist.m3u?cc=1 "
                                "/guide.xml /healthz\n")
            elif path == "/healthz":
                self._send(200, json.dumps({"tablo": TABLO, "channels": len(channels()),
                                            "sessions": REG.snapshot()}), "application/json")
            elif path.endswith(".m3u"):
                fmt = (query.get("format") or ["ts"])[0]
                cc = (query.get("cc") or ["0"])[0] in ("1", "true", "yes")
                self._send(200, playlist(host, fmt, cc), "audio/x-mpegurl")
            elif path.endswith(".xml") or path.endswith(".xmltv"):
                if os.path.exists(GUIDE_XML):
                    with open(GUIDE_XML, "rb") as f:
                        self._send(200, f.read(), "application/xml")
                else:
                    self._send(200, empty_xmltv(), "application/xml")
            elif path == "/hls/seg.ts":
                self.segment(query)
            elif path.startswith("/hls/") and path.endswith(".m3u8"):
                if (query.get("cc") or ["0"])[0] in ("1", "true", "yes"):
                    self._send(501, "captions need the raw-TS path: "
                                    "/stream/<id>.ts?cc=1\n")
                    return
                self.hls(host, path[len("/hls/"):].split(".")[0])
            elif path.startswith("/cc/") and path.endswith(".ts"):
                self.stream(path[len("/cc/"):].split(".")[0], True)
            elif path.startswith("/stream/"):
                cc = (query.get("cc") or ["0"])[0] in ("1", "true", "yes")
                self.stream(path[len("/stream/"):].split(".")[0], cc)
            else:
                self._send(404, "not found\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Busy as exc:
            self._send(503, "tablo: %s\n" % exc)
        except Exception as exc:            # noqa: BLE001
            try:
                self._send(502, "bridge error: %s\n" % exc)
            except Exception:               # noqa: BLE001
                pass

    # ---- HLS -------------------------------------------------------------
    def hls(self, host, oid):
        sess = REG.acquire(oid, HLS_WAIT)
        media, body = sess.media_playlist()
        out = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                out.append(line)
                continue
            seg = urllib.parse.urljoin(media, line)
            out.append("http://%s/hls/seg.ts?oid=%s&u=%s"
                       % (host, oid, urllib.parse.quote(seg, safe="")))
        self._send(200, "\n".join(out) + "\n", "application/vnd.apple.mpegurl")

    def segment(self, query):
        oid = (query.get("oid") or [""])[0]
        url = (query.get("u") or [""])[0]
        if not url:
            self._send(400, "missing segment url\n")
            return
        headers = {}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            resp = http_open(url, headers=headers, timeout=25)
        except urllib.error.HTTPError as exc:
            self._send(exc.code, "segment fetch failed\n")
            return
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "video/mp2t"))
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if resp.headers.get(h):
                self.send_header(h, resp.headers[h])
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            self.wfile.write(chunk)
        resp.close()

    # ---- raw TS ----------------------------------------------------------
    def stream(self, oid, cc=False):
        sess = REG.acquire(oid, STREAM_WAIT, cc)
        pos = sess.attach()
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                data, pos = sess.read_from(pos)
                if data is None:
                    break
                if data:
                    self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            sess.detach()


def main():
    if not TABLO:
        sys.exit("TABLO_HOST is required (hostname or IP of the Tablo)")
    threading.Thread(target=REG.reap, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("tablo-bridge 2.2 listening on :%d (tablo %s)\n" % (PORT, TABLO))
    srv.serve_forever()


if __name__ == "__main__":
    main()
