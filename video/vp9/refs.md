# VP9: reference frames, show-existing and scaled-reference geometry

Reference-slot module used by the stage 5 frame driver; the stage 6 show-
existing path will call the same slot map. It owns the eight reference slots
of section 5.10, the reference frame update process of section 8.10, the
output path of section 8.9 for `show_existing_frame`, and the scaled-
reference geometry of section 8.5.2.3. It does not decode a block, walk a
tile, build an inter predictor or touch pixels. `vp9_refs_show_existing` is
implemented and validated here, but `decode.cst` still returns
`I_UNSUPPORTED` for that frame type until stage 6 wires the output order.

Nothing in this directory has been compiled or run. Everything here is
**WRITTEN, NOT VERIFIED**.

## Files

| File | Holds |
|---|---|
| `video/vp9/refs.cst` | `Vp9Refs` (the eight slots / `ref_frame_map`), `Vp9RefView` (one slot), `Vp9RefFrame` (a reconstructed frame offered to section 8.10), `vp9_refs_publish`, `vp9_refs_show_existing`, the state mirror, buffer-geometry queries and all reference validation. |
| `video/vp9/scale.cst` | `Vp9Scale` and the section 8.5.2.3 formulas: the Q14 scale factors, the 1/16-sample step, base/frac/start positions, plane bounds and clipping, plus the size-ratio checks of sections 7.2.5 and 8.5.2.3. |
| `video/vp9/refs.md` | This note. |

`refs.cst` uses `core/frame.cst` for the `Frame` view it publishes,
`vp9_tables.cst` for `Vp9State` and its constants, and `header.cst` only to
validate an inter frame's three `ref_frame_idx` entries. `scale.cst` depends
on `core/errno.cst` alone; it never looks at a reference slot, so a caller
passes the reference dimensions in and the module stays pure geometry.

## Slot model and ownership

`Vp9Refs.map` is exactly the reference arrays the specification indexes:
entry `i` is what `RefFrameWidth[i]`, `RefSubsamplingX[i]`,
`RefBitDepth[i]` and `FrameStore[i]` describe, selected by bit `i` of
`refresh_frame_flags`, by `ref_frame_idx[j]` and by
`frame_to_show_map_idx`. Each entry is a `Vp9RefView`:

- a `frame.Frame` view over caller-owned memory (`pixels`/`cap`, visible
  `width`/`height`, `coded_width`/`coded_height`, the three plane pointers
  and their strides);
- the stored frame's `profile`, `bit_depth`, `subsampling_x`,
  `subsampling_y`, `color_space` and `color_range`;
- its `render_width`/`render_height`, the display hint of section 7.2.4;
- `valid`, the caller's `id` and the caller's presentation `order` hint.

The library never allocates a pixel buffer. The caller owns a pool of
buffers (one per live frame, plus the transient ninth), reconstructs each
frame in memory that **no published slot names**, publishes a view of it
into the slots named by the frame's refresh flags, and keeps it alive while
any slot still names the frame's `id`. `vp9_refs_buffer_in_use(id)` answers
when the buffer may be released. Several slots may share one decoded frame
(a key frame refreshes all eight from one buffer); such slots then carry the
same `id`, `pixels`, geometry and format. No refcount is stored: one id
query covers them all.

A slot is a publication record, never a decode target. Section 8.10 copies
`CurrFrame` into `FrameStore[i]`; this module expresses that copy by
publishing a view, so reconstruction must not write into any published
reference. Refresh flags do **not** grant a reuse exception: an inter frame
may list in `ref_frame_idx` the very slot its own `refresh_frame_flags`
will overwrite, and that old frame is still being read while the new frame
is decoded. With eight distinct live references the frame needs a ninth
buffer that no slot names. After publish, an old buffer can be freed or
reused once `vp9_refs_buffer_in_use(id)` is 0, so a pool of nine buffers
(reallocated as sizes demand while free) serves the stream. Both reference
decoders keep the current frame separate from the reference set (libvpx
allocates `new_fb_idx` from its buffer pool; FFmpeg unrefs and reallocates
`CUR_FRAME` while `s->s.refs` holds the references) and never reconstruct
over a reference.

