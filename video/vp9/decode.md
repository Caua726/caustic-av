# VP9 frame decode: block syntax, tiles and the frame driver

Stages 4-6 of the VP9 decoder. It turns the header state produced by stage 3
into reconstructed 8-bit I420 surfaces: partition tree, mode info, intra and
inter prediction, coefficients, inverse transform, residual add, loop filter,
output crop, show-existing display and reference publication. A packet of
1..8 slices is consumed through `vp9_decoder_send`/`vp9_decoder_receive`, and
each displayed slice is returned once, in bitstream order.

Status: **WRITTEN, NOT VERIFIED.** Nothing in this directory has been
compiled or run. The MD5 / `ffprobe` / `trace_headers` checks named by the
stage briefs are recorded for the test phase and have not been executed.

> **Stage 4 record.** This note describes the stage 4 driver commit, not the
> merged stage 5 decoder. `decode.cst` has since gained the inter path: it
> no longer rejects inter frames, takes no per-call `mem`/`cap` (it selects
> a buffer registered with `vp9_decoder_add_buffer`), stores two
> `RefFrames` per mode-info unit, and publishes pixels through `refs.cst`
> instead of `vp9_state_publish` plus `vp9_state_save_probs`. Read `vp9.md`
> for the current driver, ownership and per-slice order. Statements below
> about inter rejection, the caller's single output surface and the
> one-byte `RefFrames` apply only to the stage 4 commit.

## Files

| File | Holds |
|---|---|
| `video/vp9/block.cst` | Block geometry helpers not in recon_tables (partition subsize, log2 of a count, plane block size), the partition tree of 6.4.3, intra-frame mode info 6.4.4-6.4.10, residual orchestration 6.4.21, coefficient-context clearing and writing. |
| `video/vp9/tile.cst` | Tile offsets 6.4.1, one Boolean decoder per tile, superblock traversal 6.4.2, per-tile error propagation. |
| `video/vp9/decode.cst` | `Vp9Decoder`, the caller-owned work layout, packet send/receive handoff, `Vp9Output`, coded-surface and reference-pool selection, show-existing output, the frame driver through the loop filter, segment-map update, `vp9_refs_publish` and `refresh_frame_context` save. |
| `video/vp9/residual.cst`, `residual_tables*.cst` | Adopted from the stage 4 residual slice (spec 6.4.24-6.4.26); table blobs are still pending. |
| `video/vp9/{transforms,predict,loopfilter,recon_tables}.cst`, `recon.md` | Fixed reconstruction kernels adopted from `stage4-recon-fix` HEAD `54dd904`. |

## Call graph

```
vp9_decoder_init            zero + vp9_state_init
vp9_decoder_bind            attach persistent work block
vp9_decoder_send            vp9_superframe_split, validate 1..8 slices,
                            store pointers; refuses while a packet is armed
vp9_decoder_receive         per receive, decode slices until one is shown:
  _decode_slice
    vp9_header_parse
    if show_existing_frame  refs.vp9_refs_show_existing
                            _out_from_slot -> return 1
    reject pending blobs / non-8-bit / no compressed header
    inter: vp9_refs_check_header + 7.2.5 ratio check
    frame_size + _pick_buffer   capacity, not in use, publishable
    frame_alloc_coded + zero the coded surface
    _work_carve             carve arrays, vp9_coef_ctx_init
    clear Mvs/SubMvs/SubModes  per frame (6.4.4, 9.3.2 default)
    zero PrevSegmentIds     intra / error-resilient / size change (7.2, 7.2.6)
    tile_decode_all
      blk_clear_above       once per frame (7.4.1)
      per tile: vp9_bool_init
        _decode_tile
          blk_clear_left    once per 8x8 row (7.4.2)
          blk_decode_partition
            _partition_ctx + _read_partition (9.3.2, errata E1)
            _decode_block
              _mode_info    segment id, skip, tx size, intra/inter modes
              _residual     pred_intra/predict_inter; vp9_residual_decode;
                            tx_2d; tx_add_residual; nonzero contexts
        vp9_bool_exit
    vp9_adapt_frame         load saved context, adapt, save (6.1.2)
    _loop_filter            lf_frame_init + lf_filter_frame (8.8)
    _update_seg_map         PrevSegmentIds (8.1 step 3)
    _update_prev_mv         PrevRefFrames/PrevMvs (8.10 step 2)
    refs.vp9_refs_publish   reference views + Vp9State.ref_* (8.10 step 1)
    if show_frame           fill Vp9Output -> return 1
                            else return 0 and continue
```

