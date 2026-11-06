# Opus: Ogg framing, identification and the range decoder

Stage 7a of the Opus decoder. It reads the `OpusHead` and `OpusTags` header
packets, parses the Opus packet framing down to individual frame slices, and
implements the range decoder of RFC 6716 Section 4.1. It also brings the Ogg
container up far enough to find Opus streams and reassemble their packets.

It does **not** synthesise audio. `opus_decode` validates a packet and returns
`I_UNSUPPORTED`; there is no silence, no zero fill and no placeholder
synthesis anywhere. SILK, CELT and hybrid decoding land in later slices.

## Files

| File | Holds |
|---|---|
| `container/ogg.cst` | Page parser (CRC, lacing, flags), packet reassembly across pages, logical streams by serial, codec identification, `ogg_open` / `ogg_next`. |
| `audio/opus/opus_head.cst` | `OpusHead` (RFC 7845 Section 5.1) and the fixed prefix of `OpusTags` (Section 5.2). |
| `audio/opus/opus_packet.cst` | TOC byte, frame packing codes 0-3, VBR/CBR lengths, padding, per-frame duration. Also the configuration/bandwidth/mode helpers. |
| `audio/opus/range.cst` | The range decoder of RFC 6716 Section 4.1, including raw bits and `ec_tell`/`ec_tell_frac`. |
| `audio/opus/opus.cst` | Decoder state (`OpusDecoder`) and `opus_decoder_init` / `opus_decode`; audio is `I_UNSUPPORTED` for now. |

No tables are generated and no blobs are pending: the only table-like
structure is the configuration-to-duration mapping, which is arithmetic
(Table 2 of RFC 6716 Section 3.1), and `ec_tell_frac` uses the specification's
three squaring steps rather than the reference implementation's correction
table.

## Ogg pages and packets

The page format is RFC 3533 Section 6. All multi-byte integers are little
endian. A page is 27 header bytes plus N segment-table bytes plus the sum of
the lacing values, so at most 65307 bytes. `ogg_page_parse` validates, in
order: the four-byte capture pattern, the 27-byte minimum, version 0, reserved
header-type bits, the segment table against the source end, the body against
the source end, and finally the CRC. Only then does it publish any field.

The CRC is the non-reflected 0x04C11DB7 polynomial with an initial value of 0
and no final inversion, computed over the whole page with the four checksum
bytes treated as zero. It is computed bit by bit, so no 1 KB table is needed.
A mismatch is `I_CORRUPT` and the page publishes nothing.

Packet reassembly follows RFC 3533 Section 5: a lacing value of 255 says the
packet continues, 0..254 ends it. A packet of exactly 255 bytes ends with a
lacing value of 0, and a nil packet is a single 0. Because pages of different
logical streams interleave, a packet that spans pages is remembered as up to
32 `(offset, length)` fragments into the caller's source, not copied. When the
packet completes, the fragments plus the final page range are copied once into
`Ogg.pktbuf` and handed out as one contiguous view. A packet that fits in one
page is returned directly from the source, without a copy.

Bounds are fixed at compile time, so an untrusted lacing value cannot drive an
allocation: 16 tracks, 32 fragments per packet, 65536 bytes per assembled
packet. A packet that needs more is `I_UNSUPPORTED`, never truncated. The
returned view is valid until the next `ogg_next` call.

Continuation and loss rules (RFC 7845 Section 3):

- A page with the continued flag resumes the track's pending packet only when
  that packet is pending *and* the page sequence number is consecutive.
- If the continued flag is set but no packet is pending, the page's first
  packet is missing its start: its segments are discarded up to the first
  lacing value below 255 and decoding resumes with the next packet.
- If the continued flag is clear but a packet is pending, that packet can
  never complete: it is dropped.
- Sequence numbers are tracked per stream. A gap applies the same two rules.
- No page may follow a stream's EOS page; that is `I_CORRUPT`.

Streams may be grouped (interleaved) and chained (concatenated). A serial
selects a track; a serial is unique within a physical bitstream, so a repeat
is `I_CORRUPT`. A page for an unknown serial must be BOS, and the leading BOS
pages are registered by `ogg_open` so the track list is usable before the
first `ogg_next`.

