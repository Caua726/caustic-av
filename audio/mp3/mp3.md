# MP3 (MPEG-1/2/2.5 Layer III)

Status: **stage 8a, WRITTEN, NOT VERIFIED.** Nothing in this directory has
been compiled or run. Stage 8a parses streams and supplies the table layer;
PCM synthesis is stage 8b.

## What stage 8a holds

| File | Contents |
|---|---|
| `mp3.cst` | ID3v2/v1 skipping, frame-header parsing, side information, reservoir bounds, Xing/Info/VBRI metadata, `mp3_open` and `mp3_next`. |
| `mp3_tables.cst` | Constants, blob readers, bitrate/sample-rate/sfb/pretab lookups, alias coefficients, the D[512] synthesis window, Huffman table loading. |
| `mp3_tables_data.cst` | The table values, as comma-separated string blobs. |
| `../../tools/gen_mp3_tables.py` | Unrun generator that reads the public bitstream description and writes `mp3_tables_data.cst`; it validates dimensions, contiguity and Huffman prefix freedom. |

The parser borrows the caller's buffer. It never allocates, never opens a
file, and returns negative `i64` errors. A frame view (`Mp3Frame`) points
into the input and is valid only while the input is.

## API

```text
mp3_open(m, src, len)      0 or negative
mp3_next(m, frame)         1 = frame, 0 = end of audio, negative = error
mp3_header_parse(src, avail, &h)        0 or negative
mp3_side_info_parse(src, len, ver, nch, &s)  0 or negative
```

`mp3_open` clears `m` on entry, skips consecutive ID3v2 tags, excludes a
trailing ID3v1 tag, scans leading junk for the first complete Layer III
frame, and reads the first frame's VBR tag. It does not decode the first
frame's side information; `mp3_next` does.

`mp3_next` returns the frame at `m.pos`, validates the header and side
information, checks the reservoir bound, and advances by the frame length.
It does not scan past a broken frame: a sync at `m.pos` whose header is
invalid returns the header error, and a frame whose declared length passes
`audio_end` returns `I_EOF`. The parser does not silently skip a corrupt
frame in the middle of a stream: the caller should reopen from a new byte
boundary rather than editing `m.pos`, because `main_data_total` would no
longer describe the frames actually returned.

Errors used here:

* `I_EOF` — fewer bytes than a tag, header or declared frame.
* `I_BADMAGIC` — no Layer III sync in the leading-junk scan, or no sync at
  `m.pos`.
* `I_UNSUPPORTED` — Layer I/II, free-format bitrate index 0, an unknown ID3
  major version, or an unknown VBRI version.
* `I_CORRUPT` — reserved version/layer/sample-rate/bitrate, a frame too short
  for its side information, a reserved `block_type` 0 with
  `window_switching_flag` set, `big_values > 288`, an implausible
  `main_data_begin`, malformed ID3 size bytes, a truncated Xing/VBRI/LAME
  field, or `part2_3_length` totals larger than the reservoir window.
* `I_BADPARAM` / `I_DIMS` — null pointers, negative length, or a length past
  `core/bits`' `BITS_MAX_BYTES`.

## Header (ISO/IEC 11172-3 / 13818-3)

The 32-bit header is parsed most-significant bit first:

| Bits | Field | Handling |
|---|---|---|
| 11 | sync | must be all ones |
| 2 | version | 3 MPEG-1, 2 MPEG-2, 0 MPEG-2.5, 1 reserved (`I_CORRUPT`) |
| 2 | layer | 01 Layer III; 10/11 `I_UNSUPPORTED`; 00 `I_CORRUPT` |
| 1 | protection | 1 = no CRC; 0 = 16-bit CRC after the header |
| 4 | bitrate index | 0 free (`I_UNSUPPORTED`), 15 forbidden, 1..14 per Table 8 |
| 2 | sample rate | Table 9; 3 reserved |
| 1 | padding | adds one byte to the frame |
| 1 | private | exposed |
| 2 | mode | stereo, joint, dual, mono |
| 2 | mode extension | exposed; in Layer III bits mean intensity/MS stereo |
| 1 | copyright | exposed |
| 1 | original/copy | exposed |
| 2 | emphasis | exposed; 2 is reserved but accepted |

Frame length is the standard integer expression:

```text
frame_len = floor(144 * bitrate_kbps * 1000 / sample_rate) + padding   MPEG-1
frame_len = floor( 72 * bitrate_kbps * 1000 / sample_rate) + padding   MPEG-2/2.5
```

which is `samples_per_frame / 8 * bitrate / sample_rate` plus padding. The
16-bit CRC, when present, is inside that length; `crc_len` is 2 and
`side_info_offset` is 6 instead of 4.

Layer III side-information length (Table 13): MPEG-1 mono 17, stereo 32;
MPEG-2/2.5 mono 9, stereo 17.

## Side information

`mp3_side_info_parse` consumes exactly the Table 13 byte count and rejects
any leftover or short bits. Fields:

* `main_data_begin`: 9 bits MPEG-1, 8 bits MPEG-2/2.5.
* `private_bits`: 5/3 MPEG-1 mono/stereo, 1/2 MPEG-2/2.5 mono/stereo.
* `scfsi[ch][band]`: MPEG-1 only; four bands per channel.
* For each granule (2 MPEG-1, 1 MPEG-2/2.5) and channel:
  `part2_3_length` 12, `big_values` 9 (rejected above 288),
  `global_gain` 8, `scalefac_compress` 4 (MPEG-1) or 9 (MPEG-2/2.5),
  `window_switching_flag` 1, then either
  `block_type` 2 / `mixed_block_flag` 1 / two `table_select` 5 each /
  three `subblock_gain` 3 each, or three `table_select` 5 each /
  `region0_count` 4 / `region1_count` 3. MPEG-1 adds `preflag` 1.
  `scalefac_scale` and `count1table_select` are always present.

When `window_switching_flag` is set, the standard supplies
`region0_count = 7` (`8` for block_type 2 without mixed block) and
`region1_count = 63`; the parser records those defaults. `block_type` 0
with the flag set is rejected because the value is reserved.

The raw `scalefac_compress` is exposed. Stage 8b expands it with the
MPEG-1 slen table or the MPEG-2/2.5 four-partition procedure; those are
table-layer decisions, not stream-parsing decisions.

## Reservoir

The bit reservoir is a **logical** stream: the main data of earlier frames
is separated by their headers and side information, so a single absolute
byte offset cannot name its start. `Mp3Frame` reports:

* `main_data_offset` — absolute first byte of this frame's main data
  (after the side information).
* `main_data_len` — bytes physically in this frame after the side
  information.
* `reservoir_back` — how many bytes to take from the caller's saved
  previous main-data stream (the last `reservoir_back` bytes of it); this
  equals `side.main_data_begin`.
* `reservoir_len = reservoir_back + main_data_len` — total logical bytes
  available for this frame.

`Mp3.main_data_total` is the cumulative main data of all frames already
returned; the parser uses it to reject a backpointer that reaches before
the start of the stream. Consequently the first frame must have
`main_data_begin == 0`; a stream that starts in the middle of a reservoir
is rejected rather than reading bytes before the supplied buffer.

The parser checks the documented bound

```text
max_mdb_bits = max(0, min(4088 or 2040, 7680 - frame_len * 8))
main_data_begin * 8 <= max_mdb_bits
```

`main_data_begin` counts bytes; the standard states `mdb_limit` in bits
(4088 for MPEG-1's 9-bit field, 2040 for MPEG-2/2.5's 8-bit field), hence
the factor of eight on the left. The sum of all `part2_3_length` values in
a frame may not exceed `reservoir_len * 8`.

The caller owns the reservoir bytes and its history buffer. Stage 8b
copies or indexes `reservoir_back` bytes from that history plus the current
frame's `main_data_offset/main_data_len`; the parser does not allocate a
reservoir.

## ID3

