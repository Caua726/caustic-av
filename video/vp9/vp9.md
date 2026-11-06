# VP9: full frame decoding

Stages 3-6 of the VP9 decoder. Stage 3 turns a packet into the frame's
uncompressed and compressed headers, the reference and segmentation state
that survives between frames, the probability contexts, and the byte ranges
of the tiles. Stage 4 adds the tile walk, intra prediction, residual
decoding, inverse transforms and the loop filter. Stage 5 adds inter frames
(6.4.11-6.4.20 and 6.5), compound and scaled prediction (8.5.2), backward
probability adaptation (8.3-8.4), and the pixel reference slots of 8.9-8.10.
Stage 6 completes the profile-0 bitstream surface: show_existing_frame
(8.9), the packet-to-display handoff with hidden frames, the public I420
output record, variable frame sizes with a reallocatable reference pool,
error-resilient and parallel-decoding frames, intra-only frames, the full
1..64 x 1..4 tile grid, and the remaining loop-filter/segmentation/lossless
paths. `decode.cst` is the frame driver.

Every item in this note is **WRITTEN, NOT VERIFIED**: nothing has been
compiled or run, and the checks named for the test phase have not been
executed.

Only profile 0 is accepted. The syntax of profiles 1-3 is different enough
(4:2:2/4:4:4, 10/12-bit, an extra reserved bit after the profile bits) that
guessing would be worse than refusing: `vp9_header_parse` returns
`I_UNSUPPORTED` as soon as the profile bits are not 0.

## Files

| File | Holds |
|---|---|
| `video/vp9/bool.cst` | The Boolean decoder of section 9.2: init with the marker check, `read_bool`, `read_literal`, exit/padding, all bounds-checked. |
| `video/vp9/vp9_tables.cst` | Dimensions and constants, fixed lookup tables, `Vp9ProbCtx`, `Vp9Counts`, `Vp9State`, and the probability-context operations (defaults, load, load2, save, clear, publish). |
| `video/vp9/vp9_tables_data.cst` | The probability blobs. **Generated** by `tools/gen_vp9_tables.py`; currently pending (see below). |
| `video/vp9/header.cst` | Annex B superframe split, the uncompressed header, the compressed header, and the tile size table. |
| `video/vp9/tile.cst` | Tile byte ranges and the per-tile Boolean decoder, one per tile, over the partition tree. |
| `video/vp9/block.cst` | Partition tree, intra and inter mode info (6.4.4-6.4.20), motion-vector syntax, inter prediction calls, residual orchestration (6.4.21). |
| `video/vp9/residual.cst`, `residual_tables.cst` | Token decode, coefficient contexts, dequantisation (8.6, 9.3.4) and the scan/band tables. |
| `video/vp9/transforms.cst` | Inverse transforms of 8.7 and residual addition. |
| `video/vp9/predict.cst` | Intra prediction of 8.5.1. |
| `video/vp9/inter.cst`, `inter_tables.cst` | Inter prediction of 8.5.2, its subpel filters and fixed tables. |
| `video/vp9/mvpred.cst` | Motion-vector prediction of 6.5.1-6.5.14. |
| `video/vp9/refs.cst` | The eight reference slots, publish (8.10) and the show-existing view (8.9). |
| `video/vp9/scale.cst` | Scaled-reference geometry and the 7.2.5/8.5.2.3 size-ratio bounds. |
| `video/vp9/adapt.cst` | Backward probability adaptation of 8.3-8.4 over its own i64 tables. |
| `video/vp9/adapt_bridge.cst` | Copies between `vp9_tables.cst`'s u8/byte layout and `adapt.cst`'s i64 layout and runs one `refresh_probs` (6.1.2). |
| `video/vp9/loopfilter.cst` | The loop filter of 8.8. |
| `video/vp9/decode.cst` | The frame driver: packet split and send/receive handoff, pool selection, tile walk, adaptation, loop filter, segment/mv bookkeeping, show_existing output, publish. |
| `dsp/vp9_dsp.cst`, `dsp/vp9_dsp_abi.cst`, `dsp/x86_64/*` | The stage 9 acceleration table, its x86_64 kernels and the runtime dispatch. `dsp/vp9_dsp.md` is the design note. Scalar code stays in the video modules above and runs whenever a slot is null. |
| `tools/gen_vp9_tables.py` | Reads the specification text, validates every table's dimension and value range, and writes `vp9_tables_data.cst`. |

The specification used is `.work/vp9/spec/clean.txt` (VP9 Bitstream &
Decoding Process Specification v0.6). `.work/vp9/spec/vp9-v0.7.txt` was read
only to cross-check; it carries the same text for every section used here,
including the two errata below.

## State and ownership

`Vp9State` is the whole cross-frame state and is owned by the caller. The
library allocates nothing: the caller provides the memory and calls
`vp9_state_init` once, then `vp9_header_parse` for each frame slice.

Fixed capacities, all from the specification:

- 8 reference slots (`ref_width/height/valid/profile/bit_depth/ss_x/ss_y/
  color_space`); `vp9_state_publish` fills the slots named by a frame's
  `refresh_frame_flags` and must be called only after the frame has been
  reconstructed.
- 4 frame contexts (`ctxs`, `ctx_init`) plus the working context `probs`.
- 8 segments x 4 features, with the persistent loop-filter deltas and
  segmentation probabilities.
- `Vp9Counts`: `token` is 4x2x2x6x6x3 and `more_coefs` 4x2x2x6x6x2, the rest
  follow section 9.3.4.
- 256 tiles at most (64 columns x 4 rows), in `Vp9Header.tiles`.

`vp9_header_parse` updates the persistent parts of `Vp9State` (loop-filter
deltas, segmentation data, format of the last coded frame, frame contexts,
`last_*` frame record). A caller that rejects the frame must treat the state
as tainted until the next key or intra-only frame, which resets the parts
section 7.2 requires.

Probability context operations follow section 7.1.2 exactly:
`vp9_state_load_probs` copies everything except `tx_probs` and `skip_prob`;
`vp9_state_load_probs2` copies exactly those two; `vp9_state_save_probs`
copies every table and marks the context initialised;
`vp9_state_clear_counts` zeroes `Vp9Counts`. A context that has never been
saved is corrupt input: libvpx calls it "uninitialized entropy context", and
`vp9_state_load_probs` returns `I_CORRUPT` for it.

## Frame decoding (stage 4-6)

