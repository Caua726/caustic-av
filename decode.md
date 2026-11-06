# Codec dispatch and frame output

Stage 10. `decode.cst` maps a `core/codec.Track` to the VP9, Opus or MP3
decoder already in the tree, arms one packet, and hands out 0..N video frames
or one audio block. It owns no file, no allocation and no output surface it did
not receive from the caller.

Status: **WRITTEN, NOT VERIFIED.** Nothing here has been compiled or run. The
MD5 / `ffprobe` / `trace_headers` / `opus_compare` / FFmpeg-difference / fps
checks are listed at the end as NOT RUN.

## Files

| File | Holds |
|---|---|
| `decode.cst` | `Decoder`, `Output`, `decode_open`, `decode_send`, `decode_receive`, the VP9 memory wrappers, pre-skip/end-trim hooks. |
| `video/vp9/decode.cst` | The VP9 frame driver (stages 4-6, merged). |
| `audio/opus/opus.cst` | The Opus decoder (stage 7, merged). |
| `audio/mp3/mp3.cst` | The MP3 stream parser (stage 8a, not merged). |
| `audio/mp3/mp3_decode.cst` | The MP3 Layer III decode path (stage 8b, not merged). |
| `core/frame.cst`, `core/samples.cst`, `core/codec.cst` | Output views and the `codec.Track` this layer consumes. |

## API

```text
decode_open(dec, track)                0 or a negative error
decode_open_codec(dec, codec, priv, n) 0 or a negative error
decode_send(dec, pkt)                  0 accepted, negative error
decode_receive(dec, out)               1 = output, 0 = drained, negative error

decode_opened / decode_codec / decode_kind / decode_rate / decode_channels
decode_pre_skip / decode_codec_delay
decode_set_pre_skip(dec, samples)      Opus only
decode_set_end_trim(dec, samples)      Opus only
decode_close(dec)

decode_vp9_work_size(w, h)
decode_vp9_frame_size(w, h)
decode_vp9_threads_work_size(w, h, workers)
decode_vp9_bind(dec, mem, cap)
decode_vp9_add_buffer(dec, mem, cap)
decode_vp9_free_buffer(dec)
decode_vp9_replace_buffer(dec, idx, mem, cap)
decode_vp9_work_need(dec) / decode_vp9_frame_need(dec)
decode_vp9_bind_threads(dec, mem, cap, workers)
decode_vp9_set_threads(dec, workers)
decode_vp9_threads_need(dec)
```

The README's `decode.open(d.tracks[i].codec, &dec)` is a shorthand: the
implementation opens from the whole `codec.Track`, because Opus cannot start
without its codec private bytes and the timing policy needs the track's
pre-skip, codec delay and timebase. `decode_open_codec(dec, codec, priv,
priv_len)` is the codec-only form for a caller that has raw metadata.

## Dispatch table

| `codec` | Decoder | Output |
|---|---|---|
| `CODEC_VP9` | `vp9_decoder_init/send/receive` | I420 view (`OUTPUT_VIDEO`) |
| `CODEC_OPUS` | `opus_decoder_init/decode` | interleaved S16, 48 kHz (`OUTPUT_AUDIO`) |
| `CODEC_MP3` | `mp3_decode_init`, `mp3_decode_frame`, with `mp3_header_parse`/`mp3_side_info_parse` | interleaved S16 at the frame rate (`OUTPUT_AUDIO`) |
| `CODEC_VP8`, `CODEC_AV1` | — | `I_UNSUPPORTED` |
| `CODEC_H264`, `CODEC_H265` | — | `I_UNSUPPORTED` (patent-pool formats, deliberately left out) |
| `CODEC_AAC` | — | `I_UNSUPPORTED` |
| `CODEC_VORBIS`, `CODEC_FLAC`, `CODEC_THEORA`, `CODEC_PCM` | — | `I_UNSUPPORTED` |
| `CODEC_UNKNOWN` or any other value | — | `I_UNSUPPORTED` |

A track of an unsupported codec is still published by `demux.cst` and can be
reported; only `decode_open` refuses it. VP9 profile rejection (profiles 1-3,
4:2:2/4:4:4, 10/12-bit, CS_RGB) happens inside `vp9_header_parse` and
`_decode_slice` and surfaces as `I_UNSUPPORTED`.

## Memory

The `Decoder` is a value the caller owns and is deliberately large:

- `vp9.Vp9Decoder` — roughly 250 KiB (four probability contexts, counts,
  adaptation working set, reference metadata). `vp9.md` already requires
  caller storage for it.
