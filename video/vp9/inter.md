# VP9 inter prediction and motion-vector prediction

Status: **WRITTEN, NOT VERIFIED.** Nothing in this directory has been
compiled, run or compared against a decoded frame. No table is pending
generation.

This directory holds the inter-prediction half of the VP9 decoder: the
motion-vector prediction syntax of section 6.5 and the sample prediction of
section 8.5.2. It reads no bitstream, allocates nothing, and owns no state
between calls. The block decoder (a later stage) drives both modules from the
mode-info grid it already keeps.

| file | spec sections | contents |
|---|---|---|
| `inter_tables.cst` | 3, 6.4.23, 8.5.2.4, 6.5.1, 6.5.11 | constants, the four subpel filters, `mv_ref_blocks`, `mode_2_counter`, `counter_to_context`, `idx_n_column_to_subblock`, block/plane geometry |
| `mvpred.cst` | 6.5.1-6.5.14 | MV structure, candidate list, `find_mv_refs`, `find_best_ref_mvs`, `append_sub8x8_mvs`, and the helpers section 6.5 names explicitly |
| `inter.cst` | 8.5.2-8.5.2.4 | MV selection, clamping, reference scaling, the two-pass 8-tap filter, single/compound prediction, plane-block loop |

The specification used is `.work/vp9/spec/clean.txt` (VP9 Bitstream &
Decoding Process Specification v0.6). The reference decoders were read for
research only; no code was copied, ported or transcribed: libvpx
`vp9/common/vp9_reconinter.c` and `vp9/common/vp9_mvref_common.c`, FFmpeg
`libavcodec/vp9mvs.c` and `libavcodec/vp9recon.c`.

## Interfaces

### mvpred

```cst
struct MvRefCtx {
    mi_rows, mi_cols, mi_col_start, mi_col_end, mi_row, mi_col, mi_size as i64;
    use_prev_frame_mvs as i64;
    ref_sign_bias   as *u8;    // [MAX_REF_FRAMES], indices 1..3
    y_modes         as *u8;    // [mi_rows][mi_cols]
    ref_frames      as *u8;    // [mi_rows][mi_cols][2]
    mvs             as *i32;   // [mi_rows][mi_cols][2][2]
    sub_mvs         as *i32;   // [mi_rows][mi_cols][2][4][2]
    prev_ref_frames as *u8;    // [mi_rows][mi_cols][2]
    prev_mvs        as *i32;   // [mi_rows][mi_cols][2][2]
}
struct MvCandList { count as i64; row as [2]i64; col as [2]i64; }
struct MvSet      { nearest_row, nearest_col, near_row, near_col,
                    best_row, best_col as i64; }
```

`Mv` (declared in `inter_tables.cst`) is the shared `{ row, col }` pair in
1/8 luma samples; the candidate and result structs above keep their
components as scalar i64 fields so no struct field names another module's
struct type, which the Caustic reference calls out as a limitation. A caller
that wants the pair type can hold it as a local variable under whatever
alias it gave `inter_tables.cst`.

Public entry points, all returning `I_OK` or a negative code:

```cst
mv_find_refs(ctx, ref_frame, block, list, mode_ctx)              // 6.5.1
mv_find_best_ref_mvs(ctx, allow_hp, list, set)                   // 6.5.12
mv_append_sub8x8_mvs(ctx, ref_frame, block, ref_list,
                     block_mvs, set, mode_ctx)                   // 6.5.14
```

Helpers carrying the specification's names, usable by the block decoder and
by tests of the process in isolation:

```cst
mv_inside(ctx, candidate_r, candidate_c)                         // 6.5.2
mv_clamp_row(ctx, mvec, border)                                  // 6.5.4
mv_clamp_col(ctx, mvec, border)                                  // 6.5.5
mv_clamp_ref(ctx, list)                                          // 6.5.3
mv_add_ref_list(list, row, col)                                  // 6.5.6
mv_if_same_ref(ctx, r, c, ref_frame, use_prev, list)             // 6.5.7
mv_if_diff_ref(ctx, r, c, ref_frame, use_prev, list)             // 6.5.8
mv_scale_mv(ctx, cand_frame, ref_frame, row, col, &orow, &ocol)  // 6.5.9
mv_get_block_mv(ctx, r, c, ref_list, use_prev,
                &row, &col, &frame)                              // 6.5.10
mv_get_sub_block_mv(ctx, r, c, ref_list, delta_col, block,
                    &row, &col)                                  // 6.5.11
mv_use_hp(delta_row, delta_col)                                  // 6.5.13
```

`block` is the 4x4 index 0..3 inside a sub-8x8 luma block, or -1 for the
whole-block `find_mv_refs(ref_frame[j], -1)` call of 6.4.16. `ref_frame` is
LAST_FRAME, GOLDEN_FRAME or ALTREF_FRAME.

The helpers are exposed for callers and for isolated checks of one step.
`mv_clamp_row`, `mv_clamp_col`, `mv_clamp_ref`, `mv_add_ref_list` and
`mv_scale_mv` assume a context that `mv_ctx_ok` has already accepted (the
three entry points run that check and validate their own arguments); every
other public function validates whatever it is given.

`mv_find_refs` always fills both candidate slots (unused ones stay at zero)
and reports the real count in `list.count`; `mode_ctx` is
`counter_to_context[contextCounter]`, the mode context section 9.3 uses for
inter mode. Both candidates are clamped with `MV_BORDER` before return.

`block_mvs` in `mv_append_sub8x8_mvs` is the current block's `BlockMvs`,
laid out `[refList][4][2]`, row then column, i32.

### inter

```cst
struct InterFrame {
    width, height as i64;    // FrameWidth / FrameHeight, luma samples
    sub_x, sub_y as i64;     // subsampling_x / subsampling_y
    mi_rows, mi_cols as i64;
    mi_row, mi_col as i64;
    mi_size as i64;          // BLOCK_*
    filter as i64;           // resolved interp_filter, 0..3
}
struct InterRefPlane { data as *u8; stride, width, height as i64; }
struct InterPos      { start_x, start_y, step_x, step_y as i64; }

inter_load_filter(filter, taps)                                  // 128 i64
inter_mv_select(f, plane, ref_list, block_idx, block_mvs,
                &row, &col)                                      // 8.5.2.1
inter_mv_clamp(f, plane, mv_row, mv_col, &row, &col)             // 8.5.2.2
inter_mv_scale(f, plane, x, y, mv_row, mv_col, ref_w, ref_h,
               &sx, &sy, &stx, &sty)                             // 8.5.2.3
inter_block_pred(taps, ref, plane, sub_x, sub_y, pos, w, h,
                 pred, pred_stride, scratch)                     // 8.5.2.4
inter_predict(f, plane, refs, ref_count, x, y, w, h, block_idx,
              block_mvs, out, out_stride, scratch)               // 8.5.2
inter_predict_taps(f, plane, refs, ref_count, x, y, w, h,
                   block_idx, block_mvs, taps, out, out_stride,
                   scratch)                                      // same, taps loaded
inter_predict_block(f, plane, refs, ref_count, x, y, block_mvs,
                    out, out_stride, scratch)                    // 6.4.21 caller
```

`refs` points at `ref_count` (1 or 2) `InterRefPlane` entries, one per
reference list, already resolved through `ref_frame[]` and `ref_frame_idx[]`.
`width`/`height` are `RefFrameWidth`/`RefFrameHeight` for scaling; `data` and
`stride` are the plane's own pointer/stride.