`block.cst` never decodes a tile or allocates; `tile.cst` owns the
per-tile Boolean decoder and the context lifetimes; `decode.cst` owns the
packet, the surfaces, the output record and the frame order; `refs.cst`
owns the eight slot views and all show-existing reads.

## Ownership and sizes

- `Vp9Decoder` holds the persistent `Vp9State`, the parsed `Vp9Header`, and
  pointers to the caller's work block. The caller allocates the decoder and
  keeps it for the stream's lifetime.
- `vp9_decoder_init` must precede `vp9_decoder_bind` and
  `vp9_decoder_decode`: it zeroes the object and installs the persistent
  state, and `vp9_decoder_decode` cannot tell an uninitialised decoder from
  an initialised one. `vp9_decoder_bind` only requires a non-null pointer
  and a positive capacity; a block that is too small, misaligned or has no
  room for a later frame's layout is accepted there and rejected by
  `_work_carve` at the first decode (`I_NOMEM` with `work_need`, or
  `I_BADPARAM` for a misaligned base).
- The work block is caller-owned and must stay the same block across frames
  because it holds both the per-frame arrays and `PrevSegmentIds`.
  `vp9_work_size(width, height)` gives
  the bytes for a decoder that will see frames up to that visible size; it
  is a pure layout computation, no allocation. The block must be 8-byte
  aligned. The stage 4 layout was under 1 GiB; once stage 5's `Mvs`,
  `SubMvs` and `PrevMvs` arrays are included the 8192x8192 ceiling is about
  6.9 GiB (7,449,243,936 bytes), with those three arrays alone accounting
  for 6 GiB and the per-mode-info byte arrays about 0.94 GiB.
- The pixel buffers are caller-owned and registered up front with
  `vp9_decoder_add_buffer`. `frame.frame_size(coded_w, coded_h)` bytes are
  required per frame; when no free buffer is large enough the return is
  `I_NOMEM` and `d.frame_need` holds the requirement.
- The packet passed to `vp9_decoder_send` stays the caller's. The decoder
  stores its slice addresses and lengths and reads them until
  `vp9_decoder_receive` returns 0; the bytes must not move or change before
  then. Sending a second packet while the first is armed is `I_BADPARAM`.
- `Vp9Output.frame` is a view into a pool buffer. It stays valid until the
  next `receive`/`send` pair, unless the frame was published as a reference,
  in which case every slot that names it keeps the pixels alive. The decoder
  never copies a display surface and never returns a placeholder view.
- Coded size: `align64(MiCols*8) + 64` by `align64(MiRows*8) + 64`. The
  extra 64-sample band is the padding the fixed `pred_intra` writes when a
  transform block overhangs `MiCols*8`/`MiRows*8`, and the loop filter reads
  its neighbours from it. Visible size is `h.width`/`h.height`; the output
  is cropped by those fields (strides remain the coded strides).
- Because `core/frame.cst` caps a plane dimension at 65536, the largest
  visible width or height this driver accepts is 65472 (coded 65536); a
  larger frame returns `I_DIMS`. This is a surface limit, not a VP9 limit.

## Per-frame arrays