- `opus.OpusDecoder` — SILK and CELT state plus their fixed frames.
- `mp3dec.Mp3Decode` — parser, 4096-byte main-data assembly, 512-byte
  reservoir, spectral/subband scratch and synthesis history.
- `pcm[11520]` — 120 ms of stereo at 48 kHz, the longest Opus packet, written
  only by the audio paths.

Nothing in `decode.cst` allocates. VP9 additionally needs three caller blocks,
which this module forwards to `video/vp9/decode.cst`:

1. the persistent work block (`decode_vp9_work_size` → `decode_vp9_bind`);
2. up to nine I420 pixel buffers (`decode_vp9_frame_size` →
   `decode_vp9_add_buffer`, replaced with `decode_vp9_replace_buffer`);
3. optionally, the stage-9 worker block (`decode_vp9_threads_work_size` →
   `decode_vp9_bind_threads`).

After a VP9 `I_NOMEM`, `decode_vp9_work_need`, `decode_vp9_frame_need` or
`decode_vp9_threads_need` says how large the rejected block had to be; the
caller re-binds between packets and decodes the frame again. This is the same
contract `video/vp9/decode.cst` documents, only routed through the hub.

Audit inputs: every packet length is checked before parsing, every codec call
validates its own slices, and no size from the file is used as an allocation
size.

## Packet handoff and output lifetime

`decode_send` arms one packet. `decode_receive` consumes it:

- **VP9.** `vp9_decoder_send` validates the Annex B superframe (1..8 coded
  frames) and stores slice pointers; `vp9_decoder_receive` decodes until one
  frame is displayed and returns it. A hidden frame produces no output; a
  packet of only hidden frames returns 0. `send` refuses a new packet while
  the previous one is undrained. The output view points into a pool buffer and
  is valid until the next receive or send; if the frame was also published as
  a reference, its pixels stay valid while a slot names that buffer.
- **Opus.** `decode_send` stores the packet and `decode_receive` decodes it
  once. A second packet while one is pending is `I_BADPARAM`. Receive returns
  0 when pre-skip/end-trim consumes the whole packet, or when there was no
  pending packet.
- **MP3.** `decode_send` stores the packet; `decode_receive` parses and
  decodes one Layer III frame per call and advances a byte cursor inside the
  packet. A raw demuxer packet holds one frame; a container block may hold
  several, and all of them are returned before the packet is finished. A
  second packet while one is pending is `I_BADPARAM`; receive returns 0 when
  the packet's bytes are exhausted or the last frame is trimmed away.

One packet therefore yields 0..8 outputs for VP9, 0..1 for Opus and 0..N for
MP3, never a placeholder. The packet bytes stay the caller's: VP9 and MP3 hold
their addresses until the packet is drained; Opus uses them inside the receive
call. Nothing is copied except decoded PCM, which lands in the `Decoder`'s
scratch and is valid until the next receive.

## Output record

```
struct Output {
    kind;                 OUTPUT_VIDEO or OUTPUT_AUDIO (NONE before a value)
    video;                frame.Frame: I420 planes, visible/coded sizes, strides
    audio;                samples.Samples: S16 interleaved, rate, channels, count
    render_width/height;  VP9 7.2.4 render hint
    profile;              bit_depth; subsampling_x/y; color_space; color_range
    order;                VP9 presentation order hint
    pts; pts_valid; duration; timebase_num; timebase_den;
}
```

- **Video:** `video` is the `vp9.Vp9Output.frame` view. `pts_valid` is 1 only
  for the first displayed frame of a packet: the container carries one
  timestamp per packet, and a superframe's individual frames have no
  container timestamp. `order` rises across the stream and is the
  presentation hint. `duration` is 0 (the container gave no per-frame
  duration to this layer).
- **Audio:** `audio` points into `Decoder.pcm`; `count` is frames per channel;
  `pts` is the packet's timestamp; `pts_valid` is 1; `duration` is `count`
  converted from samples at the output rate to the track timebase.

`decode_receive` clears `out` before every attempt, so a zeroed view from a
0-return is not mistaken for a frame.

## Timestamps

Packet timestamps are in the track timebase (`core/packet.cst`). The demuxer
sets it:

- Matroska: 1/1e9, so packet and output `pts` are nanoseconds.
- Ogg Opus: 1/48000, so `pts` is the completing page's granule (RFC 7845
  counts 48 kHz samples through the last packet on the page). Earlier packets
  on the same page share that granule; exact per-packet starts would require
  reading each Opus TOC, which this layer does not do. The decoder passes the
  container value through unchanged.
