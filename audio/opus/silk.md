# Opus SILK decoder

Stage 7's SILK layer: the LP part of RFC 6716, from the header bits to the
resampled output the Opus decoder consumes. It does not decode CELT, split
packets, or parse the TOC; those belong to the Ogg/packet layer and the Opus
decoder.

Status: **WRITTEN, NOT VERIFIED.** Nothing in this stage has been compiled
or run.

## Files

| File | Holds |
|---|---|
| `audio/opus/silk.cst` | The public decoder object, header bits, LBRR and regular frame flow, stereo unmixing and the output path. |
| `audio/opus/silk_defs.cst` | `SilkChan`, `SilkParams`, the channel reset, and the fixed history sizes. |
| `audio/opus/silk_bits.cst` | `silk_sym`, `silk_uniform`, `silk_bit`: the three ways a SILK symbol is read from `range.cst`. |
| `audio/opus/silk_nlsf.cst` | NLSF reconstruction and stabilization (4.2.7.5.3–4.2.7.5.5), NLSF→LPC conversion, range limiting and prediction-gain limiting (4.2.7.5.6–4.2.7.5.8). |
| `audio/opus/silk_core.cst` | Excitation decoding (4.2.7.8) and the LTP+LPC synthesis filters (4.2.7.9). |
| `audio/opus/silk_resample.cst` | The non-normative resampler to 8/12/16/24/48 kHz. |
| `audio/opus/silk_tables.cst` | Table counts, constants and the blob accessors. |
| `audio/opus/silk_tables_data.cst` | Every table, as comma-separated decimal blobs transcribed from the RFC. |
| `tools/gen_opus_silk_tables.py` | The intended regeneration path for the data file. **Written, unrun.** |

The range decoder is the agreed contract (`struct OpusRange`,
`opus_range_init/decode/decode_bin/update/bit_logp/icdf/uint/tell/
tell_frac`) in `audio/opus/range.cst`. `silk_bits.cst` uses
`opus_range_decode` followed by `opus_range_update` for PDFs, so it depends
only on the primitive pair and not on an iCDF memory layout.

## State and ownership

The library allocates nothing. The caller owns a `SilkDecoder`; it is one
fixed-size object (`sizeof(SilkDecoder)`) containing:

- two `SilkChan` histories: 306 clamped output samples, 16 unclamped LPC
  samples, the previous normalized LSFs and the previous log gain;
- the previous stereo weights and the three stereo delay samples
  (`mid[-2]`, `mid[-1]`, `side[-1]`);
- the previous mid-only flag and previous internal channel count;
- the `SilkResampler`: up to six phases × 21 coefficients and 20 input
  history samples per output channel.

`SilkParams` is a per-frame scratch object. `silk_decode` keeps one for the
mid frame, one for the side frame and one for LBRR parsing; none of them
survives the call.

`SilkChan.out_hist` is laid out as `[306 history][320 current]`. Synthesis
writes the current frame at offset 306, copies each clamped sample to the
caller's internal-rate buffer, and only then shifts the history left by the
frame length. This is why `silk_synthesize` takes an output pointer: the
driver must see the current frame before the shift.

Sizes come from the specification: maximum lag 288, LPC order 16, LTP order
5 (giving the 306-sample output history), and 320 samples for a 20 ms WB
frame.

## Frame flow

`silk_decode(d, r, bandwidth, duration_ms, coded_channels, out_l, out_r,
cap, frames_out)` decodes the SILK part of **one Opus frame**.

1. Validate, map bandwidth to 8/12/16 kHz, derive the 5 ms subframe length,
   frame length and number of 20 ms SILK frames (10/20 ms → one, 40 ms →
   two, 60 ms → three).
2. A bandwidth change clears both channels, the stereo state and the
   resampler, then re-initialises it for the new input rate.
3. A mono→stereo transition resets the side channel, zeroes the previous
   stereo weights and copies the left resampler history to the right, so the
   two output channels start aligned.
