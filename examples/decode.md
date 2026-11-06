# Decode example

`examples/decode.cst` is the end-to-end user of the stage 10 hub: it reads a
whole file, calls `demux.open` / `demux.next` and `decode.open` / `send` /
`receive`, prints the track list, hashes every displayed video frame, and
writes decoded audio as interleaved S16 PCM. The library still opens no files:
the example is the only code here that calls `std/io`.

Status: **WRITTEN, NOT VERIFIED.** Nothing in this file, `examples/decode.cst`,
`examples/md5.cst` or the `decode` target has been compiled or run.

## Build and usage

```text
caustic-mk build decode
./build/decode [--md5] [--pcm <file>] [--threads N] <input>
```

The format is detected by content, not by the extension: a WebM/Matroska,
Ogg (including `.opus`), or raw MP3 stream. The named extensions are the ones
the brief covers, not a required suffix.

| Option | Meaning |
|---|---|
| `--md5` | Print the per-frame I420 MD5 (the default for video; accepted explicitly for scripts). |
| `--pcm FILE` | Write decoded interleaved S16LE PCM to `FILE`. |
| `--threads N` | VP9 tile threads, 1..16, calling thread included. Default 1 = the merged sequential path. Stage 9 API, **PENDING** (see below). |
| `--help`, `-h` | Usage on stdout, exit 0. |

## Output contract

Track information and diagnostics go to **stderr**. Payload goes to **stdout**:

```text
container: matroska/webm
track 0: video V_VP9 codec_id=V_VP9 1280x720 timebase=1/1000000000
track 1: audio A_OPUS codec_id=A_OPUS 48000 Hz 2 ch pre_skip=312 timebase=1/48000
<32 lowercase hex digits>          one line per displayed video frame
<32 lowercase hex digits>
...
decoded track 0: V_VP9 30 frames
decoded track 1: A_OPUS 144000 samples/ch at 48000 Hz
pcm: 144000 samples/ch written, 144000 decoded (Ogg Opus final-granule trim checked)
```

- **Video:** one bare lowercase MD5 per displayed frame, in the order the
  decoder returns frames (presentation order hint `order`, with show-existing
  outputs included). A packet's hashes are held until that packet drains, so a
  recoverable VP9 `I_NOMEM` and resend cannot duplicate frames that were
  already hashed. No prefix, so the stream is directly diffable against a
  libvpx `.md5` file for a single-video-track input. A file with more than one
  decodable video track emits the hashes in decode order without a label;
  track selection is not part of this example.
- **Audio:** raw S16 little-endian, interleaved, tightly packed. With `--pcm`
  it goes to that file. Without `--pcm`:
  - an audio-only input writes PCM to stdout (`decode in.opus > out.s16le`);
  - an input with any video track writes no PCM (stdout is the MD5 list), and
    the audio is decoded for the summary only. Use `--pcm` to get both.
  A second audio track is refused when a PCM sink is active, because one file
  cannot describe two interleaved streams. A video track discovered after a
  stdout PCM stream has started (a chained Ogg) is also refused.
- **Timing:** the example does not print packet timestamps. The hub output
  record carries them, but the MD5 comparison is sample/frame order only.
- **Errors after output started:** a fatal demux/decode error exits with a
  nonzero status; a `--pcm` file (or a redirected stdout) can already contain
  the blocks written before the failure. The example does not truncate or
  unlink partial output.

Exit status:

| Code | Meaning |
|---|---|
| 0 | At least one track was decoded, and no fatal demux/decode/output error occurred. Unsupported side tracks are reported but do not alone fail a mixed file. |
| 1 | Read, demux, decoder, output or end-trim failure; or no track in the file is decodable; or `--pcm` was given with no audio track. |
| 2 | Usage error: unknown option, missing option argument, or more than one input. |

## MD5 definition

`examples/md5.cst` implements RFC 1321 MD5 (state, 64-byte block, FF/GG/HH/II
rounds, the published constants, the `0x80` pad and the 64-bit little-endian
bit length). It is not a library module because the core tree has no digest
code and this is the only consumer.

The per-frame input is the **visible** I420 planes, tightly packed, plane by
plane, row by row:

```text
MD5( Y[0..h-1][0..w-1] ||
     U[0..(h+1)/2-1][0..(w+1)/2-1] ||
     V[0..(h+1)/2-1][0..(w+1)/2-1] )
```

No coded-padding bytes and no stride padding enter the hash. This is exactly
what libvpx's `test/md5_helper.h` does: for plane 0 it feeds `d_w` bytes per
row for `d_h` rows, and for chroma `(d_w + 1) >> 1` bytes per row for
`(d_h + 1) >> 1` rows, stepping by `stride[plane]`. It is also what FFmpeg's
`-f framemd5` hashes for video: the rawvideo encoder (`libavcodec/rawenc.c`)
copies the frame with `av_image_copy_to_buffer(..., align=1)`, which produces
the same tight Y/U/V bytes, and the framemd5 muxer hashes that packet.