- Raw MP3: 1/sample_rate, sample ticks; `pts` advances by
  `samples_per_frame` per packet.
- An Ogg stream of a codec whose granule unit is not parsed: denominator 0.
  `duration` is then reported as 0 rather than converted into a made-up unit.
- A codec-only open (`decode_open_codec`) has no track timebase. Opus defaults
  to 1/48000; MP3 learns 1/sample_rate from its first frame. VP9 leaves it
  0/0.

For audio, `duration` is computed as
`round(count * timebase_den / (rate * timebase_num))`, with both products
range-checked before multiplication. For Matroska Opus, 120-sample frames at
48 kHz give exact 2.5 ms multiples; other counts can round by one nanosecond.

## Pre-skip, codec delay and discard padding

- **Pre-skip.** `opus_decoder_init` arms the `OpusHead.pre_skip` value and
  drops exactly that many decoded 48 kHz samples at the start; the demuxer
  also publishes the same value in `Track.pre_skip`. `decode_open` re-arms it
  from the track when it is greater than zero, so both paths agree. Zero in
  the track means "keep the OpusHead value"; a caller that wants an explicit
  zero calls `decode_set_pre_skip(dec, 0)` before the first packet.
- **Codec delay.** Matroska's `CodecDelay` is stored in the `Decoder` and
  reported by `decode_codec_delay`, but it is not applied a second time.
  `container/matroska.cst` reports block timestamps unshifted and explains
  that the decoder should apply the pre-roll once; for Opus that pre-roll is
  the pre-skip. A caller that wants FFmpeg-style negative packet timestamps
  subtracts `codec_delay` once outside this module. A stream whose
  CodecDelay exceeds its Opus pre-skip therefore keeps that extra delay
  uncompensated; see UNCONFIRMED.
- **Discard padding.** `Packet.discard_padding` is nanoseconds at the end of
  the block (`core/packet.cst`).
  - Opus: converted to 48 kHz samples (`round(ns * 48000 / 1e9)`), added to
    whatever end trim the decoder still carries from the previous packet, and
    passed to `opus_decoder_set_end_trim`. The decoder's own cross-packet
    carry then applies. This is the WebM/Matroska path.
  - MP3: converted at the frame's sample rate and subtracted from the last
    Layer III frame of the packet. A value that covers that frame yields no
    output; a valid block padding never exceeds the block, and the excess is
    not carried into the next packet.
  - VP9: ignored, because end padding is not a video concept.
- **Ogg EOS trim.** Ogg has no DiscardPadding element. RFC 7845 puts the end
  trim in the final granule, which the caller can turn into a sample count and
  pass to `decode_set_end_trim` before the final packet; the next packet's
  DiscardPadding (if any) is added to it rather than replacing it.

`_ns_to_samples` splits the nanosecond value into whole seconds and a
remainder before multiplying, so no untrusted padding value can overflow the
conversion. The rounding choice (nearest, half up) is UNCONFIRMED.

## MP3 packet reconstruction

A raw demuxer packet is one complete MP3 frame. A Matroska block may legally
hold more than one Layer III frame, so the decoder consumes frames from the
packet one at a time. `decode_receive` parses the frame at the packet cursor:

1. `mp3_header_parse` validates the 4-byte header, derives frame length and
   side-information offsets, and rejects reserved/free-format/Layer I-II
   values.
2. The frame length must fit the remaining packet bytes (`I_EOF` otherwise),
   and the side information must fit both the frame and the packet.
3. `mp3_side_info_parse` validates and decodes the fixed Table 13 block.
4. A frame whose sample rate or channel count disagrees with the track opened
   with `decode_open` is `I_UNSUPPORTED`.
5. The `Mp3Frame` is reconstructed with offsets relative to the packet base:
   `main_data_offset = cursor + side_info_end`, `main_data_len = frame_len -
   side_info_end`, `reservoir_back = side.main_data_begin`.

The packet is bound as `mp3_dec.stream.src`/`.len` because `mp3_decode_frame`
reads main data through the parser state it embeds. The reservoir tail is kept
inside `Mp3Decode` across frames and packets, so a frame whose
`main_data_begin` reaches into earlier frames decodes as long as the frames
arrive in decode order from the demuxer. A seek that starts mid-stream with a
non-zero backpointer has no history and returns the decoder's `I_EOF`; the
demuxer always starts at frame 0, so the normal path is unaffected.

After a frame decodes, the cursor advances by its frame length and the
timestamp offset advances by `samples_per_frame` converted to the track
timebase. The next receive decodes the next frame, if any. A frame that
returns `I_CORRUPT` abandons the rest of the packet; the caller may call the
MP3 module's reset or re-open. DiscardPadding is applied only when the cursor
reaches the end of the packet, because it describes padding at the end of the
block, not of every frame.

