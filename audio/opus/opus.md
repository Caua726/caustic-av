# Opus: packets, SILK/CELT and the 48 kHz decoder

The complete decode path for Ogg Opus and WebM Opus. One Opus packet enters
as bytes, its TOC and framing are parsed, each frame is decoded by SILK,
CELT or both, and the result leaves as interleaved signed 16-bit PCM at
48 kHz.

The container layer is unchanged: `container/ogg.cst` hands out Opus packet
bytes (and the `OpusHead` from the BOS page), and `container/matroska.cst`
hands out WebM packets with `CodecPrivate` as the `OpusHead`. `opus_decode`
takes the packet bytes directly; the library performs no file I/O and the
decoder does no allocation.

Nothing in this module has been compiled, built or run. This document
records what was written, not a verified result.

## Files

| File | Holds |
|---|---|
| `container/ogg.cst` | Page parser (CRC, lacing, flags), packet reassembly, logical streams, codec identification, `ogg_open` / `ogg_next`. |
| `audio/opus/opus_head.cst` | `OpusHead` (RFC 7845 Section 5.1) and the fixed prefix of `OpusTags` (Section 5.2). |
| `audio/opus/opus_packet.cst` | TOC byte, frame packing codes 0-3, VBR/CBR lengths, padding, per-frame duration, configuration/bandwidth/mode helpers. |
| `audio/opus/range.cst` | The range decoder of RFC 6716 Section 4.1, including raw bits and `ec_tell`/`ec_tell_frac`. |
| `audio/opus/silk.cst` | SILK header bits, per-frame parameters, excitation, synthesis, stereo unmixing and resampling to 48 kHz. |
| `audio/opus/silk_bits.cst` | `silk_sym`, `silk_uniform`, `silk_bit`: the three range-coder reads SILK uses. |
| `audio/opus/silk_core.cst` | SILK excitation and LPC/LTP synthesis. |
| `audio/opus/silk_nlsf.cst` | NLSF reconstruction, stabilization, LPC conversion and range limiting. |
| `audio/opus/silk_resample.cst` | The non-normative SILK resampler (8/12/16 kHz to 8/12/16/24/48 kHz). |
| `audio/opus/silk_tables*.cst` | SILK constants, PDFs and data blobs. |
| `audio/opus/celt.cst` | CELT frame flow and synthesis, plus the two decode entry points. |
| `audio/opus/celt_state.cst` | Fixed-size `CeltDec` state. |
| `audio/opus/celt_energy.cst`, `celt_alloc.cst`, `celt_pvq.cst`, `celt_mdct.cst` | Energy, allocation, PVQ shapes and the inverse MDCT/post-filter. |
| `audio/opus/celt_tables*.cst` | CELT tables, built from the RFC formulas and the committed data blobs. |
| `audio/opus/opus.cst` | `OpusDecoder`, `opus_decoder_init`, `opus_decode`, and the pre-skip, end-trim and gain hooks. |

## Decode call graph

```
opus_decode(d, pkt, len, out, cap, got)
  opus_packet_parse             TOC, mode, bandwidth, frames, durations
  for each frame:
    if mode == SILK-only:
      opus_range_init(frame)    one range decoder per frame
      silk_decode               header bits, LBRR skip, parameters,
                                excitation, synthesis, stereo unmix,
                                resample to 48 kHz (planar f64)
      _opus_skip_redundancy    read the 4.5.1 header, shrink r.len
    else if mode == HYBRID:
      opus_range_init(frame)
      silk_decode               SILK part, always WB (16 kHz) internally
      _opus_skip_redundancy     flag + size, shrink r.len past the tail
      celt_set_frame(17, end, lm, stream_channels)
      celt_decode_with_range    continues on the same range decoder
      mix                       SILK low band + CELT bands 17..end
    else:                       CELT-only
      celt_set_frame(0, end, lm, stream_channels)
      celt_decode               binds its own range decoder over the frame
  pre-skip / end trim           output filter, after decoding
  gain + f64 -> i16             one interleaved conversion
```