`ogg_open` requires each BOS page to carry its initial header packet complete
on that page. RFC 3533 Section 5 says a physical bitstream begins with "the
bos pages of all logical bitstreams containing one initial header packet per
page"; a continued BOS packet would also make the header non-contiguous. This
is the strict reading of that sentence; it is listed under UNCONFIRMED below.

### Codec identification and header packets

The first packet of a stream is its BOS header and is consumed, not handed
out. It is identified by its first bytes:

| Leading bytes | Codec | Header packets consumed |
|---|---|---|
| `OpusHead` | Opus | 2 (OpusHead + OpusTags, RFC 7845 Section 5) |
| `0x01 "vorbis"` | Vorbis | 1 (the BOS packet) |
| `0x7F "FLAC"` | FLAC | 1 |
| `0x80 "theora"` | Theora | 1 |
| anything else | unknown | 1 |

The track exposes `head` / `head_len`, a borrowed view of the complete BOS
packet, and `codec`. For Opus it also carries `tags` / `tags_len` — the
OpusTags packet, borrowed from the source when that packet was contiguous on
one page. A comment header that spans pages is assembled in the reassembly
buffer and is not kept: the track reports `tags_len == 0` for it, because
retaining it would need a copy whose lifetime the current struct does not
provide. It also carries `serial`, `granule`, `eos`, `packets`
(data packets handed out) and `packet_index` (every packet seen, headers
included). The secondary header counts of the non-Opus mappings are mapping
specific and are not implemented here: their comment/setup packets are
delivered as packets. For Opus the count is exact because RFC 7845 states
there are exactly two mandatory headers. `ogg_track_index(o, serial)` maps a
serial back to its 0-based track index.

### Timestamps

`packet.pts` and `packet.dts` are the granule position of the page the packet
completes on, as this stage's contract specifies. RFC 7845 Section 4 defines a
page's granule as the total 48 kHz samples through the last packet completed
on it, so when a page holds several packets only the last one has its exact
position; the others share the page value. Granule `-1` (no packet completes)
is passed through unchanged. `packet.duration` is 0 because Ogg carries no
per-packet duration and this module does not look inside Opus packets. Exact
per-packet sample positions come from TOC durations and are the audio layer's
job.

## Opus timelines and pre-skip

Granule positions count samples at 48 kHz per channel, regardless of the
decoder's output rate (RFC 7845 Section 4). `OpusHead.pre_skip` is a count of
48 kHz samples produced by the decoder that must be discarded at the start:
they are the encoder's latency compensation and are not valid audio, but they
must still be *decoded*, because the decoder is predictive (Section 4.2).

The PCM sample position of a packet is

```
pcm_position = granule_position - pre_skip
```

and playback time is `pcm_position / 48000.0`. The first sample to be played
marks position 1 on the clock (the position is after that sample), so a
stream exactly one second long ends at 48000. A page marked EOS may carry a
granule smaller than the decoded length; the difference is end trimming and
must be discarded (Section 4.4). Neither the container nor this stage applies
pre-skip or end trimming: `OpusDecoder.pre_skip` is reported, and the audio
slices will drop exactly that many leading samples and honour the Ogg EOS
granule.

The granule of the ID header page and of the page where the comment header
completes is 0 (Section 4); the demuxer reports it like any other.

## Packet framing

`opus_packet_parse` turns one Ogg packet into `OpusPacket`: configuration,
stereo flag, code, mode, bandwidth, per-frame duration in 48 kHz samples,
frame count, total duration, padding and up to 48 frames (pointer, length,
duration).

- TOC: `config = toc >> 3`, `s = (toc >> 2) & 1`, `c = toc & 3`.
- Durations: SILK 10/20/40/60 ms, Hybrid 10/20 ms, CELT 2.5/5/10/20 ms.
  In 48 kHz samples: 480/960/1920/2880, 480/960, 120/240/480/960.
- Code 0: one frame, everything after the TOC.
- Code 1: two equal frames; the payload after the TOC must be even, so the
  packet length must be odd.
- Code 2: the first length is coded in one or two bytes, the second frame
  takes the rest; the packet must have room for that length.
- Code 3: `v` (VBR), `p` (padding) and `M` (frame count) in the second byte;
  `M` is 1..48 and `M * frame_duration <= 5760` samples (120 ms). Padding is
  a chain of bytes where 255 means 254 more plus another byte; the size bytes
  plus the padding bytes may not exceed `N - 2`. VBR reads `M-1` lengths and
  gives the last frame whatever remains before the padding; CBR divides the
  remainder equally and requires it to be a multiple of `M`.
