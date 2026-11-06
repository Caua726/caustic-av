# Opus CELT decoder

Stage 7c. It decodes one CELT frame payload — no Opus TOC byte, no packet
padding, no loss concealment — into 48 kHz f64 PCM. The Opus packet layer
decides the frame size, the coded band range and the channel count, then
calls `celt_decode_with_range` (the entry that continues a range decoder
SILK has already advanced, for hybrid frames) or `celt_decode` (the
standalone wrapper) once per frame.

## Files

| File | Holds |
|---|---|
| `audio/opus/celt_state.cst` | `CeltDec`: configuration, persistent energy and filter state, all per-frame scratch and the parsed tables. Fixed size; the decoder allocates nothing. |
| `audio/opus/celt_tables.cst` | Mode constants, small inverse CDFs, `CeltTab`, `log2_frac`, the bit-exact cosine/log2-tangent helpers, the PVQ U/V row builder, init-time parsing of the fixed texts, the window, `logN` and the PVQ pulse/bit cache. |
| `audio/opus/celt_tables_data.cst` | **Generated** by `tools/gen_opus_celt_tables.py`: mean energies, coarse-energy Laplace model and band caps. |
| `audio/opus/celt_energy.cst` | Laplace coarse energy, fine energy, final fine bits, energy history. |
| `audio/opus/celt_alloc.cst` | Caps, pulse/bit cache access, `clt_compute_allocation` (curve bisection, interpolation, skip decisions, intensity/dual stereo, fine/PVQ split). |
| `audio/opus/celt_pvq.cst` | PVQ codeword decode (U/V and `cwrsi`), spreading rotation, band splits and angle decoding, TF Haar changes, folding, stereo merge, `quant_all_bands`, anti-collapse. |
| `audio/opus/celt_mdct.cst` | Inverse MDCT, window and overlap-add, comb post-filter, de-emphasis. |
| `audio/opus/celt.cst` | Frame flow, synthesis, state init/reset, public API. |
| `tools/gen_opus_celt_tables.py` | Table generator and validator (written, unrun). |

Specification: `.work/opus/spec/rfc6716.txt`, sections 4.3.0-4.3.7 and
Table 55-63, plus section 3 for the fact that the packet layer owns
everything before the CELT payload. RFC 7845 (Ogg mapping) only matters to
the packet layer.

## State and ownership

`CeltDec` is caller-owned and fixed-size. It contains:

- configuration: output channels (1-2), stream channels (1-2),
  `start` (0 or 17), `end` (<= 21), `lm` (0-3), `disable_inv`.
- energy history: `old_e`, `old_log_e`, `old_log_e2`, 2 x 21 values each,
  log2 domain, without the mean table.
- per-frame work: 2 x 960 coefficients, 2 x 624 folding samples, one 960-bin
  frequency buffer, one 960-sample time buffer, 2 x 120 overlap tails,
  2 x 2048 post-filter history, de-emphasis memory.
- allocation decisions: pulses, fine bits, fine priorities, TF changes,
  dynalloc offsets, caps, collapse masks, trim, intensity, dual, balance,
  coded bands.
- the noise LCG seed, post-filter parameters of the previous two frames, and
  the parsed `CeltTab`.

`celt_init` validates the channel counts, parses the tables and resets the
history. `celt_reset` clears everything that survives a frame without
touching the tables. `celt_set_frame` validates and stores the band range and
`lm`; it is the hook the Opus layer uses for CELT-only (start 0) and hybrid
(start 17) frames.

## Frame flow

`celt_decode_with_range(d, r, pcm, pcm_cap)` decodes one frame from a range
decoder the caller already owns (`r` is an `OpusRange` bound by
`opus_range_init`). It reads the current position and never re-initialises,
shrinks or resets the decoder, so the Opus packet layer can call it after
SILK has consumed the front of a hybrid frame. The frame budget is
`r.len * 8` bits, the slice the caller bound. `celt_decode(data, len, pcm,
pcm_cap)` is the standalone wrapper: it binds a fresh range decoder at byte
0 of `data` and delegates. The wrapper requires `2 <= len <= 1275`; a NULL
payload or one with fewer than two bytes is packet loss (`I_UNSUPPORTED`, as
the reference treats `len <= 1`), because this decoder has no PLC and
refuses to pretend. The range entry accepts any slice the caller bound
(`0 <= r.len <= 1275`): an empty slice makes `tell >= total_bits`, so the
frame decodes as silence rather than failing.