All per-mode-info arrays are one byte per 8x8 unit, row-major with stride
`MiCols`: `MiSizes`, `TxSizes`, `Skips`, `SegmentIds`, `YModes`, plus four
sub-modes per unit (`SubModes`). This stage 4 note predates the two-list
`RefFrames` of stage 5, which carries two bytes per unit; see `vp9.md`.
Writes for a block that overhangs the frame are clipped to
`MiCols`/`MiRows`, exactly like libvpx's
`x_mis`/`y_mis`; every later reader only indexes valid mode-info units.

Partition and segment-prediction contexts are padded to the superblock grid
(`Sb64Cols*8`, `Sb64Rows*8`) because 6.4.3 reads and writes them beyond
`MiCols`/`MiRows` for the block that crosses the edge. The coefficient
contexts are plane-major with strides `2*Sb64Cols*8 + 16` and
`2*Sb64Rows*8 + 16`, so the 4x4 write-back of an overhanging transform
block stays inside the array; writes past `cc.max_x`/`cc.max_y` are dropped,
as every read clamps to those limits.

## Block syntax notes

- Partition probability: `header.vp9_partition_prob` implements errata E1
  (intra frames use `kf_partition_probs`). Forced partitions (no rows / no
  columns) read only the probability the spec's `cols_partition_tree` /
  `rows_partition_tree` selects, and are counted like any other partition.
- Tx size: `max_txsize_lookup` from `recon_max_txsize`; the select tree reads
  0/1/2 nodes with `tx_probs[maxTx][ctx][node]` and counts
  `counts.tx[maxTx][ctx][tx_size]`. A block below 8x8 never selects.
- Skip context is `Skips[r-1][c] + Skips[r][c-1]` gated by `AvailU`/`AvailL`;
  a `SEG_LVL_SKIP` feature suppresses the read and the count.
- Intra mode: single `y_mode` for `MiSize >= BLOCK_8X8` from
  `kf_y_mode_probs[above][left]`; sub-8x8 blocks read one mode per 4x4
  group with the 9.3.2 above/left rules, and `uv_mode` uses
  `kf_uv_mode_probs[y_mode]`. Keyframe mode reads are not counted
  (`default_intra_mode`/`default_uv_mode` are NA in 9.3.4).
- Residual: `bsize` is clamped to at least `BLOCK_8X8`; per plane the
  transform block walks only while its top-left is inside
  `(MiCols*8)>>subX`/`(MiRows*8)>>subY`. Every transform block gets a
  prediction, a token decode unless `skip`, and a coefficient-context write
  (0 for a skipped block). `EobTotal` is the sum of the returned `nonzero`
  flags; it only feeds the inter skip rule, which intra frames never take.
- The quantizer index is `get_qindex()` (base or segmentation ALT_Q,
  clipped to 0..255); `Vp9Quant` carries it and the three header deltas.

## Tables

No new generated table is introduced by this slice. Two blobs remain
pending and are checked before any tile is decoded:

| Blob | Generator | Status |
|---|---|---|
| `VP9_TBL_INV_MAP`, `VP9_TBL_KF_PROBS`, `VP9_TBL_DEFAULT_PROBS` | `tools/gen_vp9_tables.py` | pending (`VP9_TABLES_GENERATED = 0`) |
| scan / band / token / pareto / quant / mode2txfm blobs | `tools/gen_vp9_residual_tables.py` | pending (`VP9_RESIDUAL_TABLES_GENERATED = 0`) |

Until both flags are 1, `vp9_decoder_receive` returns `I_UNSUPPORTED` when
it reaches a coded slice, after parsing the header and before allocating the
surface or touching tile data. A `show_existing_frame` slice needs neither
blob and is still returned (its pixels were decoded before the blobs went
missing, if they ever did). Regenerate with the specification text and
commit the produced files; see `vp9.md` and `residual.md` for the exact
commands.

## What stages 5 and 6 add