- Lengths: 0..251 in one byte, 252..255 means `second*4 + first`; the maximum
  is 1275. Every explicit and implicit frame length is checked against 1275.
- A zero-length frame is legal (DTX or a dropped frame).

All frame pointers stay inside the packet; padding is never part of a frame.

## The range decoder

`audio/opus/range.cst` is a direct implementation of RFC 6716 Section 4.1.
State is `(val, rng)` plus the forward cursor (`offs`, `rem`), the raw-bit
cursor (`end_offs`, `end_window`, `nend_bits`) and `nbits_total`. All cells
are i64 holding unsigned 32-bit values, so signed arithmetic is exact.

Contract for the SILK and CELT slices:

```
struct OpusRange
opus_range_init(r, ptr, len)          -> 0 / negative
opus_range_decode(r, ft)              -> fs in 0..ft-1
opus_range_decode_bin(r, ftb)         -> fs in 0..(1<<ftb)-1
opus_range_update(r, fl, fh, ft)      -> 0 / negative
opus_range_bit_logp(r, logp)          -> 0 or 1
opus_range_icdf(r, icdf, ftb)         -> symbol index
opus_range_uint(r, ft)                -> 0..ft-1
opus_range_bits(r, n)                 -> n raw bits, LSB first
opus_range_tell(r)                    -> whole bits used
opus_range_tell_frac(r)               -> bits used in 1/8ths
```

`decode` and `decode_bin` leave the division factor in `ext`; the caller must
follow with `update` before the next symbol. `bit_logp`, `icdf` and `uint`
perform the whole symbol themselves. `uint` with more than 8 significant bits
takes the high part from the range coder and the rest from raw bits. A uint
result past `ft-1` is `I_CORRUPT` rather than saturated.

Bounds and error behaviour:

- `ft` is 1..65535 for `decode`/`update`, `ftb` is 0..15 for `decode_bin`,
  `logp` is 1..31, `ftb` is 1..8 for `icdf`, `ft` is 1..2^32-1 for `uint`,
  and `n` is 0..31 for raw bits. Anything else is `I_BADPARAM`.
- Reading past either end of the slice supplies zero bytes instead of
  dereferencing, as Section 4.1.2.1 requires; this is not an error. The range
  coder is expected to read into the raw-bit area near the end of a frame.
- `update` rejects `fl >= fh` or `fh > ft`, an `ext` of 0, a `val` that
  cannot absorb the update, and a resulting `rng <= 0`, all as `I_CORRUPT`.
- `icdf` gives up after 256 entries (the most an 8-bit table can hold) and
  rejects a table that leaves an empty range.
- A zero-length slice is valid: it initialises `b0 = 0` and decodes from
  zeros, which a DTX frame relies on.

`ec_tell` is `nbits_total - ilog(rng)`; `ec_tell_frac` follows the three
squaring steps of Section 4.1.6.2. A fresh decoder reports 1 bit used, the
terminator the encoder reserves. Both must match the encoder exactly, so the
implementation avoids the reference implementation's correction-table
optimisation and keeps the specification's operations.

## What the synthesis slices add

- `opus_decode` will initialise one `OpusRange` per frame (each frame is an
  independent range-coded unit) and select SILK, CELT or both from
  `OpusPacket.mode`.
- SILK runs at 8/12/16 kHz internally, is resampled to 48 kHz, and is summed
  with CELT for hybrid packets. The configuration/bandwidth helpers in
  `opus_packet.cst` give the exact path.
- `OpusDecoder` keeps the caller-visible state stable; SILK and CELT history
  (predictor memory, overlap-add state) will hang off it.
- Pre-skip and end trimming are applied to the output at 48 kHz, not by the
  demuxer.
- Output gain (`OpusHead.output_gain`, Q7.8 dB) is applied by the player or by
  the decoder on request; the raw value is preserved in `OpusDecoder.head`.

## UNCONFIRMED

- **BOS header completes on its BOS page.** Read from RFC 3533 Section 5
  ("bos pages ... containing one initial header packet per page"). The
  implementation rejects a BOS page whose last lacing value is 255. A
  different reading would allow a continued first header packet.
