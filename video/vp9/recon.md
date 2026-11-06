# VP9 reconstruction kernels

Status: **WRITTEN, NOT VERIFIED.** Nothing in this directory has been compiled,
run or compared against a decoded frame. There is no generated blob pending as
of this writing.

This directory holds the pixel-domain pieces a VP9 decoder needs after the
header and block syntax are parsed:

| file | spec sections | contents |
|---|---|---|
| `recon_tables.cst` | 4.6, 6.4.10, 6.4.22, 6.4.23, 8.6.1, 8.7.1.1 | constants, 8-bit dc/ac quant tables, cos64 table, block/transform-size derivations, shared Round2/Clip3/Clip1/Abs |
| `transforms.cst` | 8.6.2, 8.7 | 1D inverse DCT 4/8/16/32, inverse ADST 4/8/16, ADST-4 special case, Walsh-Hadamard, 2D composition, dequantisation, residual add |
| `predict.cst` | 8.5.1 | DC/V/H/TM and D45/D63/D117/D135/D153/D207 for 4x4..32x32 |
| `loopfilter.cst` | 8.8 | frame init (LvlLookup), per-edge filter size and strength, masks, narrow/wide filters, superblock traversal |

No block parser, no token scanner, no header reader and no frame allocation
lives here. In particular the caller owns the scan tables and the entropy
decoder: `transforms.cst` expects coefficients in natural (raster) order.

## Public interfaces

### recon_tables

- constants: `TX_*`, `TXT_*`, `BLOCK_*`, `MAX_SEGMENTS`, `MAX_REF_FRAMES`,
  `MAX_MODE_LF_DELTAS`, `MAX_LOOP_FILTER`, `INTRA_FRAME`, `MODE_*`.
- `recon_round2(x, n)`, `recon_clip3(lo, hi, v)`, `recon_clip1(x)`,
  `recon_abs(x)` — spec section 4.6, for BitDepth 8 where clipping applies.
- `recon_dc_quant(b)`, `recon_ac_quant(b)` — `dc_q(b)` / `ac_q(b)` for the
  8-bit row, including the `Clip3(0,255,b)` inside.
- `recon_cos64(angle)`, `recon_sin64(angle)`.
- `recon_num_8x8_wide(bs)`, `recon_num_8x8_high(bs)`, `recon_max_txsize(bs)`,
  `recon_block_from_units(w, h)`, `recon_uv_tx_size(bs, tx_size, subx, suby)`.
  The last is the spec's `get_uv_tx_size`; it short-circuits to TX_4X4 for a
  luma block smaller than 8x8, which is exactly where the spec's
  `ss_size_lookup` entries are BLOCK_INVALID.

### transforms

- `tx_idct(t, n)` — in-place inverse DCT on an already bit-reversed array.
- `tx_iadst(t, n)` — in-place inverse ADST (n = 2..4).
- `tx_iwht4(t, shift)` — in-place 4-point Walsh-Hadamard.
- `tx_2d(dq, n, tx_type, lossless)` — the 8.7.2 composition, row transforms
  then column transforms, in place.
- `tx_dequant(tokens, n, dc_q, ac_q, dq)` — 8.6.2 steps 1 and 2, with the
  spec's 16-bit conformance bound checked (I_CORRUPT on violation).
- `tx_add_residual(dst, stride, dq, n0)` — 8.6.2 step 4, clip to 0..255.
- `tx_reconstruct(tokens, tx_sz, tx_type, lossless, dc_q, ac_q, dst, stride)` —
  the three above in spec order. `dst` is the block's top-left sample after
  prediction, and the transform block is written in place.

`tx_sz` is the spec's TX_* enum (0..3); `n = tx_sz + 2`. `tx_type` is the
spec's DCT_DCT..ADST_ADST. 32x32 with an ADST type, and lossless with anything
other than 4x4 DCT_DCT, return I_UNSUPPORTED: the spec defines no 32-point
ADST and its syntax forces DCT_DCT when Lossless is set.

