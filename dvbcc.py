"""Encode CEA-608 cues as DVB subtitles (EN 300 743) and splice them into a TS.

The bitstream follows ffmpeg's own dvbsub encoder/decoder layout exactly, which
is what validates it: `ffmpeg -i out.ts -map 0:s -c:s srt -` has to give the
text back. 2 bits/pixel, one region, one CLUT, interlaced field split.
"""

PAGE_ID = 1
SUBTITLE_PID = 0x1A0
SUBTITLE_LANG = b"eng"
SUBTITLE_TYPE = 0x10          # normal subtitles, no aspect-ratio criticality

# 2-bit palette: 0 transparent, 1 black box, 2 white text, 3 grey edge
PALETTE = [
    (0, 128, 128, 255),        # Y, Cr, Cb, T   (fully transparent)
    (16, 128, 128, 0),         # black, opaque
    (235, 128, 128, 0),        # white, opaque
    (110, 128, 128, 0),        # grey, opaque
]


def _seg(stype, payload, page_id=PAGE_ID):
    return b"\x0f" + bytes([stype]) + page_id.to_bytes(2, "big") + \
        len(payload).to_bytes(2, "big") + payload


def _rle2_row(row):
    """One line of 2-bit pixels, as ffmpeg's dvb_encode_rle2 writes it."""
    out = bytearray(b"\x10")
    bitbuf = 0
    bitcnt = 6

    def put(val):
        nonlocal bitbuf, bitcnt
        bitbuf |= val << bitcnt
        bitcnt -= 2
        if bitcnt < 0:
            out.append(bitbuf)
            bitbuf = 0
            bitcnt = 6

    x, w = 0, len(row)
    while x < w:
        x1 = x + 1
        colour = row[x]
        while x1 < w and row[x1] == colour:
            x1 += 1
        run = x1 - x
        if colour == 0 and run == 2:
            put(0); put(0); put(1)
        elif 3 <= run <= 10:
            v = run - 3
            put(0); put((v >> 2) | 2); put(v & 3); put(colour)
        elif 12 <= run <= 27:
            v = run - 12
            put(0); put(0); put(2); put(v >> 2); put(v & 3); put(colour)
        elif run >= 29:
            run = min(run, 284)
            v = run - 29
            put(0); put(0); put(3)
            put(v >> 6); put((v >> 4) & 3); put((v >> 2) & 3); put(v & 3)
            put(colour)
        else:
            put(colour)
            if colour == 0:
                put(1)
            run = 1
        x += run
    # end of line
    put(0); put(0); put(0)
    if bitcnt != 6:
        out.append(bitbuf)
    out.append(0xF0)
    return bytes(out)


def _field(rows):
    return b"".join(_rle2_row(r) for r in rows)


def display_set(rows, width, height, x, y, display_w, display_h,
                page_version=0):
    """One complete display set: DDS, page, CLUT, region, object."""
    height -= height % 2                      # field split needs an even height
    rows = rows[:height]
    while len(rows) < height:
        rows.append([0] * width)

    dds = (b"\x00" + (display_w - 1).to_bytes(2, "big")
           + (display_h - 1).to_bytes(2, "big"))

    page = bytes([30, (page_version << 4) | (2 << 2) | 3])
    page += bytes([0, 0xFF]) + x.to_bytes(2, "big") + y.to_bytes(2, "big")

    clut = bytes([0, (0 << 4) | 0xF])
    for i, (yy, cr, cb, t) in enumerate(PALETTE):
        clut += bytes([i, (1 << 7) | (0xF << 1) | 1, yy, cr, cb, t])

    region = (bytes([0]) + bytes([(0 << 4) | 0x07])
              + width.to_bytes(2, "big") + height.to_bytes(2, "big")
              + bytes([(1 << 5) | (1 << 2) | 0x03])
              + bytes([0, 0, 0x03])
              # object reference: object_id 0, type 0 (bitmap), position 0,0
              # inside the region. Six bytes - the region parser only reads
              # entries while buf+5 < buf_end, so a shorter stub is ignored and
              # the display set decodes to nothing.
              + bytes([0, 0,             # object_id
                       0, 0,             # object_type=0 + x_pos=0
                       0, 0]))           # y_pos = 0

    top = _field(rows[0::2])
    bottom = _field(rows[1::2])
    obj = (b"\x00\x00" + bytes([(0 << 4) | (0 << 2) | (0 << 1) | 1])
           + len(top).to_bytes(2, "big") + len(bottom).to_bytes(2, "big")
           + top + bottom)

    return (_seg(0x14, dds) + _seg(0x10, page) + _seg(0x12, clut)
            + _seg(0x11, region) + _seg(0x13, obj) + _seg(0x80, b""))


def clear_display_set(page_version=0):
    """Page with no regions: removes whatever is on screen."""
    page = bytes([1, (page_version << 4) | (2 << 2) | 3])
    return _seg(0x10, page) + _seg(0x80, b"")


def _pts_bytes(pts):
    """The 5-byte PTS field of a PES header."""
    p = pts & ((1 << 33) - 1)
    return bytes([
        (0x2 << 4) | (((p >> 30) & 0x07) << 1) | 0x01,
        (p >> 22) & 0xFF,
        (((p >> 15) & 0x7F) << 1) | 0x01,
        (p >> 7) & 0xFF,
        ((p << 1) & 0xFE) | 0x01,
    ])


def pes(pts, payload, with_header=True):
    """PES for a DVB subtitle stream: data_identifier 0x20, substream 0x00."""
    body = (b"\x20\x00" if with_header else b"") + payload
    header = b"\x80\x80\x05" + _pts_bytes(pts)
    length = len(header) + len(body)
    return (b"\x00\x00\x01\xbd" + (length & 0xFFFF).to_bytes(2, "big")
            + header + body)


def ts_packets(pid, pes_payload, cc=0):
    """Split a PES into 188-byte TS packets (payload_unit_start on the first)."""
    out = []
    data = pes_payload
    pos = 0
    first = True
    while pos < len(data) or first:
        chunk = data[pos:pos + 184]
        pos += len(chunk)
        af = b""
        afc = 0x10                                # payload only
        if len(chunk) < 184:                      # pad the tail with an adaptation field
            pad = 184 - len(chunk) - 1
            # adaptation_field_length counts the flags byte too; the flags must be
            # zero or the demuxer tries to parse the stuffing as PCR/OPCR/extension
            # and shifts the payload by a few bytes.
            af = bytes([pad, 0x00]) + (b"\xff" * (pad - 1) if pad else b"")
            afc = 0x30
        header = bytes([0x47,
                        (0x40 if first else 0x00) | ((pid >> 8) & 0x1F),
                        pid & 0xFF,
                        afc | (cc & 0x0F)])
        out.append(header + af + chunk)
        cc = (cc + 1) & 0x0F
        first = False
        if not chunk:
            break
    return b"".join(out), cc


def crc32(data):
    """MPEG-2 CRC32 used by PSI sections."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc
