# Container sniffing and demux dispatch

Stage 10. `demux.cst` identifies a container from its leading bytes, dispatches
to the Matroska, Ogg or raw-MP3 demuxer already in the tree, and publishes one
packet at a time with a neutral `core/codec.Track` list. `core/codec.cst` holds
the shared codec identifiers and that track description.

Status: **WRITTEN, NOT VERIFIED.** Nothing in `demux.cst` or
`core/codec.cst` has been compiled or run. The `ffprobe` / packet-pts /
track-field comparisons the original briefs name are recorded at the end as
NOT RUN.

## Files

| File | Holds |
|---|---|
| `demux.cst` | Sniffing, container dispatch, track-list construction, raw-MP3 packetisation, `demux_open` / `demux_next`. |
| `core/codec.cst` | `CODEC_*` and `TRACK_*` identifiers, `codec_name`, `codec_kind`, `kind_name`, the neutral `Track`, `track_clear`. |
| `container/matroska.cst` | The Matroska/WebM demuxer this module dispatches to (stage 2, merged). |
| `container/ogg.cst` | The Ogg demuxer this module dispatches to (stage 7, merged). |
| `audio/mp3/mp3.cst` | The raw MP3 parser this module dispatches to (merged stage 8). |
| `audio/opus/opus_head.cst` | Parses OpusHead for Ogg track fields (merged). |

## API

```text
demux_sniff(src, len)          DEMUX_* or a negative error
demux_open(d, src, len)        0 or a negative error
demux_next(d, pkt)             1 = packet, 0 = end, negative = error
demux_kind(d)                  DEMUX_NONE / DEMUX_MATROSKA / DEMUX_OGG / DEMUX_MP3
demux_track_count(d)           tracks discovered so far
demux_track(d, i)              *codec.Track, or null out of range
```

`Demux`, `Track`, `Packet` and every other structure are caller-owned. The
library opens no file and allocates nothing. Matroska and raw MP3 packets
borrow the source directly; an Ogg packet that spans pages is assembled in the
Ogg state's fixed 64 KiB buffer, which `container/ogg.cst` documents. A `Demux`
embeds the Matroska, Ogg and MP3 demuxer states at once (the Ogg assembly
buffer alone is 64 KiB), so it belongs in caller storage rather than a small
stack; Caustic has no untagged union to overlay the three.

## Sniffing table

The order is fixed: container signatures first, then ID3, then the MP3 frame
scan.

| Leading bytes | Kind | Validated by |
|---|---|---|
| `1A 45 DF A3` | `DEMUX_MATROSKA` | `matroska.mk_open`: EBML header, `DocType` `webm` or `matroska`, `DocTypeReadVersion <= 4`, `EBMLReadVersion <= 1`, id/size bounds, Segment, Info, Tracks, first Cluster. |
| `4F 67 67 53` (`OggS`) | `DEMUX_OGG` | `ogg.ogg_open`: stream structure version 0, page CRC (non-reflected 0x04C11DB7), BOS ordering, one track per serial. |
| `49 44 33` (`ID3`) | `DEMUX_MP3` | `mp3.mp3_open`: ID3v2.2/2.3/2.4 size, then a complete Layer III frame after the tag. |
| anything else, with a complete Layer III frame after optional junk | `DEMUX_MP3` | `mp3.mp3_open`'s leading-junk scan (`_mp3_find_frame`). |
| nothing matching | `I_BADMAGIC` | — |
| empty source | `I_EOF` | — |

Notes:

- **EBML is a family, not a container.** The four-byte header id is enough to
  choose the Matroska path; `mk_open` is what rejects a DocType other than
  `webm`/`matroska` with `I_BADMAGIC`. A bare EBML/other file therefore sniffs
  as Matroska and fails at open, not at sniff. This differs from FFmpeg's
  `matroska_probe`, which scans the header for a known DocType string and
  lowers the score when it finds only a valid EBML header; the hub has one
  Matroska reader and no score, so the DocType test belongs in its open.
- **Ogg.** `demux_sniff` looks only at `OggS`. A page with a future structure
  version or a bad CRC is `I_UNSUPPORTED` / `I_CORRUPT` from `ogg_open`.
- **ID3v2 alone is not enough.** A file that starts with `ID3` but contains no
  Layer III frame (for example an ID3-tagged AAC file) dispatches to the MP3
  parser and fails there with the parser's error, usually `I_BADMAGIC`. This
  follows the brief's "ID3v2 magic or a valid Layer III sync" rule: the magic
  chooses the path, the parser decides whether it is really MPEG audio.