This is deliberately one level shallower than libvpx's `ref_frame_map` plus
a `RefCntBuffer` pool. That pool exists to refcount buffers shared by many
map entries; the caller can express the same thing here with `id` and
`vp9_refs_buffer_in_use`, and the specification itself indexes `FrameStore`
by the 3-bit reference value, so a second lookup layer would only add a way
to disagree with section 8.9.

The caller assigns a unique, non-negative `id` per decoded frame and a
non-negative `order` hint. `order` is not in the bitstream; the caller
decides it from its own output-order bookkeeping and this module only
carries it back through `vp9_refs_show_existing`.

## Publishing a decoded frame (section 8.10)

`vp9_refs_publish(state, slot_map, frame)` applies step 1 of section 8.10
and mirrors the same values into `Vp9State.ref_*`, which the stage 3 header
reads for its next frame. It is the only writer of both views:
`vp9_state_publish` must not be called separately.

For each `i` in `0..7` with bit `i` set in `refresh_frame_flags`:

```
slot_map.map[i] = frame.view (+ format, render size, id, order)
state.ref_width[i]     = frame.view.width       // FrameWidth, visible
state.ref_height[i]    = frame.view.height      // FrameHeight, visible
state.ref_profile[i]   = frame.profile
state.ref_bit_depth[i] = frame.bit_depth
state.ref_ss_x[i]      = frame.subsampling_x
state.ref_ss_y[i]      = frame.subsampling_y
state.ref_color_space[i] = frame.color_space
```

`refresh_frame_flags == 0` is legal (a frame shown but not stored): no slot
and no state entry changes. The free-buffer rule still applies: decoding
into a published slot corrupts it whether or not the result is stored.
`refresh_frame_flags == 0xFF` on a key frame stores the same view into all
eight slots. A key frame that does not pass `0xFF` is rejected as
`I_CORRUPT`, because section 6.2 always sets all eight bits and accepting a
narrower mask would leave a slot in the old format alive while the stream
has moved to the new one.

Everything is validated before anything is written, so a rejected publish
leaves both structures untouched:

| Check | Result |
|---|---|
| null `state`, `slot_map` or `frame`; `frame.valid == 0`; bad `key_frame`, `refresh_flags`, `id`, `order`; null or non-positive plane pointers; luma not at the block start; stride narrower than the coded width; overlapping or truncated planes | `I_BADPARAM` |
| `profile != 0`, `bit_depth != 8`, or subsampling other than 4:2:0 | `I_UNSUPPORTED` |
| `color_space` outside 0..6 (7 is CS_RGB, barred in profile 0) or `color_range` outside 0..1; key frame without `0xFF` | `I_CORRUPT` |
| visible or coded dimension outside 1..65536, coded smaller than visible, coded smaller than the 8-sample motion-vector grid, render dimension outside 1..65536 | `I_DIMS` |
| `cap` smaller than the frame needs, including with the caller's strides | `I_NOMEM` |
| the source buffer is named by **any** published slot, including one this frame refreshes | `I_BADPARAM` |

The layout check recomputes the end of the V plane from the caller's
strides with `core/frame`'s overflow-checked `frame_mul`/`frame_add`; it
accepts any stride at least as wide as the coded width and rejects planes
that overlap or run past `cap`. It requires the luma plane to start at
`pixels` (the `frame_alloc_coded` layout) and a positive block pointer, so
the arithmetic describes the block the caller actually owns. The overlap
check is what stops a caller from decoding a new frame over a reference:
a published slot is still read through `ref_frame_idx` while the frame is
decoded, whether or not the frame refreshes it. The specification has no
meaning for a slot whose pixels changed underneath it, so it is reported
rather than silently accepted. All arithmetic in the module is
overflow-checked; no size from the header is ever used for allocation
without first passing `vp9_refs_frame_bytes`.

## Show-existing (section 8.9)

`vp9_refs_show_existing(slot_map, map_idx, out)` validates `map_idx` in
0..7 and the slot's `valid` flag, copies the stored view into `out` and
returns. It recomputes no pixels, changes no slot and touches no
`Vp9State` field: a show-existing frame has `refresh_frame_flags == 0`, so
section 8.10 is a no-op for it.

