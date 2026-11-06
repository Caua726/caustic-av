# VP9: residual and coefficient decoding

Stage 4 of the VP9 decoder, residual slice. It turns one transform block of
one plane into dequantized coefficients: the `tokens()` syntax of section
6.4.24 with `get_scan` (6.4.25) and `read_coef` (6.4.26), the context
derivation and counting of sections 9.3.2 and 9.3.4, and the
dequantization of section 8.6.1 that section 8.6.2 applies before the
inverse transform.

It does not predict, inverse transform or write samples. Block geometry
(`get_uv_tx_size`, `get_plane_block_size`, the per-4x4-block loop of
`residual()`, the Above/Left context write-back) belongs to `block.cst`,
which owns the coordinates and the context arrays. The inverse transform
is stage 5.

Only profile 0 is accepted, as everywhere in this decoder, so BitDepth is 8:
`read_coef` never reads the `high_bit` elements of `DCT_VAL_CATEGORY6`, and
the quantizer lookups use row 0.

## Files

| File | Holds |
|---|---|
| `video/vp9/residual.cst` | `Vp9Quant`, `Vp9CoefCtx`, the context-array helpers, `vp9_tx_type`, token decoding, dequantization, and the one-call `vp9_residual_decode`. |
| `video/vp9/residual_tables.cst` | The fixed table accessors: scan selection, coefficient bands, token tree, energy classes, extra bits, category probabilities, pareto table, mode-to-transform map, and the profile-0 `dc_q`/`ac_q` lookups. |
| `video/vp9/residual_tables_data.cst` | The table blobs. **Generated** by `tools/gen_vp9_residual_tables.py`; currently pending (see below). |
| `tools/gen_vp9_residual_tables.py` | Reads the specification text, validates every table (dimensions, ranges, scan permutations, token-tree structure, `extra_bits` consistency, cat_probs padding), and writes `residual_tables_data.cst`. |

The specification used is `.work/vp9/spec/clean.txt` (VP9 Bitstream &
Decoding Process Specification v0.6).

## Interface

### Inputs

```cst
struct Vp9Quant {
    qindex      as i64;   // get_qindex(): 0..255, segmentation resolved
    y_dc_delta  as i64;   // delta_q_y_dc
    uv_dc_delta as i64;   // delta_q_uv_dc
    uv_ac_delta as i64;   // delta_q_uv_ac
}

struct Vp9CoefCtx {
    above        as *u8;   // plane-major: plane*above_stride + x4
    left         as *u8;   // plane-major: plane*left_stride  + y4
    above_stride as i64;
    left_stride  as i64;
    max_x        as [3]i64;   // 4x4 units per plane
    max_y        as [3]i64;
}
```

`Vp9Quant` carries the header's quantizer fields verbatim; `vp9_dequant`
adds the deltas and clips to 0..255 itself, exactly as section 8.6.1's
`Clip3 ( 0, 255, b )` requires, so a caller never has to pre-clip.

`Vp9CoefCtx` is the `AboveNonzeroContext` / `LeftNonzeroContext` pair of
section 6.4.21 at 4x4 granularity. The caller owns both byte arrays and
allocates them; each must hold `3*stride` bytes (three planes).
`vp9_coef_ctx_init` records the strides and the per-plane
limits `(2*MiCols)>>sx` and `(2*MiRows)>>sy`, and `vp9_coef_ctx_clear`
zeroes three planes of each. `above_stride` must be at least `2*mi_cols`
and `left_stride` at least `2*mi_rows`; a smaller buffer returns `I_NOMEM`.
Arranging the arrays plane-major keeps one allocation per tile for all
three planes.

### Calls

```cst
fn vp9_coef_ctx_init(cc, above, above_stride, left, left_stride,
                     mi_cols, mi_rows, ss_x, ss_y) as i64
fn vp9_coef_ctx_clear(cc) as i64
fn vp9_tx_type(plane, tx_sz, lossless, is_inter, mode) as i64
fn vp9_tokens_decode(br, p, counts, plane, tx_sz, tx_type, is_inter,
                     start_x, start_y, cc, tokens, nonzero) as i64
fn vp9_dequant(tokens, tx_sz, plane, q, out) as i64
fn vp9_residual_decode(br, p, counts, q, plane, tx_sz, tx_type, is_inter,
                       start_x, start_y, cc, out, nonzero) as i64
```

Per transform block, `block.cst` calls:

```cst
ret = vp9_residual_decode(&br, &st.probs, &st.counts, &q, plane, tx_sz,
                          tx_type, is_inter, startX, startY, &cc, coef,
                          &nonzero);
if (ret < 0) { return ret; }
// write-back of section 6.4.21, clipped to the plane limit:
for (i = 0; i < (1 << tx_sz); i++) {
    if ((startX >> 2) + i < cc.max_x[plane]) {
        cc.above[plane * cc.above_stride + (startX >> 2) + i] = nonzero;
    }
    if ((startY >> 2) + i < cc.max_y[plane]) {
        cc.left[plane * cc.left_stride + (startY >> 2) + i] = nonzero;
    }
}
```

The clip matters only for a block that extends past the frame edge: the
specification writes all `1 << txSz` entries unconditionally, but every read
of these arrays is clipped by `max_x` / `max_y`, so entries past the limit
are never observed. libvpx zeroes them (its `ctx_shift`), FFmpeg leaves them
alone; writing only inside the limit keeps the write inside the stride and
matches both observably.

The write-back is outside the `if (!skip)` guard in `residual()`: for a
skipped block, and for a transform block whose origin is outside the frame,
`nonzero` stays 0 and zeros are written for every 4x4 entry it covers.
`block.cst` must therefore run the same write-back with 0 when it skips the
call, or the next block along the row/column sees stale context.

`br` is the tile's `Vp9Bool`, `p` the frame's working `Vp9ProbCtx`
(`st.probs`), `counts` `st.counts`. All three are required. The function
updates `counts.token` and `counts.more_coefs` at the exact read points;
`block.cst` must not count them again.

`tokens` and `out` are caller arrays of at least 1024 `i64`; the block's
`n0*n0` entries (`n0 = 4 << tx_sz`) are written in natural raster order.
`vp9_residual_decode` dequantizes in place, so one buffer suffices.
`vp9_dequant` also supports `out == tokens`: it handles index 0 first and
then reads each element before writing the same index. `vp9_tokens_decode`
leaves the raw signed `Tokens` in raster order for callers that want them
(future scan-order inspection), and reports `nonzero` through its out
parameter.

`start_x` / `start_y` are plane sample coordinates of the block's top-left
corner, the same values `residual()` passes to `tokens()`; only blocks the
syntax actually decodes (`startX < maxx && startY < maxy`) may be passed.
A block origin with `(start_x>>2) >= max_x[plane]` or
`(start_y>>2) >= max_y[plane]` is `I_BADPARAM`.

`nonzero` is the value `tokens()` returns. `decode_block` (section 6.4.4)
resets `EobTotal` before `residual()` and ORs every plane's `nonzero` into
it, so `block.cst` must accumulate the values this module returns across
all three planes of the block; `EobTotal == 0` with an inter block of at
least 8x8 is what sets `skip = 1` after the residual, and `skip` is what
the next block's `Skips[]` entry and the loop filter see. The module cannot
compute it: it sees one transform block, not the block or the frame.

`tx_type` is `TxType` from section 6.4.25. `vp9_tx_type` derives it from
the mode; the 4x4 transform blocks of a sub-8x8 intra block pass
`sub_modes[blockIdx]`, every other block passes `y_mode` (an inter block is
DCT_DCT either way). `tx_type` is
forced to `DCT_DCT` inside the decoder for chroma and 32x32, exactly as
`get_scan` does, so a caller may pass the mode-derived value for every
plane.

### Errors and ownership

| Code | Meaning here |
|---|---|
| `I_OK` (0) | success; `nonzero` is 0 or 1 |
| `I_BADPARAM` | null pointer, plane/size/flag/quantizer index out of range, block origin outside the plane, context strides inconsistent with the recorded limits |
| `I_NOMEM` | `vp9_coef_ctx_init` called with a stride below `2*mi_cols` / `2*mi_rows` |
| `I_UNSUPPORTED` | the residual table blobs are still pending |
| `I_CORRUPT` | truncated Boolean stream, zero coefficient probability, scan position or band outside the block, token-tree loop, Dequant outside signed 16-bit |

Nothing allocates, opens a file or reads outside caller memory. The token
cache is 1 KiB of stack, cleared to `segEob` entries per block.

The coefficient range check is a deliberate strictness: section 8.6.2 says
a conforming stream keeps `Dequant` representable in `8 + BitDepth` bits,
so a larger value is reported instead of being truncated to `int16` as the
reference decoders do when their range check is compiled out.

## Token decoding

`segEob` is `16 << (txSz << 1)` (16, 64, 256, 1024). For every scan index
`c < segEob`:

1. `pos = scan[c]` and `band = coefband_4x4[c]` for 4x4 or
   `coefband_8x8plus[c]` for larger transforms.
2. `ctx` is derived once for the position (see below); `more_coefs` and
   the token both use it.
3. `checkEob` is 1 at the start and after every non-zero token. When it is
   1, `more_coefs` is read with
   `coef_probs[txSz][plane>0][is_inter][band][ctx][0]`; 0 ends the block.
4. `token` is decoded through `token_tree`; node probabilities come from
   `pareto(node, coef_probs[..][Min(2, 1+node)])`.
5. `TokenCache[pos] = energy_class[token]`; a zero token stores 0 and
   clears `checkEob`, otherwise `read_coef` reads the extra bits and a
   sign bit follows (`read_literal(1)`, 1 = negative).

After the loop, `Tokens[scan[i]] = 0` for every position from `c` to
`segEob-1`, so all 16/64/256/1024 entries are written exactly once.
`nonzero` is `c > 0`, the value section 7.4.16 defines: it says the block
was not empty at scan position 0, not that a particular sample is non-zero.

`read_coef` reads `extra_bits[token][1]` bits with the fixed
`cat_probs[cat][e]` probabilities, most significant first, adding them to
`extra_bits[token][2]`. `cat_probs` row 0 is unused (tokens 0..4 have no
extra bits); row 6 is the 14-bit `DCT_VAL_CATEGORY6` row, which is where
BitDepth 8 stops — the `high_bit` loop of section 6.4.26 runs
`BitDepth - 8 == 0` times.

The `TokenCache` is cleared before each block. The scan orders are such
that both neighbours of any `c > 0` position were already visited, so the
clear is not observable on valid streams; it makes a corrupt scan table
deterministic instead of reading a previous block's energies.

## Context derivation

At `c == 0`, section 9.3.2 ORs the context entries covered by the block:

```
above |= AboveNonzeroContext[plane][x4 + i]     for i < 1<<txSz, x4+i < maxX
left  |= LeftNonzeroContext [plane][y4 + i]     for i < 1<<txSz, y4+i < maxY
ctx = above + left                               (0..2)
```

At `c > 0`, `n = 4 << txSz`, `i = pos / n`, `j = pos % n`:

| Position | `nb[0]`, `nb[1]` |
|---|---|
| `i > 0 && j > 0` | `DCT_ADST`: `(i-1)*n+j` twice; `ADST_DCT`: `i*n+j-1` twice; otherwise the pair |
| `i > 0` | `(i-1)*n+j` twice |
| `i == 0` | `i*n+j-1` twice |

`ctx = (1 + TokenCache[nb[0]] + TokenCache[nb[1]]) >> 1` (0..5). A
neighbour index outside `0..segEob-1` is `I_CORRUPT`; with a valid scan
table it cannot happen.

Band 0 occurs only at `c == 0` (both band tables start `0, 1, 1, ...`), so
the contexts 3..5 the specification leaves unused at band 0 are never read.
The accessor still checks the `coef` table bounds and rejects a coded
probability of 0.

## Counting (section 9.3.4)

The missing `more_coefs` special case of stage 3's erratum E2 is implemented
as follows: one count in
`counts_more_coefs[txSz][plane>0][is_inter][band][ctx][value]` for every
position where the syntax reads `more_coefs`, at that position's band and
context, by value and before branching on it. Consequently:

- the read at `c == 0` is counted even when it is 0 and ends the block;
- a run of zero tokens produces no `more_coefs` read, so no count;
- a block that fills the transform reads `more_coefs` at the second-to-last
  position at the latest; there is no read after the final token.

`counts_token[txSz][plane>0][is_inter][band][ctx][Min(2, token)]` is
counted for every token, including zero tokens.

libvpx increments `eob_branch[band][ctx]` at the top of every EOB check
(`vp9/decoder/vp9_detokenize.c`) and FFmpeg increments `eob[band][nnz][val]`
for every read (`libavcodec/vp9block.c`, `decode_coeffs_b_generic`), which
is this rule. Stage 5's `adapt_coef_probs` is the only consumer.

## Dequantization

For a block of size `n0 = 4 << txSz`:

- `dqDenom` is 2 for 32x32, 1 otherwise;
- `Dequant[0] = Tokens[0] * dc_q(qindex + delta) / dqDenom` (plane 0 uses
  `delta_q_y_dc`, chroma `delta_q_uv_dc`);
- every other entry is `Tokens[i] * ac_q(qindex + delta) / dqDenom`
  (plane 0 has no AC delta; chroma adds `delta_q_uv_ac`);
- `dc_q`/`ac_q` clip their argument to 0..255 and read row 0 of the
  specification's lookups.

The division is by 1 or 2 and Caustic's integer division truncates toward
zero (`-7 / 2 == -3`). Both reference decoders compute the same value: they
dequantize the magnitude (`(val * q) >> 1` in libvpx, `/ 2` in FFmpeg) and
apply the sign afterwards, which equals truncating division of the signed
product.

## Tables and pending blobs

All table data lives in `video/vp9/residual_tables_data.cst`, generated
from the specification by `tools/gen_vp9_residual_tables.py`. The language
has no array initialisers, so a table is a byte blob with checked
accessors.

| Blob | Size | Contents | Specification |
|---|---|---|---|
| `VP9_TBL_RES_SCAN` | 4064 B (2032 x u16 LE) | `default`/`col`/`row` scans for 4x4, 8x8, 16x16 and `default_scan_32x32` | 10.1 |
| `VP9_TBL_RES_BAND` | 1040 B | `coefband_4x4`, `coefband_8x8plus` | 10.1 |
| `VP9_TBL_RES_TOKENS` | 1201 B | `token_tree` (signed bytes), `energy_class`, `extra_bits`, `cat_probs`, `pareto_table`, `mode2txfm_map` | 9.3.2, 6.4.24, 10.2, 10.3 |
| `VP9_TBL_RES_QUANT` | 1024 B (512 x u16 LE) | `dc_qlookup[0]`, `ac_qlookup[0]` | 8.6.1 |

The generator checks what the specification itself implies: each scan is a
permutation of `0..size-1`, bands are 0..5, `energy_class` is 0..5 with 0
at ZERO_TOKEN, `extra_bits` matches the category/base arithmetic of
6.4.24 (`base = 5 + (2^cat - 2)`, 14 extra bits for category 6),
`cat_probs[0]` is the unused all-zero row and the other rows are 1..255,
pareto values are 1..255, `mode2txfm_map` has 14 entries in 0..3, and the
quantizer rows are 256 nondecreasing values.

**Pending blobs.** `residual_tables_data.cst` is committed with
`VP9_RESIDUAL_TABLES_GENERATED = 0` and four empty strings, so
`vp9_residual_tables_ready()` is 0 and every entry point that needs a table
returns `I_UNSUPPORTED`. Nothing in this slice has been generated or run.
Regenerate with:

```
python3 tools/gen_vp9_residual_tables.py --spec .work/vp9/spec/clean.txt \
    -o video/vp9/residual_tables_data.cst
```

Stage 3's three probability blobs (`VP9_TBL_INV_MAP`, `VP9_TBL_KF_PROBS`,
`VP9_TBL_DEFAULT_PROBS`) are still pending as well. Both generators must
run before a tile can be decoded end to end: without the stage-3 blobs the
working probability context is zeroed, and this module rejects a coded
probability of 0.

## Errata and UNCONFIRMED items

All items below are **UNCONFIRMED** until an external reference run
confirms them; this phase compiles and runs nothing.

### R1. `more_coefs` counting special case (continuation of E2)

Still missing from the specification text. The adopted rule is the one
stated above; it matches libvpx and FFmpeg read for read. It is consumed
only by stage 5's `adapt_coef_probs`, so a wrong rule would not change the
tokens of the current frame, only the adapted probabilities of later
frames. Stage 3's external check (a multi-frame file with
`refresh_frame_context = 1` and `frame_parallel_decoding_mode = 0`) is the
check to run.

### R2. Token node probability mapping

Section 9.3.2 gives the token probability as
`pareto(node, coef_probs[txSz][plane>0][is_inter][band][ctx][Min(2,1+node)])`,
so the root of `token_tree` uses node 0 and probability index 1, node 1
uses index 2 *without* the pareto table, and every deeper node uses
`pareto(node, ...)` on probability index 2. Implemented literally. Both
references arrive at the same result with a precomputed 255x8 pareto
table: their `pareto8_full[prob-1]` is the specification's 128-row
`pareto_table` for odd probabilities, and the even ones are the average of
the two neighbouring odd rows, which is exactly section 9.3.2's
`(pareto_table[x] + pareto_table[x+1]) >> 1`.

### R3. 32x32 dequant division rounding

Section 8.6.2 writes `( Tokens * get_ac_quant ) / dqDenom` without saying
how a negative numerator rounds. Adopted: truncation toward zero (Caustic's
`/`). libvpx shifts the magnitude right (`v = (val * dqv) >> dq_shift`) and
negates afterwards; FFmpeg casts the (unsigned) product back to `int` and
divides by 2. Both therefore compute `sign * (|Tokens| * q / 2)` with a
truncating division of the magnitude, which equals truncating division of
the signed product. A 32x32 block with a negative coefficient whose
magnitude times the AC quantizer is odd is the case that distinguishes the
candidates (a floor or round-to-nearest rule would differ by one).

### R4. Dequant range strictness

Section 8.6.2 makes the 16-bit representability of `Dequant` a bitstream
conformance requirement. This decoder returns `I_CORRUPT` when a value
exceeds it; the reference decoders write `int16` and only check the range
when their `CONFIG_COEFFICIENT_RANGE_CHECKING` build option is on. A
conforming stream should never trip the check, but no vector has been run,
so the claim that no vector trips it is UNCONFIRMED.

### R5. TokenCache initialisation

The reference decoders leave the cache uninitialised and rely on every
scan order visiting both neighbours of a position before it. This
implementation clears the cache for determinism. Not observable on a valid
stream; UNCONFIRMED only in the sense that no run has compared outputs.

### R6. `coefband_8x8plus` for chroma