Decoding then follows Table 56:

1. **silence**: the bit position is captured *before* this symbol. If the
   range decoder has consumed the frame, or the very first symbol (logp 15)
   says silence, the whole frame is silent and the remaining symbols are
   skipped (the reference pretends `len*8` bits were read). The path still
   runs synthesis and the filters so the overlap tail and filter state decay
   exactly as a zero frame would; energies are set to the -28 floor.
2. **post-filter**: only for `start == 0` and only if 16 bits fit, gated on
   the tell captured before the silence symbol. Octave (uniform 6),
   4+octave raw bits, gain (3 raw bits) and tapset ({2,1,1}/4). Period =
   (16<<octave) + fine - 1, so 15..1022.
3. **transient** (logp 3, only when LM > 0), **intra** (logp 3). The
   reference gates these on the same tell variable and refreshes it only
   where a block was actually entered: after the post-filter block, and after
   the transient symbol when it is read. This decoder threads that variable
   the same way. On a valid stream the stale and current values coincide at
   every gate (the only symbol the stale value can hide is the silence flag,
   and its non-silent outcome leaves the whole-bit tell unchanged), so this
   is a fidelity fix to the gate order, not a change in what is parsed.
4. **coarse energy**: per band and channel, Laplace with the frame-size and
   intra/inter model for as long as 15 bits remain; otherwise the small
   uniform symbol, a single logp-1 bit, or -1.
5. **TF changes**: the reference gating rule (`tf_select_rsv`), the
   differential flags, then `tf_select`, then Tables 60-63.
6. **spread** ({7,2,21,2}/32) when 4 bits fit.
7. **caps** from the cap table, then **dynalloc boosts** with the shrinking
   `dynalloc_logp` and the `quanta = min(8*width, max(48, width))` step.
8. **trim** ({2,2,5,10,22,46,22,10,5,2,2}/128) when 6 bits fit after boosts.
9. **allocation**: caps, curves, skip, intensity, dual stereo, leftover
   redistribution, fine/PVQ split (see below).
10. **fine energy** bits, then **PVQ shapes** (bands), then the
    **anti-collapse** bit (reserved earlier), then the **final fine bits**.
11. **denormalise**, inverse MDCT, overlap-add, comb post-filter,
    de-emphasis and interleaved PCM.

The post-filter is applied to every output channel with the same transition
(the state left by the previous frame crossed to this frame's parameters,
over the squared window), and only after the channel loop is the filter
state advanced once: previous := current, current := this frame's, and for
LM > 0 previous := current again. Advancing inside the channel loop would
give channel 1 different endpoints and would corrupt the following frame at
LM = 0. De-emphasis and the history shift follow per channel.

The frame budget checks use `opus_range_tell` (bits) where the syntax is
counted in bits and `opus_range_tell_frac` (1/8 bits) where the allocation
is. The final tell must not exceed `8*len`; otherwise `I_CORRUPT`.

## Bit allocation

`celt_alloc.cst` reproduces the reference's integer procedure, since it
drives the range decoder:

- caps: `(table + 64) * C * N >> 2`, table indexed by `[LM][C-1][band]`.
- static curves: 11 rows from RFC Table 57; bisection over 1/64 steps.
- trim offsets: `C * width * (trim-5-LM) * (end-j-1) * (1<<(LM+3)) >> 6`,
  with the one-bin adjustment.
- skip: backwards from the last band, with the reference's band-bit estimate
  and the one reserved bit; a kept band stops the loop.
- intensity: `start + uint(coded+1-start)`; dual: one logp-1 bit, only when
  intensity stereo is in use. The reservation is `LOG2_FRAC_TABLE[n]` with
  `n` the band count, and the table entry `n` is `log2_frac(n+1, 3)` — the
  1/8-bit cost of the uniform symbol over `n+1` values. Building it as
  `log2_frac(n, 3)` leaves the stereo reservation one 1/8 bit short except
  at fullband and three short for a hybrid frame.