The shown frame's `w`, `h`, subsampling and bit depth are taken from the
slot, as the specification says. The caller must therefore not read
`Vp9Header.width`, `height`, `render_width`, `render_height`, `mi_cols`,
`mi_rows`, `sb64_cols`, `sb64_rows`, `frame_is_intra` or the color fields on
this path: stage 3 leaves them zero because neither `frame_size` nor
`compute_image_size` ran (stage 3 Pending review note 3). `frame_type` and
`intra_only` in the header are copied from the previous frame and are not
needed here either.

The specification names only width, height, subsampling and bit depth for
the shown frame. The color space and color range returned by this module are
the ones the frame that filled the slot was decoded with; both reference
decoders keep the buffer's color metadata and nothing in section 8.9
contradicts that, but it is not stated explicitly (U3 below).

## Frame size changes and buffers

A new `FrameWidth`/`FrameHeight` never invalidates a reference slot. A
reference of a different size is legal, bounded by the ratio checks below
and handled by scaled prediction; slots keep their own geometry. Only the
current frame needs memory for its own size.

- `vp9_refs_coded_size(width, height)` rounds the visible size up to the
  8-sample motion-vector grid (`MiCols * 8` by `MiRows * 8`): partition
  decoding truncates edge blocks to that grid, so reconstruction never
  writes past it. Publish requires the coded size to cover that grid; a
  caller may allocate a larger coded size, and any coded size at least as
  large as the grid is accepted.
- `vp9_refs_frame_bytes(width, height)` returns the I420 byte count for
  that 8-sample-grid coded size through `core/frame`, which bounds both
  dimensions by 65536 and checks every multiplication and addition. It is
  the geometry minimum, not the stage 5 driver's buffer size: `decode.cst`
  pads the coded surface to `round64(MiCols*8) + 64` and reports the larger
  `vp9_frame_size` in `d.frame_need`, because 6.4.21 writes the overhanging
  edge block into that band. Raw header fields are never used as a size.
- `vp9_refs_buffer_needs_resize(mem, cap, width, height)` returns 1 when a
  free buffer is absent (null `mem`, with a valid capacity) or too small
  for a frame of that visible size, 0 when its capacity suffices and the
  caller only has to form the view with `frame_alloc_coded`. It answers a
  capacity question about a buffer the caller already knows is free; the
  function never looks at the slot map.
- A render-size change affects only `render_width`/`render_height` per slot
  (section 7.2.4 says the render size is a hint and has no effect on
  decoding). It never requires a new buffer.

Before decoding a frame, a buffer must satisfy two things: it must hold the
coded frame size the driver reports in `d.frame_need`, and no published slot
may still name its pixels. The stage 5 driver expresses this through its
pool:

- Up to nine non-overlapping ranges are registered with
  `vp9_decoder_add_buffer`. Registration rejects overlaps, and the pool
  index is the `Vp9RefFrame.id` passed to `vp9_refs_publish`, so
  `vp9_refs_buffer_in_use(&refs, i) == 0` (slot names id `i`) and "no slot
  overlaps this range" are the same statement for a registered buffer.
  `_pick_buffer` chooses the first index that is not `held`,
  `pool_cap[i] >= need`, and not in use.
- `vp9_refs_buffer_publishable(map, mem, cap)` is the address-level form of
  the same rule, for a caller managing memory outside that pool. It checks
  every published slot's pixel range, including a slot this frame refreshes:
  there is no refresh exception, because the frame may still predict from
  the slot it overwrites.
- `vp9_refs_buffer_needs_resize(mem, cap, width, height)` is the capacity
  half for such a caller; the driver keeps the same capacity in `pool_cap`
  and compares it against the size of the frame it is about to decode.

The publish step repeats the address check (`_conflict`); a source buffer
named by any slot is rejected with `I_BADPARAM`. When a stream keeps eight
distinct frames alive, decoding any next frame needs a transient ninth
buffer; after publish, the buffers no refreshed slot names any more become
free and may be reused for later frames. That is the same lifetime rule
libvpx gets from its buffer pool.