`Vp9Decoder` owns `Vp9State`, the eight `refs.cst` slots, a reference pixel
pool, the armed packet and the work block. The caller allocates the decoder
object, a persistent work block (`vp9_work_size`) and up to nine pixel
buffers (`vp9_decoder_add_buffer`); the library allocates nothing.
`Vp9Decoder` is large (the `Vp9State` probability contexts plus `adapt.cst`'s
i64 working set and counts, roughly 250 KiB), so it belongs in caller
storage, not on a small stack.

### Packet and output handoff

`vp9_decoder_send(d, data, len)` validates the Annex B split (single frame or
1..8 frame superframe) and stores the slice addresses and lengths; the bytes
themselves stay the caller's. `vp9_decoder_receive(d, out)` then decodes
slices from that packet until it has one frame to display: it returns 1 with
`out` filled, or 0 when the packet has no further output. A packet therefore
yields 0..8 outputs, in bitstream order, one per `receive` call.

- A hidden coded frame (`show_frame == 0`) updates the reference slots and
  yields no output; `receive` continues with the next slice.
- A shown coded frame yields the pixels just reconstructed.
- `show_existing_frame` yields the stored slot named by
  `frame_to_show_map_idx` without decoding a tile or changing any state.

`send` refuses a new packet while the previous one is undrained, so no frame
of a packet can be skipped or handed out twice. The output record
(`Vp9Output`) carries the I420 view (visible `width`/`height`, plane pointers
and coded strides), the 7.2.4 render hint, profile, bit depth, subsampling,
color space/range and a monotonically rising `order` hint. A packet with only
hidden frames returns 0 and never produces a placeholder frame; the caller
recognises "no output" by the return value, not by a zeroed view.

The output view is valid until the next `receive` or `send` call. If the
frame was published into a reference slot, its pixels stay valid while any
slot names that buffer, which is what makes a later `show_existing` view of
the same pixels safe.

### Reference pixel lifetime

Each pool buffer is a caller-owned range. A decode picks a buffer whose pool
index is named by no published slot (`refs.in_use`), whose range is
publishable (`vp9_refs_buffer_publishable`) and whose capacity holds
`vp9_frame_size(width, height)`; if none qualifies the call returns
`I_NOMEM` with `d.frame_need`, and the caller may
`vp9_decoder_replace_buffer` on an index returned by
`vp9_decoder_free_buffer`. Nine buffers suffice because the packet handoff
is lazy: at most one displayed frame exists outside the eight reference
slots, and that frame was just decoded into the single transient buffer.
With fewer than nine registered buffers an inter stream can reach `I_NOMEM`
when every reference slot is live. Decoding never writes a published
reference.

After the loop filter the frame is published with `refs.vp9_refs_publish`:
every slot named by `refresh_frame_flags` stores a view of the decoded
buffer (8.10's `FrameStore[i] = CurrFrame`, represented as a view because the
pixels already are the frame), and `Vp9State.ref_*` is updated by the same
call. `refresh_frame_flags == 0` publishes nothing and leaves the buffer
free. A frame may reference the very slot it later refreshes; publish is
after the tiles, so the old pixels are only replaced once prediction is
finished. `vp9_refs_buffer_in_use(map, id)` tells the caller when a buffer
may be reused or freed.

`coded_w = round64(MiCols*8) + 64`, likewise for height; the 64-sample band
on the right and bottom is what 6.4.21's overhanging edge blocks write.
`core/frame.cst`'s `FRAME_MAX_DIM` currently caps the coded size, so the
largest visible side this driver accepts is 65472 (coded 65536); a visible
side of 65536 cannot be represented with its padding and returns `I_DIMS`
(see Pending review notes).

### Per-slice order

`vp9_decoder_receive` calls `_decode_slice` for one slice of the armed packet
at a time. The output a slice may produce is returned immediately, so the
order below is the bitstream order of section 8.1. One slice:

1. `vp9_header_parse` (6.2-6.3). If `show_existing_frame == 1`, section 8.9
   is the whole slice: `refs.vp9_refs_show_existing` copies the stored slot
   view, `_out_from_slot` fills the output record from that slot (never from
   the zeroed header geometry), and the slice returns. No compressed header
   is parsed, no tile is decoded, no filter runs, `PrevSegmentIds` and
   `PrevRefFrames` are untouched, and nothing is published (8.10 step 2 is
   explicitly skipped when `show_existing_frame == 1`).
2. Format, pending-table and compressed-header checks. A profile-0 surface
   that is not 8-bit 4:2:0 returns `I_UNSUPPORTED`; a pending probability or
   coefficient blob also returns `I_UNSUPPORTED` before any surface is
   touched.
3. For an inter frame: `vp9_refs_check_header` against the pixel slots and
   the 7.2.5 size-ratio requirement (at least one reference must satisfy
   `scale.vp9_scale_ratio_ok`); the per-reference bound of 8.5.2.3 is
   enforced again by `inter.cst` at every prediction.
4. Select a free pool buffer (not named by a slot, publishable, large
   enough), form the coded frame view, zero it.
5. Carve the per-frame work arrays. Clear `PrevSegmentIds` once, before any
   tile block can read it, when `frame_is_intra == 1 || error_resilient_mode
   == 1 || seg_map_clear == 1` (7.2's `setup_past_independence` plus 7.2.6's
   size-change clear; the stage 4 and stage 5 forms are the same OR). Clear
   the current frame's `Mvs`/`SubMvs` (6.5 reads both lists of a candidate
   before deciding, and 8.10 copies the field for the next frame) and
   `SubModes` (an inter block writes no sub-mode, and 9.3.2 must see
   `DC_PRED` for an inter neighbour).
6. `tile_decode_all` (6.4): one Boolean decoder per tile, above contexts
   once per frame, left contexts once per 8x8 row. For each block,
   `block.cst` decodes the partition tree, mode info, prediction and
   residual, and writes the mode-info arrays through `mvpred.cst`.