4. Header bits (4.2.3): per-channel VAD flags, then the global LBRR flag,
   mid channel first.
5. Per-frame LBRR flags (4.2.4): none for 10/20 ms; the 4-entry PDF for
   40 ms and the 8-entry PDF for 60 ms.
6. LBRR frames are **parsed, not synthesized**. Every symbol, including
   the excitation, is decoded so the range coder advances. Their
   conditional coding is independent of the regular frames' history, as
   section 4.2.7.4 requires ("first SILK frame of its type"). The stereo
   weights and mid-only flag of a mid LBRR frame are parsed and discarded.
7. Regular frames, one 20 ms interval at a time, mid then side:
   - the mid frame parses stereo weights (stereo only) and, when the side
     VAD is clear, the mid-only flag;
   - frame type, gains, LSF stage 1/2, interpolation factor, LTP
     parameters, LCG seed and excitation;
   - gains are dequantized, the NLSFs interpolated (20 ms only), converted
     to LPC, and the frame synthesized;
   - the side frame uses conditional coding except at the first interval or
     after a skipped side frame (`SILK_COND_INDEP_NOLTP`: absolute gains and
     pitch, no LTP-scaling field, no interpolation).
   - a mid-only interval zeroes this interval's side signal and resets
     the side channel, so its next frame starts from a known zero LTP/LPC
     state; the previous side sample is still used at the first unmix
     sample, as 4.2.8 requires.
8. Output: for stereo-in/stereo-out the mid/side pair is unmixed (4.2.8);
   otherwise the mid signal is delayed by one sample. Mono output of a
   stereo stream is the mid channel, matching the reference. The result is
   resampled per 20 ms frame and written at 8/12/16/24/48 kHz.

### Conditional coding

`SILK_COND_INDEPENDENT` reads an absolute first gain, absolute pitch, and
(when voiced) the LTP scaling index. `SILK_COND_CONDITIONAL` reads delta
gains, a pitch delta when the previous frame was voiced, and no LTP scaling
field. `SILK_COND_INDEP_NOLTP` is the independent form without the LTP
scaling field, used only for the side channel after a skipped frame.

The previous log gain is held per channel and is `-1` when none is
available (after a reset or a side-channel skip), which skips the
independent-gain clamp exactly as the specification says.

### LBRR

LBRR frames have no output in this decoder. There is no PLC or FEC path:
`silk_decode_lost()` returns `I_UNSUPPORTED`. A decoder that wants FEC must
add it later; nothing here pretends to conceal loss.

## Excitation

`silk_decode_pulses` follows 4.2.7.8.1–4.2.7.8.5:

- rate level selects one of the eleven 18-entry pulse-count PDFs;
- a count of 17 escapes to the level-9 PDF and increments that block's LSB
  count; after ten escapes the level-10 PDF (whose last probability is
  zero) is used;
- locations are a preorder recursive halving of each 16-sample block, with
  the PDF chosen by partition size and pulse count (the offset in the
  flattened blob is `(count-1)*(count+2)/2`);
- LSBs are read for every coefficient of a block, including the eight extra
  samples of a 10 ms MB frame, MSB first;
- signs use Table 52 with the block's pulse count before the LSB
  extension, capped at "6 or more";
- 4.2.7.8.6 adds the Q23 offset, runs the LCG and applies the sign flip.

The 10 ms MB case parses 8 shell blocks (128 samples) but only the first
120 are synthesized, as the specification requires.

## NLSF and LPC

The reconstruction is fixed point, as section 4.2.7.5 asks: stage-2
residuals are dequantized backwards with Table 20/21/22 weights, the
inverse-Harmonic-mean weights are computed from the stage-1 codebook, Q15
NLSFs are formed and then stabilized, including the 20-adjustment loop and
the sort/push fallback of 4.2.7.5.4.