`vp9_refs_reset(state, slot_map)` clears all slots and the `Vp9State`
reference mirror together, for decoder open or a resync path that discards
every reference. It does not touch the probability contexts or the
last-frame record; a full decode reset still needs `vp9_state_init`.

## Reference validation at use time

Section 7.2 requires the selected references to match the current frame in
profile, bit depth, chroma subsampling and color space (color range is not
part of the requirement). The stage 3 header already checks this when it
reads an inter frame's three indices; these functions repeat it so the tile
decoder never reads a slot the header did not validate against the state
that publish actually wrote:

- `vp9_refs_check_use(slot_map, idx, profile, bit_depth, ss_x, ss_y,
  color_space)` checks one reference and returns `I_CORRUPT` for an
  unpublished or mismatched slot.
- `vp9_refs_check_header(slot_map, h)` checks all three
  `ref_frame_idx` entries of an inter frame, in order. It returns 0 for an
  intra frame or a show-existing frame, which select no reference list.
- `vp9_refs_get(slot_map, idx, out)` copies the view after the same
  availability check. Inter prediction uses it to obtain the reference
  geometry and plane pointers; `scale.cst` then uses the visible
  `width`/`height` of that view, never the coded size.

Publish itself re-checks the *source* frame's format, so a slot can only
ever contain a profile-0, 8-bit, 4:2:0 frame.

## Scaled-reference geometry (section 8.5.2.3)

`Vp9Scale` is filled once per (current frame, selected reference) pair and
is immutable afterwards. The specification's `REF_SCALE_SHIFT` is **14**,
so the scale factors are Q14: `1 << 14` (16384) means a 1:1 reference. (A
"16.16" shorthand would be wrong for this syntax; the constant is part of
section 3.) Divisions truncate, and the code assumes the language's signed
right shift is arithmetic, as the reference decoders rely on (U5).

```
xScale = (RefFrameWidth  << 14) / FrameWidth
yScale = (RefFrameHeight << 14) / FrameHeight
stepX  = (16 * xScale) >> 14
stepY  = (16 * yScale) >> 14

baseX  = (x * xScale) >> 14
baseY  = (y * yScale) >> 14
lumaX  = (plane > 0) ? x << subsampling_x : x
lumaY  = (plane > 0) ? y << subsampling_y : y
fracX  = ((16 * lumaX * xScale) >> 14) & 15
fracY  = ((16 * lumaY * yScale) >> 14) & 15
dX     = ((clampedMv[1] * xScale) >> 14) + fracX
dY     = ((clampedMv[0] * yScale) >> 14) + fracY
startX = (baseX << 4) + dX
startY = (baseY << 4) + dY
```

`x`/`y` are the plane coordinates of the block's top-left sample, so for
chroma they are chroma samples while the scale factor stays the
luma-derived one; `lumaX`/`lumaY` convert back to luma for the fractional
part, exactly as the NOTE in section 8.5.2.3 requires. `stepX`/`stepY` are
in 1/16 sample units: 16 is one reference sample per current sample.

The reference plane's last sample (section 8.5.2.4) is

```
lastX = ((RefFrameWidth  + subX) >> subX) - 1
lastY = ((RefFrameHeight + subY) >> subY) - 1
```

with `sub = subsampling` for a chroma plane and 0 for luma. The block end is
`start + (count - 1) * step`; `vp9_scale_end` computes it and
`vp9_scale_clip_x`/`vp9_scale_clip_y` apply `Clip3(0, last, v)`.

The API mapping is one-to-one:

| Specification | Function / field |
|---|---|
| `xScale`, `yScale` | `Vp9Scale.x_scale`, `y_scale` (Q14) |
| `stepX`, `stepY` | `Vp9Scale.x_step`, `y_step` |
| scaled / not scaled | `Vp9Scale.scaled`, `vp9_scale_is_scaled` |
| `baseX`, `baseY` | `vp9_scale_base` |
| `lumaX`, `lumaY` | `vp9_scale_luma` |
| `fracX`, `fracY` | `vp9_scale_frac` |
| `startX`, `startY` | `vp9_scale_start` |
| scaled motion vector alone | `vp9_scale_mv` |
| `lastX`, `lastY` | `vp9_scale_last_x`, `vp9_scale_last_y` |
| `Clip3(0, last, v)` | `vp9_scale_clip_x`, `vp9_scale_clip_y` |
| end of a span | `vp9_scale_end` |