`get_scan` forces `DCT_DCT` for chroma and 32x32, but `tokens()` still
selects the band with `txSz == TX_4X4 ? coefband_4x4 : coefband_8x8plus`.
Implemented as written: chroma 4x4 blocks use `coefband_4x4`, larger
chroma blocks use `coefband_8x8plus`. No ambiguity is known here; recorded
because the two band tables are easy to transpose.

## Limitations and deferred work

- Profile 0 only. The `high_bit` loop of `DCT_VAL_CATEGORY6` for BitDepth
  10/12 is not written; a profile-1..3 frame never reaches this module
  because `vp9_header_parse` refuses it first.
- No inverse transform or reconstruction (stage 5), no loop filter.
- Block geometry, `skip` handling, `predict_intra`/`predict_inter` and the
  Above/Left write-back are `block.cst`'s.
- Counting covers `token` and `more_coefs` only. Every other syntax element
  is counted by the decoder that reads it.
- The four residual blobs and stage 3's three probability blobs are
  pending; nothing decodes tiles until the two generators have run.
- No test suite was added, per the writing-phase policy. `tests/run.cst` is
  untouched.

## Checks for the test phase (not run)

Nothing below has been executed. They are recorded so the test phase can
pick them up:

1. Generate both blobs, then decode the first key frame of
   `vp90-2-00-quantizer-00.webm` and compare its MD5 with libvpx's
   `c3fbb7abbdb5bd4ed4a7e34768c17df1` (stage 3's E1 check; a wrong
   partition table or scan table both diverge here).
2. Decode the libvpx `vp90-2-*` suite frame by frame and compare each
   frame's MD5 with the reference, in particular:
   - `vp90-2-02-size-08x08`..`vp90-2-02-size-66x66` for edge blocks and
     the chroma clipping of the context derivation (odd sizes exercise the
     clipped `max_x`/`max_y` entries);
   - `vp90-2-08-tile_1x2.webm`, `vp90-2-08-tile_1x4.webm`,
     `vp90-2-08-tile-4x4.webm`, `vp90-2-08-tile-4x1.webm` for tile
     boundaries and the context clear;
   - `vp90-2-16-intra-only.webm` for the intra `tx_type` selection
     (`row`/`col` scans) and `vp90-2-19-skip.webm` for the all-zero
     residual path that feeds `EobTotal`;
   - `vp90-2-14-resize-*` and `vp90-2-21-resize_inter_*` for frames whose
     size changes between coded frames.
3. Encode with `-t 32x32` and a quantizer whose `ac_q` is odd, so a
   negative 32x32 coefficient has an odd magnitude times the quantizer;
   compare against `ffmpeg -f md5`. That is the case R3 distinguishes.
4. `ffprobe -show_frames` on the same file, to compare frame sizes and
   frame count against the MD5 sequence.

## Reference implementations read

Read for behaviour only; no code was copied, ported or transcribed.

- libvpx `main`: `vp9/decoder/vp9_detokenize.c` (the token loop, EOB / zero
  / one nodes, `vp9_pareto8_full`, the token cache, the 32x32 dequant
  shift, the `eob_branch` counting), `vp9/common/vp9_entropy.c`
  (`vp9_coef_con_tree`, category probabilities, `coefband_trans_4x4`,
  `coefband_trans_8x8plus`, `vp9_pt_energy_class`, the full pareto table).
- FFmpeg `master`: `libavcodec/vp9block.c` (`decode_coeffs_b_generic`,
  `decode_coeffs`: token loop, band counters, cache and neighbour arrays,
  `qmul` selection, 32x32 division), `libavcodec/vp9data.c` (scan orders
  and their `_nb` tables).
- Specification sections read in full: 6.4.21-6.4.26, 7.4.15-7.4.17,
  8.4.3, 8.6.1-8.6.2, 9.3.2-9.3.4, 10.1-10.3, 10.5.

## Status

WRITTEN, NOT VERIFIED. Nothing in this directory has been compiled or run;
the blobs are pending and the checks above have not been executed.