- Stage 5: `inter_frame_mode_info`/`inter_block_mode_info`, ref frames,
  motion vectors, inter prediction, `read_is_inter`, segment-id prediction
  with `PrevSegmentIds`, `adapt_coef_probs`/`adapt_noncoef_probs` and the
  `refresh_probs` load/adapt/save order, plus `load_probs2` after
  adaptation. `block.cst` already refuses an inter block with
  `I_UNSUPPORTED` and keeps the per-mi arrays the loop filter needs.
- Stage 6: `show_existing_frame` through `refs.vp9_refs_show_existing`; the
  `vp9_decoder_send`/`vp9_decoder_receive` packet handoff that returns 0..8
  displayed frames per packet and keeps hidden frames out of the output; the
  `Vp9Output` record; variable frame sizes with pool reallocation; the
  publishable-buffer check; and the remaining profile-0 paths enumerated in
  `vp9.md`'s "Remaining conformance paths". The public `decode` hub and the
  examples are stage 10, not part of this driver.

## UNCONFIRMED items

Every item below is read from the spec and the reference decoders; none has
been confirmed by a decoded frame yet.

1. **Overhanging blocks and padding.** A partition may start inside the
   coded frame and end past `MiCols*8`/`MiRows*8`; 6.4.21 decodes any
   transform block whose top-left is inside, and 8.5.1 clamps reference
   reads to `maxX`/`maxY` while writing the full block. `pred_intra` now
   does that and the driver pads each plane by a 64-sample band. The
   loop filter may read padding; it is zeroed per frame. External check:
   decode a frame whose width/height is not a multiple of 64 and compare
   its MD5 with libvpx, then a 1920x1080 frame with a 16x16 transform at
   the bottom edge (`MiCols*8 = 1920`, block starts at y=1072).
2. **Sub-8x8 mode index for 4x8 (UNCONFIRMED erratum).** Section 6.4.21
   numbers transform blocks with a flat `blockIdx` counter and 8.5.1 selects
   `sub_modes[blockIdx]`. For BLOCK_4X8 the two transform blocks are at
   4x4 offsets (0,0) and (0,1); the flat counter gives 0 and 1 while the
   references index the 2x2 array as `y*2+x`, giving 0 and 2 (libvpx
   `bmi[(row << 1) + col]`, FFmpeg `b->mode[y*2+x]`). This decoder uses the
   reference rule; the difference can only show on a 4x8 block with
   different sub-modes. Check with a stream that codes BLOCK_4X8.
3. **Per-mode-info fill clipping.** The spec's 6.4.4 fill loops run over
   all `num_8x8` units, which for an overhanging block reaches past
   `MiCols`/`MiRows` into conceptually padded arrays. This decoder clips the
   writes to the real grid (libvpx `x_mis`/`y_mis`). Every read that could
   observe the clipped cells is itself bounded by `MiCols`/`MiRows`, so the
   two are equivalent; confirm with the same overhang stream as item 1.
4. **`refresh_probs` reloads the saved context before adapting.** Section
   6.1.2 prints `load_probs(frame_context_idx)` and, for inter frames,
   `load_probs2(frame_context_idx)` again after the tiles and before the
   adaptation. The implementation does exactly that, so the adaptation
   prior is the saved context, not the forward-updated working tables.
   Both reference decoders agree in effect: libvpx's `adapt_coef_probs`
   reads `pre_fc = &cm->frame_contexts[idx]` and writes `cm->fc`; FFmpeg's
   `ff_vp9_adapt_probs` adapts `s->prob_ctx[framectxid]` in place. The
   intra-frame exception also matches: `load_probs2` is not called, so the
   header's forward-updated `skip_prob`/`tx_probs` are saved by
   `save_probs` and later adapted only when an inter frame reloads them.
   Confirm with two frames that set `refresh_frame_context = 1`, then a
   third that codes forward updates, comparing every MD5 with libvpx.