`clamped_mv_0` is the vertical (row) component and `clamped_mv_1` the
horizontal (column) one, i.e. `clampedMv[0]` and `clampedMv[1]`. The clamp
itself is section 8.5.2.2, stage 5's `mvpred.cst`, and is not repeated here;
`scale.cst` only rejects a vector large enough to break i64 arithmetic.

A one-pixel size difference can round to exactly 1.0 at a large dimension:
with `FrameWidth` 32768 and `RefFrameWidth` 32769, `(32769 << 14) / 32768`
truncates to 16384. The specification's own arithmetic then gives
`base = x`, `step = 16` and `frac = x & 15`, so the unscaled path is the
mathematically correct one and `scaled` follows the scale factors, not the
dimension comparison. The plane bounds still use the true reference
dimensions.

## Size-ratio conformance

Sections 7.2.5 and 8.5.2.3 require, for a reference to be scaled:

```
2 * FrameWidth  >= RefFrameWidth      (RefFrameWidth  <= 2 * FrameWidth)
2 * FrameHeight >= RefFrameHeight     (RefFrameHeight <= 2 * FrameHeight)
FrameWidth  <= 16 * RefFrameWidth
FrameHeight <= 16 * RefFrameHeight
```

This is Pending review note 1 from the stage 3 header, which deliberately
did not enforce it. `scale.cst` provides both levels:

- `vp9_scale_ratio_ok(fw, fh, rw, rh)` — one reference; 1 or 0.
- `vp9_scale_any_ratio_ok(fw, fh, r0w, r0h, r1w, r1h, r2w, r2h)` — the
  section 7.2.5 frame-level requirement that at least one of the three
  references passes.
- `vp9_scale_setup` — returns `I_CORRUPT` when the selected reference fails,
  because a block that actually predicts from it is using an out-of-range
  scale. With the bounds, `xScale` is in [1024, 32768] Q14 and the step is
  in [1, 32]; both are checked after the division.

The current driver uses `vp9_scale_ratio_ok` per listed reference in
`_decode_slice` and `inter.cst`'s `inter_scale_ok` per reference a block
actually predicts from; it does not call `vp9_scale_any_ratio_ok` or
`vp9_scale_setup` (see "`scale.cst` status").

## Integration points

### Current driver (stage 4-5)

1. `vp9_refs_init(&refs)` and `vp9_state_init(&st)` once per decoder;
   register up to nine non-overlapping pixel ranges with
   `vp9_decoder_add_buffer`. Registration rejects overlaps, so a pool
   index not named by any slot cannot overlap a published reference.
2. For each frame, `_pick_buffer` selects an index with `i != held`,
   `pool_cap[i] >= frame_need`, and `vp9_refs_buffer_in_use(&refs, i) == 0`;
   the driver then forms the padded coded view and zeroes it.
3. For an inter frame: `vp9_refs_check_header(&refs, &h)`, then
   `vp9_refs_get(&refs, idx, &v)` for each of the three indices. The driver
   requires at least one reference to pass `vp9_scale_ratio_ok`; the
   per-reference bound and the 8.5.2.3 arithmetic live in `inter.cst`
   (`inter_scale_ok`/`inter_mv_scale`).
4. Decode the tiles, adapt the probabilities, run the loop filter, update
   the segment/motion maps, then publish.
5. Fill `Vp9RefFrame` with `valid = 1`, `id = buf` (the pool index), the
   view, header format and render size, the caller's `order`, and
   `refresh_flags = h.refresh_frame_flags` (0..0xFF; a key frame must be
   0xFF). `vp9_refs_publish` writes the slots and the `Vp9State` mirror.