`opus_decode` validates the whole packet before decoding its first frame:
mode-specific frame size, bandwidth, every frame length, and the output
capacity. Those failures write nothing and leave SILK/CELT state untouched.
A range-coder failure after decoding has begun returns the error and leaves
`*got` untouched; `out` may contain part of the packet and the caller must
discard it.

## Modes, frame sizes and CELT bands

The TOC configuration selects the mode (RFC 6716 Section 3.1, Table 2):

| Config | Mode | Bandwidth | Durations |
|---|---|---|---|
| 0-3 | SILK-only | NB | 10 / 20 / 40 / 60 ms |
| 4-7 | SILK-only | MB | 10 / 20 / 40 / 60 ms |
| 8-11 | SILK-only | WB | 10 / 20 / 40 / 60 ms |
| 12-13 | Hybrid | SWB | 10 / 20 ms |
| 14-15 | Hybrid | FB | 10 / 20 ms |
| 16-19 | CELT-only | NB | 2.5 / 5 / 10 / 20 ms |
| 20-23 | CELT-only | WB | 2.5 / 5 / 10 / 20 ms |
| 24-27 | CELT-only | SWB | 2.5 / 5 / 10 / 20 ms |
| 28-31 | CELT-only | FB | 2.5 / 5 / 10 / 20 ms |

Durations become samples at 48 kHz: 120 / 240 / 480 / 960 / 1920 / 2880.
A SILK frame is one Opus frame duration; `silk_decode` internally splits
40 and 60 ms into two and three 20 ms SILK frames.

SILK runs at 8, 12 or 16 kHz (NB, MB, WB) and resamples to 48 kHz. Hybrid
always runs SILK at WB (16 kHz) regardless of the SWB/FB label; CELT covers
the band above 8 kHz.

The CELT end band comes from the audio bandwidth, per Table 55:

| Bandwidth | First uncoded band (`end`) | Last coded frequency |
|---|---|---|
| NB | 13 | 4.8 kHz |
| MB / WB | 17 | 8 kHz |
| SWB | 19 | 12 kHz |
| FB | 21 | 20 kHz |

CELT-only uses start band 0; Hybrid uses start band 17, so the low 17 bands
(0-8 kHz) are not coded in the CELT layer (Section 4.3).

## Hybrid combine

A hybrid frame has one range decoder for the whole frame. SILK reads the
VAD/LBRR flags, per-frame parameters and excitation from the front of that
range stream; CELT then continues from the same `(val, rng)` and reads raw
bits from the end of the same frame. `celt_decode_with_range` exists for
exactly this: `celt_decode` remains the standalone entry point that binds a
range decoder at byte 0, and the Opus layer passes the already advanced
decoder for hybrid frames.

Both layers produce 48 kHz output for the same interval and the same output
channel count, so combining is a sample-wise sum:

```
out[i] = silk[i] + celt[i]
```