- **False positives.** The raw-sync test accepts the first complete Layer III
  frame that `mp3_open` validates. FFmpeg's `mp3_read_probe` requires several
  consecutive frames and a low false-sync rate; this hub does not. A binary
  file with a plausible 4-byte sync and one full frame length could therefore
  sniff as MP3. This is recorded under UNCONFIRMED. Adding a multi-frame rule
  would be a behaviour change, not a syntax fix.
- **Cost.** On a non-container file the MP3 scan is byte-by-byte over the
  available bytes, the same scan `mp3_open` performs. `demux_open` runs
  `demux_sniff` and then `mp3_open` again for the MP3 path, so the leading
  scan happens twice. Both are bounded by `len` and allocate nothing; a later
  stage may make the sniff return a prepared parser instead.

## Track list

`core/codec.Track` is all scalars plus two borrowed views:

| Field | Meaning |
|---|---|
| `codec` | `CODEC_*`, the value `decode.open` dispatches on. |
| `kind` | `TRACK_VIDEO`, `TRACK_AUDIO`, `TRACK_SUBTITLE` or `TRACK_UNKNOWN`, from the container where it states a TrackType. |
| `codec_id`, `codec_id_len` | The container's own CodecID bytes exactly as stored, not NUL-terminated. Matroska only; `null`/0 when the container has no such string (Ogg, raw MP3). |
| `priv`, `priv_len` | Codec private bytes: Matroska CodecPrivate, or the Ogg BOS packet (OpusHead for Opus). Borrowed; never freed here. |
| `width`, `height` | Matroska PixelWidth/PixelHeight, else 0. Ogg Theora geometry is not parsed. |
| `rate`, `channels` | Audio output rate and interleaved channel count, else 0. |
| `pre_skip` | Opus only: 0 means "use the OpusHead value", a positive value overrides it. Filled from OpusHead for both containers. |
| `default_duration` | Container frame duration in ns, else 0. |
| `codec_delay` | Matroska CodecDelay in ns, else 0. Not subtracted from packet timestamps (see below). |
| `seek_pre_roll` | Matroska SeekPreRoll in ns, else 0. |
| `timebase_num`, `timebase_den` | Seconds per packet timestamp tick. `den == 0` means "this stage cannot interpret the container's granule unit". |
| `number` | Container track identity: Matroska TrackNumber, Ogg serial, or 1 for raw MP3. |

### Matroska codec mapping

| CodecID | `codec` | Notes |
|---|---|---|
| `V_VP8` | `CODEC_VP8` | listed, decode refuses |
| `V_VP9` | `CODEC_VP9` | decode dispatches |
| `V_AV1` | `CODEC_AV1` | listed, decode refuses |
| `V_MPEG4/ISO/AVC` | `CODEC_H264` | listed, decode returns `I_UNSUPPORTED` |
| `V_MPEGH/ISO/HEVC` | `CODEC_H265` | listed, decode returns `I_UNSUPPORTED` |
| `V_THEORA` | `CODEC_THEORA` | listed, decode refuses |
| `A_OPUS` | `CODEC_OPUS` | CodecPrivate parsed for channels/pre-skip; rate fixed at 48000 |
| `A_VORBIS` | `CODEC_VORBIS` | listed, decode refuses |
| `A_FLAC` | `CODEC_FLAC` | listed, decode refuses |
| `A_MPEG/L3` | `CODEC_MP3` | decode dispatches |
| `A_AAC` and any `A_AAC/...` | `CODEC_AAC` | listed, decode returns `I_UNSUPPORTED` |
| `A_PCM/...` | `CODEC_PCM` | listed, decode refuses |
| anything else | `CODEC_UNKNOWN` | listed, decode returns `I_UNSUPPORTED` |

`kind` still comes from the TrackType field, so a subtitle track with an
unknown CodecID keeps `TRACK_SUBTITLE`. That is deliberate: the codec table
says what the payload is, the container says what the track is for.

An audio SamplingFrequency is a float. It is truncated to whole Hz. Matroska
already rejects a non-finite or non-positive frequency for a track with an
Audio master; the conversion repeats the guard, treating NaN as `I_CORRUPT`
and a value above 2^62 Hz as `I_DIMS`, so no later sample-count arithmetic can
overflow.

For `A_OPUS`, the output rate is always 48000 Hz; RFC 7845 Section 4 counts
granules at that rate regardless of the container's SamplingFrequency.
Channels and pre-skip come from the OpusHead in CodecPrivate when it parses;
when it does not, the container's channel count is published and `decode.open`
reports the parse error.