6. A buffer may be reused or freed once `vp9_refs_buffer_in_use(index) == 0`.
   `held` keeps the last shown buffer of the current superframe out of
   selection; earlier shown-but-unpublished slices are not individually
   held (see `vp9.md`, P9).

### Stage 6 (show-existing, output order)

- Tile and block decoding reads reference views only through
  `vp9_refs_get` and must never index `slot_map.map` directly.
- `vp9_refs_show_existing(&refs, h.frame_to_show_map_idx, &out)` is the
  whole output path for a show-existing frame. Do not consult the header's
  geometry or color fields (Pending note 3).
- A size change is handled by reconstructing the new frame in a free buffer
  and leaving references alone; `vp9_refs_buffer_needs_resize` answers the
  capacity question for a buffer the caller already knows is free, and
  `vp9_refs_buffer_publishable` confirms no slot names it.
- `vp9_refs_reset` discards every reference on a resync path; reset the
  rest of `Vp9State` separately with `vp9_state_init`.

## `scale.cst` status

`decode.cst` calls only `vp9_scale_ratio_ok`, once per listed reference.
The rest of `scale.cst` (`vp9_scale_setup`, the base/frac/start/mv/last/
clip/end queries and `vp9_scale_any_ratio_ok`) is a specification-shaped
reference API that the current driver does not call: `inter.cst` implements
8.5.2.3 itself in `inter_mv_scale`/`inter_scale_ok`, and the frame-level
"at least one reference" check is the loop in `_decode_slice`. The two
implementations are deliberately kept separate for now, because rewiring
`inter.cst` onto `scale.cst` is a code change with output risk and is not
needed for the merge. A later cleanup can alias one to the other or delete
the unused half; until then, `scale.cst` is a second, spec-shaped copy of
the formulas and must be kept in step with `inter.cst`.

## UNCONFIRMED items

Recorded as UNCONFIRMED; no external run has confirmed any of them. The
reference decoders were read (not copied) to see how they behave.

### U1. Per-reference versus per-frame ratio conformance (UNCONFIRMED)

Section 7.2.5 requires the bounds for at least one of the three references;
section 8.5.2.3 states them as a conformance requirement for "the reference
frame being used". This module enforces both. libvpx's
`setup_frame_size_with_refs` (`vp9/decoder/vp9_decodeframe.c`) errors only
when none of the three references has a valid size, and
`vp9_setup_scale_factors_for_frame` (`vp9/common/vp9_scale.c`) later marks a
failing reference's factor `REF_INVALID_SCALE`; FFmpeg's
`decode_frame_header` (`libavcodec/vp9.c`) sets `REF_INVALID_SCALE` for a
failing reference and errors only if no reference is valid. Neither rejects
at header time a frame that merely lists an out-of-range reference. Both
references do fail at use, though: libvpx's
`dec_build_inter_predictors_sb` calls `vp9_is_valid_scale` and raises
`VPX_CODEC_UNSUP_BITSTREAM`; FFmpeg's `inter_recon`
(`libavcodec/vp9recon.c`) sees the invalid scale and sets
`AVERROR_INVALIDDATA`. We report `I_CORRUPT` at use, which is a stricter
error class, not a stricter accept/reject decision. Untested against a real
stream until the test phase.

### U2. The section 8.5.2.4 step bound of 80 (UNCONFIRMED)

The NOTE under section 8.5.2.4 says `xStep` is "at most equal to 80 due to
the restrictions on scaling". Under the four bounds above the formula
yields at most 32, and libvpx's `vp9_setup_scale_factors_for_frame` /
`get_fixed_point_scale_factor` produce the same values. The NOTE is looser
than what the formula can produce; it changes no arithmetic here and is
recorded only so a later reader does not take it as a tighter limit.

### U3. Color space and range of a shown frame (UNCONFIRMED)

Section 8.9 specifies `w`, `h`, subsampling and bit depth for a
show-existing output, and says nothing about color space or color range.
`Vp9RefView` returns the values the stored frame was decoded with. Both
reference decoders keep the frame buffer's color metadata across a
show-existing output, but the specification does not state the rule.

### U4. A shown frame does not update the "last coded frame" record
(UNCONFIRMED)

