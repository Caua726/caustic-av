# caustic-av

**Audio and video containers and codecs for [Caustic](https://github.com/Caua726/Caustic), written from the specifications: the job FFmpeg and GStreamer do for C, with neither of them inside.**

![status](https://img.shields.io/badge/status-VP9%20and%20MP3%20working%2C%20Opus%20broken-yellow)
![license](https://img.shields.io/badge/license-MIT-blue)

> **Decoders only, no encoders.** VP9 Profile 0 matches the official libvpx
> vectors and MP3 matches FFmpeg in a short comparison. **Opus compiles and runs
> but its output is wrong**: it does not match FFmpeg in any mode. See
> [Status](#status).

## What it is

A library that takes media files apart, separates their tracks, and decodes
what is inside them. Decoding and demuxing are the written part; muxing and
encoding are future work. A small hub (`caustic_av.cst`) wraps sniffing,
demuxing and codec dispatch; the per-format modules stay available underneath
it.

It is not a wrapper. FFmpeg, GStreamer and every other C codec library stay out
of the code and out of the build: each container and codec here is implemented
in Caustic, from its specification. FFmpeg appears only as an independent
program the test phase would compare against; no FFmpeg source is copied or
ported, because that would make this library a derivative of LGPL/GPL code.

The codec, container and hub modules allocate nothing: surfaces, sample
blocks, work areas and reference pools are all caller-owned. (The optional VP9
worker path asks `std/thread` for its stacks; the example allocates the file
and its decoder objects.) The library opens no files: the whole input is a
borrowed byte range. Bytes go in, borrowed packet views come out, and every
failure is a negative `i64`.

## Formats

### Containers

| Format | Demux | Mux | State |
|---|---|---|---|
| Matroska | yes | no | written (demux) |
| WebM | yes | no | written (demux; the Matroska reader) |
| Ogg | yes | no | written (demux) |
| WAV | no | no | planned |
| MP4 | no | no | planned |

The Matroska/WebM demuxer reads the EBML header, Segment, Info, Tracks and the
cluster stream, including SimpleBlock, BlockGroup/Block, Xiph/fixed/EBML
lacing, `DiscardPadding`, `CodecDelay` and `SeekPreRoll`. WebM is accepted as
the restricted DocType of the same reader. A track whose codec has no decoder
still opens and reports its CodecID and CodecPrivate.

The Ogg demuxer parses pages, verifies the page CRC, reassembles packets that
span pages, tracks logical streams by serial number and identifies Opus,
Vorbis, FLAC and Theora headers. Only the Opus path has a decoder behind it.

`demux.cst` sniffs the input by content and dispatches to the Matroska, Ogg or
raw-MP3 path, publishing a neutral `core/codec.Track` list. The raw-MP3 path
recognises an ID3v2 tag or a Layer III sync and publishes one audio track;
there is no muxer for any of these formats.

Matroska/WebM ContentEncodings (compression and encryption), compressed
headers and MP4/WAV demuxing are not written.

### Audio

| Codec | Decode | Encode | State and written limits |
|---|---|---|---|
| Opus | broken | no | output wrong (see Status). Ogg and WebM; OpusHead mapping family 0, 1-2 channels, 48 kHz output, signed 16-bit PCM |
| MP3 | yes | no | matches FFmpeg in a short comparison. Layer III only (MPEG-1, MPEG-2 and MPEG-2.5); CBR and VBR; ID3v2.2/2.3/2.4 and ID3v1 skipping; Xing/Info and VBRI metadata; sync scan; free format refused |
| PCM | no | no | planned |
| FLAC | no | no | planned |
| Vorbis | no | no | planned |

Opus is decoded from packets that the Ogg or Matroska demuxer hands out; the
audio layer itself performs no file I/O. SILK, CELT and Hybrid frames are
covered, including the 2.5-60 ms frame durations and NB/MB/WB/SWB/FB
bandwidths. The `OpusHead` pre-skip and the EOS end trim are applied by the
decoder, not by the container; WebM's `CodecDelay` stays a container value
for the caller to account for. Mapping families 1 and 255, more than two
channels, PLC, FEC and DTX are refused.

MP3 is different: the MP3 stream is read by `audio/mp3/` itself, not by a
container demuxer. `mp3.cst` skips ID3 tags, scans leading junk for the first
complete Layer III frame, parses the header and side information, and exposes
the reservoir, Xing/Info and VBRI fields. `mp3_decode.cst` and `mp3_synth.cst`
then produce PCM. The hub adds a raw-MP3 demux path and also dispatches
Matroska `A_MPEG/L3` blocks, including a block that holds several frames. Layer
I, Layer II and free-format bitrates are `I_UNSUPPORTED`.

### Video

| Codec | Decode | Encode | State and written limits |
|---|---|---|---|
| VP9 | yes | no | matches the official vectors. Profile 0, 8-bit 4:2:0 (I420) |
| VP8 | no | no | planned |
| AV1 | no | no | planned |

The VP9 decoder accepts Profile 0 only: 8-bit 4:2:0 output. It covers the
uncompressed and compressed headers, Annex B superframe splitting, keyframes
and intra-only frames, inter frames with single and compound prediction,
switchable interpolation, motion-vector prediction, segmentation, backward
probability adaptation, the loop filter, a 1..64 x 1..4 tile grid, variable
frame sizes, scaled references, hidden frames and `show_existing_frame`. One
packet can produce 0..8 displayed frames.

Coded slices are blocked until the VP9 probability and residual table blobs
exist: `vp9_decoder_receive` returns `I_UNSUPPORTED` after the header is
parsed, before any surface is allocated, while `VP9_TABLES_GENERATED` or
`VP9_RESIDUAL_TABLES_GENERATED` is 0. Header parsing and the show-existing
output path do not read them, but no coded frame can be decoded until they
are generated.

Profiles 1-3 (4:2:2/4:4:4, 10/12-bit), CS_RGB and the VP9 encoder are not
written.

The optional SIMD kernels (`dsp/`) and VP9 tile threading
(`video/vp9/threads.cst`) are merged; both are opt-in and default to the scalar/sequential path. The
SIMD kernels cover residual add, compound average and part of intra
prediction, not subpel filtering or the loop filter.

### Generated tables

No generator runs at build time. The committed table files are
comma-separated strings parsed at decoder init, and `tools/` contains the
generator sources that produce them.

| Generator | Output | State |
|---|---|---|
| `tools/gen_vp9_tables.py` | `video/vp9/vp9_tables_data.cst`: `inv_map`, keyframe probabilities, default probabilities | committed; generated and checked by the vector results |
| `tools/gen_vp9_residual_tables.py` | `video/vp9/residual_tables_data.cst`: scans, coefficient bands, token tree, energy classes, extra bits, category probabilities, pareto, quantizers, mode-to-transform | committed; generated and checked by the vector results |
| `tools/gen_opus_silk_tables.py` | `audio/opus/silk_tables_data.cst`: SILK PDFs and tables | committed; generator not run in this phase; no blob pending |
| `tools/gen_opus_celt_tables.py` | `audio/opus/celt_tables_data.cst`: `e_means`, `e_prob_model`, `caps` | committed; generator not run in this phase; its `--check-only` byte comparison has not run |
| `tools/gen_mp3_tables.py` | `audio/mp3/mp3_tables_data.cst`: bitrate/sample-rate/sfb/pretab/alias/synthesis-window/Huffman/scalefactor tables | committed; generator not run in this phase; analytic window/DCT blobs still pending |

The MP3 analytic IMDCT window and DCT-twiddle placeholders in that file are
still pending (`MP3_WINDOW_TABLES_GENERATED = 0`), but the MP3 decode path
builds its windows and twiddles analytically at init and does not consume
them; the committed 512-entry synthesis window is the tabulated vector it
reads. The Opus SILK and CELT data blobs are committed and no Opus blob is
pending, but the derivations recorded as UNCONFIRMED in `celt.md` are not
confirmed until an external comparison runs.

### Left out on purpose

**H.264, H.265 and AAC** are not planned, because their patents are licensed
through active paid pools (see [Patents](#patents)). A container holding one of
these tracks still opens and lists it, with its codec identified; asking to
decode that track returns `I_UNSUPPORTED`.

This has a real cost: many `.mp4` and `.mkv` files found in the wild use these
codecs, and their video or audio will not play.

## Layout

```
caustic-av/
  Causticfile
  caustic_av.cst        the hub: sniff, demux and decode wrappers
  demux.cst  demux.md   container sniffing and dispatch
  decode.cst decode.md  codec dispatch and the output record
  core/
    errno  buf  bits  codec  packet  frame  samples

  container/
    ebml.cst  matroska.cst  ogg.cst

  video/vp9/
    bool  vp9_tables  header  tile  block  residual  residual_tables
    recon_tables  transforms  predict  inter  inter_tables  mvpred  refs  scale
    adapt  adapt_bridge  loopfilter  decode  threads
    vp9_tables_data  residual_tables_data

  audio/opus/
    opus_head  opus_packet  range
    silk  silk_defs  silk_bits  silk_core  silk_nlsf  silk_resample
    silk_tables  silk_tables_data
    celt  celt_state  celt_energy  celt_alloc  celt_pvq  celt_mdct
    celt_tables  celt_tables_data  opus

  audio/mp3/
    mp3  mp3_tables  mp3_tables_data  mp3_decode  mp3_synth

  dsp/
    vp9_dsp_abi.cst  vp9_dsp.cst
    x86_64/ vp9_cpu.cst  vp9_intra.cst  vp9_idct.cst
             vp9_interp.cst  vp9_dsp_x86.cst

  tools/                five table generators
  tests/                run.cst, test_core.cst, test_matroska.cst, vectors/small/
  examples/             decode.cst  md5.cst  decode.md
```

The hub's demux layer (`demux.cst`) sniffs the source and dispatches to
`container/matroska.cst`, `container/ogg.cst` or the raw-MP3 parser in
`audio/mp3/`. The MP3 stream is still read by `audio/mp3/` itself, because an
MP3 file has no container packet layer: `mp3.cst` owns ID3, sync, headers and
the reservoir. `dsp/` holds only the VP9 kernels that gain from machine code;
the scalar kernels remain in `video/vp9/`, so a missing slot means the codec's
own code, not an error. The x86_64 module is selected at run time and is inert
on other targets. The example adds file I/O and heap ownership on top; the
library modules take borrowed ranges and caller-owned buffers.

Each written part carries a design note beside its code:

- Hub: `demux.md` (sniffing, dispatch, track list), `decode.md` (codec
  dispatch, packet handoff, output record, timestamps).
- VP9: `vp9.md` (state, headers, driver), `video/vp9/decode.md` (block syntax,
  tiles), `recon.md`, `residual.md`, `adapt.md`, `inter.md`, `refs.md`,
  `threads.md`.
- DSP: `dsp/vp9_dsp.md`.
- Opus: `audio/opus/opus.md`, `silk.md`, `celt.md`. The Ogg page and packet
  design lives in the Ogg section of `opus.md` and in the header comment of
  `container/ogg.cst`; there is no separate `container/ogg.md`.
- MP3: `audio/mp3/mp3.md` and `audio/mp3/mp3_decode.md`.
- Example: `examples/decode.md`.

## The API

The public hub is `caustic_av.cst`. It re-exports the shared vocabulary and
offers the shortest path through the library:

```
av_sniff(src, len)                     DEMUX_* by content, or negative
av_open(&d, src, len)                  0 or negative
av_next(&d, &pkt)                      1 = packet, 0 = end, negative = error
av_kind(&d)                            DEMUX_NONE/MATROSKA/OGG/MP3
av_track_count(&d)  av_track(&d, i)    the published codec.Track list
av_decode_open(&dec, &track)           0 or negative
av_decode_open_codec(&dec, id, priv, n)
av_decode_send(&dec, &pkt)             0 accepted, negative error
av_decode_receive(&dec, &out)          1 = output, 0 = drained, negative error
av_codec_name(id)  av_kind_name(kind)  diagnostics
av_error_name(status)
```

`Demux` and `Decoder` are caller-owned values; the library allocates neither.
`demux.cst` holds `demux_sniff`, `demux_open`, `demux_next`, `demux_kind`,
`demux_track_count` and `demux_track`. `decode.cst` holds `decode_open`,
`decode_open_codec`, `decode_send`, `decode_receive`, `decode_close`, the
pre-skip/end-trim hooks and the VP9 memory wrappers
(`decode_vp9_work_size`, `decode_vp9_bind`, `decode_vp9_add_buffer`,
`decode_vp9_bind_threads`, and the `need` queries). The hub wrappers keep the
underlying negative-`i64` errors unchanged.

`decode_open` takes a whole `core/codec.Track`, not just a codec id: Opus
cannot start without its private `OpusHead` bytes, and the timing policy needs
the track's pre-skip, codec delay and timebase. A `Track` carries the codec id,
the media kind, the container's own CodecID bytes, codec-private bytes,
geometry/rate/channels, pre-skip, codec delay, seek pre-roll and timebase.
VP9 additionally needs a persistent work block, up to nine I420 pool buffers
and optionally a worker block; the `decode_vp9_*` helpers size and bind them,
and the `need` queries report the size after a recoverable `I_NOMEM`.

One packet yields 0..8 VP9 frames, 0..1 Opus blocks or 0..N MP3 frames. The
`Output` record is either an I420 `frame.Frame` (`OUTPUT_VIDEO`) or an
interleaved S16 `samples.Samples` block (`OUTPUT_AUDIO`), plus the packet
timestamp, duration, timebase, render size and VP9 color fields as applicable.
Packet views are borrowed and valid until the next `demux_next`, so a decoder
must consume the armed packet before the caller advances the demuxer; a frame
or sample block is valid until the next receive. A `Demux` embeds the three
demuxer states (including Ogg's 64 KiB assembly buffer) and a `Decoder` embeds
the selected codec state, so both belong in caller storage rather than a small
stack.

### Decode example

`caustic-mk build decode` builds the example CLI:

```
./build/decode [--md5] [--pcm <file>] [--threads N] <file>
```

The container is detected by content: Matroska/WebM, Ogg (including `.opus`),
or a raw MP3 stream. The track list and diagnostics go to stderr. For video,
stdout is one lowercase MD5 per displayed frame, computed over the visible
I420 planes tightly packed (the libvpx/FFmpeg `framemd5` layout); for audio,
the payload is interleaved S16LE PCM, written to `--pcm` or to stdout when the
input is audio-only. `--threads` is the optional VP9 tile-worker count, 1..16
with the calling thread included; the default is the sequential path.
`examples/md5.cst` implements RFC 1321 for the frame hashes. The example has no
track selection, no seeking and no image output, and it does not trim MP3
LAME/VBRI gapless delay or padding.

`--yuv` writes displayed frames as raw I420 on stdout instead of their MD5.
`tools/play.sh` pipes that to `mpv`: in real time with the file's own audio,
`live` (silent, as fast as frames arrive), or `<N>` to pre-decode N seconds and
then play with audio and seeking. The audio it plays comes from `mpv`, not from
this library.

## Dependencies

No external library is imported by the written code. It uses Caustic's
standard library (`std/math` for MP3, `std/thread` for the VP9 worker path)
and files inside this repository. The example additionally uses `std/io`,
`std/mem` and `std/env`.

`caustic-compact` is **not used**. It was in the earlier plan for zlib over
compressed Matroska tracks and MP4 headers, and for CRC; the written demuxers
implement neither compressed path, and the Ogg CRC is computed bit by bit in
`container/ogg.cst`. Codecs need most-significant-bit-first reading,
which lives in `core/bits`.

## Roadmap

The decode path was written in this order; stages 8 and 9 are merged into
`main`:

1. **Foundation** — `core/` (errno, buffer and bit readers, packet, frame,
   samples) and the EBML reader.
2. **Containers** — the Matroska/WebM demuxer and the Ogg demuxer.
3. **VP9 Profile 0** — headers and superframes, keyframes and intra-only
   frames, inter frames with compound prediction, adaptation, segmentation,
   tiles, variable size, scaled references and show-existing output.
4. **Opus** — the range decoder, SILK, CELT, Hybrid, and the Ogg/WebM packet
   paths.
5. **MP3** — the stream parser (ID3, sync, header, side information,
   reservoir, Xing/VBRI) and the MPEG-1/2/2.5 Layer III decode and synthesis
   path; merged.
6. **SIMD and threads** — `dsp/` with the runtime table and x86_64 SSE2
   kernels, and multi-threaded VP9 tile columns; merged.
7. **Hub and example** — `core/codec.cst`, `demux.cst`, `decode.cst` and
   `caustic_av.cst` for sniffing, track publication, decoder dispatch and the
   output record; `examples/decode.cst` and `examples/md5.cst` with the
   `caustic-mk build decode` target.

Future work, deliberately separate from the written decode path:

- **Containers:** WAV and MP4 demuxing; muxing for Ogg, Matroska/WebM and the
  rest.
- **Audio codecs:** PCM, FLAC and Vorbis decode; Opus and MP3 encode.
- **Video codecs:** VP8 and AV1 decode; VP9 encode.
- **Conversion:** YUV/RGB, scaling and sample-format paths (no `convert/`
  tree is written).
- **Speed:** the SIMD table is installed only on x86_64 System V; AArch64 has
  no NEON kernels. Subpel filtering and the loop filter have no SIMD and the
  loop filter runs on one thread.

## Status

Results below use `caustic -O2 examples/decode.cst -o build/decode`.

| Part | Result | Checked against |
|---|---|---|
| VP9 Profile 0 | **works**: all 331 vectors with an official `.md5` pass frame by frame with 1 thread; 265 of them re-run with 4 threads also pass | libvpx `.md5` files |
| VP9 on real video | first minute of a 720p video, 1800/1800 frames | FFmpeg `framemd5` |
| MP3 | **works**: maximum sample difference of 1 on MPEG-1 stereo and mono, MPEG-2 and MPEG-2.5 | FFmpeg PCM, short generated files |
| Opus | **broken**: SILK, CELT and hybrid all decode without error but the audio is wrong (SNR below 0 dB) | FFmpeg PCM, short generated files |
| Matroska/WebM | exercised by every VP9 vector | same vectors |

Speed on that 720p video (low bitrate): 72 fps with 1 thread, 87 fps with 4, on a
machine where FFmpeg's VP9 decoder reaches about 1600 and 3200 fps. Busier video
will be slower.

Not checked yet:

- The RFC 6716 vectors through `opus_compare`; pointless until Opus is fixed.
- MP3 beyond the four short files above: real music, free-running VBR, ID3 and
  sync recovery. The MP3 output keeps the encoder delay, so it is compared
  after alignment.
- SIMD kernels against the scalar ones, and the threaded path on the rest of the
  vectors.
- The complete `bbb`, `sintel` and `tos` movie files; only `bbb_1280x720` was
  compared, up to frame 14975 of 17895.
- Errata marked UNCONFIRMED in the codec notes have not been reviewed one by one.
- The test runner only has the core and Matroska cases, with no codec suite. The
  VP9 and audio comparisons above were run by hand.

Known unsupported paths:

- **VP9:** Profiles 1-3, 10/12-bit, 4:2:2/4:4:4 and CS_RGB; encoding.
- **Opus:** mapping families 1 and 255, more than two channels, PLC, FEC,
  DTX, LBRR synthesis, redundant CELT frame synthesis, soft clipping and
  anything other than signed 16-bit output. Output gain is stored and can be
  enabled at codec level, but the hub neither exposes nor applies it.
- **MP3:** Layer I, Layer II, free-format bitrate, CRC verification, error
  concealment, de-emphasis and LAME/VBRI gapless trimming. The parser does not
  skip a corrupt frame in the middle of a stream; for MP3, the hub applies
  Matroska `DiscardPadding` only.
- **Containers and hub:** muxing, encryption, compressed tracks/headers,
  `TrackTimestampScale` other than 1.0, negative `DiscardPadding`;
  Matroska/WebM, Ogg and raw MP3 only, no WAV or MP4. Ogg identifies Vorbis,
  FLAC and Theora but has no decoder for them and no parsed granule unit for
  them. Raw-MP3 sniffing accepts one complete Layer III frame (no multi-frame
  probe), and Ogg chained tracks become visible during iteration rather than at
  open.
- **Example:** no track selection, no seeking, no image output, unlabelled MD5
  output when several video tracks exist, and no MP3 gapless trim. Its Ogg
  Opus initial-granule estimate for a cropped/joined stream is UNCONFIRMED.
- **Codecs:** PCM, FLAC, Vorbis, VP8 and AV1 decoders, and every encoder.

## Testing

```sh
caustic-mk run test        # core and Matroska cases, a few seconds
caustic-mk run test-opt    # the same through the optimizing backend
caustic -O2 examples/decode.cst -o build/decode
./build/decode --md5 file.webm
```

Compare one VP9 vector with its official hashes:

```sh
awk '{print $1}' vp90-2-03-deltaq.webm.md5 > ref.txt
./build/decode --md5 vp90-2-03-deltaq.webm 2>/dev/null | diff - ref.txt
```

The vectors live in the gitignored `tests/vectors/`. They come from
<https://storage.googleapis.com/downloads.webmproject.org/test_data/libvpx/>.

## Patents

The licence of this code covers this code. **It grants no patent rights.**

The formats implemented here are public specifications, but that does not
guarantee that no third party claims patents on them: Sisvel runs pools
claiming patents on VP9 and AV1, Access Advance runs another for AV1, and AV1 is
the subject of patent litigation. Anyone shipping a product built on this library
should check their own position. Nothing here is legal advice.

## Licence

MIT.