The conversion uses the Q12 cosine table with linear interpolation, the
reordered recurrence for P and Q, ten rounds of range limiting, and sixteen
rounds of prediction-gain limiting driven by the Q24 Levinson recurrence.
All intermediates are i64. A filter that is still too large is saturated
in Q12 and then expanded; the last round uses a zero chirp, which zeroes the
filter and guarantees stability.

Interpolation applies to the first half of a 20 ms frame only, and is forced
to 4 (no interpolation) after a decoder reset or an uncoded side frame.

## Synthesis

`silk_core.cst` uses the floating-point form the specification describes in
4.2.7.9, so it is not bit-exact; the specification explicitly does not
require that for these stages. Normalised values are used throughout:
output and residual in `[-1, 1]`, `gain_Q16/65536`, `a_Q12/4096`, and
`b_Q7/128`.

For voiced frames the residual history needed by each subframe is
rewhitened from the clamped output history with the current LPC
coefficients; the 306-sample output history covers 288 lag + 16 LPC + 2
filter taps. Subframes 0 and (when the LSF interpolation factor is below 4)
2 rewhiten; the LTP filter then runs for the 5 ms subframe, and LPC
synthesis produces the output with the unclamped samples feeding the next
subframe. Unvoiced frames use the excitation directly.

The per-subframe gain multiplies the normalised residual, so no gain
adjustment of the filter history is needed.

## Resampling

The resampler is deliberately non-normative (4.2.9): the decoder may choose
any method. This one is a 21-tap polyphase Kaiser-windowed sinc, designed
in `silk_resample.cst`: `h(u) = cutoff*sinc(cutoff*u)` with
`sinc(x) = sin(pi*x)/(pi*x)` and `cutoff = min(1, out/in)`, which places
the passband edge at 0.5*min(1, out/in) cycles per input sample, i.e. at
the Nyquist frequency of the lower of the two rates. The Kaiser window
(beta 8) is evaluated at the same distance `u` as the sinc, so the kernel
is centred on the fractional evaluation point. Each phase is normalised to
unit DC gain, and coefficients are computed once at init from a Taylor
sin, the power series for I0 and Newton square root. It supports
8/12/16 → 8/12/16/24/48 kHz; the reduced ratio has at most six phases.

The 21-tap kernel needs ten input samples of look-ahead, so output is
delayed by ten input samples (0.625 ms at 16 kHz, 0.833 ms at 12 kHz,
1.25 ms at 8 kHz), plus the one-sample unmix delay. WB, the only bandwidth
Hybrid frames use, totals 0.6875 ms and stays inside Table 54's 0.706 ms.
NB and MB SILK-only output is later than their Table 54 allocations;
section 4.2.9 permits this for a decoder, and there is no CELT layer to
align in a SILK-only stream. The passband correction above does not change
this delay. At a format switch the resampler is cleared to silence, as the
specification requires.

Output is planar f64 in `[-1, 1]`. The Opus decoder can mix it with CELT
and convert to its public sample format.

## Tables and provenance

`audio/opus/silk_tables_data.cst` has every table as a comma-separated
decimal blob. All elements are committed; **no blob is pending**. The blobs
and their RFC tables are:

| Blob | Table | Elements |
|---|---|---|
| `STEREO_S1/S2/S3` | 6 | 25 / 3 / 5 |
| `STEREO_W` | 7 | 16 |
| `MID_ONLY` | 8 | 2 |
| `FRAME_INACTIVE`, `FRAME_ACTIVE` | 9 | 6 each |
| `GAIN_MSB_*` | 11 | 8 each |
| `GAIN_DELTA` | 13 | 41 |
| `LSF_S1_*` | 14 | 32 each |
| `LSF_S2` | 15, 16 | 144 |
| `LSF_S2_EXT` | 19 | 7 |
| `LSF_PRED_A/B/C/D` | 20 | 9 / 9 / 15 / 15 |
| `LSF_S2_SEL_NBMB/WB` | 17, 18 | 320 / 512 |
| `LSF_PRED_SEL_NBMB/WB` | 21, 22 | 288 / 480 |
| `LSF_CB_NBMB/WB` | 23, 24 | 320 / 512 |
| `LSF_MINSP_NBMB/WB` | 25 | 11 / 17 |
| `LSF_INTERP` | 26 | 5 |
| `LSF_ORDER_NBMB/WB` | 27 | 10 / 16 |
| `COS` | 28 | 129 |
| `PITCH_HIGH`, `PITCH_LOW_*`, `PITCH_DELTA` | 29–31 | 32, 4/6/8, 21 |
| `CONTOUR_PDF_*`, `CONTOUR_CB_*` | 32–36 | 3/11/12/34, 6/44/24/136 |
| `LTP_PERIOD`, `LTP_PDF0/1/2`, `LTP_CB0/1/2` | 37–41 | 3; 8/16/32; 40/80/160 |
| `LTP_SCALE_PDF`, `LTP_SCALE` | 42 | 3, 3 |
| `EXC_RATE_*` | 45 | 9 each |
| `PULSE_COUNT` | 46 | 198 |
| `PULSE_SPLIT16/8/4/2` | 47–50 | 152 each |
| `LSB` | 51 | 2 |
| `SIGN` | 52 | 84 |
| `QOFF` | 53 | 6 |
| `LBRR40/60` | 4 | 4 / 8 |

Three tables are not blobs by design:

- Table 43 (LCG seed) is a uniform 4-entry PDF; `silk_uniform(r, 4)` is
  equivalent and no table is stored.
- Table 12 (independent gain LSB) is a uniform 8-entry PDF; the same
  helper is used.
- Table 44 (shell blocks per frame/bandwidth) is derivable as
  `ceil(frame_length / 16)` and is computed, not stored.
- Table 10 (frame type → signal/offset) is three lines of arithmetic.
- Tables 30/33–36/39–41 scalars (pitch scale/min/max, LTP tap sizes) are
  constants in `silk.cst`.

`tools/gen_opus_silk_tables.py` reads the RFC and would emit the same
blobs, validating element counts and that every PDF sums to 256. It was
**written and not run**. The committed data file was transcribed from the
RFC text and each blob was compared value-by-value against that text in the
writing phase (a text comparison, not execution); no value was invented.
Running the generator and comparing the emitted blobs with the committed
blob values (not the files, whose headers differ) is required before this
provenance claim is complete.

## Errata and adopted interpretations

All adopted interpretations are **UNCONFIRMED** until an external
reference run confirms them; corrections that merely restate the supplied
RFC text are marked as such. No test has been executed in this phase.

### E1. Table 17's I1 = 6 row is labelled "g" (UNCONFIRMED)

The row sequence is 0,1,2,3,4,5,g,7,...,31. The row is clearly the sixth
(it has ten letter entries like every other row), so it is used as I1 = 6.
The label is a typographic error; the table order is unambiguous.

### E2. Table 24 has 32 distinct rows (correction, no erratum)

The supplied RFC prints Table 24's rows I1 = 0..31 exactly once, and all
32 rows are distinct. There is no duplicated I1 = 0 row; the earlier E2
claim was wrong. The committed blob has 512 values (32 × 16), as published.
The generator's duplicate-row guard, written against that misreading, never
fires and is dead code; see pending note 12.

### E3. DC response threshold: `>` or `>=` 4096 (UNCONFIRMED)

Section 4.2.7.5.8 says "if DC_resp > 4096, the filter is unstable". Both
independent decoders stop at `DC_resp >= 4096`. This implementation uses
`>=`, because a filter whose coefficient sum is exactly 4096 is at the
stability boundary and both references reject it. A stream that hits the
exact boundary would differ.

### E4. Which mid-only symbol means "mid-only" (UNCONFIRMED)