`mp3_decode_frame` writes little-endian S16 into the `Decoder` scratch and
returns frames per channel (1152 for MPEG-1, 576 for MPEG-2/2.5). The hub
checks the returned count against the scratch before building the
`samples.Samples` view.

MP3 output rate/channels are the frame's header values. A codec-only open
fills `Decoder.rate/channels` from the first decoded frame; a track-based open
already has them and the per-frame parse rejects a later frame that differs.

## VP9 caller-memory wrappers

The wrappers only check that the decoder is open for VP9 and forward:

```
decode_vp9_bind            -> vp9_decoder_bind
decode_vp9_add_buffer      -> vp9_decoder_add_buffer
decode_vp9_free_buffer     -> vp9_decoder_free_buffer
decode_vp9_replace_buffer  -> vp9_decoder_replace_buffer
decode_vp9_bind_threads    -> vp9_decoder_bind_threads   (stage 9, PENDING)
decode_vp9_set_threads     -> vp9_decoder_set_threads    (stage 9, PENDING)
decode_vp9_threads_work_size -> vp9_threads_work_size    (stage 9, PENDING)
decode_vp9_threads_need    -> Vp9Threads.need            (stage 9, PENDING)
```

The sizes (`work_size`, `frame_size`, `threads_work_size`) are static and do
not require an open decoder. The rest are `I_BADPARAM` unless the object is
open for `CODEC_VP9`.

Tile threads are optional; the default is the merged sequential path. A
caller enables them with a worker block sized for the largest frame and a
worker count, between packets. The worker block holds per-worker contexts and
the pool spawns/joins inside each frame; output is meant to agree with the
sequential path, subject to the stage 9 note about above-contexts across tile
rows. Nothing in this hub forces threads on.

## Errors

`decode_open` returns `I_BADPARAM` for a null decoder/track, a negative private
length, or a null private pointer with bytes; `I_UNSUPPORTED` for every codec
without a decoder; and the Opus parser's or codec initialiser's negative code
otherwise. On failure the `Decoder` is cleared and `opened` stays 0, so send
and receive return `I_BADPARAM`.

`decode_send` returns `I_BADPARAM` for a null/unopened decoder or packet, a
negative length, a null data pointer with bytes, a negative discard padding,
or a packet already pending. Codec errors are passed through.

`decode_receive` returns `I_BADPARAM` for a null/unopened object,
`I_UNSUPPORTED` only if the object's codec somehow changed, and the codec's
error otherwise. An audio decode error abandons the pending packet; the caller
may re-open or use the codec module's reset. A VP9 error abandons the rest of
the packet and taints the reference state until the next key/intra-only frame,
as `video/vp9/vp9.md` records.

## Integration points and assumptions

- **MP3 (stage 8a/8b, not merged).** The hub imports
  `audio/mp3/mp3.cst` and `audio/mp3/mp3_decode.cst` and uses:
  `mp3_header_parse`, `mp3_side_info_parse`, `Mp3Header.frame_len`,
  `side_info_offset`, `side_info_len`, `side_info_end`, `sample_rate`,
  `channels`, `samples_per_frame`, `version`; `Mp3SideInfo.main_data_begin`;
  `Mp3Frame` and its fields; `Mp3Decode.stream.src/len`, `mp3_decode_init`,
  `mp3_decode_frame(m, frame, pcm, cap_bytes)`. This is exactly the API in
  stage 8a's and 8b's notes. If the merge renames anything, these call sites
  are the adaptation point. Until both branches merge, the hub branch does not
  compile.
- **VP9 (stages 4-6 merged; 9 not merged).** The base `vp9_decoder_*`,
  `vp9_work_size`, `vp9_frame_size` and `Vp9Output` are on `main`. The thread
  wrappers and `Vp9Threads.need` are written against the documented stage 9
  API (`vp9_decoder_bind_threads`, `vp9_decoder_set_threads`,
  `vp9_threads_work_size`, `Vp9Threads.need`); they are PENDING until that
  branch merges. The SIMD layer changes no public function signature and needs
  no hub call.
- **Opus (stage 7 merged).** Uses `opus_decoder_init`, `opus_decode`,
  `opus_decoder_rate/channels/pre_skip/set_pre_skip`,
  `opus_decoder_end_trim/set_end_trim`, and `opus.OpusDecoder` by value.