### Ogg mapping

| BOS magic | `codec` | `kind` | rate | timebase |
|---|---|---|---|---|
| `OpusHead` | `CODEC_OPUS` | audio | 48000 | 1/48000 |
| `\x01vorbis` | `CODEC_VORBIS` | audio | 0 (header not parsed) | 0/0 |
| `\x7fFLAC` | `CODEC_FLAC` | audio | 0 | 0/0 |
| `\x80theora` | `CODEC_THEORA` | video | 0 | 0/0 |
| anything else | `CODEC_UNKNOWN` | unknown | 0 | 0/0 |

Only Opus gets a parsed timebase, because RFC 7845's granule is defined as the
48 kHz sample count. Vorbis and FLAC granules are codec-defined and Theora
granules count frames; this stage does not parse those headers, so the
timestamp unit stays explicitly unknown rather than guessed.

The track's `priv` is the BOS packet, so the Opus decoder receives the same
OpusHead the container did. `pre_skip` is filled from that header.

### Raw MP3

One track, index 0:

- `codec = CODEC_MP3`, `kind = TRACK_AUDIO`, `number = 1`.
- `rate` and `channels` from the first Layer III header.
- `timebase = 1/rate`; timestamps are sample ticks.
- `codec_id = null`, `priv = null`: ISO/IEC 11172-3 has no container-level
  CodecID or private block. `codec_name(CODEC_MP3)` reports `A_MPEG/L3` for
  diagnostics.
- `default_duration = 0`: one frame is 1152 or 576 samples, which is exact in
  sample ticks but not in nanoseconds at 44.1 kHz. `Packet.duration` carries
  the sample count.
- LAME/VBRI encoder delay and padding are parsed by `mp3.cst` (`encoder_delay`,
  `encoder_padding`, `frame_count`, `lame_present`) but are not represented in
  this track and are not applied by the hub. See PENDING.

## Packet lifetime and ownership

`demux_next` fills a `core/packet.Packet` whose `data` is a borrowed view:

- **Matroska:** points into the source buffer. `pts`, `dts` and `duration`
  are nanoseconds; `discard_padding` is nanoseconds and is present only on the
  last lace of a BlockGroup; `keyframe` is the block key flag.
- **Ogg:** points into the source buffer, or for a packet that spans pages
  into `Ogg.pktbuf`, the demuxer's own 64 KiB assembly buffer. `pts`/`dts`
  are the completing page's granule (for Opus, 48 kHz samples; granule -1 is
  passed through); `duration` is 0; every Opus packet is a keyframe.
- **Raw MP3:** points into the source buffer at `Mp3Frame.offset`; `len` is
  the frame length; `pts`/`dts` are sample ticks; `duration` is
  `samples_per_frame`; `keyframe` is 1 only when `reservoir_back == 0`, i.e.
  the frame's main data does not reach into earlier frames.

The view is valid until the next `demux_next` call, which is the point where an
Ogg assembly buffer may be overwritten. The source buffer itself must outlive
the `Demux` and every packet. A caller that decodes in a different order must
copy first; this module never copies.

`packet.track` is a 0-based index into the list `demux_track` returns. A packet
whose index would fall outside the list is `I_CORRUPT`, never a silent clamp.

## Raw MP3 iteration

`_open_mp3` reads the first frame's header at `mp3_state.first_frame` without
consuming it, fills the track, and leaves the parser positioned there.
`_next_mp3` then calls `mp3_next`, which validates the frame and its side
information and advances. The frame is published whole: header, optional CRC,
side information and main data.

- A frame whose sample rate or channel count differs from the first frame is
  `I_UNSUPPORTED`. The track list and its timebase were fixed at open, and
  silently retiming the rest of the stream would misplace every later
  timestamp.
- `mp3_next` does not resynchronise after a corrupt frame in mid-stream; it
  returns the header error and stops advancing. The hub passes that through.
- The bit reservoir is the decoder's business: `decode.cst` keeps the saved
  main-data tail inside `Mp3Decode` and feeds it the frame's
  `main_data_offset`/`main_data_len`. The demuxer only publishes bytes.
- Sample-tick timestamps stop at 2^62; a stream that long is `I_CORRUPT`
  rather than a wrapped timestamp.

## Ogg track discovery

