#!/usr/bin/env python3
"""Filter a large XMLTV feed down to the handful of channels a Tablo receives.

Configured by JSON (default /data/channels.json):

  {
    "source": "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
    "channels": {
      "WCCOTV41.us@HD": {"id": "4.1", "name": "4.1 WCCO"}
    }
  }

"id" is what the guide publishes as the channel id. Clients match it against
the playlist tvg-id (the channel number) first and fall back to a display-name,
so either form works; this deployment uses station call signs because
Dispatcharr's EPG channel mapping was built on them. "name" becomes the first
display-name and is the safest thing to match on.
"""
import gzip
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

CONFIG = os.environ.get("EPG_CONFIG", "/data/channels.json")
OUT = os.environ.get("EPG_OUT", "/data/guide.xml")
INTERVAL = int(os.environ.get("EPG_INTERVAL", "21600"))
UA = os.environ.get("EPG_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

FIELDS = {"title": "title", "sub-title": "sub", "desc": "desc",
          "category": "cat", "episode-num": "episode"}


def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    mapping = {}
    for site_id, meta in cfg["channels"].items():
        mapping[site_id] = {"id": str(meta["id"]), "name": meta.get("name") or str(meta["id"])}
    return cfg["source"], mapping


def parse(stream, mapping):
    """Streaming parse; keeps only mapped channels so memory stays flat."""
    names, progs = {}, []
    cur = curch = None
    for event, elem in ET.iterparse(stream, events=("start", "end")):
        tag = elem.tag.split("}")[-1]
        if event == "start":
            if tag == "programme":
                cid = elem.get("channel")
                cur = ({"channel": mapping[cid]["id"], "start": elem.get("start"),
                        "stop": elem.get("stop"), "title": "", "sub": "",
                        "desc": "", "cat": "", "episode": ""} if cid in mapping else None)
            elif tag == "channel":
                cid = elem.get("id")
                curch = {"id": mapping[cid]["id"], "names": []} if cid in mapping else None
            continue
        if tag == "programme":
            if cur and cur["start"] and cur["stop"]:
                progs.append(cur)
            cur = None
        elif tag == "channel":
            if curch:
                names[curch["id"]] = curch["names"]
            curch = None
        elif cur is not None and tag in FIELDS:
            cur[FIELDS[tag]] = (elem.text or "").strip()
        elif curch is not None and tag == "display-name":
            if elem.text:
                curch["names"].append(elem.text.strip())
        elem.clear()
    return names, progs


def build():
    source, mapping = load_config()
    req = urllib.request.Request(source, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as resp:
        with gzip.GzipFile(fileobj=resp) as gz:
            names, progs = parse(gz, mapping)
    progs.sort(key=lambda p: (p["channel"], p["start"]))
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<tv generator-info-name="tablo-legacy-m3u">\n')
        for tvg in sorted({m["id"] for m in mapping.values()}):
            label = next(m["name"] for m in mapping.values() if m["id"] == tvg)
            f.write('  <channel id="%s">\n' % tvg)
            f.write("    <display-name>%s</display-name>\n" % esc(label))
            for dn in names.get(tvg, [])[:3]:
                f.write("    <display-name>%s</display-name>\n" % esc(dn))
            f.write("  </channel>\n")
        for p in progs:
            f.write('  <programme start="%s" stop="%s" channel="%s">\n'
                    % (p["start"], p["stop"], p["channel"]))
            f.write("    <title>%s</title>\n" % esc(p["title"] or "Unknown"))
            if p["sub"]:
                f.write("    <sub-title>%s</sub-title>\n" % esc(p["sub"]))
            if p["desc"]:
                f.write("    <desc>%s</desc>\n" % esc(p["desc"]))
            if p["cat"]:
                f.write("    <category>%s</category>\n" % esc(p["cat"]))
            if p["episode"]:
                f.write('    <episode-num system="onscreen">%s</episode-num>\n' % esc(p["episode"]))
            f.write("  </programme>\n")
        f.write("</tv>\n")
    os.replace(tmp, OUT)
    return len(progs)


def main():
    while True:
        started = time.time()
        try:
            n = build()
            sys.stderr.write("epg: wrote %d programmes in %.0fs\n" % (n, time.time() - started))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("epg: FAILED %s\n" % exc)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if os.environ.get("EPG_ONESHOT"):
        print("programmes:", build())
    else:
        main()