5. **Superframe output order is lazy.** A packet is consumed one slice per
   `receive` call, so a superframe's hidden frames are decoded and its
   shown/show-existing slices returned in bitstream order. A caller that
   never drains the packet gets `I_BADPARAM` on the next send rather than a
   silently dropped frame. UNCONFIRMED until a multi-slice superframe with
   a hidden altref (and ideally a trailing show-existing) is decoded.
6. **PrevSegmentIds clears.** Section 8.1 step 3 updates `PrevSegmentIds`
   only when segmentation is enabled and the map is updated. Section 7.2's
   `setup_past_independence` also zeroes the whole map whenever
   `FrameIsIntra == 1 || error_resilient_mode == 1`, before any tile reads
   it (6.4.14), and section 7.2.6 zeroes it again when the frame size is
   first seen or changes. `_decode_slice` applies both clears after the
   work block is carved and before `tile_decode_all`, because an
   error-resilient inter frame reads the map through `get_segment_id` while
   its tiles are decoded; `_update_seg_map` then performs only the step 3
   copy. The final map is the specification's: a copy of this frame's
   `SegmentIds` when the map is updated, otherwise zero for any intra or
   error-resilient frame, with no stale ids surviving a resolution change.
   The merged driver consumes it through `get_segment_id`. UNCONFIRMED
   until a segmentation-predicted inter frame is decoded.
7. **Per-tile `exit_bool`.** Each tile's trailing padding is checked to be
   zero (9.2.3). The reference decoders do not perform this check; a
   non-conforming encoder that leaves non-zero padding would be rejected
   here with `I_CORRUPT`. Conformance requires zero, so this is the
   spec-literal choice. Confirm against the vp90-2-* vectors.
8. **Loop-filter gate.** `_loop_filter` runs only when `loop_filter_level`
   is non-zero (section 8.1); `lf_filter_plane` has the same early return.
   A non-zero segment ALT_L cannot turn filtering on for an otherwise
   level-0 frame. Same rule as stage 3's recon.md item 10.
9. **Lossless blocks.** `h.lossless` forces `tx_mode = ONLY_4X4` and the
   residual module returns DCT_DCT; `tx_2d` then takes its 4x4 WHT path.
   A lossless frame with any other transform size would be corrupt, but the
   header parser already derives `tx_mode` from `lossless`.
10. **Show-existing reads the slot, not the header.** Section 8.9 names
    only `w`, `h`, subsampling and bit depth. The implementation also
    returns the slot's color space, color range and render hint, which are
    stored with the published frame and cannot disagree with its pixels.
    The specification does not say these three fields are meaningful on
    this path; recorded as U3 in `refs.md`. Confirm with a stream that
    changes color space before a show-existing slice and compare the
    reported metadata with `ffprobe`.
11. **The decode target cannot be a live reference.** `vp9_refs_publish`
    refuses a source buffer named by any published slot, including a slot
    the frame refreshes. This is deliberately stricter than "the frame may
    overwrite its own reference": the old pixels are still read by
    `ref_frame_idx` during the tiles. The waiting transient buffer is what
    makes the rule satisfiable with nine buffers. Confirm with an inter
    frame that lists a slot in both `ref_frame_idx` and
    `refresh_frame_flags`.

## Pending review notes (documentation / naming / minor robustness)

- **Inter skip rule keys on the frame, not the block.**
  `block.cst:634` turns a block into a skip block when
  `b.h.frame_is_intra == 0 && bs >= BLOCK_8X8 && eob == 0`. Section 6.4.4
  keys that rule on the block's `is_inter`, not on the frame type: an intra
  block inside an inter frame must keep its coded skip value. The branch is
  unreachable in stage 4 (inter frames are rejected before a tile is
  parsed); stage 5 must replace `frame_is_intra` with the block's decoded
  `is_inter` before it enables inter frames.