`out` points at the first sample of the region being predicted (not at the
plane origin), and `out_stride` is that plane's row stride. `scratch` is
caller-owned memory of at least `INTER_SCRATCH_BYTES` = 20672 bytes (323 rows
by 64 samples, the worst case of 8.5.2.4's intermediate array at h = 64 and
yStep = 80). Nothing else allocates; a compound block's second prediction is
held in a 4 KiB local array inside `inter_predict_taps`, and the intermediate
array lives in `scratch`.

The plane buffers must cover the mode-info grid, `MiCols*8` by `MiRows*8`
luma samples, because that is the extent 6.4.21 predicts over, including the
coded padding to the right and bottom of an odd-sized frame. `core/frame.cst`
provides exactly this separation through `coded_width`/`coded_height`.

`inter_predict_block` mirrors the call site in 6.4.21:

- plane block size from `get_plane_block_size(MiSize, plane)` (implemented by
  `it_plane_block_dims`);
- `MiSize >= BLOCK_8X8`: one prediction over the whole plane block,
  `blockIdx` 0;
- `MiSize < BLOCK_8X8`: one 4x4 prediction per 4x4 piece, with
  `blockIdx = y * num4x4w + x` inside the plane block.

`x`/`y` are the plane coordinates of the block's top-left sample
(`baseX`/`baseY` in 6.4.21).

## Conventions

- **Units.** A stored MV component is 1/8 luma sample (7.4.13), including the
  extra high-precision bit when `allow_high_precision_mv` is set. 8.5.2.2
  converts to the plane's 1/16 samples with `(2*mv) >> subsampling`.
  Everything in `InterPos` is 1/16 samples.
- **Arithmetic shift.** `>>` is the specification's two's-complement
  arithmetic shift, so a negative value floors toward minus infinity; this is
  what makes `Round2(x, n)` the biased rounding of section 4.6 and what makes
  `Clip3(0, last, (p >> 4) + ...)` select the edge sample for a negative
  start position.
- **Phase mask.** `p & 15` on a negative `p` yields the low four bits
  (0..15), the two's-complement phase the specification means. The builds are
  written as `p & 15` rather than a modulo, whose sign would differ.
- **Rounding.** `Round2(x, n) = (x + (1 << (n-1))) >> n`. The two filter
  passes and the compound average use it; the MV chroma averages use
  `round_mv_comp_q2/q4`, whose `/` truncates toward zero after the sign
  adjustment, exactly as 8.5.2.1 writes it.
- **Storage.** MV components in the mode-info grid are i32 (values are
  bounded by the MV syntax and clamping); all arithmetic widens to i64.
  Intermediate filtered samples are stored as u8 after `Clip1`, which is
  exact because `Clip1` bounds them to 0..255.
- **Bit depth.** 8-bit only. `core/frame.cst`'s I420 layout cannot hold
  10/12-bit samples and the header parser refuses profile 2 before these
  kernels are reached.
- **No `bool`.** Flags (`use_prev_frame_mvs`, `allow_hp`, `different`,
  `is_compound`, ...) are i64 0/1 and validated when they enter from outside.

## What 8.5.2 does not need

- Profiles 1, 2 and 3: 4:2:2, 4:4:4 and high bit depth are outside this
  library's I420 8-bit frame. The functions accept all four VP9 subsampling
  combinations because the specification's arithmetic is written for them,
  but the decoder only ever supplies 4:2:0.
- Interpolation filter `SWITCHABLE` at block level: `inter_load_filter`
  returns `I_UNSUPPORTED`; the caller resolves the block's `interp_filter`
  first (6.4.16).
- Alpha, auxiliary frames and superframe framing: not inter prediction.

## Error contracts

- `I_BADPARAM`: null pointer, a block or tile index outside its array, a
  plane outside 0..2, a block size outside `BLOCK_4X4..BLOCK_64X64`, a region
  that does not fit the mode-info grid, or a stride narrower than the region.
- `I_DIMS`: a region wider/taller than 64 samples, or a scaling step above
  the specification's maximum of 80.