`ogg_open` registers the leading BOS pages. A chained bitstream adds a new
serial later, during `ogg_next`, and `_next_ogg` publishes that track before
returning the packet. `demux_track_count` can therefore grow between calls for
Ogg; for Matroska and MP3 it is fixed by `demux_open`. A caller should read the
track for a packet after `demux_next` returns, not from a snapshot taken at
open.

## Errors

| Code | When |
|---|---|
| `I_BADPARAM` | null `Demux`/`Packet`, negative length, null source with bytes. |
| `I_EOF` | empty source; a container header or page cut short; a declared frame past the source. |
| `I_BADMAGIC` | no recognised signature; EBML DocType neither webm nor matroska; no Layer III frame after the ID3 tag/junk. |
| `I_UNSUPPORTED` | Matroska: `DocTypeReadVersion > 4`, `EBMLReadVersion > 1`, `EBMLMaxIDLength` outside 1..4, `EBMLMaxSizeLength` outside 1..8, `TrackTimestampScale != 1.0`, or a negative BlockGroup `DiscardPadding`. Ogg: version other than 0, or too many tracks. MP3: a Layer I/II or free-format stream that carried an ID3 tag, or a frame whose rate/channels change mid-stream. Without an ID3 tag, the sniff scan accepts only a Layer III frame and reports `I_BADMAGIC`. |
| `I_CORRUPT` | malformed page/EBML/block/header; a track index outside the published list; a timestamp past the arithmetic ceiling. |
| `I_DIMS` | a Matroska audio rate too large to represent; an ID3-tagged MP3 larger than `core/bits`' byte ceiling. |

`demux_open` clears the `Demux` first and again on failure, so a failed open
leaves `demux_kind == DEMUX_NONE`, no tracks, and `demux_next` returning 0.
Container modules already behave that way; the hub preserves it.

## Integration points and assumptions

- **MP3 (merged stage 8).** `demux.cst` imports `audio/mp3/mp3.cst` and uses
  `mp3_open`, `mp3_next`, `mp3_header_parse`, the `Mp3` state,
  `Mp3Frame.header/side`, `offset`, `length`, `samples_per_frame`,
  `reservoir_back`, `first_frame`, and the Xing/LAME fields named above.
  Those names were re-checked against merged `main` (`1cc1c63`) and match, so
  no call-site adaptation was needed.
- **Matroska/Ogg** use only already-merged public functions
  (`mk_open`, `mk_next`, `mk_track_count`, `mk_track`, `mk_track_codec_eq`,
  `ogg_open`, `ogg_next`, `ogg_track_count`, `ogg_track`).
- **Opus** uses only `opus_head_parse`, which is merged.
- The README's `demux.open(src, len, &d)` spelling maps to
  `demux_open(&d, src, len)`; Caustic passes the object first and writes no
  return value through a pointer.

## References read

Read only to understand behaviour; no code was copied, ported or transcribed:

- FFmpeg `libavformat/matroskadec.c` — `matroska_probe` (EBML header id,
  header size vint, DocType string search) and the demuxer entry.
- FFmpeg `libavformat/oggdec.c` — `ogg_probe` (`OggS` and version <= 7),
  stream/serial model.
- FFmpeg `libavformat/mp3dec.c` — `mp3_read_probe` (consecutive-frame count,
  false-sync rejection, ID3-only fallback).
- RFC 3533 Sections 5 and 6 (page header, lacing, BOS, granule) from
  `.work/opus/spec/rfc3533.txt`.
- RFC 7845 (Opus granule and pre-skip) through the repository's Opus notes.
- The repository's `container/matroska.cst`, `container/ogg.cst` and their
  notes, for the already-implemented contracts.

## UNCONFIRMED

- **Sniff scope.** Whether `demux_sniff` should also validate a bare EBML
  DocType, as FFmpeg's probe does, or defer it to `mk_open`. This module
  defers; the result is still `I_BADMAGIC`, only from `demux_open` rather than
  `demux_sniff`.
- **MP3 false positives.** A single complete Layer III frame is accepted. The
  FFmpeg probe's multi-frame score would be stricter; no threshold is adopted
  here because the brief names a "valid sync", not a frame count.
- **SamplingFrequency truncation.** A fractional Matroska rate is truncated
  toward zero. No real file is known to carry one; the alternative (round to
  nearest or refuse fractional rates) is untested.
- **Ogg non-Opus timebases.** Only Opus gets a granule unit here. Whether a
  caller would prefer `1/rate` from the Vorbis/FLAC identification header,
  which this stage does not parse, is open.