- leftover bits are spread by `percoeff` over the band-edge span in 2.5 ms
  units (not frame bins) and the remainder one bin at a time.
- fine/PVQ split: the reference's `dof`, `NClogN`, two extra offsets for the
  second and third fine bit, the N=2 special case, the stereo degree of
  freedom, and the two fine priorities. Over-cap bits become extra fine bits
  through the excess/balance chain.

The pulse/bit cache is built from the definition at init:
`pulses2bits(N,q) = log2_frac(V(N,get_pulses(q)),3)`, with
`qmax` the largest q whose `V` fits in 32 bits. `bits2pulses` performs the
same 6-step binary search and the same tie break as the reference. Deriving
the cache removed the only large generated table; the RFC explicitly allows
any equivalent V computation (4.3.4.2).

## PVQ, splits and stereo

- `celt_pvq_decode` builds the U row once (`U(n,k) = U(n-1,k) + U(n,k-1) +
  U(n-1,k-1)`, saturated at 2^32), decodes a uniform integer with
  `V = U(n,k) + U(n,k+1)`, then walks the dimensions, stepping the row down
  with `U(n-1,*)` after each symbol. The decoded vector is normalised to
  `gain` and rotated by the spreading rotation (theta = pi/4 * g_r^2,
  g_r = N/(N + f_r K), f_r from Table 59), including the extra interleaved
  rotation of 4.3.4.3 when the block is large enough.
- `quant_partition` recurses while `b > pulses2bits(qmax) + 11 && N > 2`
  (the reference's `cache[qmax] + 12`, with its cache entry equal to
  `pulses2bits(qmax) - 1`), coding the mid/side angle with `compute_theta`
  and rebalancing as it goes. The angle PDFs are the uniform, step (stereo,
  N>2) and triangular ones of 4.3.4.4; the mid/side gain is
  `cos(pi/2 * itheta/16384)` and its complement, and the bit difference
  `delta` comes from the bit-exact log2 tangent.
- `quant_band` applies the TF recombine (`haar1`), time divide, Hadamard
  sequency interleave and their inverses, and scales the folding history by
  sqrt(N) for the next band.
- `quant_band_stereo` handles the N=2 one-bit side and the general mid/side
  split, then `stereo_merge`; the `inv` bit flips the side unless the output
  is mono.
- Folding: when a band has no pulses, it is either zero, noise from the LCG,
  or a copy of a previous band plus a ±2^-8 ripple. The folding source is
  chosen exactly as the reference does (lowband offset, spread, transient,
  TF), and the collapse masks of the source bands are ORed.
- Anti-collapse fills each collapsed time block with ±r noise, where r is
  derived from the current energy minus the minimum of the two previous
  frames and capped by `0.5*2^(-depth/8)`, then renormalises the band.

## Synthesis

The inverse MDCT is the RFC 4.3.7 definition:

```
u(m)  = sum_k X[k] cos(pi/N (m+1/2)(k+1/2))
s(t)  = (1/2) u(t + N/2)          t = 0 .. 2N-1
```

Only `t < N + 120` is evaluated; the rest is folded into that range with
`u(2N-1-m) = -u(m)` and `u(2N+m) = -u(m)`. The window is the RFC low-overlap
window: the 120-sample Vorbis taper `W(n) = sin(pi/2 sin(pi/2 (n+1/2)/120)^2)`
at each edge and 1 in the flat middle; for a 2.5 ms block the two tapers meet
and the window is the full 240-sample basic window. The window is power
complementary with a hop of N: the rising edge of a frame pairs with the
falling edge of the previous one over 120 samples, the flat middle belongs to
one frame, and each frame stores a 120-sample tail for the next.

Denormalisation multiplies each band by `2^(logE + eMeans)` (capped at
2^32). The post-filter is the three-tap comb filter of 4.3.7.1 with the
squared MDCT window as the parameter cross-fade; its history is 1024 samples
plus the frame. De-emphasis is `y[n] = x[n] + 0.8500061035*y[n-1]`. Mono
stream to stereo output duplicates the denormalised spectrum; stereo stream
to mono output averages the two spectra before the inverse MDCT.

## Tables and provenance

| Table | Size | Source |
|---|---|---|
| band edges | 22 | RFC Table 55, 2.5 ms differences; also the reference `eband5ms` |
| static allocation | 11 x 21 | RFC Table 57 |
| trim / spread / tapset / small-energy PDFs | 11 / 4 / 3 / 3 | RFC Table 56, 58, 59 |
| TF adjustments | 4 x 8 | RFC Tables 60-63 |
| post-filter taps and gain | 3 x 3 + formula | RFC 4.3.7.1 |
| de-emphasis coefficient | 1 | RFC 4.3.7.2 |
| mean band energy | 25 | reference data table (the RFC text does not name or print it) |
| coarse-energy Laplace model | 4 x 2 x 42 | reference data (RFC 4.3.2.1 names `e_prob_model`) |
| band caps | 4 x 2 x 21 | reference data (RFC 4.3.3 defines its conversion and names the cap source) |
| window | 120 | RFC 4.3.7 formula, computed at init |
| logN and log2_frac table | 21 / 24 | conservative log2 formula, computed at init; `LOG2_FRAC_TABLE[n] = log2_frac(n+1, 3)` |
| PVQ pulse/bit cache | 177 x 41 | `log2_frac(V(N,K),3)`, computed at init |
| PVQ U/V | computed | RFC 4.3.4.2 recurrence |

`celt_tables_data.cst` is the generator's output. The three tables it holds
are data constants of the RFC's reference definition, not algorithm code;
every value is bounded and dimension-checked by the generator. The
licence/provenance wording is recorded as UNCONFIRMED for review.

No table is left pending. `tools/gen_opus_celt_tables.py` has **not been
run** in the writing phase. Its `--check-only` mode now validates the
constants (including Tables 60-63, whose earlier expectation map mixed the
transient and non-transient columns) and compares the committed
`celt_tables_data.cst` byte for byte with the text the generator renders, so
the check is reproducible from the repository alone. That comparison has not
been executed here; it is listed under the required checks.

## Interface for the Opus decoder

```
use "celt.cst" as celt;

celt.celt_init(&d, channels, stream_channels)          // once
celt.celt_reset(&d)                                   // state reset
celt.celt_set_frame(&d, start, end, lm, stream_channels)
celt.celt_frame_size(&d)                              // 120<<lm
celt.celt_decode(&d, data, len, pcm, pcm_cap)         // frames or <0
celt.celt_decode_with_range(&d, &r, pcm, pcm_cap)     // frames or <0
```

- `start` is 0 for a CELT-only frame and 17 for a hybrid frame.
- `end` is the first uncoded band; the packet layer derives it from the
  bandwidth (RFC 3.1/4.3: hybrid codes bands 17..end, fullband uses 21).
- `lm` is log2(frame_size/120), 0..3 for 2.5/5/10/20 ms.
- `pcm` is interleaved f64, `pcm_cap` counts scalar values and must be at
  least `frame_size * channels`.
- The return value is frames per channel on success.

`celt_decode_with_range` is the hybrid path and the lowest-level entry:

- `r` is an `OpusRange` the caller initialised with `opus_range_init` over
  the frame slice. For a hybrid frame that slice starts at the first byte of
  the packet and SILK has already advanced the decoder; CELT continues from
  the same position, sharing the front cursor, the raw-bit cursor and the
  slice. A zeroed or uninitialised range has `rng == 0`, which
  `opus_range_tell` reports as `I_CORRUPT`; the entry returns that error
  instead of decoding garbage. `r.ptr` must be non-null when `r.len > 0`
  (`I_BADPARAM`), and `r.len` must be 0..1275.
- CELT neither re-initialises `r`, nor rewinds it, nor changes `r.len`. The
  caller may narrow `r.len` before the call (the packet layer does this to
  move the raw-bit reader past a redundant-CELT tail); CELT uses whatever
  slice remains. The frame budget is `r.len * 8` bits for every gate
  (`tell`, `tell_frac`, final energy, anti-collapse), and the final tell
  must stay within it. A silent frame, including an empty slice, uses the
  reference accounting: the decoder advances `nbits_total` so the final
  tell is exactly `r.len * 8`, which lets an empty slice return a silent
  frame instead of failing the budget check.
- The redundant-CELT header and the redundant-frame trimming that precede
  this call belong to the packet layer; CELT starts at the byte where the
  high band starts.
- After the call the caller may keep using `r` (a hybrid frame has no
  further CELT data, but the packet layer owns the decision).

`celt_decode` is a thin wrapper for CELT-only frames: it rejects a NULL or
shorter-than-two-byte payload with `I_UNSUPPORTED` (packet loss), bounds
`len` to 1275, binds `r` at `data[0]` and calls the range entry.

The range decoder contract used is the one agreed for the Ogg/packet/range
slice:

```
OpusRange, opus_range_init, opus_range_decode, opus_range_decode_bin,
opus_range_update, opus_range_bit_logp, opus_range_icdf, opus_range_uint,
opus_range_bits, opus_range_tell, opus_range_tell_frac
```

`opus_range_bits(r, n)` is `ec_dec_bits`, the raw-bit reader. The range
decoder's raw-bit path is not equivalent to `decode_bin` plus `update`: it
reads the low window bits directly. The CELT layer needs it for the
post-filter period/gain, fine energy, final energy, anti-collapse and the
N=1/N=2 sign bits. It is provided by the range slice
(`audio/opus/range.cst`).

`OpusRange` supplies `len` for the frame budget, and `opus_range_tell`
supplies the bit position for every optional-symbol gate. No other range
entry is needed by this layer. `opus_range_rng` (the final range value,
which libopus uses as the folding/anti-collapse seed) is **not** used:
`celt_quant_all_bands` keeps its own LCG state in `d.rng`, seeded from zero
at reset. That divergence is recorded as Pending review note 1.

## UNCONFIRMED items

All are unconfirmed until the external comparison runs; no test was executed
in the writing phase.

### U1. MDCT layout and scale (UNCONFIRMED)

The RFC gives the inverse MDCT as N frequency samples to 2N time samples
with a factor 1/2, and the low-overlap window by formula. This decoder uses
that definition directly: DCT-IV kernel, `u(2N-1-m) = -u(m)` folding,
120-sample tapers, 120-sample overlap tail, hop N. The resulting output is
the overlap-add of exactly these windowed values.

The reference implementation (libopus `clt_mdct_backward`) computes the same
transform through a folded half-length representation: it writes N+60
samples per frame, stores a 60-sample tail, and mixes previous and current
values with the 120-entry window table in 60 pairs. It should be the same
transform, but the factor-of-two placement of the tail and the global `1/2`
scale depend on its FFT normalisation. Both are candidates for a single
global amplitude/time-offset difference, not for parsing differences. Verify
with the FFmpeg difference and MD5 checks first.

### U2. Post-filter parameter timing (read-confirmed, external check pending)

RFC 4.3.7.1 says the transition uses the square of the MDCT window but not
where the segment boundaries are. This decoder follows the reference's
timing: for LM=0 the filter of the previous frame is used for the whole
120-sample frame and the new parameters take effect next frame; for LM>0 the
first 120 samples fade old→previous and the rest fade previous→new. The
parameters that carry across frames (`pf_period/gain/tapset` and their
`_old` copies) are advanced once per frame, after every output channel has
been filtered, exactly as libopus updates them outside its channel loop and
as FFmpeg keeps one parameter set per block; see `celt_postfilter_apply` and
`celt_postfilter_advance`. The external checks still have to confirm the
sample alignment.

### U3. Noise sequence for folding and anti-collapse (UNCONFIRMED)

The LCG is seeded from the range decoder's final state in libopus. This
decoder keeps an independent seed (`d.rng`), so noise-filled and collapsed
blocks carry different random values. Only those blocks' output differs; the
bitstream parse does not. The noise magnitude is `12-bit centred` for
noise-filled bands (rather than the raw unsigned window of the reference)
and ±2^-8 for folded bands; renormalisation removes the scale, leaving a
possible DC/shape difference.

### U4. Bit-exact integer helpers (UNCONFIRMED)

`celt_log2_frac`, `celt_bitexact_cos` and `celt_bitexact_log2tan` are
integer definitions whose rounding is part of the format: the allocation and
the mid/side split depend on them. They were reconstructed from the
reference behaviour and from the requirement that the allocation is exactly
recoverable (RFC 4.3.3), not copied as code. The generator checks
`log2_frac` against `ceil(8*log2(v))`, allowing the helper's own one-unit
rounding; the two cosine helpers are checked only by their use. A mismatch
shows up as diverging allocations.

### U5. `bit_deinterleave` generalisation (UNCONFIRMED)

The reference uses a 16-entry table for the recombine mask, valid while the
block count is at most 4. This decoder contains the equivalent bit-doubling
formula for 8 bits, which matches the table on 0..15 and extends it. Valid
48 kHz streams should stay inside the table's range, but the general form is
not exercised by the reference.

### U6. Provenance of the three data tables (UNCONFIRMED)

The RFC names the coarse-energy model (`e_prob_model`, 4.3.2.1) and the cap
source (4.3.3) and refers to the mean band energy without printing it; all
three tables are printed only in the reference implementation. They are
reproduced here as data. Confirm that treating these constants as
specification data is acceptable for the project's licence position, and
record the wording in this note.

## Pending review notes

The review's numbered findings, with the state of each after this fix pass.
Items 2, 3, 6, 7 and 8 are fixed on this branch; the rest are recorded and
deliberately unchanged. None of them affects valid-stream parsing except
where stated.

1. **Noise seed and magnitude divergence (recorded, not fixed).**
   `celt_pvq.cst` fills noise bands with 12-bit centred values
   (`((seed >> 20) & 0xFFF) - 2048`); libopus fills with the unsigned
   `(opus_int32)seed >> 20`. More importantly, `d.rng` is never seeded from
   the range decoder's final state: libopus sets `st->rng = dec->rng` at
   every frame end, while here the LCG is seeded from zero at reset and only
   threads through `quant_all_bands`. RFC 4.3.5 only asks for a
   pseudo-random signal, so this is not a spec violation, but folded, noise
   and anti-collapse blocks will not match the reference sample for sample
   and the MD5 / `opus_compare` criteria will differ on them. Either match
   the reference (seed from the range's final `rng`, unsigned noise) or keep
   the divergence as intentional; not decided here.
2. **Generator TF validation and `--check-only` (fixed).** The expectation
   map mixed the transient and non-transient columns of RFC Tables 60-63
   (for example it wanted `[1, 0]` for the non-transient 5 ms row where
   Table 61 says `[0, -2]`), so the committed tool would have rejected its
   own correct `TF_TABLE`. `validate()` now checks all 32 entries against
   the four tables, and `--check-only` renders the output and compares it
   byte for byte with the committed `celt_tables_data.cst`. The comparison
   has not been executed.
3. **Documentation accuracy (fixed).** The `celt_tables.cst` comment no
   longer claims the RFC prints the 24 `LOG2_FRAC_TABLE` entries; U6 no
   longer says 4.3.6 names `eMeans`; the stale "unused local" note about
   `celt_synthesise` and the energy module is gone; the data-table paragraph
   now describes the generator check instead of claiming an unverified diff.
   The DTX/silence wording and the interface section were brought in line
   with the code.
4. **Dead Haar on the decoder's scratch X (recorded).**
   `_celt_quant_band` applies `celt_haar1` to `x` in the forward recombine
   and time-divide loops; libopus guards those calls with `if (encode)`.
   In the decoder `x` is scratch and fully overwritten by
   `quant_partition`, so the result is correct and the work is dead. The undo
   loops still apply the one transform that matters. Left as is: removing
   the forward calls would break the symmetry with the reference's structure
   for no output change.
5. **Pointer-to-integer arithmetic with a hard-coded 8-byte f64
   (recorded).** `celt.cst` (the synthesis output pointer), `celt_mdct.cst`
   (`celt_tbl_offset`) and `celt_pvq.cst` (`celt_elem_ptr`) form pointers as
   `cast(*f64, cast(i64, base) + off * 8)`, while the rest of these files
   uses `&base[off]`. It depends on f64 being 8 bytes, which the fixed-size
   buffers already assume. Converting to the array form would touch every
   call site; not done in this pass.
6. **A one-byte payload is packet loss (fixed).** `celt_decode` now rejects
   `len <= 1` with `I_UNSUPPORTED`, matching libopus
   (`data == NULL || len <= 1` goes to `celt_decode_lost`). A one-byte CELT
   payload used to reach the budget check and return `I_CORRUPT`.
7. **Dead declarations (fixed).** Removed the unused `CELT_MAX_LM`,
   `CELT_SHORT_MDCT`, `CELT_MAX_FRAME`, `CELT_MAX_BINS`, `CELT_MAX_NORM`,
   `CELT_MAX_PERIOD`, `CELT_MAX_FINE`, `CELT_QTHETA_OFFSET`,
   `CELT_QTHETA_TWOPHASE`, `CELT_PVQ_CACHE` and the four `CELT_SPREAD_*`
   constants from `celt_tables.cst`, and the write-only `rate` field from
   `CeltDec` together with its initialisation.
8. **Pulse-cache index (fixed).** `celt_pulses2bits` now rejects
   `q > CELT_MAX_PSEUDO` (40) before indexing the row, so the
   out-of-bounds read is impossible by construction instead of merely
   unreachable through today's callers.
9. **Provenance wording (recorded).** `celt_log2_frac`,
   `celt_bitexact_cos/log2tan`, `celt_laplace_decode`, `_celt_cwrsi` and the
   rate arithmetic necessarily reproduce the reference's integer steps: the
   format is defined by them. The blanket "no code was copied, ported or
   transcribed" below is too strong for those helpers; the U6 licence
   wording should cover them as well as the three data blobs. The wording is
   not settled here.
10. **Empty-slice accounting (resolved in 7d).** The range contract says an
    empty slice makes `tell >= total_bits`, so the frame decodes as silence,
    but the final `opus_range_tell(r) > r.len*8` check would reject it
    unless the range's `nbits_total` is adjusted. The 7d integration adopts
    the reference accounting: when `silence != 0`, it advances
    `nbits_total` so the tell becomes exactly `r.len*8` before synthesis.
    That is the recorded choice; a silent empty slice now returns frames.
11. **Generated header wording vs U6 for `eMeans`.** The generated
    `celt_tables_data.cst` header (and the generator docstring) says the RFC
    "names" all three data tables, while U6 records that the RFC only refers
    to the mean band energy without naming or printing it;
    `e_prob_model` and the caps are named only in the reference. Reconcile
    the header wording when the generator is next edited; the values are
    unaffected.
12. **`CELT_TBL_ALLOC_TEXT` joined into one literal for the compiler.**
    Caustic has no implicit adjacent-string concatenation, so the eleven
    pieces became a single line; the text is unchanged (717 characters, 231
    values, same order and single spaces), no data change.

Other low-severity notes carried over, no effect on valid-stream parsing or
memory safety:

1. **Direct O(N^2/2) inverse MDCT.** Correct and auditable, but the 20 ms
   block costs ~0.55 M multiply-adds per channel. A folded FFT-based IMDCT
   is deferred (performance work, not correctness).
2. **Tables are parsed per decoder.** `celt_init` builds the PVQ cache
   (~2 M operations) and parses the texts for each instance. A process-wide
   const table would remove that cost; the current shape keeps the "caller
   owns all memory, library allocates nothing" rule.
3. **Silence path.** It does not run the coarse-energy -1 updates or the
   allocation; it jumps to zero coefficients and the -28 floor. The output
   and history update match the reference's zeros, but the intermediate
   state values are not identical.
4. **`old_e` clamps.** The reference's ±28 clamps are fixed-point guards;
   the f64 path leaves valid values unclamped.
5. **Post-filter history shift** is a linear memmove of 2048-N samples per
   channel per frame; a ring buffer would avoid the copy.
6. **Output format.** f64 interleaved. The Opus layer converts; there is no
   S16/F32 path here and no clipping.

## Checks required (NOT RUN)

The following would be needed before this stage can be called verified.
Nothing here was executed.

- **MD5**: decode the RFC 6716 CELT test vectors (the `opus-*` suite,
  `testvector*.bit` through `opus_demo`-style frames) and compare frame MD5s
  with the reference. CELT-only and hybrid files.
- **ffprobe / ffmpeg difference**: decode the same Ogg Opus files with
  FFmpeg and compare sample-by-sample (`ffmpeg -f s16le` difference), both
  channels, all four frame sizes.
- **trace_headers**: `ffmpeg -bsf:a trace_headers` on the vectors to confirm
  the CELT frame boundaries and bandwidth/band-range mapping used by
  `celt_set_frame`.
- **opus_compare**: `tools/opus_compare` on the decoded output versus the
  reference output, for the CELT-only vectors.
- **Frame sizes / fps**: 2.5, 5, 10 and 20 ms frames, mono and stereo,
  checking sample count and continuity across frame-size changes.
- **Hybrid**: at least one hybrid (SILK+CELT, start band 17) stream through
  `celt_decode_with_range`, after the SILK slice lands, confirming that the
  shared range decoder continues at the right byte and that `r.len * 8` is
  the budget SILK left.
- **PLC rejection**: a NULL payload, an empty one or a single byte returns
  `I_UNSUPPORTED` (by inspection; not run).
- **Table generator**: `python3 tools/gen_opus_celt_tables.py --check-only`
  from the repository root, which validates the constants against RFC
  Tables 55/57/60-63 and compares the committed `celt_tables_data.cst` with
  the rendered text (by inspection; not run).
- **TF row**: the 2.5 ms rows of Tables 60-63 now come from the per-band
  flag alone (`[0, -1]` in all four tables); a transient 2.5 ms frame with
  `tf_select = 1` exercises the corrected entry.

## Reference implementations read

Read only for behaviour and to settle ambiguities; no code was copied,
ported or transcribed. Data constants are recorded separately above. The
integer helpers whose arithmetic is part of the format are an exception to
that blanket statement — see Pending review note 9.

Confirmed in this pass (post-filter state update and tell gating in
`celt_decoder.c`; `LOG2_FRAC_TABLE` and `compute_pulse_cache` in `rate.c`;
the `quant_partition` split condition in `bands.c`; `parse_postfilter` and
`celt_postfilter` in FFmpeg `dec_celt.c`).

- xiph/opus `main`: `celt/celt_decoder.c` (frame flow, state, synthesis,
  tf_decode, post-filter timing), `celt/celt.c` (comb filter, tf table,
  init_caps), `celt/quant_bands.c` (coarse/fine/final energy, eMeans,
  e_prob_model), `celt/laplace.c`, `celt/rate.c` (allocation,
  LOG2_FRAC_TABLE, compute_pulse_cache), `celt/rate.h` (bits2pulses,
  pulses2bits, get_pulses), `celt/bands.c` (denormalise, anti-collapse,
  stereo, haar/Hadamard, quant_all_bands, quant_partition, quant_band),
  `celt/vq.c` (alg_unquant, exp_rotation, renormalise),
  `celt/cwrs.c` (U/V, cwrsi, decode_pulses), `celt/mdct.c`,
  `celt/modes.c` (eBands, allocVectors, window, overlap, mdct size),
  `celt/static_modes_float.h` (window, logN, cache index/bits/caps),
  `celt/mathops.h` (FRAC_MUL16, cos_norm).
- FFmpeg `master`: `libavcodec/opus/dec_celt.c` (frame flow, energy, TF,
  anti-collapse, post-filter application, MDCT/overlap usage),
  `libavcodec/opus/celt.c` (bit allocation and quant_all_bands ordering),
  `libavcodec/opus/celt.h` (mode constants, caps/alloc sizes),
  `libavutil/float_dsp.c` (vector_fmul_window semantics),
  `libavutil/tx.h` (MDCT length/flags), `libavcodec/opus/pvq.c` (cross-check
  of the PVQ cache idea; the decoder here computes V directly).

## Status

WRITTEN, NOT VERIFIED. Nothing was compiled or run. This revision fixes the
review's B1-B5 and PENDING 2, 3, 6, 7 and 8; the stage is complete when a
read-only reviewer approves it and the checks above have been executed in a
later phase.