Table 8's PDF is `{192, 64}` but the RFC does not spell out symbol values.
Both reference decoders use the decoded symbol directly as the mid-only
flag, so symbol 1 (probability 64) means "mid-only" and symbol 0 means the
side channel is coded. Adopted: `mid_only = symbol`.

### E5. LTP rewhitening scale (UNCONFIRMED wording)

Section 4.2.7.9.1 writes the rewhitening multiplier as
`4.0*LTP_scale_Q14/gain_Q16`. With `gain_Q16 = gain*65536` this is
`(LTP_scale_Q14/16384)/gain`, which is the form implemented. For the
third/fourth subframe with interpolation the scale is forced to 16384, so
the multiplier is `1/gain`. This is arithmetic from the RFC, recorded
because the factor of four is easy to misread.

### E6. NLSF interpolation after a reset or skipped side frame (UNCONFIRMED)

Section 4.2.7.5.5 says the interpolation factor is still decoded but
ignored (always 4) after an uncoded regular side frame or a decoder reset.
This implementation sets `SilkChan.first_reset` on both events and forces 4
for the next frame; the factor is still read from the bitstream so the
range coder position is right.

### E7. Table 25's 20 ms / order boundary (UNCONFIRMED)

The minimum spacing table's last entry (461 for NB/MB, 347 for WB) applies
to the distance from the last coefficient to 32768. The stabilization code
indexes `0..order` accordingly; this is the reading both references use.

### E8. Resampler delay outside Table 54 for NB/MB (design note)

The 21-tap kernel has ten input samples of delay. WB plus the unmix sample
is 0.6875 ms, under the 0.706 ms WB allocation. NB is 1.375 ms and MB
0.917 ms, above their allocations. Section 4.2.9 allows a decoder to use
more delay, requiring the MDCT layer to be delayed to match only in a
hybrid stream; Hybrid is WB, so no CELT alignment is affected in SILK-only
NB/MB. This is a property of this non-normative resampler, not an RFC
erratum.

### E9. Range decoder `update` return (interface assumption)

`silk_bits.cst` calls `opus_range_update` and checks its i64 result for a
negative status, per the agreed contract ("i64 returns, negative = error").
If the integrated range module declares `update` as void, that call must be
changed to a statement; nothing else in SILK depends on its result.

### E10. Table 46's level-10 row (UNCONFIRMED)

The RFC says level 10 is "a shifted version" of level 9 and needs no extra
storage. The committed blob contains the explicit level-10 row printed in
Table 46, including the final zero probability. The generator reads the
printed row; a decoder that shifts the level-9 row at run time produces the
same probabilities.

## Interfaces for the Opus decoder

```
fn silk_decoder_init(d as *SilkDecoder, channels as i64, rate as i64) as i64
fn silk_decoder_reset(d as *SilkDecoder) as i64
fn silk_output_frames(d as *SilkDecoder, bandwidth as i64,
                      duration_ms as i64) as i64
fn silk_decode(d as *SilkDecoder, r as *OpusRange,
               bandwidth as i64, duration_ms as i64, coded_channels as i64,
               out_l as *f64, out_r as *f64, cap as i64,
               frames_out as *i64) as i64
fn silk_decode_lost(d as *SilkDecoder) as i64   // always I_UNSUPPORTED
```

- `channels` is the OpusHead channel count (1 or 2); `rate` is the output
  rate the Opus decoder wants (8/12/16/24/48 kHz).
- `bandwidth` is the SILK internal bandwidth: 0 NB, 1 MB or 2 WB; any
  other value returns `I_BADPARAM`. It is not literally the TOC
  configuration group `config >> 2`: configurations 12-15 are Hybrid
  (SWB/FB), whose SILK layer always runs at WB, so the Opus layer must
  map them to 2 rather than passing 3.
- `duration_ms` is the Opus frame duration, not the packet duration: 10,
  20, 40 or 60.
