# tablo-legacy-m3u

Turn a first-generation, network-connected **Nuvyyo Tablo** into a plain M3U
live TV source with an XMLTV guide, so Jellyfin, Emby, Dispatcharr, or anything
else that understands IPTV can watch it.

Tablo never shipped an M3U or HDHomeRun interface, and the vendor's own apps for
these devices were shut down in September 2026. The local REST API on port 8885,
however, is still open and still works. This project wraps it.

## Which hardware this works with

This speaks the **legacy local REST API on port 8885**. Whether a unit has it is a
one-command question — if this returns JSON with a `model` field, the unit is
supported:

```sh
curl -s http://<tablo-ip>:8885/server/info
```

| Hardware | Works | Notes |
| --- | --- | --- |
| Legacy network Tablo — 2-Tuner, 4-Tuner, DUAL LITE, QUAD (Nuvyyo era, firmware 2.2.x) | **Yes** | What this project was written against: a Tablo 2-Tuner on firmware 2.2.60. No authentication anywhere — every endpoint answers unauthenticated. Set `MAX_SESSIONS` to the number of tuners the unit actually has (default 2). |
| 4th-generation Tablo (2023 and later, the current models) | **No** | There is no local API at all. It is managed through Tablo's cloud service and the only API is cloud-authenticated, so there is no port to point a bridge at. This is a different device, not a different firmware. |
| Legacy HDMI-only models (Tablo DUAL HDMI, QUAD HDMI) | **No** (expected) | Different firmware branch; they stream to a TV over HDMI rather than over the network. Run the command above before assuming. |
| HDHomeRun and other network tuners | Not applicable | They already speak M3U/HDHomeRun or are supported natively by Jellyfin, Emby and Dispatcharr — no bridge needed. |

```
antenna ──► Tablo (2 or 4 tuners)
                │  POST /guide/channels/<id>/watch  ──► HLS
                ▼
        tablo-bridge  (:8050)          tablo-epg
        /playlist.m3u                  filtered /guide.xml
        /stream/<id>.ts  ◄── ffmpeg remux of the HLS
                │
                ▼
        Jellyfin / Emby / Dispatcharr
```

## What it does

- `tablo-bridge` — serves `/playlist.m3u`, one entry per channel the Tablo has
  tuned, rebuilt live from the device on every request. `/stream/<id>.ts` opens
  a Tablo watch session and remuxes its HLS to continuous MPEG-TS with ffmpeg
  stream copy, so any M3U client can play it. Returns `503` when no tuner is
  free.
- `tablo-epg` — downloads a big public XMLTV feed on a timer, keeps only the
  stations you list, and rewrites `/data/guide.xml` atomically. The Tablo's own
  guide data is gone (the cloud service behind it was retired), so this is how
  the channels get listings.

## Quick start

```sh
git clone <this repo> && cd tablo-legacy-m3u
cp .env.example .env                     # set TABLO_HOST and PUBLIC_HOST
cp channels.example.json data/channels.json   # set your stations
docker compose up -d --build             # ffmpeg apt install makes this slow once
```

Then point a client at it:

- Playlist: `http://<PUBLIC_HOST>/playlist.m3u`
- Guide: `http://<PUBLIC_HOST>/guide.xml`

In Jellyfin: **Dashboard → Live TV → Tuner Devices → M3U Tuner** with the
playlist URL, then **TV Guide Data Providers → XMLTV** with the guide URL.
Channel numbers and `tvg-id`s already match, so no manual mapping is needed.

## Configuration

| Variable | Where | Meaning |
| --- | --- | --- |
| `TABLO_HOST` | bridge | Tablo address on the LAN (API is port 8885) |
| `PUBLIC_HOST` | bridge | `host:port` clients use to reach the bridge |
| `PORT` | bridge | listen port, default `8050` |
| `GROUP_NAME` | bridge | M3U `group-title`, default `Live TV` |
| `GUIDE_XML` | bridge | file served at `/guide.xml`, default `/data/guide.xml` |
| `FFMPEG` | bridge | ffmpeg binary, default `ffmpeg` |
| `PROBESIZE` | bridge | bytes ffmpeg may read before it settles the stream list, default `1000000` |
| `ANALYZEDURATION` | bridge | cap on that probe in microseconds, default `3000000` (3 s) |
| `PROBE_TRIES` | bridge | retunes the session if the probe sees video but no audio, default `2` |
| `PROBE_INFO_TIMEOUT` | bridge | seconds to wait for ffmpeg's stream list, default `8` |
| `EPG_CONFIG` | epg | channel map, default `/data/channels.json` |
| `EPG_OUT` | epg | output guide, default `/data/guide.xml` |
| `EPG_INTERVAL` | epg | refresh seconds, default `21600` (6h) |
| `DEBUG` | bridge | `1` logs tune timings |