7. `adapt_bridge.vp9_adapt_frame`: 6.1.2 exactly. If
   `error_resilient_mode == 0 && frame_parallel_decoding_mode == 0`, load
   the saved frame context (the adaptation prior is the context saved by the
   previous save, not the header's forward-updated working tables),
   `adapt_coef_probs`, and for an inter frame load `load_probs2` and
   `adapt_noncoef_probs`; then, if `refresh_frame_context == 1`, save the
   context. `error_resilient_mode == 1` forces
   `frame_parallel_decoding_mode = 1` in 6.2, so neither a load nor an
   adaptation happens, while `refresh_frame_context` is 0 and nothing is
   saved. `LastFrameType`, `FrameIsIntra`, `interpolation_filter`, `tx_mode`
   and `allow_high_precision_mv` all come from the parsed header. Counts were
   cleared per frame and accumulated by the tiles; frames that skip
   adaptation simply discard them. `refresh_frame_context == 1` with
   `frame_parallel_decoding_mode == 1` still saves the header's forward
   updates (8.1's refresh_probs save step), without adapting.
8. Loop filter (8.8) when `loop_filter_level != 0`, then 8.1 step 3
   (`PrevSegmentIds`), then 8.10 step 2 (`PrevRefFrames`/`PrevMvs` from the
   current arrays).
9. Publish (8.10 step 1). If `show_frame == 1`, fill the output record from
   the decoded frame; otherwise return 0 and continue with the next slice.

### Output and display rules

- One `receive` call returns at most one frame. Hidden frames are decoded as
  receive advances through the packet, but they are never turned into an
  output view.
- Show-existing returns at the time its slice appears in the bitstream, so
  its display position is preserved even though the pixels are older.
- The shown geometry is `FrameWidth`/`FrameHeight` for a coded frame and the
  slot's `RefFrameWidth`/`RefFrameHeight` for show-existing; the two may
  differ, including across a resolution change in the same packet.
- `render_width`/`render_height` are the 7.2.4 hints: the current frame's for
  a coded frame, the stored frame's for show-existing. They never affect
  decoding or buffer selection.
- The output has no fake fallback: `receive` returns 0 for "no more output"
  and a negative code for an error. A packet with no displayed slice yields
  no output at all.
- `order` rises by one per returned frame (hidden frames do not consume an
  output number), so a caller can detect that a show-existing output is a
  new presentation event even though it repeats an old storage slot.

### Variable frame size and reference resizing

A stream may change `FrameWidth`/`FrameHeight` at any coded frame, and a
show-existing slice may show a frame of a different size without changing
the stream size at all.

- A new size never invalidates a reference slot. Slots keep their own visible
  and coded geometry; scaled inter prediction (7.2.5 and 8.5.2.3) is what
  makes the difference legal.
- Only the current frame needs a buffer for its own size:
  `vp9_frame_size(width, height)` is 6.4's coded geometry,
  `round64(MiCols*8) + 64` by the same for height. A free buffer that cannot
  hold it produces `I_NOMEM` and `frame_need`; the caller replaces a free
  buffer (`vp9_decoder_free_buffer`, `vp9_decoder_replace_buffer`) before
  the next `send`. A packet that `receive` has already advanced cannot be
  re-sent; pre-size the pool and work block for the stream's largest frame,
  or resync at the next key/intra frame. A key frame republishes all eight
  slots from its one buffer, so no old slot of the previous size survives
  it; an inter frame leaves the other slots at their sizes.
- `MiCols`/`MiRows`, `Sb64Cols`/`Sb64Rows`, `seg_map_clear` and
  `use_prev_frame_mvs` are recomputed by `compute_image_size` (7.2.6) for
  every frame that runs it. A size change sets `seg_map_clear` and disables
  `use_prev_frame_mvs`; a show-existing frame runs neither, so it changes
  no size bookkeeping.
- The render size (7.2.4) is output metadata only. A render-size change
  needs no buffer and never affects prediction; show-existing reports the
  render hint stored with the slot.
- Show-existing reads its geometry and format from the slot
  (8.9: `RefFrameWidth`, `RefFrameHeight`, `RefSubsamplingX/Y`,
  `RefBitDepth`), not from the zeroed header fields listed in stage 3's
  Pending note 3. It can therefore display a frame smaller or larger than
  the coded frames around it, even across a resolution change.
- SVC needs no separate syntax here: the spatial layers are ordinary slices
  of different sizes inside one superframe, with hidden layers feeding the
  reference slots and the shown (or show-existing) layer producing output.
  Because the handoff is a per-slice loop over the packet, the layers keep
  their bitstream order and each is published before the next reads it.

### Inter mode info

`block.cst` implements `inter_frame_mode_info` (6.4.11): neighbour reference
frames and the `LeftIntra`/`AboveIntra`/`LeftSingle`/`AboveSingle` flags,
`inter_segment_id`, skip, is-inter, the conditional `read_tx_size`, then
either `inter_block_mode_info` or `intra_block_mode_info` with the adaptive
intra tables and their counts. `read_ref_frames` (6.4.17) covers single,
compound and `SEG_LVL_REF_FRAME`; `find_mv_refs`/`find_best_ref_mvs` (6.5)
fill NearestMv/NearMv/BestMv, and `read_mv` (6.4.19-20) reads the joint,
class, class0/fr/hp and bit syntax. `append_sub8x8_mvs` supplies only
NearestMv/NearMv for a 4x4 piece; BestMv always stays the whole-block 6.5.12
value. The inter-mode entropy context is the 6.5.1 counter over the two
nearest neighbours, computed once per block before any inter_mode read (the
reference decoders do the same; the appends' internal `find_mv_refs` calls
must not move it). Sub-8x8 blocks read one inter_mode per 4x4 piece and
write `BlockMvs`; `mv_store_block_mvs` writes Mvs/SubMvs for every covered
mode-info cell and both reference lists (single-reference blocks present the
zeroed list 1).

Prediction is `inter.cst` (8.5.2): per plane, per reference list, MV
selection, clamping, scaling and the two-pass 8-tap filter; compound blocks
average with `Round2(a+b, 1)`. `blockIdx` is derived from the plane block
(`it_plane_block_dims`), never from the MiSize alone, so the 8.5.2.1
averages cannot read another piece's vector or the next reference list.

### Stage 5-6 integration changes

These touch adopted stage 4-5 modules and are required for ordinary frame
sizes; they do not change valid-stream output:

- `MiRows`/`MiCols` are `ceil(height/8)`/`ceil(width/8)`, so a partition at
  the right or bottom edge can yield a block whose top-left is inside the
  mode-info grid but whose body overhangs it (1080p and 720p both do).
  `inter_frame_ok` and `mvpred.mv_ctx_ok` accept such a block (start inside,
  overhang allowed), where the adopted slices required the block to fit.
  `inter_predict_taps`/`inter_predict_block` let the written region extend
  into the caller's 64-sample padding band instead of rejecting
  `x + w > MiCols*8`. `mvpred`'s clamps then see the negative distance to
  the overhung edge that 6.5.4/6.5.5 print. The added tests live in
  `inter.cst`/`mvpred.cst` and are marked as stage 5 integration changes.
- `loopfilter.cst`'s `LfBlockInfo` gained `ref_stride`: `ref_frames` now has
  two entries per mode-info cell (list 0 and list 1), and the filter reads
  list 0 at `idx * ref_stride`.
- `decode.cst`'s public decode call no longer takes a per-call output block:
  the output is a view into the selected pool buffer, and the caller
  registers buffers up front. `frame_need`/`work_need` report the sizes to
  grow.
- Stage 6 replaces the single-output `vp9_decoder_decode` with the
  `vp9_decoder_send`/`vp9_decoder_receive` pair and the `Vp9Output` record.
  A packet is consumed lazily, one slice per receive, so at most one display
  view exists beside the reference slots. The old call could only return the
  last displayed slice of a superframe; the new one returns every displayed
  slice exactly once, in order.
- Stage 6 adds the `show_existing_frame` branch before every table and tile
  check, and the pre-decode `vp9_refs_buffer_publishable` test in
  `_pick_buffer`, so a pool buffer is only ever chosen when no published
  slot names its range.

### Coefficient and probability counts

`block.cst` and `residual.cst` update the section 9.3.4 counters in the
`vp9_tables.cst` layout: partition, skip, tx size, is-inter, comp mode,
single/compound reference, inter mode, switchable filter, the MV tables, and
the token/more_coefs tables (the E2 rule). `adapt_bridge.cst` copies those
counts and the working probabilities into `adapt.cst`'s layout, calls its
`refresh_probs`, and copies back; the four saved contexts are not touched
except the one the frame loads/saves. The extra copies are per frame, not
per block.

## Headers

`vp9_header_parse(st, h, data, len)` expects one frame slice: a superframe
must have been split first with `vp9_superframe_split`.

- Superframe split (Annex B): a chunk whose last byte is not a superframe
  marker, or whose index is too short or whose first index byte does not
  equal the last byte, is one frame. Once both marker checks pass, a
  zero-sized frame, a size past the frame area, or sizes that do not sum to
  the frame area is `I_CORRUPT`. The last frame of a one-frame superframe is
  legal.
- Uncompressed header: frame marker, profile, `show_existing_frame` (which
  returns after checking that every remaining bit of the slice is zero and
  that the referenced slot has been published by `vp9_state_publish`),
  frame type, show/error-resilient flags, sync code, color config, sizes and
  render sizes, reference indices and sign biases, interpolation filter,
  refresh/parallel flags, frame context index, `setup_past_independence`
  and the save-probs rules, loop filter, quantization, segmentation and tile
  info, then `header_size_in_bytes`.
- Inter frames read three `ref_frame_idx` values; every one of them must
  name a slot published by `vp9_state_publish` and must match this frame in
  profile, bit depth, chroma subsampling and color space, otherwise
  `I_CORRUPT` (sections 7.2 and 8.2). Only then does frame size with refs
  read `found_ref` until one is set and take that reference's dimensions.
  The header records the sizes; the section 7.2.5 ratio requirement is
  applied by `_decode_slice` before any buffer is chosen (at least one of the
  three references must pass `scale.vp9_scale_ratio_ok`) and again per
  reference by `inter.cst` at 8.5.2.3. Inter frames inherit the color
  configuration of the last coded frame, as they have no color config of
  their own.
- Compressed header: `load_probs`, `load_probs2`, `clear_counts`, then
  `init_bool(header_size_in_bytes)` and, in specification order, tx mode,
  tx mode probs, coefficient probs, skip probs and — for inter frames —
  inter mode, switchable interpolation, is-inter, reference mode,
  reference-mode probs, y mode, partition and motion-vector probs. Tile data
  is not touched.
- Tile sizes: the size fields and tile data are interleaved
  (`[size][tile][size][tile]...`); the last tile extends to the end of the
  slice. Each tile is at least one byte. The result is `h.tiles`, indexed
  row-major in the order `decode_tiles` visits tiles.

`h.tile_data_off` is `uncompressed_bytes + header_size_in_bytes`, the first
byte of the tile region. `h.uncompressed_bytes` is the byte just past the
trailing bits of the uncompressed header.

## Tables and pending blobs

`tools/gen_vp9_tables.py` is the only source of the probability bytes. It
locates each table in the specification text by name, strips `//` comments,
checks the element count, checks 1..255 for coded probabilities (0 only in
slots the specification marks unused), checks the `default_tx_probs` and
`default_coef_probs` unused slots are zero, and writes three blobs:

| Blob | Size | Contents |
|---|---|---|
| `VP9_TBL_INV_MAP` | 255 | `inv_map_table` (6.3.5) |
| `VP9_TBL_KF_PROBS` | 1038 | `kf_partition_probs`, `kf_y_mode_probs`, `kf_uv_mode_probs` (10.4) |
| `VP9_TBL_DEFAULT_PROBS` | 2051 | the `default_*` tables in the order printed in `vp9_tables_data.cst` (10.5) |

Regenerate with the specification text supplied explicitly:

```
python3 tools/gen_vp9_tables.py --spec <path to clean.txt> \
    -o video/vp9/vp9_tables_data.cst
```

**Pending blobs.** `vp9_tables_data.cst` is committed with
`VP9_TABLES_GENERATED = 0` and three empty strings. Those three blobs still
need generating; until then `vp9_probctx_default` returns `I_UNSUPPORTED`,
the state sets `probs_pending`, and the decoder must not decode tiles. The
header parser is unaffected: every compressed-header field is read with a
fixed probability (252 or 128), so the bit position and every header field
are correct without the blobs. `vp9_tables_ready()` reports the state, and
`Vp9Header.probs_pending` carries it to the caller.

## Remaining conformance paths

Stage 6 closes the profile-0 syntax surface for the `vp90-2-*` suite. The
paths and where they live:

| Path | Where |
|---|---|
| Segmentation map prediction across intra/inter frames (`inter_segment_id`, `get_segment_id`, temporal update, `PrevSegmentIds`) | `block.cst` 6.4.12-6.4.14; `decode.cst` 7.2/8.1 step 3; header `seg_*` state |
| Error-resilient frames (`setup_past_independence`, no `UsePrevFrameMvs`, forced parallel mode, no adaptation) | `header.cst` 6.2; `decode.cst` `_decode_slice`; `adapt.cst` `refresh_probs` |
| `frame_parallel_decoding_mode` with `refresh_frame_context`: skip load/adapt, still save when requested; counts discarded | `adapt_bridge.cst`, `adapt.cst` |
| Intra-only frames (color defaults, refresh mask, key-like partition table, no inter refs) | `header.cst`, `vp9_partition_prob`, `block.cst` |
| Tiles 1x1..64x4 (per-tile Boolean decoder, above contexts once per frame, left contexts per 8x8 row) | `tile.cst` 6.4.1-6.4.2, 7.4 |
| Loop filter deltas, sharpness and level 0 (a level-0 frame is never filtered, even with a segment ALT_L) | `loopfilter.cst` 8.8; `decode.cst` gate |
| Lossless frames (4x4 WHT, DCT_DCT type selection) | `header.cst` (tx_mode), `residual.cst`, `transforms.cst` |
| Skip blocks (skip context, `SEG_LVL_SKIP`, EobTotal-to-skip rule for inter >= 8x8) | `block.cst` 6.4.4/6.4.8/6.4.11 |
| `allow_high_precision_mv` (UseHp, class0/hp bits, counts only when read) | `block.cst` 6.4.19-20, `mvpred.cst` 6.5.12-13 |
| Switchable interpolation and compound reference modes | `block.cst` 6.4.16-17, `inter.cst` 8.5.2 |
| `show_existing_frame` and display handoff (0..8 outputs per packet, hidden frames, stored-slot output) | `decode.cst` 8.9/8.10, `refs.cst` |
| Variable frame sizes, scaled references and the 7.2.5/8.5.2.3 bounds | `header.cst` 6.2.5/7.2.5, `decode.cst`, `scale.cst`, `inter.cst` |

Everything above is implemented but **not verified**: no frame has been
decoded. The test phase must compare every displayed frame of the suite with
libvpx's MD5 (the `.md5` files beside the vectors), check the container
metadata with `ffprobe`, and compare the header fields with
`trace_headers` (`-bsf:v trace_headers`). Superframe order needs a file with
hidden alt-ref frames; variable size needs a resolution-change stream;
show-existing needs a stream whose superframe ends with a show-existing
slice.

## Errata

All four items are **UNCONFIRMED** until an external reference run confirms
them. The reference decoders below were read (not copied) to settle each
ambiguity; no test has been executed in this phase.

### E1. Partition probability selection is inverted in the prose (UNCONFIRMED)

Section 9.3.2 says:

> If `FrameIsIntra` is equal to 0, the probability is given by
> `kf_partition_probs[ctx][node2]`. Otherwise ... `partition_probs[ctx][node2]`.

That is backwards: a key/intra frame is the one that uses the fixed keyframe
table. FFmpeg's `libavcodec/vp9.c` (`decode_sb`) selects
`ff_vp9_default_kf_partition_probs[bl][c]` exactly when
`s->s.h.keyframe || s->s.h.intraonly`, and otherwise uses
`s->prob.p.partition[bl][c]`. libvpx's `set_partition_probs`
(`vp9/common/vp9_onyxc_int.h`) does the same: it points `xd->partition_probs`
at `vp9_kf_partition_probs` when `frame_is_intra_only(cm)` and at
`cm->fc->partition_prob` otherwise.

**Adopted rule.** Intra frames (key and intra-only) use
`kf_partition_probs[ctx][node2]`; inter frames use the working context's
`partition_probs[ctx][node2]`. `vp9_partition_prob` in `header.cst` is the
single accessor for this decision so stages 4-6 cannot drift.

**External check (test phase, not run).** Decode the first frame of
`vp90-2-00-quantizer-00.webm` (an intra frame, per
`ffmpeg -i ... -c copy -bsf:v trace_headers -f null -`, which prints
`frame_type = KEY_FRAME`) and compare its MD5 with libvpx's
`c3fbb7abbdb5bd4ed4a7e34768c17df1`. A wrong partition table changes the
partition tree and the frame MD5. Stage 4's target.

### E2. The `more_coefs` counting special case is missing (UNCONFIRMED)

Section 9.3.4 says "There is one special case (for `more_coefs`) that is
described at the end of this section", but neither v0.6 nor v0.7 contains
that description: the section ends with the counting table. The token syntax
in section 6.4.24 still fixes when the element is read
(`checkEob` is set at c = 0 and after every non-zero token), but not how the
missing read is counted.

**Adopted rule.** Count one entry in
`counts_more_coefs[txSz][plane>0][is_inter][band][ctx][value]` for every
position where the syntax reads `more_coefs`, at the band and context of
that position, before branching on the value. In particular:

- the read at c = 0 is counted even when it is 0 and ends the block;
- no count is added after the last coefficient when the block fills the
  transform, because no `more_coefs` is coded there;
- a run of zero tokens between two non-zero tokens produces no
  `more_coefs` read at all (only the non-zero token re-arms `checkEob`).

This is what libvpx's decoder does (`vp9/decoder/vp9_detokenize.c` counts
`eob_branch[band][ctx]` at the top of every EOB check) and what its encoder
writes (`vp9/encoder/vp9_tokenize.c` counts `eob_branch` at c = 0, after each
non-zero token and once for the final EOB). The counting is only consumed by
stage 5's `adapt_coef_probs`; stage 4 must count this way so a later frame
that refreshes its context adapts identically.

**External check (test phase, not run).** Decode a multi-frame sequence with
`refresh_frame_context = 1` and `frame_parallel_decoding_mode = 0` (libvpx's
default encoder settings) and compare every frame's MD5 with libvpx/FFmpeg.
`vp90-2-00-quantizer-00.webm` refreshes contexts between frames, so a wrong
count changes the adapted probabilities and diverges by the second frame.
Stage 5's target: every frame MD5 of that file.

### E3. Superframe frame sizes are little-endian, not f() order (UNCONFIRMED)

Annex B writes `frame_sizes[i]` with the `f(SzBytes)` descriptor, and section
4.9.1 defines `f(n)` as reading bits from high to low order, which would make
the sizes big-endian. The reference encoder writes them least-significant
byte first (`vp9/vp9_cx_iface.c`, `write_superframe_index`: `*x++ = this_sz &
0xff; this_sz >>= 8;`), libvpx's decoder reads them the same way
(`vp9/decoder/vp9_decoder.c`, `vp9_parse_superframe_index`), and FFmpeg's
`vp9_superframe_split` bitstream filter also shifts each byte up by `j * 8`.
Real superframes are therefore little-endian.

**Adopted rule.** `vp9_superframe_split` reads each size least-significant
byte first. The index marker, `bytes_per_framesize_minus_1` and
`frames_in_superframe_minus_1` are unaffected.

**External check (test phase, not run).** Split a superframe produced by
libvpx (for example a VP9 file encoded with `-auto-alt-ref 1`, which puts
hidden frames in superframes) and confirm the per-frame byte ranges match
`ffmpeg -c copy -bsf:v vp9_superframe_split` output, then decode it. A
big-endian reading produces frame sizes in the millions and fails the
sum check immediately.

### E4. Profile-0 intra-only frames never assign `color_range` (UNCONFIRMED)

Section 6.2 says that when `intra_only == 1` and `Profile == 0` the frame
codes no color config; the branch assigns `color_space = CS_BT_601`,
`subsampling_x = subsampling_y = 1` and `BitDepth = 8`, but never
`color_range`. Section 7.2.2 defines `color_range` and gives no default, so
the value is unassigned by the syntax.

Both reference decoders set studio range (0). libvpx
`read_uncompressed_header` writes `cm->color_range = VPX_CR_STUDIO_RANGE`
for this branch (with a comment that the 4:2:0 default is normative), and
FFmpeg `decode_frame_header` writes `avctx->color_range = AVCOL_RANGE_MPEG`.

**Adopted rule.** `color_range = 0` for a profile-0 intra-only frame. Inter
frames inherit it through `st.cur_color_range` until the next key or
intra-only frame sets a new value.

## Pending review notes

Low-severity notes from the stage 3 review. None changes valid-stream
output or memory safety; they are recorded here so stages 4-6 do not assume
checks the header does not perform.

1. **Frame-size ratio (section 7.2.5/8.5.2.3).** The header does not enforce
   the conformance bounds that for at least one reference `i`, all of
   `2 * FrameWidth >= RefFrameWidth[i]`, `2 * FrameHeight >=
   RefFrameHeight[i]`, `FrameWidth <= 16 * RefFrameWidth[i]` and
   `FrameHeight <= 16 * RefFrameHeight[i]` hold. Section 8.5.2.3 repeats
   those bounds where the scale factors are computed, and libvpx rejects a
   frame whose references all fail them ("Referenced frame has invalid
   size"). Stage 4/6 must not assume the header bounded the scale ratio:
   recheck it where the scale factors are built and treat a ratio outside
   1/2..16 as `I_CORRUPT`. **Stage 5 does:** `_decode_slice` requires at
   least one of the three references to pass `scale.vp9_scale_ratio_ok`,
   and `inter.cst` rechecks each reference it actually uses before
   predicting.
2. **Profile-0 intra-only `color_range`.** The value is set to 0 as the
   references do, but the specification never assigns it. Recorded as E4,
   UNCONFIRMED.
3. **`show_existing_frame` header fields.** On that path only `profile`,
   `show_existing_frame`, `frame_to_show_map_idx`, `frame_type`,
   `intra_only`, `show_frame` and `refresh_frame_flags` are meaningful.
   `width`, `height`, `mi_cols`, `mi_rows`, `sb64_cols`, `sb64_rows`,
   `render_width`, `render_height`, the color fields and `frame_is_intra`
   stay zero because neither `frame_size` nor `compute_image_size` runs
   (sections 6.2 and 7.2.6) and must not be read. `frame_type` and
   `intra_only` are copied from `st.last_frame_type`/`st.last_intra_only`.
   Stage 6 must take the shown frame's geometry and format from
   `frame_to_show_map_idx` and the `st.ref_*` arrays.
4. **Segment-map clear split.** `h.seg_map_clear` covers only the clear
   that `compute_image_size` performs when the frame size is first seen or
   changes (section 7.2.6). `setup_past_independence` (section 7.2) also
   clears `PrevSegmentIds` for the whole frame with no size condition and
   does not touch `SegmentId` for the current frame. The merged
   `_decode_slice` performs one clear before the tiles when
   `frame_is_intra == 1 || error_resilient_mode == 1 || seg_map_clear == 1`;
   `_update_seg_map` only performs the 8.1 step 3 copy. The stage 4 form and
   the stage 5 form are the same predicate, so the merge resolution
   preserved stage 4's semantics rather than introducing a new rule. The
   size-change part is not spelled out as a `PrevSegmentIds` clear in the
   specification, but indexing the old map at new dimensions would be
   undefined, so it stays. Recorded as UNCONFIRMED.

5. **Reference pool API.** The public call is now the
   `vp9_decoder_send`/`vp9_decoder_receive` pair. The output is a view into
   a pool buffer registered with `vp9_decoder_add_buffer`; no per-call
   `mem`/`cap` exists and the decoder never copies pixels. A caller that
   only wants one frame can register one buffer, but then it cannot decode
   an inter frame (which needs published references). `show_existing` needs
   no decode target at all; it copies the slot view.

6. **Coded size at the dimension ceiling.** `coded_w = round64(MiCols*8) +
   64` exceeds `core/frame.cst`'s `FRAME_MAX_DIM` once the visible side is
   above 65472, so 65472 is the largest visible side this driver accepts
   (coded 65536); a larger frame returns `I_DIMS`. The ceiling is the core
   module's, not VP9's.

7. **`adapt_bridge.cst` copies per frame.** The bridge is a layout adapter,
   not a second probability model: it converts the working set, one saved
   context and the counts, runs `adapt.cst`'s `refresh_probs`, and converts
   back. A later pass could make `adapt.cst` operate on the
   `vp9_tables.cst` layout directly, removing the copy and the duplicated
   i64 working set in `Vp9Decoder`.

8. **Stage 5 integration changes in adopted modules.** As recorded above,
   `inter.cst` and `mvpred.cst` accept an edge block that overhangs
   `MiCols*8`/`MiRows*8`, and `loopfilter.cst`'s `LfBlockInfo` gained
   `ref_stride`. These are documented at their definitions and in
   `inter.md`'s pending notes; they do not change valid-stream output.

9. **Packet ownership and drain discipline.** `vp9_decoder_send` stores
   pointers into the caller's packet and refuses to arm a second packet
   until `vp9_decoder_receive` has returned 0 for the first. That is what
   keeps every output handed out once, but it makes the packet buffer and
   the output-view lifetime caller obligations; both are documented in
   `decode.cst` and `decode.md`. A caller that forgets the final receive
   gets `I_BADPARAM` on the next send rather than silent frame loss.

10. **One output buffer outside the slots.** Because the packet handoff is
    lazy, the pool of nine is sufficient even when a superframe contains
    several displayed slices: the previous output may be reused by the next
    `receive` call, and its view expires then. A caller that wants to retain
    more than the current frame must copy it; the decoder deliberately
    allocates no display queue. If a future API wants multiple live output
    views, the pool has to grow (libvpx uses 8 + 1 + 3 = 12 frame buffers).

11. **`order` is a hint, not a timestamp.** It is a monotonic counter over
    returned frames; hidden frames do not consume one, show-existing does.
    The decoder cannot derive presentation timestamps from the bitstream,
    so the caller must keep its own timebase.

12. **Two-pass packet reuse.** A caller that gets `I_NOMEM` for a later
    slice of a superframe cannot resume that packet: the slices already
    decoded have published references and advanced the persistent state.
    Size the pool and the work block for the stream's largest frame before
    sending, or resync at the next key/intra frame. Never resend a packet
    whose decoding has already advanced. This is the same taint rule as a
    rejected header.

## Stage 5 review PENDING notes

The read-only review of stage 5 approved the branch with these low-severity
notes. They are recorded here so the next stage does not rediscover them;
none changes valid-stream output or memory safety. P1, P3, P4, P7 and P10
also carry the detailed correction in the named module note.

- **P1 — `decode.md` is a stage 4 record.** It still describes inter
  rejection, `vp9_state_publish`/`save_probs`, caller `mem`/`cap`, one-byte
  `RefFrames` and the old output path. The note now says so at the top and
  points here for the current driver; its "under 1 GiB" work-layout claim is
  corrected (with stage 5's motion arrays the 8192x8192 ceiling is about
  6.9 GiB).
- **P2 — `Vp9Decoder` size.** Corrected from "roughly 140 KiB" to roughly
  250 KiB in the ownership section above.
- **P3 — `refs.md` is not a stage 6 slice.** Its header, integration steps
  and the `publishable` versus `in_use` invariant were rewritten for the
  stage 5 driver; see that file.
- **P4 — `scale.cst` is mostly unused and duplicates `inter.cst`.** Only
  `vp9_scale_ratio_ok` is called by `decode.cst`; `inter.cst` has its own
  8.5.2.3 implementation (`inter_mv_scale`), and the rest of `scale.cst` is
  a specification-shaped reference API. Documented in `refs.md`; no code
  change.
- **P5 — uninitialised `SubModes` on an inter frame.** `block.cst` reads the
  neighbour sub-mode for an intra block, but 6.4.4 writes `SubModes` only
  for an intra block, so an inter neighbour left the value undefined. The
  driver now clears `SubModes` per frame; `MODE_DC` is 0, which is exactly
  the `DC_PRED` the reference decoders return for an inter neighbour.
- **P6 — `block.cst`'s `mctx`.** `mv_find_refs` requires a non-null
  mode-context output, but `_inter_block_mode_info` computes the block's
  inter-mode context separately; the scratch value is documented as
  intentionally unconsumed.
- **P7 — `adapt.cst`'s `clear_counts`/`setup_past_independence_probs` are
  unused in production.** The decoder uses `vp9_tables.cst`'s
  `vp9_state_clear_counts`/`vp9_state_setup_past_independence`; the
  `adapt.cst` functions remain for standalone/layout-parity use. Documented
  in `adapt.md`.
- **P8 — layout pointers before the capacity check.** `_work_carve` forms
  every pointer with base+offset arithmetic before checking `cap`; the
  pointer values are never dereferenced until the null-base, alignment and
  capacity checks pass. Documented in the function and here; moved no code.
- **P9 — one held buffer.** `vp9_decoder_decode` keeps a single `held`
  index for the last shown slice. A packet with several shown-but-
  unpublished slices can reuse an earlier one as a later decode target; that
  only costs an extra pool buffer, because the API returns only the final
  shown view. Documented at the declaration. Stage 6's per-slice
  `receive` replaces this call and hands every shown slice out before the
  next one is decoded, so no held index is needed.
- **P10 — `inter.md`'s table sentence.** "Nothing is generated and nothing
  is pending" is true of `inter_tables.cst` alone; the whole decoder still
  has the three probability blobs and the residual table blobs pending. The
  sentence now says so and points at the pending-blob sections.

**Merge-resolution note.** The stage 5 merge onto stage 4 initially left
two identical `PrevSegmentIds` clear sites. The duplicate stage 5 site was
removed; the surviving clear is the stage 4 one, immediately before
`tile_decode_all`, with the condition `frame_is_intra == 1 ||
error_resilient_mode == 1 || seg_map_clear == 1`. Stage 4's semantics and
`_update_seg_map`'s 8.1 step 3 copy are unchanged.

## Stage 6 review PENDING notes

The read-only review of stage 6 approved the branch with these low-severity
notes; none changes valid-stream output or memory safety.

- **Maximum visible side is 65472.** `FRAME_MAX_DIM` is 65536 and the coded
  surface adds a 64-sample band, so the largest visible side is 65472 with a
  coded side of 65536, not 65536 itself. The two places that quoted only
  65536 above are corrected; `decode.md` already states the 65472 limit.
- **Resend guidance.** A packet whose `receive` has already decoded and
  published slices cannot be re-sent: the published references and the
  persistent state have advanced. Pre-size the pool and work block for the
  stream's largest frame before `send`, or resync at the next key/intra
  frame; never resend an advanced packet. The `I_NOMEM` note in "Variable
  frame size" now says this.
- **`vp9_decoder_free_buffer` names a lookup, not a release.**
  `vp9_decoder_free_buffer(d)` returns the index of a registered buffer that
  no slot names; it frees nothing and the caller still owns the memory. A
  later API rename (`..._pick_free_buffer` or an explicit `idx`
  out-parameter) would remove the misleading verb. No code change now.
- **`refs.cst`'s `id` is a buffer identity, not a frame counter.** The
  field comment calls it "caller's frame counter, unique per buffer", but
  `decode.cst` passes the pool index so `vp9_refs_buffer_in_use(id)` answers
  "may this buffer be reused". Recorded in `refs.md`; the comment should say
  "caller's buffer identity (the pool index in this driver)".

## Limitations and deferred work

- Profile 0 only, 8-bit 4:2:0; other profiles, 4:2:2/4:4:4, 10/12-bit and
  CS_RGB return `I_UNSUPPORTED` (or `I_CORRUPT` where 6.2 makes CS_RGB a
  conformance violation for profile 0).
- The three probability blobs are pending (above). Until they are generated,
  tile decoding is blocked by design; header parsing, show-existing,
  mode-info syntax and the packet split are unaffected.
- The planning brief's stage-3 test file (`tests/test_vp9_headers.cst`, its
  IVF/raw fixtures and the `trace_headers` oracle commands) was **not
  written**: the coordinator removed the test phase and instructed that no
  new test suites or oracle scripts be added. `tests/run.cst` is untouched.
  No stage 4-6 tests were added for the same reason.
- Multi-threaded tile decoding, row-mt and lossless encoder support are not
  part of this stage. The SIMD kernels live in `dsp/` from stage 9 on; see
  the Stage 9 section below and `dsp/vp9_dsp.md`.

### Stage 9 scope (written, not this note's stages)

Stage 9 adds `dsp/`: a runtime dispatch table and SSE2 kernels for
intra V/H/TM, the residual add and the compound average, with the scalar
kernels of `predict.cst`, `transforms.cst` and `inter.cst` left in place as
the reference and as the fallback for every other mode, target and CPU. No
public signature changed. The DC and directional predictors, the inverse
transforms, the 8-tap interp filter and the loop filter stay scalar, and no
AVX2 kernel exists because caustic's assembler has no AVX mnemonics; the
reasons and the pending list are in `dsp/vp9_dsp.md`. Nothing was measured.

### Stage 10 scope (not this stage)

Stage 10 owns the decode example and the public `decode` hub. The threaded
path is the separate stage 9 slice: the hot kernels are already isolated in
`block.cst`, `residual.cst`, `transforms.cst`, `predict.cst`, `inter.cst` and
`loopfilter.cst`; a threaded path must either keep the above/left context
lifetimes of 7.4 per tile row or serialise them, and must sum the
per-tile counts before `vp9_adapt_frame`. None of that changes the
bitstream semantics documented here.

- **Stage 10 (`examples/decode.cst`):** the `.webm`/`.ogg`/`.opus`/`.mp3`
  decode example and the public `decode` hub. That layer maps
  `Vp9Output` to the shared `frame.Frame` plus color metadata, passes the
  packet bytes borrowed from the demuxer, and carries container timestamps,
  since VP9 codes none. The container's CodecPrivate / `vpcC` color
  overrides belong to that layer, not to the codec driver.
- The encoder is outside the decoder stages entirely.

## Reference implementations read

Only for behaviour; no code was copied, ported or transcribed.

Stage 6 read these files straight from their upstream URLs:

- libvpx `main`, `vp9/decoder/vp9_decodeframe.c` (show_existing header
  path, `read_uncompressed_header` reference checks, the `decode_tiles`
  loop-filter gate `cm->lf.filter_level && !cm->skip_loop_filter`, the
  frame-end `vp9_adapt_coef_probs`/`vp9_adapt_mode_probs` sequence and the
  `refresh_frame_context` save);
- libvpx `main`, `vp9/decoder/vp9_decoder.c` (the separate working
  `cm->fc`, `frame_contexts`, and buffer count
  `FRAME_BUFFERS = REF_FRAMES + 1 + REFS_PER_FRAME`);
- libvpx `main`, `vp9/common/vp9_entropy.c` and
  `vp9/common/vp9_entropymode.c` (adaptation reads its prior from
  `frame_contexts[idx]` and writes the working context, so the header's
  forward updates are not the adaptation prior);
- FFmpeg `master`, `libavcodec/vp9.c` (`prob_ctx` copy, compressed-header
  forward updates into `s->prob.p`, `ff_vp9_adapt_probs` adapting the saved
  context, the `refreshctx && !parallelmode` condition, and the
  frame-progress reference lifetime);
- FFmpeg `master`, `libavcodec/vp9prob.c` (`ff_vp9_adapt_probs`, the saved
  `prob_ctx` as the prior and the key/intra skip/tx copy);
- FFmpeg `master`, `libavcodec/vp9block.c` (block mode and MV syntax;
  read alongside `vp9.c` for the inter paths).

Stage 5 read these four files straight from their upstream URLs:

- libvpx `main`, `vp9/decoder/vp9_decodeframe.c` (tile decode, per-tile
  `init_bool`, `decode_block`/`decode_partition`, `dec_build_inter_predictors_sb`
  call site, skip-on-zero-Eob handling);
- libvpx `main`, `vp9/decoder/vp9_decodemv.c` (inter frame mode info,
  reference-frame and compound contexts, `read_mv`, MVs and interp filter
  contexts, `append_sub8x8_mvs` call site, multi-list ModeContext use);
- FFmpeg `master`, `libavcodec/vp9.c` (reference frame allocation and
  replacement, per-frame `ff_progress_frame` reference lifetime);
- FFmpeg `master`, `libavcodec/vp9block.c` (`decode_mode`, block contexts,
  inter mode and MV syntax, filters, intra-in-inter mode groups).

Earlier stages additionally read, for behaviour and to settle the errata:

- libvpx `main`: `vp9/decoder/vp9_dsubexp.c`, `vp9/decoder/vp9_detokenize.c`,
  `vp9/common/vp9_entropymode.c`, `vp9/common/vp9_onyxc_int.h`,
  `vp9/encoder/vp9_tokenize.c`, `vp9/vp9_cx_iface.c`,
  `vp9/decoder/vp9_decoder.c`, `vpx_dsp/bitreader.h`,
  `vpx_dsp/bitreader.c`.
- FFmpeg `master`: `libavcodec/vp9.c` (header parsing and reference checks),
  `libavcodec/vp9block.c`; FFmpeg `n6.1`:
  `libavcodec/vp9_superframe_split_bsf.c`.
- SerenityOS `master`: `LibMedia/Video/VP9/Parser.cpp`, `TreeParser.cpp`,
  `SyntaxElementCounter.*` (E2).
- The inter slice also read libvpx `vp9/common/vp9_reconinter.c` and
  `vp9/common/vp9_mvref_common.c`, and FFmpeg `libavcodec/vp9mvs.c` and
  `libavcodec/vp9recon.c`.

## Status

WRITTEN, NOT VERIFIED. Nothing in this directory has been compiled or run.
The MD5, `trace_headers`, `ffprobe`, `opus_compare` and FFmpeg-difference
checks above are recorded for the test phase and have not been executed.