- **Ogg chained-track visibility.** The track list grows during iteration.
  A caller that opens decoders for every track at `demux_open` time will miss
  a chained stream; the alternative (scan the whole file at open) would cost
  a second full pass.
- **MP3 Xing/LAME trimming.** The parser exposes encoder delay/padding, but
  the track list does not carry them and the hub does not trim. The merged MP3
  notes leave gapless trimming to the layer that owns the stream (the caller
  of `mp3_next`/`mp3_decode_frame`); that layer is this hub, and it is not
  done. PENDING (review item 3).

## Pending review notes

Items 1-3 are behavioural/contract notes from the post-rebase review; 4-10 are
LOW.

1. **Ogg Opus per-packet pts is page-end, not packet start.**
   `container/ogg.cst` sets `Packet.pts` to the completing page's granule,
   which for Opus is the sample count through the last packet on that page;
   earlier packets on the page share it. Neither the container module nor
   `demux.cst` derives per-packet starts from the Opus TOC durations.
   `decode.cst` passes the value through unchanged. A caller that needs exact
   starts must walk the TOC or accept page granularity.
2. **OpusHead output gain has no decode-level application or accessor.**
   `opus_decoder_init` stores the Q7.8 value and
   `opus_decoder_set_apply_gain` exists, but `decode.cst` has no decode-level
   getter/setter and leaves `apply_gain` at its default (off). RFC 7845
   Section 5.1 says players and media frameworks SHOULD apply it by default,
   with any additional volume adjustment applied in addition; this hub leaves
   that choice to the caller. PENDING.
3. **Raw-MP3 gapless trimming belongs to this hub and is not done.**
   The merged MP3 notes say neither MP3 stage trims and that dropping encoder
   delay/padding belongs to the layer that owns the stream (the caller of
   `mp3_next`/`mp3_decode_frame`). That layer is `demux.cst`/`decode.cst`:
   `Track` carries no `encoder_delay`/`encoder_padding`, and no hub API
   exposes them. PENDING.
4. **"copies no packet byte" was wrong for Ogg.** An Ogg packet that spans
   pages is copied into `Ogg.pktbuf`. `demux.cst` and the ownership section
   above now say so. LOW.
5. **`Mp3Frame.index` is always 0 in the hub's reconstructed frames.**
   `mp3_decode_frame` does not read the field, so decoded output is
   unaffected; it is not the stream frame number. LOW.
6. **Stale not-merged text.** Removed after rebasing onto `1cc1c63`; stages 8
   and 9 are merged, and their names were re-checked against that tree with
   no mismatch. LOW.
7. **EOS-trim advice has no accessor.** `decode.md` suggests using
   `decode_set_end_trim` for an Ogg Opus EOS granule, but neither `Packet` nor
   this hub exposes an EOS flag or the final granule; a caller must track it
   through `container/ogg.cst` or the raw packet stream. The setter itself
   exists. LOW.
8. **Unreachable branch in `_receive_mp3`.** `count == 0` can only follow the
   last-frame DiscardPadding path, which sets `pending = 0`, so
   `if (d.pending == 0) return 0; continue;` always returns and the `continue`
   is dead. No behavioural effect. LOW.
9. **Matroska `I_UNSUPPORTED` causes were missing from the error table.**
   `mk_open` returns it for `DocTypeReadVersion > 4`, `EBMLReadVersion > 1`,
   `EBMLMaxIDLength` outside 1..4, `EBMLMaxSizeLength` outside 1..8 and
   `TrackTimestampScale != 1.0`; `mk_next` returns it for a negative
   BlockGroup `DiscardPadding` (start padding). The error table above now
   lists them. LOW.
10. **`_id_prefix("A_AAC")` also matches `A_AACX`.** The prefix test maps any
    CodecID beginning with `A_AAC` to `CODEC_AAC`. A Matroska mapping that
    starts that way is not standard, so this is LOW; tightening it to exact
    `A_AAC`/`A_AAC/` is a future fix.

## Checks NOT RUN

Nothing was compiled or executed. The original criteria would require:

- `ffprobe -show_streams` comparison of codec id/type, geometry, rate,
  channels and private data for each committed vector.
- packet `pts`/`dts`/`duration`/`size`/`flags` comparison against `ffprobe`
  for the WebM and Ogg vectors and a generated MP3.
- An MD5 or byte comparison of the packet bytes against the parser modules'
  own output.
- A sniff matrix over every vector plus negative inputs (empty, text, bare
  EBML header, ID3-tagged non-MP3).