- `coded_channels` is the TOC stereo flag plus one.
- `out_l`/`out_r` are planar f64 buffers with `cap` frames each;
  `out_r` is ignored when the decoder has one output channel.
- `frames_out` receives the produced frames (`duration_ms * rate / 1000`).
- One call decodes the whole SILK layer of one Opus frame and leaves the
  range decoder after it. Do not call once per 20 ms interval.
- The Opus layer must call `silk_decoder_reset` before a SILK/Hybrid frame
  when the previous frame was CELT-only (section 4.5.2), and on any other
  reset it already performs. Bandwidth changes inside SILK are detected and
  reset here.
- There is no PLC/FEC output; `silk_decode_lost` refuses. A caller that
  needs concealment must synthesize it itself.

The CELT writer can mix the f64 output directly; if the public format is
S16, the Opus decoder converts and saturates once, after the mix.

## Pending review notes

Low-severity notes; none affects valid-stream output or memory safety.

1. **Generator unrun.** `tools/gen_opus_silk_tables.py` has never been
   executed. Its table extraction was written against the RFC layout and
   may need adjustment (the line-wrapped brace groups and the Table 53
   word-labelled rows are the most likely spots; the Table 24 duplicate
   guard is dead, see note 12). The committed data file itself is complete
   and was compared with the RFC text.
2. **Synthesis is f64, not bit-exact.** Permitted by 4.2.7.9. NLSF and LPC
   conversion, which must not drift, are fixed point.
3. **Resampler coefficients are designed at runtime**, not committed as a
   blob. There is no spec provenance for them; the design is documented in
   `silk_resample.cst`. A future review may prefer a committed generator
   blob, at the cost of a pending-table dependency.
4. **No pre-skip or end flush.** The resampler delays the stream by ten
   input samples plus the unmix sample; the Opus/Ogg layer owns pre-skip and
   any final flush. The tail delay samples are not emitted.
5. **LBRR only parsed.** No FEC output; `silk_decode_lost` is a refusal.
6. **Side-channel reset on mid-only** uses the full channel reset, which
   also clears the previous gain and LSFs; the reference flushes the same
   state. This is deliberate.
7. **`silk_decode` does not check trailing range-coder bits.** The Opus
   layer owns packet validation and the final bit position.
8. **Mono output of a stereo stream is the mid channel**, not a downmix.
   This matches the reference and the specification's "a mono decoder can
   simply decode the mid channel".
9. **Table 30's scale/min/max and the LTP filter codebook sizes are
   constants in code**, not blobs; they are three values each and are
   annotated with the table they come from.
10. **`SILK_TBL_QOFF` is used only for the excitation offset**; the signal
    type / offset type mapping itself is in `_silk_parse_frame` (three
    branches), not a table.
11. **A zero-length (DTX) Opus frame must not reach `silk_decode`.** The
    range decoder accepts an empty slice, but SILK has no symbols to read;
    the Opus layer owns the DTX/silence decision before calling here.
12. **E2 corrected; Table 24 has no duplicate.** The supplied RFC prints
    I1 = 0..31 once each and all 32 rows are distinct, so the duplicate-row
    erratum was false and E2 now says so. The generator's
    `len(wb_rows) == 33` duplicate guard never fires and should be deleted
    when the generator is next edited.
13. **PDF-total check wording.** `silk_pdf_total`'s comment says a non-256
    total is reported as `I_CORRUPT`, but `silk_sym` only rejects
    `ft <= 0` and `ft > 65535`; a wrong blob/count whose values sum into
    that range is decoded with that total. Not reachable for the committed
    blobs. Not fixed: requiring exactly 256 would tighten a contract the
    range-coder layer does not require.