- `I_UNSUPPORTED`: `SWITCHABLE` passed as a tap set.
- `I_CORRUPT`: the four reference-size conformance inequalities of 8.5.2.3
  fail, or a stored reference-frame value is outside
  `INTRA_FRAME..ALTREF_FRAME`.
- Nothing is written to an out parameter, and no sample is modified, before
  the arguments, the block geometry and every reference plane have been
  validated. In `mvpred`, a corrupt neighbour array discovered part-way
  through a search leaves the candidate list partially filled; the caller
  discards it along with the frame.

## Table provenance and pending values

Nothing is generated and nothing is pending. Every table in
`inter_tables.cst` was typed in source from the specification and re-read row
by row against it when this module was completed:

- `subpel_filters[4][16][8]` (8.5.2.4) as four comma-separated blobs, 128
  entries each; negative taps are kept as printed, so the source can be
  diffed against the spec text;
- `mv_ref_blocks[13][8][2]` (6.5.1);
- `mode_2_counter[14]` and `counter_to_context[19]` (6.5.1), with the mode
  context values of section 3 and `INVALID_CASE` as 9;
- `idx_n_column_to_subblock[4][2]` (6.5.11);
- `num_8x8_blocks_wide_lookup` and `num_8x8_blocks_high_lookup` (10.2),
  `wide = {1,1,1,1,1,2,2,2,4,4,4,8,8}` and
  `high = {1,1,1,1,2,1,2,4,2,4,8,4,8}`. An earlier grouping of this file
  inherited from the stage 4 branch was off by one group for the rectangular
  sizes (for example BLOCK_8X4 came out two units wide); it is corrected in
  the inter slice, and stage 4 carries its own fix.

The blobs are parsed by `it_blob_load`, which bounds-checks the walk and
returns `I_CORRUPT` on a short or malformed literal. No `tools/` generator is
needed for this module: there is no binary data whose source of truth is
outside this source file.

## Ambiguities and errata

All items were resolved between the specification text and the two reference
decoders named above. Until a decoded frame is compared they remain
**UNCONFIRMED**.

1. **UNCONFIRMED - chroma MV precision conversion.** 8.5.2.2 writes
   `(2 * mv[0]) >> sy`. For a negative odd input this is a floor shift, not
   truncation toward zero. libvpx's `clamp_mv_to_umv_border_sb` multiplies by
   `1 << (1 - ss)` (the same doubling for 4:2:0) and clamps in q4, agreeing in
   precision; FFmpeg likewise scales the MV by 2 before its scaled path. The
   shift is implemented literally.
2. **UNCONFIRMED - chroma MV averaging indices.** For a sub-8x8 block whose
   chroma plane is subsampled, 8.5.2.1 averages `blockIdx` with
   `blockIdx + 2` (subsampling 0,1) or `blockIdx + 1` (1,0), and all four
   luma vectors for 4:2:0 regardless of `blockIdx`. libvpx's
   `average_split_mvs` uses the same pairs; the note after the process
   explains the bottom 4:2:2 chroma piece. Implemented literally.
3. **UNCONFIRMED - base and fractional position.** 8.5.2.3 computes `baseX`
   from the plane coordinate `x` but `fracX` from the luma coordinate
   `lumaX`. FFmpeg's scaled path recomputes the equivalent constants and
   comments that libvpx scales the two components separately; the
   specification's form is implemented as written.
4. **UNCONFIRMED - compound averaging.** `Round2(preds[0] + preds[1], 1)`
   with both predictions already `Clip1`-bounded; no second clip exists in
   the specification and none is added.
5. **UNCONFIRMED - edge clamping order.** 8.5.2.4 clamps the reference row
   and column with `Clip3(0, lastY/lastX, ...)` in the horizontal pass and
   does not clamp the vertical pass at all, relying on
   `intermediateHeight`. Both references emulate the edge horizontally and
   vertically before filtering; for a conforming start position the results
   are identical. The intermediate array is sized from
   `((h-1)*yStep + 15) >> 4) + 8`, as the specification defines it.