For VP9 profile 0, 8-bit 4:2:0, `w`/`h` are the visible width/height in
`Output.video.width`/`height`; `stride_y`, `stride_u` and `stride_v` locate
rows in the caller's pool buffer and are deliberately not hashed. Odd visible
dimensions use the ceiling chroma sizes, matching both references.

## PCM policy

- **Opus:** always 48 kHz, interleaved S16. The effective pre-skip is the
  OpusHead value (`decode_pre_skip`), already applied by the hub. WebM
  `DiscardPadding` is applied by the hub to the packet that ends a block.
- **Ogg Opus end trim:** RFC 7845 Sections 4.3/4.4 make the final page granule
  the end of the PCM sample position, with `position = granule - pre-skip`.
  The number of output samples to keep is that final position minus the
  stream's initial granule position (the granule before its first decoded
  sample). A normal stream starts at granule zero, so the initial granule is
  zero. For a cropped or joined stream (Section 4.5) the example estimates it
  from the first audio page as
  `first_granule - first_page_returned_samples - pre_skip`, which is
  `first_granule - first_page_decoded_samples` when the pre-skip lies inside
  that page; normal files give exactly zero. The example delays writing the
  last decoded block by one packet, then, at EOF, removes
  `decoded_total - (final_granule - pre_skip - initial_granule)` samples from
  the tail and writes the shortened block. The removed count is bounded by the
  final packet, as the RFC requires; a larger trim is reported as unsupported.
  If the pre-skip spans the whole first audio page, the initial-granule
  estimate is smaller than the true one by that overflow, so the trim is
  under-applied by at most `pre-skip` samples (recorded as UNCONFIRMED).
- **MP3:** native sample rate from the frame header, interleaved S16. The hub
  applies Matroska `DiscardPadding`; LAME/VBRI encoder delay and padding are
  **not** trimmed, because the stage 10 decode note records that the merged
  path does not implement gapless trim (`PENDING` there).
- **Byte order:** the decoder writes S16 into a host `i16` scratch. Every
  supported target is little-endian, so the bytes are S16LE; the example does
  not byte-swap. The reference command is `ffmpeg -f s16le`.
- No resampling, mixing, gain or channel conversion is performed. The file is
  the decoder output, exactly.

## Memory and threads

- The input file is read whole into one `std/mem` bins allocation; it must
  outlive the demuxer and every packet.
- Every `decode.Decoder` is a bins allocation (`sizeof(Decoder)`), one per
  opened track, and is released after `decode_close`.
- VP9 caller memory follows the hub contracts:
  - `decode_vp9_work_size(w, h)` bytes for the persistent work block, bound
    with `decode_vp9_bind`;
  - `decode_vp9_frame_size(w, h)` bytes for each of the nine pool buffers
    (`VP9_POOL_BUFFERS`), added with `decode_vp9_add_buffer`;
  - `decode_vp9_threads_work_size(w, h, N)` bytes for the optional worker
    block, bound with `decode_vp9_bind_threads`.
  `w`/`h` are the container track dimensions. After a recoverable
  `I_NOMEM`, the example asks `decode_vp9_work_need`,
  `decode_vp9_frame_need` or `decode_vp9_threads_need`, grows the work block,
  replaces only a pixel buffer no reference slot names, or grows the worker
  block, and sends the same packet again, up to 32 times. A track without
  dimensions is refused with `I_DIMS`.
- Threads are opt-in through `--threads`; the default never binds a worker
  block and uses the merged sequential walk. The stage 9 note documents that
  output is meant to be identical for valid streams.

## Unsupported and error paths

- Listed but not decoded: VP8, AV1, H.264, H.265, AAC, Vorbis, FLAC, Theora,
  PCM and unknown codecs return `I_UNSUPPORTED` from `decode_open`. They stay
  in the track list with a message; a file where nothing is decodable exits 1.
- Subtitle and unknown TrackType entries are listed and skipped.
- VP9 profiles above 0, 4:2:2/4:4:4, 10/12-bit and CS_RGB surfaces are refused
  by the VP9 path with `I_UNSUPPORTED`; the example passes the message through.
- Malformed containers, malformed OpusHead, corrupt packets, a short read and
  an unopenable `--pcm` file are fatal, with `err_name` and the exact negative
  code on stderr, and exit 1.
- No image formats (BMP and the rest) are handled: this is an audio/video
  decoder example.

## Assumptions and PENDING

- **Stage 8 MP3 is not merged.** The hub imports `audio/mp3/mp3.cst` and
  `audio/mp3/mp3_decode.cst`. The example uses only the hub's documented
  `decode_open`/`send`/`receive`, so it should not need changes when those
  branches land; if the hub call sites change after merge, this example is
  unaffected at the API level.