- **`blk_subsize` comment corrected.** The comment above
  `block.cst:blk_subsize` claimed the rectangular source-size rows of
  `subsize_lookup` were handled. The function has always returned
  `BLK_INVALID` for a source size outside `BLOCK_4X4..BLOCK_64X64`; the
  comment now states that, and that the rectangular results of
  `PART_HORZ`/`PART_VERT` on square sizes are returned normally. No code
  change.
- `tile.cst:64-65` defines `t_max_cols_log2`/`t_max_rows_log2` locally
  instead of importing `VP9_MAX_TILE_COLS_LOG2`/`VP9_MAX_TILE_ROWS_LOG2`
  from `vp9_tables.cst`; the two values (6 and 2) are duplicated. The
  constants exist in the tables module and the local copies are only to
  avoid a dependency for two numbers.
- `frame_need`/`work_need` are stale on success and after
  `vp9_decoder_free`. A decode only writes them when it finds the surface
  or the work block short, so a successful decode leaves the previous
  shortfall in place. They are "last rejected requirement" reports, not
  current requirements; stage 6's accessor should say so, or the driver
  could clear them at the start of each decode. They are plain public
  fields today.
- **Zero-coefficient transform skip is an optimisation.** `block.cst`
  calls `tx_2d`/`tx_add_residual` only when `nonzero == 1`. An all-zero
  dequantized block adds nothing to the prediction, so skipping the
  inverse transform produces the same samples as running it; the
  per-4x4 nonzero contexts are still written with 0 and `dq` is not read
  on the skipped path. Documented so a later reader does not mistake the
  branch for a syntax shortcut.
