# Legacy Tablo local API notes

Everything here was established by experiment against a first-generation
Nuvyyo Tablo 2-Tuner running firmware 2.2.60. There is no official
documentation for any of it. The device is end-of-life and the vendor shut down
the legacy apps in September 2026; the local API kept working.

## Ports

| Port | Serves |
| --- | --- |
| 8885 | JSON REST API (no authentication) |
| 80 | `Nuvyyo Tablo Server` HTML and the video streams |
| 18080 | same server, alternate |

## Identity and state

```
GET /server/info
{"server_id":"SID_...","name":"Tablo","version":"2.2.60","model":{"tuners":2,"type":"dual"},
 "availability":"ready","setup_completed":true}

GET /server/tuners
[{"in_use":false,"channel":null,"recording":null,"channel_identifier":null}, ...]
```

`/server/tuners` lists four slots even on a two-tuner unit, and slots go stale:
a slot can read `in_use:true` with a null channel long after its client is gone.
Treat it as advisory, not as accurate capacity accounting.

## Channels

```
GET /guide/channels
["/guide/channels/1845639", ...]          # paths, not objects

POST /batch                               # body: the array of paths
{"\/guide/channels/1845639": {"object_id": 1845639, "channel": {"call_sign": "WCCO",
  "major": 4, "minor": 1, "network": "CBS", "resolution": "hd_1080",
  "channel_identifier": "S21234_004_01", "source": "ota", "logos": [...]}}}
```

`/batch` is the generic multi-get for this API and works for recordings,
movies, sports events, and channels alike.

## Guide data is dead

```
GET /guide/airings           -> []
GET /guide/series            -> []
GET /guide/channels/<id>/airings -> 404 endpoint not found
```

The cloud EPG service behind these devices was retired, so there are no
programmes to read from the box. Guide data has to come from somewhere else,
which is what `epg_gen.py` is for.

## Watching live TV

```
POST /guide/channels/<object_id>/watch      body: {}
{
  "token": "08fab103-...",
  "expires": "2026-10-30T20:42:31Z",
  "keepalive": 165,
  "playlist_url": "http://192.168.86.20:80/stream/pl.m3u8?8cklsVtXbieXBe_UgKpGOA",
  "video_details": {"width": 0, "height": 0, "container_format": "mpeg4"},
  "canRecord": true
}
```

`playlist_url` is a **master** playlist:

```
#EXTM3U
#EXT-X-VERSION:4
#EXT-X-STREAM-INF:BANDWIDTH=10000000
/stream/pls.m3u8?M9Oop3E254cXfdjnODgq6g&fmt=v4
```

That media playlist changes every second:

```
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:1
#EXTINF:1.001,
#EXT-X-PROGRAM-DATE-TIME:2026-10-30T20:39:29.000Z
#EXT-X-BYTERANGE:1370708@0
/stream/segw.ts?zH-klQN_x2qJCdnIb2Xugg
```

Practical consequences:

- Segments are **byte ranges into a growing file whose URL token rotates**.
  Fetch with an explicit `Range` header; a bare GET of `segw.ts` may return the
  whole chunk or truncate, depending on size.
- The token in the media playlist URL rotates every few seconds. Anything that
  re-reads the playlist (ffmpeg does) handles that transparently; anything that
  caches segment URLs must re-read the playlist.
- `keepalive: 165` suggests a heartbeat, but polling the playlist is enough in
  practice — a 300-second test pull ran continuously across several token
  rotations and past the reported `expires`.

## Tuner session lifecycle

There is **no way to release a session**. `DELETE /guide/channels/<id>/watch`
returns `404 endpoint not found`, and nothing else acknowledges a stop.

Sessions are reclaimed lazily: a second `watch` for a different channel blocks
until the device frees a tuner, which was measured at **9.1 s** while a stale
session was held. That latency is the whole reason a three-second upstream
timeout (Dispatcharr's default tolerance) tears the stream down before the
first byte.

Playing a channel keeps its session alive; abandoning it leaves the tuner
allocated for a long, unpredictable time. If you need deterministic capacity,
cache a warm session per channel and fan out readers from it rather than
calling `watch` per viewer.

## Probing behaviour

- `HEAD` on a stream URL that returns `200` with an empty body makes at least
  one proxy (Dispatcharr) conclude the channel is dead and replace the stream
  with an error frame. Answering `405` instead leaves it to fall back to `GET`.
- `ffprobe` needs the first bytes quickly. Without
  `-analyzeduration 0 -probesize 32768 -fflags +genpts+nobuffer` and
  `-flush_packets 1` on output, ffmpeg buffers long enough that probing times
  out — the symptom is "streams and format are both null" in the client log.

## Codecs

H.264 video (1080i or 720p depending on the station) with AAC audio, wrapped in
MPEG-TS after the segments are concatenated. Stream copy works; no transcoding
is needed to remux.