- **Stage 9 threads are not merged.** `--threads` calls
  `decode_vp9_threads_work_size` and `decode_vp9_bind_threads`, which the hub
  marks PENDING. Until the stage 9 branch merges, the hub itself does not
  compile; the example's thread path is written against the documented API.
- **VP9 table blobs are still pending.** `vp9_decoder_receive` returns
  `I_UNSUPPORTED` until `tools/gen_vp9_tables.py` and
  `tools/gen_vp9_residual_tables.py` have been run and their blobs committed.
  A VP9 run of this example will therefore stop with that message in the
  current tree; this is the decoder saying it is not ready, not a frame hash.
- **MP3 synthesis window.** The stage 8b note says it reads the committed
  512-entry vector through `mp3_synth_window`. Whether the merged branch left
  it zero-filled is a merge-time check; this example cannot see it.
- **Ogg Opus end trim** follows RFC 7845 Sections 4.3/4.4 and was written
  from the RFC text. It has not been compared sample-for-sample with FFmpeg or
  `opus_compare`. The one-block delay is enough because the RFC limits the
  discard to the last packet; a larger trim is reported, not silently
  truncated. **UNCONFIRMED:** the initial granule position for a
  cropped/joined stream is estimated as
  `first_granule - first_page_returned - pre_skip`. That is exact when the
  pre-skip lies inside the first audio page (the normal encoder layout); when
  the pre-skip spans the whole page it is low by the overflow, so the end trim
  is under-applied by at most `pre-skip` samples. No joined/cropped vector has
  been examined.
- **Multiple video tracks** are hashed in decode order with no per-track
  label. This is a documented example limitation, not a library contract.
- **No track selection or seeking.** The example decodes every track it can.

## Reference implementations read

Read from the upstream URLs while writing this example; nothing was copied,
ported or saved:

- libvpx `test/md5_helper.h` — per-plane visible rows and chroma rounding,
  the layout the frame MD5 follows.
- FFmpeg `libavformat/hashenc.c` and `libavcodec/rawenc.c` — framemd5 hashes
  the rawvideo packet produced by `av_image_copy_to_buffer(..., align=1)`.
- RFC 1321 (MD5) — state, rounds, constants, padding and digest order.
- RFC 7845 Sections 4.2-4.4 — pre-skip, PCM sample position and end trimming.
- The repository's `demux.md`, `decode.md`, `video/vp9/decode.cst` and
  `video/vp9/decode.md` for the caller-memory and output contracts.

## Checks NOT RUN

Nothing was compiled, built or executed. The checks the original criteria
would require, all **NOT RUN**:

- `caustic-mk build decode` and `./build/decode <file>` on the committed WebM,
  Ogg and MP3 vectors.
- `ffprobe -show_streams` / `-show_packets` against the example's track list.
- Per-frame MD5 against libvpx's `vp90-2-*` `.md5` files and
  `ffmpeg -i in.webm -f framemd5 -`.
- `trace_headers` (`-bsf:v trace_headers`) field comparison for VP9.
- PCM byte comparison: `ffmpeg -i in.ogg -f s16le -` versus
  `./build/decode in.ogg --pcm out.s16le`; `opus_compare` on the same output.
- MP3 `ffmpeg -f s16le` difference at the native rate, including gapless
  behaviour.
- A frame-by-frame I420 difference against FFmpeg for VP9.
- Sequential versus `--threads 2/4` output equality and any fps figure.

## Pending review notes

1. The example trusts the container's `Track.width`/`height` for the VP9 work
   and pixel sizes. A stream whose coded frame is larger is handled by the
   `I_NOMEM` growth path, but a stream that declares no dimensions is refused
   instead of parsing the first key-frame header itself.
2. The one-block PCM delay buffer is a fixed 23,040 bytes. The current Opus and
   MP3 paths cannot exceed it; a future multichannel decoder would need either
   a larger buffer or a real streaming trim.
3. MD5 output for multiple video tracks is unlabelled and in decode order.
   Labelling it would improve diagnostics but would break the bare-hash diff
   this example is written for.
4. `sink_pending_rate` is recorded but not used; the format check compares the
   live block's rate and channels against the sink's open values.
5. The example does not print timestamps or durations. It could, from
   `Output.pts`/`duration` and the track timebase, without changing the
   payload streams.
6. A VP9 `I_NOMEM` after a superframe has already returned some frames makes
   the caller resend the packet. The duplicate hashes are dropped, but the
   decoder re-runs the leading slices; the hub's own note says the reference
   state is tainted by an error, so this path is only expected on a
   resolution-changing key frame where the shortfall is found before the first
   displayed output.