* **ID3v2.2/2.3/2.4** at the start: 10-byte header, `"ID3"`, major 2..4,
  revision != 0xFF, four syncsafe size bytes (each high bit clear), and a
  10-byte footer when v2.4 flag 0x10 is set. The extended header and padding
  are inside the size field, so no separate extended-header parse is needed.
  Consecutive tags are skipped. Unknown major versions are
  `I_UNSUPPORTED`; truncated tags are `I_EOF`; bad size bytes `I_CORRUPT`.
* **ID3v1** at the end: if the last 128 bytes begin `"TAG"`, `audio_end`
  is moved back by 128. The tag contents are not parsed.
* Leading junk before the first frame is scanned one byte at a time. A
  candidate is accepted only when its header parses and its complete frame
  fits before `audio_end`.

## Xing/Info and VBRI

Only the **first** frame is inspected; the metadata is not used to skip or
trim audio.

* Xing/Info is read at the first frame's `side_info_end`, i.e. after the
  header, the optional CRC and the side information. The four-byte tag is
  `"Xing"` (VBR) or `"Info"` (nominally CBR). The 32-bit flags select, in
  order, frames (4 bytes), bytes (4), TOC (100) and quality (4). The TOC is
  copied to `Mp3.toc`; `frame_count`, `vbr_bytes`, `xing_quality` and
  `toc_present` expose it. `is_vbr` is 1 for `"Xing"` and 0 for `"Info"`.
* A LAME extension is recognised when the nine-byte encoder string starts
  with `"LAME"`, `"Lavf"` or `"Lavc"`. The 24-bit delay/padding word is at
  offset 21 in that extension: delay = `v >> 12`, padding = `v & 0xFFF`.
  `encoder_delay`, `encoder_padding`, `lame_present` and `encoder_kind`
  expose it. Audio is not trimmed.
* VBRI is checked at frame start + 36, is accepted when its version is 1,
  and exposes `vbri_delay`, `vbri_quality`, `vbr_bytes` and `frame_count`.
  A Xing/Info tag takes precedence when both are present.

A recognised tag that is cut short is `I_CORRUPT`. `frame_count` is 0 when
the tag did not carry it; it is not estimated by scanning.

## Tables

All values come from the public Layer III bitstream description in
`.work/docs/mp3_bitstream.txt` (Tables 8, 9, 23, 24-41, 42-59 and 60).
No value is copied from a decoder implementation.

* Bitrate and sample-rate tables: Table 8/9. Index 0 and 15 are stored as 0
  so the parser decides free/forbidden; MPEG-2.5 has its own row with
  indices 9..15 stored as 0 (forbidden by Table 8).
* Scalefactor bands: widths from Tables 42-59 in the order 44.1, 48, 32,
  22.05, 24, 16, 11.025, 12, 8 kHz. The published tables list the bands
  that carry scalefactors; each row here has a final entry equal to
  `576 - sum(long widths)` or `192 - sum(short widths)`, the region above
  the last scalefactor band, which the specification says has factor 1.0.
  `MP3_SFB_LONG_SCALEFAC_COUNT` (21) and
  `MP3_SFB_SHORT_SCALEFAC_COUNT` (12) give the number of bands that
  actually carry scalefactors.
* Pretab: Table 23 (21 bands) plus a zero for the tail band.
* Alias coefficients: Table 60, scaled by 1e9 so the printed decimals are
  exact. `mp3_alias_c` returns the f64 form.
* Synthesis window: the 512 printed `D[i]` values, scaled by 1e9.
  `mp3_synth_window` and `mp3_synth_window_load` expose f64 forms.
* Huffman: tables 0-31. Table 0 is the all-zero table and has no codewords.
  Tables 4 and 14 have no codewords in the standard; selecting them is
  `I_UNSUPPORTED`. Tables 1, 2, 3, 5-13 and 15 are individual; tables
  16-23 share one codeword table and 24-31 share another. `mp3_huff_load`
  builds `code[x*16+y]`/`len[x*16+y]`, rejects duplicates, lengths above
  19 and codewords that do not fit their length. `mp3_huff_linbits` holds
  the linbits from the Table 40/41 headings.