### predict

```cst
struct IntraEdge { have_left, have_above, not_on_right, max_x, max_y as i64; }
pred_intra(plane, stride, x, y, tx_sz, mode, edge) as i64
```

`x`/`y` are the transform block's top-left sample in the plane's own
coordinates. `max_x`/`max_y` are the last valid sample indices of that plane:
`(MiCols*8)-1` and `(MiRows*8)-1` for luma, shifted by the subsampling for
chroma. `not_on_right` is the spec's `notOnRight`. The block must fit inside
`0..max_x`/`0..max_y`; out-of-range or inconsistent requests return
I_BADPARAM before any sample is read.

The function builds the spec's above/left arrays, fills `pred` and writes the
block; the caller chooses the mode (y_mode, uv_mode or sub_modes[blockIdx]) and
invokes it for every transform block of a prediction block in raster order.

### loopfilter

```cst
struct LfParams   { mi_cols, mi_rows, sub_x, sub_y, level, sharpness,
                    delta_enabled, seg_enabled, seg_abs_or_delta,
                    ref_deltas*, mode_deltas*, seg_alt_l_active*,
                    seg_alt_l_data*; }
struct LfFrame    { mi_cols, mi_rows, sub_x, sub_y, level, sharpness,
                    delta_enabled, lvl_lookup[64]; }
struct LfBlockInfo{ mi_sizes*, tx_sizes*, skips*, segment_ids*,
                    ref_frames*, y_modes*; }
struct LfThresh   { limit, blimit, thresh; }

lf_frame_init(lf, params)                        // 8.8.1, once per frame
lf_filter_plane(lf, blocks, plane, data, stride) // one plane, both passes
lf_filter_frame(lf, blocks, y, u, v, sy, su, sv)
```

`LfBlockInfo` points at arrays the decoder already maintains, one entry per
8x8 mode-info block, row-major `[mi_rows][mi_cols]`. `ref_frames` is
`RefFrames[row][col][0]`; `isIntra` is derived from it being `INTRA_FRAME`,
rather than being a separate array that could disagree. `tx_sizes` is the luma
transform size; the chroma size comes from `recon_uv_tx_size`.

`lf_filter_plane` walks superblocks in raster order and runs pass 0 (vertical
boundaries) then pass 1 (horizontal boundaries) for each superblock, which is
the order section 8.8's note requires. `lf_filter_frame` runs the planes one
after another: planes never read each other, so this is equivalent to the
spec's plane-inside-superblock loop.

## Fixed point and scaling

- **1D transforms**: every butterfly multiplies by `cos64`/`sin64`, i.e.
  round(16384*cos(angle*pi/64)), and rounds with `Round2(x, 14)`. `Round2` is
  `(x + (1<<(n-1))) >> n` with an arithmetic shift, so negative intermediates
  round toward minus infinity exactly as the spec's 2's-complement shift does.
- **2D transform**: row outputs are written back without an extra shift;
  column outputs are rounded by `Min(6, n+2)` = 4, 5, 6, 6. Lossless is the
  exception: row WHT shift 2, column WHT shift 0, and no final rounding.
- **Intermediates**: T is stored as i32 (the spec bounds it to 8+BitDepth =
  16 bits). All products and the ADST's S array are computed in i64 so no
  step depends on wrapping; S is bounded by 24+BitDepth = 32 bits by the spec.
- **Dequantisation**: `(token * q) / dqDenom` with truncation toward zero
  (spec 4.1), and the 16-bit conformance bound is checked.
- **Prediction**: `Round2` with n = 1 or 2 and `Clip1` for TM; values are
  written back as u8.
- **Loop filter**: BitDepth 8, so `limitBd == limit`, `blimitBd == blimit`,
  `threshBd == thresh` and the narrow filter's signed domain is -128..127 with
  the 0x80 offset. The wide filter's weights sum to 2^log2Size, so its output
  needs no clip.