- **Version 0 accepted for OpusHead.** RFC 7845 Section 5.1 says the version
  MUST be 1 and that implementations SHOULD accept values up to 15; the task
  brief says 0..15. Version 0 is accepted here although the RFC defines no
  version 0. No reference behaviour was checked.
- **Reserved Ogg header-type bits.** Bits 3-7 of the header-type byte are
  rejected as `I_CORRUPT`. The RFC does not assign them; a future extension
  could use them, but the version byte would change first.
- **Raw-bit prefetch.** The reference decoder fills its raw-bit window to at
  least 25 bits before every read; this implementation fetches only until the
  requested count is available. The returned values are identical (the extra
  bytes would only be prefetched), but the exact `end_offs` timing differs.
  Not verified against a bitstream.
- **`ec_dec_icdf` with ftb up to 8.** The reference has a second 16-bit-entry
  variant (`ec_dec_icdf16`) used by some CELT paths; it is not implemented
  here. The task contract names the 8-bit one only.
- **Granule -1 on a page with completed packets.** The demuxer passes `-1`
  through as the packet timestamp instead of rejecting the page. RFC 3533
  defines -1 as "no packets finish on this page", so such a page is
  inconsistent; the choice is leniency, not a spec reading.

## Pending review notes

- `packet.pts` is the completing page's granule for every packet on the page
  (the stage contract), so packets before the last one on a multi-packet page
  get a later position than their true start. The audio layer can correct it
  from TOC durations; the demuxer does not.
- Secondary header counts for Vorbis (3), Theora (3) and FLAC (mapping
  defined) are not consumed; their headers are delivered as packets. Only
  Opus's two are exact.
- Opus header placement rules from RFC 7845 ("ID header alone on the first
  page", "comment header finishes the page it completes on") are assumed, not
  enforced.
- `Ogg` is a large caller-owned struct (about 76 KB, dominated by
  `pktbuf`). A future interface may take a caller-provided assembly buffer
  instead.
- `opus_decode` validates `cap`/`out` even though it writes nothing; the
  output sink is `*i16` interleaved at 48 kHz and may move to `core/samples`
  (which already tags S16/S32/F32) when the synthesis path lands.
- A track's `tags` view exists only when the OpusTags packet was contiguous
  within one page; a comment header that spans pages reports
  `tags_len == 0`. Retaining it would need a copy with a defined owner.

## References read

Specifications in `.work/opus/spec/`:

- `rfc3533.txt` — Ogg encapsulation (Sections 4-6).
- `rfc7845.txt` — Ogg Opus (Sections 3, 4, 5.1, 5.2).
- `rfc6716.txt` — Opus (Sections 3.1-3.4, 4.1.1-4.1.6).

Reference implementations read from raw URLs, for behaviour only; nothing was
copied, ported or transcribed. Every URL below prefixed with
`https://raw.githubusercontent.com/`:

- `xiph/opus/main/src/opus_decoder.c` — decoder state, 48 kHz output and
  pre-skip handling.
- `xiph/opus/main/celt/entdec.c` — renormalisation, `ec_decode`, `update`,
  `bit_logp`, `icdf`, `uint`, `bits`.
- `xiph/opus/main/celt/entcode.h` — `(val, rng)`, `EC_CODE_EXTRA`,
  `nbits_total = 9` before normalisation, `ec_tell`.
- `xiph/opus/main/celt/entcode.c` — the `ec_tell_frac` squaring procedure.
- `FFmpeg/FFmpeg/master/libavformat/oggdec.c` — page walk, CRC seeding,
  per-stream continuation and sequence handling.
- `FFmpeg/FFmpeg/master/libavformat/oggparseopus.c` — Opus header, granule
  and pre-skip handling, TOC duration.
- `FFmpeg/FFmpeg/master/libavcodec/opus/dec.c` — range-decoder setup per
  frame and the SILK/CELT split.

## Status

WRITTEN, NOT VERIFIED. Nothing in this stage has been compiled, built or run.
No test was added or changed, and no table generator was written. The checks
the original criteria would require (`opus_compare` against the RFC 6716
vectors, `ffprobe` track/timing comparison, byte-for-byte Ogg packet
comparison) have not run.