`mp3_tables_data.cst` can be reproduced by
`tools/gen_mp3_tables.py --spec ... -o ...`. The generator is committed but
has not been run in this phase; it validates sfb contiguity and totals,
the 512 D values, the alias count, and Huffman dimensions and prefix
freedom.

## Pending blobs

These are analytic, not printed in the documentation, and are **not**
written yet:

* `MP3_WINDOW_LONG` — 36 normal long-block window values.
* `MP3_WINDOW_START` — 36 start-block values.
* `MP3_WINDOW_STOP` — 36 stop-block values.
* `MP3_WINDOW_SHORT` — 12 short-block values.
* `MP3_DCT_TWIDDLE` — the DCT/IMDCT twiddles stage 8b chooses.

`MP3_WINDOW_TABLES_GENERATED` is 0 and `mp3_window_tables_ready()` returns
0. Stage 8b must not synthesise until these exist. The values have to come
from the window definitions in the specification, not from a decoder.

## What stage 8b adds

Main-data parsing (scalefactors with scfsi, MPEG-2 partition expansion),
Huffman decoding of `big_values`/count1 with linbits and signs, region
bounds from `region0_count`/`region1_count`, requantisation, reordering for
short blocks, joint-stereo processing, alias reduction, IMDCT and the
polyphase synthesis filterbank. Stage 8b also owns the reservoir buffer,
gapless trimming from the LAME/VBRI delay and padding, and frame-to-PCM
output.

## Reference reading

These were read only to understand behaviour; no code was copied, ported or
transcribed:

* `lieff/minimp3`, `minimp3.h` — frame search, header tests, side-info and
  reservoir shape.
* `FFmpeg/FFmpeg`, `libavcodec/mpegaudiodec_template.c` — Layer III decode
  order and side-info use.
* `FFmpeg/FFmpeg`, `libavcodec/mpegaudiodata.c` — common table layout.
* `FFmpeg/FFmpeg`, `libavcodec/mpegaudiodec_common.c` — sfb and Huffman table
  dimensions, used only to cross-check the numbers read from the public
  text.
* `FFmpeg/FFmpeg`, `libavformat/mp3dec.c` — Xing/Info and VBRI field order
  and LAME delay/padding position.

## UNCONFIRMED

These are reference-derived or interpretation choices, not confirmed by a
conformance run:

* **UNCONFIRMED** — the Xing/Info field order and flag bits are not in the
  supplied ISO text; the implementation follows the common Xing layout
  (frames, bytes, TOC, quality in flag-bit order) and the FFmpeg demuxer's
  reading.
* **UNCONFIRMED** — whether a Xing/Info tag moves by the two CRC bytes. This
  parser places it at `side_info_end`, including the CRC; some demuxers use
  a side-info-only offset table and therefore differ on protected frames.
* **UNCONFIRMED** — the VBRI fixed offset 36 and its 18-byte
  version/delay/quality/bytes/frames layout. VBRI is not specified in the
  supplied ISO text; the implementation follows the published Fraunhofer
  layout used by the FFmpeg demuxer.
* **UNCONFIRMED** — the LAME delay/padding word at offset 21 in the
  nine-byte encoder extension. The LAME tag is not in the supplied ISO
  text; the implementation follows the common LAME layout.
* **UNCONFIRMED** — the final sfb row entry. The supplied text lists only
  the scalefactor bands; the tail entry here is derived from the fixed
  totals 576/192 and the statement that lines above the last band have
  factor 1.0. It matches the widths in the independent reference.
* **UNCONFIRMED** — tables 4 and 14 are treated as unsupported because the
  supplied tables print no codewords for them. A stream that selects one is
  rejected rather than guessed at.
* **UNCONFIRMED** — `emphasis == 2` is reserved by the table but accepted
  and exposed, because rejecting a whole stream over an emphasis value is a
  policy choice not required by the syntax.

## Verification

Nothing here is verified. The checks the original criteria require — an MD5
of decoded PCM against a reference, `ffprobe` metadata, `trace_headers`
field comparison, `opus_compare` (not applicable to MP3), an FFmpeg
difference run, and an fps/throughput measurement — have **NOT RUN**.