6. **UNCONFIRMED - fractional part and step after scaling.** `stepX` is
   `(16*xScale) >> 14` and `fracX` is the low four bits of the scaled luma
   position; the start is `(baseX << 4) + dX`. This is exactly 8.5.2.3; the
   conformance inequalities are enforced as `I_CORRUPT` (FFmpeg reports
   invalid data for an invalid reference scale).
7. **UNCONFIRMED - `find_best_ref_mvs` low-bit lowering.** 6.5.12 moves an
   odd candidate toward zero (`deltaRow += deltaRow > 0 ? -1 : 1`) and
   `use_mv_hp` compares `Abs(component) >> 3` with
   `COMPANDED_MVREF_THRESH`. libvpx's `lower_mv_precision` is the same
   operation; the two's-complement `& 1` test is used.
8. **UNCONFIRMED - `append_sub8x8_mvs` order.** 6.5.14 fills `NearestMv`
   from `BlockMvs[refList][2]` for block 3 and then scans
   `BlockMvs[refList][1]`, `BlockMvs[refList][0]` before the reference-list
   candidates. libvpx and FFmpeg both carry a slightly different candidate
   scan here; the specification's order is implemented, since the
   specification is primary and a conformance vector is what would settle it.
9. **UNCONFIRMED - `mvsSame` before the intra test.** 6.5.8 loads both
   candidates and compares their vectors before checking whether either is
   intra. FFmpeg's copy of this logic notes in a comment that it reproduces a
   libvpx quirk; the specification's literal order is implemented.
10. **UNCONFIRMED - `scale_mv` is a sign flip, not a scaled difference.**
    VP9 never scales the vector magnitude between references; it only
    negates both components when the sign biases differ. Both references
    agree.
11. **UNCONFIRMED - mode context counts only the first two neighbours.**
    6.5.1 adds `mode_2_counter[YModes]` only in the `i < 2` loop; the other
    six neighbours set `differentRefFound` but do not contribute.
12. **UNCONFIRMED - zero-initialised neighbour arrays.** 6.5 reads
    `Mvs`/`SubMvs` for both reference lists before deciding which one to use,
    so the caller must clear the mode-info arrays at frame start (including
    entries of intra blocks). This is the specification's array semantics;
    a caller that leaves them uninitialised can change 6.5.8's `mvsSame`
    result without changing any syntax.
13. **UNCONFIRMED - no intra edge filter or upsampling here.** As recorded in
    `recon.md` for 8.5.1, VP9 inter prediction also has neither; the
    horizontal pass is the only smoothing.
14. **UNCONFIRMED - scratch sizing is an implementation choice.** The
    specification's intermediate has no fixed bound; 323x64 is the exact
    maximum for the block and step limits the specification states
    (`h <= 64`, `yStep <= 80`). A larger step or block is rejected with
    `I_DIMS` rather than overrunning the caller's buffer.

## Open review notes

- This branch is based on `main` and does not carry stage 4's
  `recon_tables.cst`, so `inter_tables.cst` repeats `Round2`, `Clip3`,
  `Clip1`, `Abs` (section 4.6) rather than importing them. When the branch is
  rebased onto the reconstruction stage, decide whether to keep that
  self-contained copy or alias `rt.recon_*`; the definitions are identical,
  but having two copies is a style question, not a behavioural one.
- Function names follow the specification (`find_mv_refs`, `clamp_mv_ref`,
  ...) with a module prefix; the codec's house style elsewhere prefixes the
  module (`pred_`, `lf_`, `tx_`). A reviewer may prefer `mvpred_*` or
  `inter_*` throughout.
- `MvSet.best_row`/`best_col` are set from the first slot by
  `mv_append_sub8x8_mvs`, which 6.5.14 does not define. It lets one struct
  travel through the caller; a reviewer may prefer a separate type for the
  append result.