## Table provenance

- `dc_qlookup[0][0..255]` and `ac_qlookup[0][0..255]`: the 256 decimals the
  spec prints in 8.6.1, stored as a comma-separated text blob and decoded by
  `recon_qvalue`. Storing the decimals rather than bytes keeps the source
  auditable against the spec; the two tables were compared entry by entry with
  the specification text and match. Only the BitDepth 8 row is carried.
- `cos64_lookup[0..32]`: the 33 values from 8.7.1.1 as 2-byte little-endian
  words (66 bytes). `recon_cos64` implements the spec's four-way reflection,
  and `recon_sin64(a) = recon_cos64(a + 96)` (the same as cos64(a - 32), with
  the argument kept non-negative).
- `SINPI_1_9..SINPI_4_9`: 8.7.1.6 constants, written in source.
- Block/transform geometry: the spec names `num_8x8_blocks_wide_lookup`,
  `num_8x8_blocks_high_lookup`, `max_txsize_lookup` and `ss_size_lookup` but
  prints only names and block triples. `recon_num_8x8_*` and
  `recon_max_txsize` are the named tables written as conditionals over the 13
  block sizes; `recon_uv_tx_size` reproduces `ss_size_lookup` geometrically
  (`(w >> subx, h >> suby)`, at least 1x1), which is exact for every reachable
  case and avoids a 52-entry copy.
- Loop filter: **no table**. 8.8.3 and 8.8.4 compute filter size, limit,
  blimit and thresh; the masks are per-sample comparisons. The libvpx
  `lfthr`/`lf_limits` arrays are a cache of those formulas.

## Ambiguities and errata

All items below were resolved by reading the spec against the reference
implementations; until a decoded frame is compared they remain UNCONFIRMED.

1. **UNCONFIRMED — no intra edge filter and no 32x32 upsampling.** The stage
   brief mentioned both. VP9 8.5.1 contains neither: aboveRow/leftCol are used
   directly for every size, and both references do the same (libvpx
   `vp9_predict_intra_block`/`build_intra_predictors`; FFmpeg
   `check_intra_mode` + `intra_pred`). Intra edge filtering and directional
   upsampling belong to AV1. `predict.cst` therefore implements 8.5.1 only.
2. **UNCONFIRMED — TxType orientation.** Section 3 defines ADST_DCT as "rows
   DCT and columns ADST" and 8.7.2 selects rows-DCT for DCT_DCT and ADST_DCT.
   libvpx's `transform_2d` is declared `{cols, rows}`, so its `{iadst, idct}`
   entry for ADST_DCT is the same thing. The two references agree; the row and
   column selection in `tx_2d` follows the spec text literally.
3. **UNCONFIRMED — loop filter `delta_enabled` vs `delta_update`.** 8.8.1
   step 3 tests `loop_filter_delta_update` and step 4 tests
   `loop_filter_delta_enabled`. Reading 6.2.8, `delta_update` only controls
   whether new delta values are read; whether the deltas are applied is
   `delta_enabled`. `LfParams.delta_enabled` is that one flag: 0 fills every
   `LvlLookup` entry for the segment with `lvlSeg`, 1 applies the deltas.
4. **UNCONFIRMED — LvlLookup[segment][INTRA_FRAME][1].** 8.8.1 writes only
   `[INTRA_FRAME][0]` when deltas are enabled (modeType is 0 for every intra
   mode). The entry is left at 0 here, as the spec leaves it unwritten; a
   conforming bitstream never reads it. A corrupt stream that pairs
   INTRA_FRAME with an inter mode will simply get no filtering for that block.