- **Init and bind contract.** `vp9_decoder_init` must precede
  `vp9_decoder_bind` and `vp9_decoder_decode`; `vp9_decoder_decode` can
  only check that the work pointer is non-null, not that the persistent
  state was installed. `vp9_decoder_bind` accepts any non-null block with a
  positive capacity and leaves alignment and the per-frame size to
  `_work_carve` (`I_BADPARAM` for a misaligned base, `I_NOMEM` plus
  `work_need` when a later frame's layout does not fit). Both points are
  restated under "Ownership and sizes".
- `Vp9Work` is a private layout struct: its field order is the on-disk
  work layout. A reviewer may prefer a version/magic word or an explicit
  `vp9_work_layout` out-parameter API instead of `_work_carve` computing
  the total on a null base.
- `_update_seg_map` writes `w->prev_seg_ids` in place. The section 7.2/7.2.6
  clear happens before the tiles, so a tile decode that fails after the
  clear leaves the map zeroed while the rest of the persistent state is
  tainted. The step 3 copy happens only after a successful tile decode,
  adaptation and filter, so the map is consistent with the published
  reference metadata. Stage 6's lazy packet means a *later slice* of the
  same packet can still fail after an earlier slice updated the map; the
  state is then tainted exactly as a rejected header is. Both cases are
  recoverable only through a key/intra-only frame, which is the documented
  taint rule.
- `_work_carve` uses plain `i64` sums after capping dimensions at 8192; the
  merged largest total is about 6.9 GiB (7,449,243,936 bytes, including
  stage 5's motion arrays), far below the i64 range, so no overflow check is
  needed, but the invariant is only stated in a comment. It also forms the
  layout pointers before the capacity check; none is dereferenced until the
  null-base, alignment and capacity checks pass.
- `blk_log2` returns `BLK_INVALID` for counts other than 1/2/4/8/16; every
  caller checks. A reviewer may prefer distinct error codes.
- The `i64 -> i32` copy before `tx_2d` exists because `residual.cst` returns
  dequantized coefficients as `i64` while `transforms.cst` takes `i32`.
  residual.md guarantees the value is within signed 16-bit.
- `frame_need`/`work_need` are plain public fields rather than accessor
  functions; the send/receive API leaves them for a later wrapper.
- `vp9_decoder_send` stores raw pointers into the caller's packet and
  requires the caller to keep them valid until receive returns 0. An API
  that owns or copies the packet would remove that lifetime rule, at the
  cost of an allocation the library currently never makes.
- `_pick_buffer` tests both the slot `id` bookkeeping and the actual pixel
  range (`vp9_refs_buffer_publishable`). That is deliberate redundancy: the
  id lookup assumes every published frame came from this pool, while the
  range check is true even if a caller mixed the refs API with its own
  slots. A reviewer may prefer one mechanism and an invariant comment.
- `vp9_decoder_receive` cannot resume a packet after `I_NOMEM` on a later
  slice. Sizing the pool for the stream's maximum frame before send avoids
  it; a resume API would need a per-slice saved position and a way to
  rewind published state, which the specification does not define.
- `Vp9Output` embeds `frame.Frame` by value, so a caller that keeps the
  struct but frees the buffer holds a dangling view. The lifetime rule is
  documented at the top of `decode.cst`; a wrapper that copied into a
  caller-provided surface would remove the hazard.
- **Call-graph order (cosmetic).** The call graph listed
  `frame_alloc_coded` before `_pick_buffer`, but the driver must pick the
  buffer first because `frame_alloc_coded` needs that buffer's `mem`/`cap`.
  The graph now shows `frame_size + _pick_buffer`, then
  `frame_alloc_coded + zero the coded surface`. No code change.

## Reference implementations read

Read online, not copied or ported:

- libvpx `main`: `vp9/decoder/vp9_decodeframe.c` (decode_tiles,
  decode_partition, decode_block, tx/partition contexts), `vp9_decodemv.c`
  (intra frame/block mode info), `vp9/common/vp9_blockd.c` and
  `vp9_blockd.h` (above/left block mode accessors, get_y_mode_probs),
  `vp9/common/vp9_onyxc_int.h` (set_mi_row_col, partition context),
  `vp9/common/vp9_reconintra.c` (intra edge building), `vp9/common/vp9_blockd.h`
  (tx contexts), `vpx_scale/generic/yv12config.c` (aligned frame buffer and
  border), `vp9/decoder/vp9_decoder.c` and `vp9/common/vp9_alloccommon.c`
  (`setup_past_independence` call order and the segmentation-map reset on a
  frame-size change).
- FFmpeg `master`: `libavcodec/vp9.c` (decode_sb, decode_tiles, tile offsets,
  keyframe partition table, frame header and reference checks),
  `libavcodec/vp9block.c` (decode_mode, coefficient context writes, block
  size to partition mapping), `libavcodec/vp9recon.c` (intra_recon, edge
  handling, end_x/end_y), `libavcodec/vp9shared.h` (BlockSize/BlockLevel
  enums).
- Stage 6 additionally read libvpx `vp9/decoder/vp9_decodeframe.c`
  (show-existing header return, `read_uncompressed_header` reference checks,
  `cm->lf.filter_level` gate, frame-end adaptation and context save),
  `vp9/decoder/vp9_decoder.c` (separate working context and the
  `FRAME_BUFFERS` count), `vp9/common/vp9_entropy.c` and
  `vp9/common/vp9_entropymode.c` (saved context as the adaptation prior),
  and FFmpeg `libavcodec/vp9.c`/`vp9prob.c` for the equivalent
  `prob_ctx`/`ff_vp9_adapt_probs` order.

No external code was copied, ported or transcribed.

## Status

WRITTEN, NOT VERIFIED. Nothing compiled, nothing run. Checks the original
briefs require are listed as NOT RUN: MD5 against libvpx for every
`vp90-2-*` vector, `ffprobe` stream metadata, `trace_headers` field
comparison, FFmpeg frame difference, fps measurement, superframe/display
order comparison, and a resolution-change stream with show-existing.
`opus_compare` is not applicable to this codec. Nothing in this directory
has produced runtime evidence.