### channels.json

```json
{
  "source": "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
  "channels": {
    "WCCO-DT.us_locals1": {"id": "WCCO", "name": "4.1 WCCO"}
  }
}
```

`id` is the guide channel id. A client matches it to the playlist `tvg-id`
(the channel number the Tablo reports) first and falls back to a display-name,
so either the number (`"4.1"`) or the station call sign (`"WCCO"`) maps. This
deployment uses call signs because Dispatcharr's EPG channel mapping is built
on them; `name` becomes the first display-name and always matches.

Finding the source ids: download the feed once and grep for your call sign —

```sh
curl -sL "$SOURCE" | zcat | grep -oE '<channel id="[^"]*(WCCO|KSTP)[^"]*"'
```

## Using it behind Dispatcharr

Dispatcharr can consume the bridge the same way a client does: add the playlist
as an M3U account, add `guide.xml` as an XMLTV source, create channels from the
imported streams, and put them in a channel profile so it only serves the ones
you want.

One caveat, learned the hard way: **Dispatcharr's stream proxy gives up on an
upstream after about three seconds, and a Tablo can take nine seconds to hand
out a tuner when a stale session is holding one.** Through the proxy that
produces a `188`-byte "Channel is stopping" frame instead of video. Until the
bridge grows a warm-session cache, point the media server's *tuner* straight at
the bridge and let Dispatcharr serve only the guide.

## Known limitations

- **Tuner sessions leak.** The device has no release endpoint (`DELETE` returns
  404). A session lingers after the client disconnects and is only reclaimed
  when another request needs the tuner — which is where the multi-second
  `watch` stalls come from. With two tuners, overlapping viewers can queue.
- **No keepalive is required in practice** — polling the HLS playlist keeps the
  session alive — but sessions are not explicitly closed.
- **MPEG-TS only.** The bridge stream-copies, so no transcoding. If a client
  cannot play H.264/AAC in TS, the media server has to transcode.
- **HEAD is refused on purpose** (405). Some probes treat a 200-with-no-body as
  a dead channel.
- Everything is OTA-only; there is no IPTV or OTT content on these devices.

## Notes on the device API

See [docs/TABLO-API.md](docs/TABLO-API.md) for the endpoints, the HLS play
structure, byte-range segments, token rotation, and tuner behaviour.

## Credits

Prior art that informed this: `macoirc/tablo_npvr` (M3U generator, redirects to
the Tablo playlist), `tmm1/tablo-for-channels` (Go, Channels DVR), and Nuvyyo's
own Kodi add-on. Guide data comes from whatever XMLTV feed you point it at;
the example uses epgshare01.

## Licence

MIT. Unaffiliated with Nuvyyo/Tablo. This talks to an undocumented API on
end-of-life hardware; it can break whenever the device firmware changes, and
the vendor has already stopped supporting these models.
## Closed captions

The Tablo carries CEA-608/708 inside the H.264 SEI. Jellyfin cannot select an SEI
caption track (it is in the video bitstream, not a subtitle stream) and MPEG-TS has
no text subtitle type ffmpeg can encode, so the only form every client renders is
captions burned into the picture:

- `/playlist.m3u?cc=1` — one `NAME CC` entry per channel. The entries publish
  `tvg-id="<CALL>-CC"` because Dispatcharr identifies a stream by `tvg_id` alone,
  so a variant sharing the plain call sign collapses onto the plain stream.
- `/cc/<oid>.ts` — that channel with captions drawn in: `libx264 -preset ultrafast
  -tune zerolatency -crf 23`, audio copied. Costs roughly 4.5x realtime of one
  core on an N100 per captioned session; `CC_MAX` (default 2) caps concurrency.
- Plain `/stream/<oid>.ts` stays stream-copy and carries no captions. `/hls/`
  cannot (it proxies the device playlist) and answers 501 for `cc=1`.

Extraction is a feeder ffmpeg (`-i <watch url> -c copy -f mpegts -`) piped into
`movie=/dev/stdin[out0+subcc]`, because `movie=` cannot take an HTTP URL and a
watch URL answers with a playlist, not media. The current cue is written to a file
and drawn with `drawtext=textfile=...:reload=1`; cue write times are compared
against cue start times so the encoder stays in step with the extractor.

`/healthz` shows each session with `cc` and the caption text it is currently
holding, which is the fastest way to tell "no captions in this programme" from
"the extractor broke".