The show-existing header codes no color config and no size, and the stage 3
parser returns before `compute_image_size`. This slice therefore leaves
`Vp9State.cur_*`, `last_*` and `have_last_frame` untouched on that path, so
a following inter frame inherits the format of the last *coded* frame, not
of the frame that was shown. The syntax implies this (nothing assigns the
values on that path), but no text says it in one sentence.

### U5. Arithmetic shift of negative motion-vector products (UNCONFIRMED)

The specification writes `>> REF_SCALE_SHIFT`; libvpx evaluates the same
expression on a signed 64-bit value and relies on an arithmetic shift. This
code assumes Caustic's `>>` on a negative `i64` is arithmetic, as C's is on
the platforms the references target. Nothing has been compiled or run, so
the assumption is unverified. A logical shift would change `base` and the
scaled motion vector for negative coordinates only.

## Decisions that are not errata

- **No `ref_frame_map` indirection.** The specification indexes
  `FrameStore[i]` by the 3-bit reference value, so `Vp9Refs.map` is that
  array. libvpx's extra map plus buffer pool exists for refcounting; its
  result is represented here by several slots sharing one buffer `id`.
- **Coded size is MiCols*8 by MiRows*8.** Partition decoding guarantees no
  block crosses the motion-vector grid, so a caller need not pad to a
  superblock. A larger buffer is accepted.
- **Reconstruction never targets a published slot.** Section 8.10 copies
  `CurrFrame` into `FrameStore[i]`; the module models that with a published
  view, so a frame is decoded into free/transient memory and only then
  published. Refresh flags grant no exception, because the frame may still
  read the slot it refreshes through `ref_frame_idx`. Publish rejects a
  source buffer named by any slot; a caller that ignored
  `vp9_refs_buffer_publishable` has already corrupted that reference.
- **Color range is stored but not compared.** Section 7.2 lists profile,
  bit depth, subsampling and color space as the match requirement; color
  range is carried for the output side only.

## Reference implementations read

Read for behaviour only; no code was copied, ported or transcribed.
`libvpx` `main`, `vp9/decoder/vp9_decodeframe.c` (reference reads and
update, `resize_context_buffers`, `setup_frame_size_with_refs`,
`read_uncompressed_header`'s show-existing branch and
`next_ref_frame_map` construction); `libvpx` `main`, `vp9/common/vp9_scale.c`
(`get_fixed_point_scale_factor`, `vp9_scale_mv`,
`vp9_setup_scale_factors_for_frame`) and `vp9/common/vp9_scale.h`
(`vp9_is_valid_scale`, `vp9_is_scaled`); FFmpeg `master`,
`libavcodec/vp9.c` (reference availability and pixfmt checks,
`update_size`, `mvscale`/`mvstep`, the show-existing branch of
`decode_frame`, `CUR_FRAME` reallocation, `next_refs` publication); FFmpeg
`master`, `libavcodec/vp9block.c` (block reference selection and where the
selected `b->ref` reaches prediction) and `libavcodec/vp9recon.c` /
`libavcodec/vp9_mc_template.c` (the `REF_INVALID_SCALE` check in
`inter_recon` and the scaled prediction it guards). The primary source
remains `.work/vp9/spec/clean.txt` sections 5.10-5.11, 6.2, 7.2.4-7.2.6,
8.2, 8.5.2, 8.9 and 8.10.

## Status

WRITTEN, NOT VERIFIED. Nothing in `refs.cst` or `scale.cst` has been
compiled, linked or run. The checks the original stage criteria would
require — libvpx/FFmpeg MD5 agreement over the `vp90-2-*` suite, `ffprobe`
dimension and show-existing output, `trace_headers` field comparison, and
the FFmpeg difference metric — have **not** been run. No test or oracle
script was added.

No new table generator or probability blob belongs to this slice; the three
pending blobs of stage 3 (`VP9_TBL_INV_MAP`, `VP9_TBL_KF_PROBS`,
`VP9_TBL_DEFAULT_PROBS` in `vp9_tables_data.cst`) are unchanged and still
need generating before tile decoding can work.