- **Track vocabulary.** `decode_open` takes `core/codec.Track`, which
  `demux.cst` fills. This avoids a dependency on the demuxer object while
  still delivering codec private, pre-skip, codec delay and timebase.

## References read

Read only to understand behaviour; no code was copied, ported or transcribed:

- FFmpeg `libavcodec/decode.c` (send/receive, drain, packet props) — generic
  framing, not a codec.
- FFmpeg `libavformat/matroskadec.c`, `oggdec.c`, `mp3dec.c` — the container
  contracts that feed this layer.
- VP9 specification Annex B (superframe syntax) and Sections 8.9/8.10 through
  `.work/vp9/spec/clean.txt` and the repository's `video/vp9/vp9.md`.
- RFC 6716 / RFC 7845 (Opus frame durations, pre-skip, granule) through
  `.work/opus/spec/` and the repository's `audio/opus/opus.md`.
- The stage 8a/8b MP3 notes on the `stage8a-mp3` / `stage8b-mp3` branches.

## UNCONFIRMED

- **Ogg Opus packet start.** The packet `pts` is the completing page's
  granule, not the packet's own start. Output `pts` is passed through, so a
  page with several packets gives them all the same timestamp. Exact
  per-packet starts need the TOC duration walk, which this layer does not
  perform.
- **nanoseconds to samples rounding.** `_ns_to_samples` rounds to nearest,
  half up. A DiscardPadding written from a non-integral sample duration could
  round differently from the muxer's intent; no comparison was run.
- **CodecDelay policy.** The decoder does not shift timestamps by CodecDelay,
  on the theory that Opus pre-skip already covers it. A file where the two
  disagree keeps the difference; this is unverified against ffmpeg's
  packet-time output.
- **MP3 gapless trimming.** The stage 8b note assigns LAME/VBRI delay and
  padding trimming to the decode path, but the branch read does not implement
  it. The hub applies only Matroska DiscardPadding and does not read the
  parser's `encoder_delay`/`encoder_padding`. PENDING.
- **MP3 frame independence.** `keyframe` in the demuxer is 0 when a frame's
  main data reaches into the reservoir. The decoder still needs the preceding
  frames; a caller that treats every packet as seekable would be wrong.
- **Multi-frame MP3 timestamps.** A container block with several Layer III
  frames gets each frame's pts by adding the previous frames' durations in
  the track timebase. For Matroska that conversion is rounded to whole
  nanoseconds, so the second and later frames of a block can differ by a
  nanosecond from the exact product. Each new packet resets to the container
  pts, so the error does not accumulate across blocks.
- **VP9 superframe timestamps.** Only the first output of a packet carries the
  container pts. A caller that needs per-frame times must derive them from
  `order` and the stream's frame rate, which this layer does not know.
- **Threaded VP9 output equality.** The stage 9 note qualifies equality across
  tile rows with the above-context reading; the hub inherits that caveat.
- **PCM scratch size.** 11520 values covers 120 ms stereo at 48 kHz, the
  Opus packet maximum. A future multichannel Opus mapping (family 1/255) is
  already refused by `opus_head_parse`.

## Pending review notes

1. `decode_send` accepts a zero-length Opus packet (with a null data pointer)
   and lets `opus_decode` reject it at receive time. Failing earlier would be
   possible with `opus_packet_parse`, but that duplicates the decoder's own
   validation.
2. `decode_open` does not store the track's width/height for VP9. The caller
   sizes the work and pixel blocks from the track it already has, or from the
   stream's key-frame header; the hub does not need them.
3. `decode_codec_delay` is informational. A future API could return a
   shifted `pts` and let the caller choose, instead of documenting the
   subtraction outside the module.
4. `_receive_mp3` drops the packet's last frame when `DiscardPadding` covers
   it instead of carrying the excess. Valid files do not hit this; the
   alternative would match the Opus decoder's carry semantics.
5. `decode_set_end_trim` has no upper bound beyond the Opus decoder's
   non-negative check; a caller can ask to trim more than the stream holds,
   which simply drops everything.

## Checks NOT RUN

Nothing was compiled or executed. The original criteria would require:

- `ffprobe -show_streams` / `-show_packets` on the committed WebM and Ogg
  vectors, and on generated MP3 vectors, compared with `demux.cst` +
  `decode.cst`.
- MD5 or PCM-difference against `ffmpeg -f s16le` for Opus and MP3, and an
  I420 plane comparison against FFmpeg for VP9.
- `trace_headers`/bitstream-field comparison for VP9.
- `opus_compare` for the Opus output.
- An fps/throughput measurement, sequential and threaded.
- `tests/run.cst` (`caustic-mk run test`) — not extended, not run.