5. **UNCONFIRMED — wide filter tap indexing.** The spec's absolute sample
   offsets are p_k = -(k+1), q_k = +k. With that substitution the loop in
   8.8.5.3 reproduces libvpx's `filter8`/`filter16` weights exactly (for
   example `3*p3 + 2*p2 + p1 + p0 + q0` for the first p output of the 8-wide
   filter), and the modified range is p_(n-1)..q_(n-1). Implemented literally.
6. **UNCONFIRMED — luma 32x32 has no ADST.** 8.7.1.9 defines the ADST only
   for n <= 4; `tx_reconstruct` returns I_UNSUPPORTED for 32x32 with an ADST
   type instead of silently applying a 16-point ADST to 32 samples.
7. **UNCONFIRMED — lossless forces DCT_DCT and 4x4.** 6.2.9 and the syntax
   force TxType = DCT_DCT and a 4x4 transform when Lossless is set; the
   reconstruct process checks Lossless before looking at TxType, so the
   combination is rejected as I_UNSUPPORTED rather than silently using the
   wrong 2D path.
8. **UNCONFIRMED — bit depth.** Only BitDepth 8 exists here. Profiles 0, 1 and
   3 are 8-bit and are representable; a profile 2 (10/12-bit) frame cannot be
   stored in `core/frame.cst`'s I420 layout and must be rejected by the
   decoder before these kernels are called. The quant tables therefore carry
   only `qlookup[0]`.
9. **UNCONFIRMED — division.** All `/` in the dequantiser follows spec 4.1
   (truncation toward zero), which is also Caustic's integer division.
10. **UNCONFIRMED — level-zero frame gate.** Section 8.1 invokes the loop
    filter only when `loop_filter_level` is non-zero, before 8.8.1 runs.
    `lf_filter_plane` therefore returns immediately when `LfFrame.level` is 0,
    even if a segment's ALT_L feature data would otherwise produce a positive
    level. libvpx's `vp9_loop_filter_frame` has the same early return.

## What the later integration must supply

- The scan/EOB decoder and the coefficient buffer in natural order;
  `tx_reconstruct` does not unscramble scans.
- `tx_type` from the intra mode / inter transform-type syntax, with 32x32
  forced to DCT_DCT.
- `dc_q`/`ac_q` for the plane from `recon_dc_quant`/`recon_ac_quant` plus the
  segment qindex and the dc/ac deltas (spec 8.6.1's `get_dc_quant` /
  `get_ac_quant`, which are the caller's because they need segment state).
- Per-block availability for prediction: `haveLeft`, `haveAbove`,
  `notOnRight`, and `maxX`/`maxY` derived from MiCols/MiRows and subsampling.
- The per-mi arrays for the loop filter (`LfBlockInfo`), and `LfParams` from
  the frame header, including persistent ref/mode deltas and segment ALT_L
  feature data.
- Profile rejection for 10/12-bit (I_UNSUPPORTED) before any kernel.

## References read

Behaviour was cross-checked against (read online, not copied):

- webmproject/libvpx `main`:
  `vp9/common/vp9_reconintra.c`, `vp9/common/vp9_idct.c`,
  `vpx_dsp/inv_txfm.c`, `vp9/common/vp9_loopfilter.c`,
  `vpx_dsp/loopfilter.c`, `vp9/common/vp9_idct.h`.
- FFmpeg `master`: `libavcodec/vp9recon.c`, `libavcodec/vp9dsp_template.c`,
  `libavcodec/vp9lpf.c`, `libavcodec/vp9dsp.c`.

No external code was copied, ported or transcribed; the implementations above
are written from the specification, and the references were used only to
confirm what the spec text implies (the items listed as UNCONFIRMED).

## Pending work

- No table blob is pending generation. The two quant tables and the cosine
  table are committed source in `recon_tables.cst`; there is no generator to
  run, because the data is written directly from the spec and is small enough
  to audit in place.
- Nothing has been compiled or executed. The next step is the stage-4
  integration writer, then a reviewer reading against 8.5.1, 8.6, 8.7 and 8.8.