SILK writes planar f64, CELT writes interleaved f64, and the decoder sums
them per channel in the interleaved scratch buffer before the single
conversion to i16. There is no extra upmix: both layers already duplicate a
mono coded frame into a stereo decoder, and a stereo coded frame in a mono
decoder is handled by each layer (SILK keeps the mid channel, CELT averages
the two spectra, both of which are the encoder's downmix).

The SILK resampler's delay and the CELT overlap-add delay are properties of
the two synthesis paths. RFC 6716 Section 4.2.9 makes the resampler method
non-normative and budgets the delay on the encoder side; the direct sum is
the reference decoder's behaviour. The exact sample alignment of this
resampler against libopus is UNCONFIRMED until the ffmpeg-difference check.

## Channel conversion and output format

`OpusHead.channels` fixes the decoder's output channel count at 1 or 2. The
TOC stereo flag fixes the coded channel count per frame and may change
between frames.

- Mono coded frame, stereo decoder: SILK duplicates the mono output into
  both planar channels; CELT duplicates its spectrum/output.
- Stereo coded frame, mono decoder: SILK decodes and keeps the mid channel
  (the encoder's downmix); CELT denormalises both channels and averages
  their spectra before the inverse MDCT.
- SILK bandwidth changes, mono/stereo coded transitions and side-channel
  resets are detected inside `silk_decode` (Sections 4.2.7.1 and 4.5.2).

Output is interleaved signed 16-bit, `channels` values per frame, at
48 kHz. `opus_decode` returns frames per channel and writes the same value
to `*got`. The f64 sample is scaled by 32768, rounded to nearest, and
saturated to [-32768, 32767]. A NaN from a corrupt frame becomes 0 instead
of an undefined cast; infinities saturate. No soft clipping is applied; the
reference's optional soft clip is not implemented (see UNCONFIRMED).

## Pre-skip, end trim and output gain

RFC 7845 Section 4.2: `OpusHead.pre_skip` counts 48 kHz samples that are
decoded but discarded at the start. They are never skipped before decoding,
because the codec is predictive. `opus_decoder_init` arms the count;
`opus_decode` decodes every frame and drops the first `pre_skip` output
frames, reporting fewer frames on the packets that overlap the skip. The
returned frame count is already net of the skip, so the Ogg layer must not
subtract `pre_skip` a second time when it assigns timestamps; it only uses
the value to compute PCM positions.

RFC 7845 Section 4.4: the EOS page may carry a granule smaller than the
decoded length. The Ogg layer computes the difference and calls
`opus_decoder_set_end_trim` before decoding the last packet; `opus_decode`
drops that many frames from the end of that packet. A count larger than the
packet's emitted frames continues into the following packet, so a trim that
crosses a packet boundary is honoured; a correct Ogg caller never needs
that, because the RFC says the discarded amount SHOULD fit in the last
packet.

Hooks:

```
opus_decoder_pre_skip(d)              // OpusHead value
opus_decoder_skip_remaining(d)        // still to drop
opus_decoder_set_pre_skip(d, n)       // re-arm, e.g. for cropping
opus_decoder_set_end_trim(d, n)       // for the next packet
opus_decoder_end_trim(d)
```

`opus_decoder_set_pre_skip` is a stream-start control. It updates the
effective `pre_skip` and `skip_remaining`; `head.pre_skip` keeps the value
parsed from the packet, so the header stays a faithful record and the
effective controls are the only mutable copy. Calling it after decoding has
started begins a new skip at the current output position; a decoder reused
for a new logical stream should call `opus_decoder_reset` or re-init
instead.

Output gain is the signed Q7.8 dB value from `OpusHead` (Section 5.1).
RFC 7845 leaves applying it to the player, so the decoder stores the raw
value and computes the linear factor `10^(gain/5120)` but leaves it off by
default. `opus_decoder_set_apply_gain(d, 1)` enables it; the factor is
applied to the summed output before the i16 conversion, with saturation.

## State lifetime and configuration switching

`OpusDecoder` owns `SilkDecoder` and `CeltDec` by value and all scratch
buffers are fixed-size arrays inside it. `opus_decoder_init` parses
`OpusHead`, initialises both codecs and sets the fixed output rate to
48 kHz. `opus_decoder_reset` clears both codecs, the mode history, the
pre-skip and the end trim for a new logical stream with the same header.
Destroying the decoder is the caller's job; the struct never allocates.

Reset rules implemented from RFC 6716 Section 4.5.2:

- SILK is reset when a SILK-only or Hybrid frame follows a CELT-only frame.
- CELT is reset whenever the operating mode changes and the new mode is
  Hybrid or CELT-only.
- A SILK bandwidth change resets SILK inside `silk_decode`.
- Hybrid to WB SILK keeps both states and adds the final CELT overlap to the
  first 2.5 ms of SILK output. This is done by decoding a 2.5 ms silence
  frame with CELT (`start = 0`, the SILK packet's end band) and summing its
  output into the SILK block, the same construction as the reference.
- Hybrid to NB/MB SILK resets SILK through the bandwidth change.
- Transitions that the reference handles with PLC (SILK to CELT without
  redundancy, CELT to SILK without redundancy, and similar non-normative
  cases) are not concealed here. CELT is reset as required, SILK is reset
  when the previous frame was CELT-only, and the discontinuity is left as
  is rather than filled with invented audio.

Redundancy (RFC 6716 Section 4.5.1) is parsed but not synthesised. After
the SILK part of a SILK-only or Hybrid frame, the decoder checks the same
bit budget as the reference: SILK-only assumes a redundancy header when at
least 17 bits remain, and Hybrid reads the 12-bit probability flag when at
least 37 bits remain. When redundancy is present, the decoder reads
`celt_to_silk` and the size, checks that the size fits in the whole bytes
remaining after the size symbol (`(8*len - tell) >> 3`; a larger Hybrid
size is `I_CORRUPT`), then shrinks the range slice so the main CELT frame's
raw bits stop before the redundant tail and its bit budget excludes it. The
shrink is safe at that point because no raw bit has been read yet
(`end_offs == 0`, `nend_bits == 0`): SILK uses no raw bits, and
`opus_range_uint(r, 256)` stays on the range-coded path. In `OpusRange`,
`len` bounds both the forward zero-fill cursor and the raw-bit reader, so
after the shrink every CELT budget and comparison uses `r.len * 8`, not the
original frame length; `tell`/`tell_frac` are unaffected. The
`celt_to_silk` flag is read so the header stays aligned but is not otherwise
used, and the redundant frame itself is skipped. The main audio is
therefore correct, but a stream that relies on redundancy for a clean
transition will have a discontinuity rather than the reference's cross-faded
redundant frame.

The Hybrid to WB SILK overlap flush is independent of this: the reference
skips that flush only in one redundancy combination, and this decoder has
no redundant frame to combine with, so it performs the flush on every
Hybrid to WB SILK transition.

## Packet-loss concealment, DTX and FEC

There is no PLC and no FEC output:

- A zero-length frame (DTX or a lost frame, Section 3.2.1) is refused with
  `I_UNSUPPORTED` before any frame is decoded. The decoder never fills the
  gap with silence, noise or a repeated frame. The check walks every entry
  in the parser's frame table, so a code-3 VBR packet with a zero-length
  last frame is also refused before the first frame advances the range
  state. A zero-octet *packet* is different: `opus_packet_parse` returns
  `I_CORRUPT` for it under [R1].
- `I_UNSUPPORTED` from a zero-length frame is a policy refusal, not a
  corruption signal: a valid transition stream that uses DTX will not
  decode at all with this decoder.
- `opus_decode_lost(d)` always returns `I_UNSUPPORTED`.
- `opus_decode_fec(...)` always returns `I_UNSUPPORTED`.
- LBRR (low-bitrate redundancy) frames inside a SILK payload are parsed so
  the range decoder advances, but they are not synthesised.

A caller that wants concealment must implement it outside this decoder. The
one place the spec's silence behaviour is used deliberately is the Hybrid
to WB SILK overlap flush described above: that silence frame is not a
concealment, it is the normative way to drain the CELT overlap buffer.

## Ogg and WebM packet paths

No file I/O is performed anywhere in the library. Both containers expose
borrowed packet bytes:

- **Ogg:** `ogg_open` / `ogg_next` in `container/ogg.cst` hand out the
  `OpusHead` BOS packet as the track's `head` and each audio packet as
  `OggPacket.ptr` / `OggPacket.len`. Granules are 48 kHz sample counts;
  pre-skip and EOS trimming are applied with the hooks above. A packet that
  spans pages is assembled in the container, never in the decoder.
- **WebM/Matroska:** `mk_open` / `mk_next` in `container/matroska.cst` hand
  out each block's bytes as `Packet.data` / `Packet.len`; the track's
  `codec_priv` is the `OpusHead` for an `A_OPUS` track. `mk_next` also
  exposes `DiscardPadding` in nanoseconds, which the caller converts to
  output samples for `opus_decoder_set_end_trim`. WebM's `CodecDelay` is a
  container-level value and is not applied here; the decoder applies only
  `OpusHead.pre_skip`, so a WebM caller must account for `CodecDelay`
  itself if it is non-zero.

The decoder receives raw packet bytes in both cases; nothing in this module
opens, seeks or reads a file.

## Packet framing (summary)

`opus_packet_parse` turns one packet into `OpusPacket`: configuration,
stereo flag, code, mode, bandwidth, per-frame duration in 48 kHz samples,
frame count, total duration, padding and up to 48 frames.

- TOC: `config = toc >> 3`, `s = (toc >> 2) & 1`, `c = toc & 3`.
- Code 0: one frame, the rest of the packet.
- Code 1: two equal frames; the payload after the TOC must be even.
- Code 2: the first length is coded in one or two bytes, the second frame
  takes the rest.
- Code 3: VBR/CBR and padding flags, a padding chain (255 means 254 more
  bytes plus another byte), `M` frame lengths for VBR or equal division for
  CBR, and at most 120 ms of audio.
- Lengths are 0..1275; zero is legal and means DTX or a lost frame.
- Every frame pointer stays inside the packet; padding is never part of a
  frame.

Rejected as `I_CORRUPT` (never clamped or truncated): an empty packet; an
odd code 1 payload; a code 2 packet without room for its length; a code 3
packet with zero frames, more than 120 ms, padding past the packet, lengths
that do not fit, or a CBR remainder that is not a multiple of the frame
count; any frame over 1275 bytes.

## The range decoder contract

`audio/opus/range.cst` implements RFC 6716 Section 4.1. State is
`(val, rng)` plus the forward cursor, the raw-bit cursor and `nbits_total`.
All cells are i64 holding unsigned 32-bit values.

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

SILK reads with `decode` + `update` (through `silk_bits.cst`), CELT also
uses `bit_logp`, `icdf`, `uint`, `bits`, `tell` and `tell_frac`. The raw-bit
reader returns the low window bits directly, which is not equivalent to
`decode_bin` plus `update`, and is required for the CELT post-filter,
fine/final energy, anti-collapse and small-N sign bits.

`decode` and `decode_bin` leave the division factor in `ext`; the caller
must follow with `update` before the next symbol. `bit_logp`, `icdf` and
`uint` perform the whole symbol themselves. `uint` with more than 8
significant bits takes the high part from the range coder and the rest from
raw bits. A uint result past `ft-1` is `I_CORRUPT` rather than saturated.

Bounds and error behaviour:

- `ft` is 1..65535 for `decode`/`update`, `ftb` is 0..15 for `decode_bin`,
  `logp` is 1..31, `ftb` is 1..8 for `icdf`, `ft` is 1..2^32-1 for `uint`,
  and `n` is 0..31 for raw bits. Anything else is `I_BADPARAM`.
- Reading past either end of the slice supplies zero bytes instead of
  dereferencing, as Section 4.1.2.1 requires; this is not an error.
- `update` rejects `fl >= fh` or `fh > ft`, an `ext` of 0, a `val` that
  cannot absorb the update, and a resulting `rng <= 0`, all as `I_CORRUPT`.
- `icdf` gives up after 256 entries and rejects a table that leaves an empty
  range.
- A zero-length slice is valid: it initialises `b0 = 0` and decodes zeros.

`ec_tell` is `nbits_total - ilog(rng)`; `ec_tell_frac` follows the three
squaring steps of Section 4.1.6.2. A fresh decoder reports 1 bit used, the
terminator the encoder reserves.

## Ogg pages, packets and timelines (summary)

The page format is RFC 3533 Section 6; all multi-byte integers are little
endian. A page is 27 header bytes plus N segment-table bytes plus the sum of
the lacing values, at most 65307 bytes. `ogg_page_parse` validates the
capture pattern, the 27-byte minimum, the segment table and body against the
source end, and then the CRC; only after that does it check version 0 and
the reserved header-type bits, and only then does it publish any field. The
CRC is the non-reflected 0x04C11DB7 polynomial with initial value 0 and no
final inversion, computed bit by bit over the page with the checksum bytes
zero.

Packet reassembly follows RFC 3533 Section 5: 255 continues, 0..254 ends.
A packet that spans pages is remembered as up to 32 `(offset, length)`
fragments and copied once into `Ogg.pktbuf` when complete; a packet that
fits in one page is returned directly from the source. Bounds are compile
time (16 tracks, 32 fragments, 65536 bytes per assembled packet); a larger
packet is `I_UNSUPPORTED`, never truncated.

Continuation and loss rules (RFC 7845 Section 3):

- A continued page resumes a pending packet only when the sequence number is
  consecutive.
- If continuation is flagged with no pending packet, the missing prefix is
  discarded up to the first lacing value below 255.
- If a packet is pending and the next page is not continued, the packet is
  dropped.
- A page after a stream's EOS page is `I_CORRUPT`.
- A stream's first packet is its BOS header; for Opus the second header
  (OpusTags) is also consumed. The track exposes `head` / `head_len`,
  `codec`, `serial`, `granule`, `eos`, `packets` and `packet_index`.

Granule positions count 48 kHz samples per channel, including the pre-skip
region; `PCM sample position = granule - pre_skip`. The demuxer passes the
completing page's granule to every packet that completes on it; exact
per-packet positions come from TOC durations in the audio layer.

## Bounds, allocation and errors

Everything in the decoder is caller-owned and fixed-size:

- `OpusDecoder` contains both codec states and the two SILK planar scratch
  buffers (2880 f64 each, a 60 ms SILK frame) and one CELT/hybrid
  interleaved scratch (1920 f64, 20 ms stereo). No size comes from a packet.
- Every frame length is checked against its maximum before it is used.
- Every band index, LSF index, PVQ index and scratch offset is checked in
  the SILK/CELT layers before indexing.
- `cap` and the output pointer are validated before decoding, so a short
  caller buffer returns `I_NOMEM` without advancing codec state.
- All failures are negative i64 codes; a successful decode is a non-negative
  frame count. `*got` is written only on success.

## UNCONFIRMED

All items below are unconfirmed until an external comparison runs; nothing
was compiled or executed in the writing phase.

- **BOS header completes on its BOS page.** Read from RFC 3533 Section 4
  ("bos pages ... containing one initial header packet per page"). A
  different reading would allow a continued first header packet.
- **Version 0 accepted for OpusHead.** RFC 7845 Section 5.1 says version
  MUST be 1 and that implementations SHOULD accept values up to 15; this
  decoder accepts 0..15 for compatibility.
- **Reserved Ogg header-type bits.** Bits 3-7 are rejected as `I_CORRUPT`;
  the RFC does not assign them.
- **Raw-bit prefetch.** The reference fills its raw-bit window to at least
  25 bits before every read; this implementation fetches only until the
  requested count is available. The returned values are identical, but the
  exact `end_offs` timing differs.
- **`ec_dec_icdf` with ftb up to 8.** The reference's 16-bit-entry variant
  is not implemented; the CELT slice uses the 8-bit contract only.
- **Granule -1 on a page with completed packets.** The demuxer passes -1
  through as the packet timestamp instead of rejecting the page.
- **Hard saturation instead of soft clipping.** The reference decoder can
  apply a soft clip after decoding. This decoder rounds and saturates; loud
  signals may differ by more than rounding.
- **Hybrid -> WB SILK overlap flush.** Implemented with a 2-byte
  `0xFF 0xFF` CELT silence frame, which depends on the CELT silence
  detection and on the CELT overlap state being preserved across the mode
  switch. The construction follows the reference, but no hybrid stream has
  been decoded to confirm the output.
- **SILK resampler delay versus CELT.** The resampler is designed from the
  delay budget in Section 4.2.9 but is not bit-identical to the reference.
  Hybrid and mode-switching output may be time-shifted or differ in
  amplitude even when parsing is correct.
- **Hybrid sum alignment.** The sum assumes SILK's resampler and CELT's
  overlap-add produce the same 48 kHz frame interval. This is the
  reference's model; no vector has been checked.
- **Malformed redundancy sizes.** If a redundancy header would leave the
  main frame shorter than the bits already consumed, the reference disables
  redundancy and continues with a zero-length main frame; this decoder
  returns `I_CORRUPT`. The reference calls the case non-normative. Not
  verified against a stream.
- **Output gain default.** The raw `OpusHead` gain is stored and the linear
  factor is available, but it is off by default. The reference's
  `OPUS_SET_GAIN` is a caller request, not an automatic application of the
  header field.

## Pending review notes

Low-severity notes; none affects valid-stream memory safety.

- **The second Opus header is consumed without a magic check.** `ogg.cst`
  advances past the OpusTags packet and does not verify its `OpusTags`
  magic; a malformed comment packet is accepted by the demuxer.
  `opus_head.cst` provides `opus_tags_parse` for a caller that wants to
  validate or read it.
- **`OpusDecoder` is large.** Two embedded codec states plus the scratch
  arrays dominate it; callers must own it (or place it in static storage).
  A future interface could take caller-provided sub-buffers.
- **S16 output only.** `opus_decode` writes interleaved i16. A move to
  `core/samples` (S16/S32/F32) would be a public interface change; the
  current contract is the one the stage-7 skeleton fixed.
- **`opus_decode_fec` exists only to refuse.** It keeps the FEC request
  explicit instead of silently treating an FEC call as a normal decode. If
  the caller has no FEC path, it can be dropped.
- **`opus_decode_lost` has no output parameters.** It refuses by design, so
  there is no length or buffer to validate.
- **Redundant CELT frames are skipped.** The redundancy header is parsed
  and the range slice shrinks correctly, so the main frame stays aligned,
  but the redundant frame is not decoded or cross-faded. A transition that
  relies on it gets the main audio and a discontinuity.
- **The CELT reset ignores `prev_redundancy`.** Section 4.5.2 makes the
  reset conditional on how the previous frame's redundancy was used. This
  decoder tracks no redundancy state, so it resets on every mode change
  into Hybrid or CELT-only. That is the common path; a redundancy-driven
  transition may lose one overlap window.
- **`opus_decoder_clear` zeroes the whole struct, including the CELT table
  cache.** Re-initialising rebuilds the tables. This is simple but not the
  cheapest path for a seek-heavy player.
- **The `Hybrid -> WB SILK` flush changes CELT state.** It programs
  `start = 0`, `lm = 0` and the SILK packet's end band. The next CELT or
  Hybrid frame resets CELT anyway under Section 4.5.2, so this is
  intentional.
- **End trim may span packets.** The count is carried into the following
  packet when it exceeds the current one, so a segment-level crop is not
  silently truncated. A correct Ogg caller does not need the carry: RFC
  7845 Section 4.4 says the discarded amount SHOULD fit in the last packet.
- **No end-of-stream flush for the SILK resampler.** The last resampler
  delay samples are not emitted; pre-skip and end trim address the start
  and the EOS granule, not the resampler tail.

## References read

Specifications in `.work/opus/spec/`:

- `rfc6716.txt` — Sections 3, 3.1, 3.2.1-3.2.5, 4, 4.1, 4.2.8, 4.2.9,
  4.3, 4.3.1-4.3.7, 4.4, 4.5, 4.5.1, 4.5.2, 4.5.3.
- `rfc7845.txt` — Ogg Opus Sections 3, 4, 4.2, 4.3, 4.4, 4.5, 5.1.
- `rfc3533.txt` — Ogg encapsulation Sections 4-6.

Reference implementations read from raw URLs for behaviour only; nothing
was copied, ported or transcribed. Every URL below prefixed with
`https://raw.githubusercontent.com/`:

- `xiph/opus/main/src/opus_decoder.c` — packet-to-frame loop, the SILK/CELT
  mode split, hybrid start/end bands, `celt_accum`, the Hybrid -> WB SILK
  silence flush, pre-skip and output gain.
- `xiph/opus/main/silk/dec_API.c` — superframe handling, `payloadSize_ms`,
  internal sample rate, LBRR skipping, stereo/mono transitions and the
  resampler call.
- `xiph/opus/main/celt/celt_decoder.c` — `start`/`end`/`LM` control, the
  shared `ec_dec` entry, silence detection and the accumulation flag.
- `FFmpeg/FFmpeg/master/libavcodec/opus/dec.c` — the independent SILK/CELT
  split, hybrid `start = 17`, CELT band-end table and the resampler delay
  buffering.

## Status

WRITTEN, NOT VERIFIED. Nothing in this stage has been compiled, built or
run. No test was added or changed, and no table generator was written or
executed. The checks the original criteria would require — `opus_compare`
against the RFC 6716 vectors, `ffprobe` track/timing comparison,
`trace_headers`, MD5 and byte-difference against FFmpeg, and an audio
comparison of hybrid output — have not run.