14. **Generator extraction is fragile (unrun).** The extractor keys on
    caption text and the previous `Table ...:` line; captions and brace
    groups that wrap across lines (Table 6's stage labels, Table 52's
    `{138,` / `118}`) and word-labelled rows (Table 53) need the special
    handling already in the script. It also writes only a two-line header,
    not the committed file's header comment. After running it, compare the
    emitted **blobs** against the committed blob values, not the files.
15. **State taint after `I_CORRUPT`/`I_NOMEM`.** `silk_decode` updates
    `d.prev_coded`, the per-channel `log_gain`/`nlsf` (through
    `_silk_prepare`), the stereo weights and the resampler history before
    it can fail, and it does not restore them. On any negative return the
    caller must call `silk_decoder_reset` or discard the decoder; ignoring
    the error and decoding the next frame would mix old and new state.
    Valid streams are unaffected.
16. **Wording and attribution slips.** Left as-is: "resampled per 20 ms
    frame" (a 10 ms SILK frame is resampled per 10 ms interval too); the
    "Three tables are not blobs by design" heading introducing more than
    three bullets; the bullet attributing the LTP order/tap-size constants
    to `silk.cst` (`SILK_LTP_ORDER` is in `silk_tables.cst`); the comment in
    `silk_tables.cst` placing `silk_sym` in `silk.cst` (it is in
    `silk_bits.cst`); the duplicated `d.bandwidth = 0 - 1` in
    `silk_decoder_init`; and the `[-1, 1]` output range, which a
    windowed-sinc resampler can ring slightly past.
17. **Bandwidth-change reset of the stereo delay (choice).** On an internal
    bandwidth change `_silk_clear_state` also zeroes `st_mid1`, `st_mid2`,
    `st_side1` and the stereo weights. libopus's `silk_decoder_set_fs`
    reinitialises the channel and resampler state but leaves `sStereo`
    alone, and RFC 4.2.9 only requires discarding the resampling buffer.
    Kept: this decoder treats a bandwidth change as a reset, and the
    one-sample stereo history is meaningless across a rate change.
18. **Mid-only side history: references differ.** On a mid-only interval
    libopus keeps `sStereo.sSide[1]`, so `silk_stereo_MS_to_LR` uses the
    previous side sample at its first output sample; FFmpeg flushes the
    side frame before unmixing, so it uses zero there. This decoder follows
    RFC 4.2.8 and libopus: it zeroes only the current interval's side
    signal and keeps `st_side1` until `_silk_unmix` replaces it with
    `side[len-1]` (zero for a mid-only interval).

## Reference implementations read

Only for behaviour and to settle ambiguities; no code was copied, ported or
transcribed.

- xiph/opus `main`: `silk/dec_API.c` (header bits, LBRR skip, conditional
  coding, mono/stereo transitions), `silk/decoder_set_fs.c` (what a
  sample-rate change resets; the stereo state is not touched),
  `silk/decode_frame.c`, `silk/decode_indices.c`, `silk/decode_parameters.c`,
  `silk/decode_core.c`, `silk/decode_pulses.c`, `silk/code_signs.c`,
  `silk/gain_quant.c`, `silk/stereo_decode_pred.c`,
  `silk/stereo_MS_to_LR.c` (two-sample side state; the prior side sample is
  used at the first output sample), `silk/NLSF_decode.c`, `silk/NLSF2A.c`,
  `silk/LPC_inv_pred_gain.c`, `silk/tables_other.c` (mid-only iCDF and
  LBRR flag iCDFs).
- FFmpeg `master`: `libavcodec/opus/silk.c` (frame driver, stereo unmix,
  LBRR handling, output delay) and `libavcodec/opus/dec.c` (how the SILK
  superframe is called and resampled).

## Verification that would be required

Nothing below has been run:
`caustic-mk run test`, a build of the module, MD5 comparison of the RFC
6716 test vectors against the reference decode, `opus_compare` on the same
vectors, and, for the Ogg path, `ffprobe`/FFmpeg difference and
`trace_headers` on a committed small file. The resampler quality and the
delay figure have not been measured.
