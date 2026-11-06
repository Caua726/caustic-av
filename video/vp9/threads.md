# VP9 tile threading: work split, contexts, lifetime and errors

Stage 9b. Optional multi-threaded tile decoding for the profile-0 VP9
decoder. The single-threaded path is untouched in behaviour: the threaded
path is chosen per frame only when a caller binds a worker block, asks for
more than one thread and the frame carries more than one tile column.

Status: **WRITTEN, NOT VERIFIED.** Nothing in this stage has been compiled
or run. No fps figure was measured, and the checks listed at the end have
not been executed.

## Where the work is split

VP9 tiles are only partly independent. Specification section 2 defines a
tile as a region "intended to be able to be decoded and encoded
independently" and immediately qualifies it:

> For VP9 this intention is only partially met. Partitioning into columns
> works as expected, but partitioning into rows does not. The decode of a
> tile depends on the decode of the tile above.

The reason is in 6.4/7.4: `clear_above_context()` runs once before the
first tile (7.4.1), `clear_left_context()` runs at the start of every 8x8
mode-info row inside a tile (7.4.2), and the note after 6.5.2 adds that
"information from a block in a different tile can be used if the block is
above, but not if the block is to the left". So the above partition,
segment-prediction and nonzero contexts, the above mode-info entries, and
the above pixel row at the top of a tile row all come from the tile above.

The parallel unit is therefore a **tile column for the whole frame**: worker
*w* decodes tile columns `[first, last)` of **every** tile row, in ascending
row order. Two workers therefore never touch the same column of any array:

- tiles of different columns in the same row are independent by the spec's
  column rule, and
- the tile-above dependency of a column is resolved inside the worker that
  owns that column, because that worker decoded the previous tile row of the
  same columns first.

This is the same decomposition FFmpeg's slice-threaded VP9 decoder uses
(one slice job per tile column, all tile rows decoded by that job) and the
same constraint libvpx encodes with `assert(tile_rows == 1)` in
`decode_tiles_mt`: libvpx only enables tile workers when there is a single
tile row, because its above contexts are shared without column ownership.
This decoder keeps the shared above arrays but assigns every column to
exactly one worker, which is why it can thread a multi-row tile grid too.

The split itself is fixed and contiguous: worker *w* gets tile columns
`[w*cols/k, (w+1)*cols/k)`, so every worker gets at least one column when
`k <= cols`. There is no work stealing and no size-based ordering; the
assignment does not depend on scheduling, and tiles in one row are assumed
to cost roughly the same. Uneven tiles only cost idle time, never
correctness.

What each worker does per tile is exactly what the sequential walk does:
`get_tile_offset` for the mode-info bounds, a Boolean decoder over that
tile's `h.tiles.off/len` slice, `_decode_tile` (8x8 rows, `blk_clear_left`
per row, partition tree), and the 9.2.3 exit check.

## Context isolation

| State | Owner | Why it is safe |
|---|---|---|
| Above partition context (`above_part`) | Frame-shared, one column range per worker | Indexed by the global mode-info column; a tile writes only `[MiColStart, MiColEnd)` of its own column. |
| Above segment-prediction context (`above_segpred`) | Frame-shared, per column | Same indexing. |
| Above nonzero contexts (`above_nz`, three planes) | Frame-shared, per 4x4 column range | Indexed by the global 4x4 column (`(sx>>2)+i`); a tile's writes stay inside its columns, chroma included. |
| Left partition/segment/nonzero contexts | Private per worker | Cleared at every 8x8 row of the tile (7.4.2) and indexed by the global mode-info row; two workers at the same row would collide if shared. |
| `Vp9CoefCtx` | Private per worker | Points at the shared above arrays and the worker's own left arrays, with the frame's 4x4 limits. |
| 1024-entry coefficient buffer, inter-prediction scratch | Private per worker | Reused sequentially inside one worker. |
| Section 9.3.4 counters | Private per worker | Incremented by every mode-info/token read; summed after the join (see below). |
| Mode-info arrays (`mi_sizes`, `tx_sizes`, `skips`, `segment_ids`, `ref_frames`, `y_modes`, `sub_modes`, `interp_filters`, `mvs`, `sub_mvs`) | Frame-shared, disjoint cells | `_decode_block` writes `[c, c+x_mis) x [r, r+y_mis)` of its own block, and a block never straddles a tile column: get_tile_offset returns multiples of 8 and the partition tree stays inside the superblock. |
| `prev_seg_ids`, `prev_ref_frames`, `prev_mvs` | Read-only during tiles | Written by `_update_seg_map`/`_update_prev_mv` after the join. |
| Pixel planes of the decode target | Frame-shared, disjoint blocks | Every block writes only its own plane rectangle. A worker's intra prediction reads the row above, which is either in its own tile row or in the previous tile row of the same tile column, which it decoded itself. The loop filter runs later, on the joined frame. |
| Header, probability tables, published reference slots | Read-only | Tile decode only reads `Vp9State.probs` and the `refs.cst` views; adaptation and publication run after the join. |

The block context (`Vp9Blk`) is copied per worker by `blk.blk_copy`, with
the private pointers above overwritten by `threads.cst`. Adding a field to
`Vp9Blk` therefore requires deciding whether it is shared or private; the
copy starts from the shared template, so forgetting to override a field
makes it shared rather than dangling.

## Pool lifetime and memory

Nothing allocates. The caller provides:

- the existing persistent work block (`vp9_decoder_bind`) — unchanged, one
  per decoder;
- a second 8-byte aligned block for the worker slots
  (`vp9_decoder_bind_threads`), sized with `vp9_threads_work_size(width,
  height, workers)` for the largest frame the decoder will see;
- the thread handles, which live in `Vp9Threads`, a field of the
  caller-owned `Vp9Decoder`.

The thread block holds, per worker: one `Vp9Blk`, one `Vp9CoefCtx`, one
`Vp9Counts` and one group of `[left_part arows][left_segpred arows]
[left_nz 3*(2*arows+16)][coef 1024*i64][scratch INTER_SCRATCH_BYTES]`, all
8-byte aligned. The above context arrays and every frame-wide work array
stay in the work block. `vp9_threads_size` (and the decoder-level
`vp9_threads_work_size`) compute the layout before any frame exists;
`vp9_threads_prepare` carves it again per frame from the frame's actual
`MiCols`/`MiRows` and reports `I_NOMEM` with `d.thr.need` set if the block
is too small. The caller then re-binds a larger block (between packets) and
decodes the frame again; there is no internal growth.

For scale: a worker slot is about 56 KiB at 1080p and about 119 KiB at the
8192x8192 mode-info ceiling (the `Vp9Counts` set alone is 25.9 KiB and the
inter scratch 20.2 KiB); 16 workers therefore need roughly 0.9-1.9 MiB,
plus 2 MiB of stack per spawned thread created by `std/thread.cst`.

Threads are spawned per frame, never kept idle between frames: for a frame
with `k` workers, workers `1..k-1` are spawned, worker 0 runs on the caller
thread, and every started worker is joined before `tile_decode_all`
returns. If `thread.spawn` fails for some worker, its column range is
decoded by the caller when the join loop reaches that worker's slot (spawn
refusal is not a decode error), so every tile is decoded exactly once.
`vp9_decoder_free` only forgets the block; it must not be called while a
packet is being drained.

Worker count semantics: the count includes the calling thread (`2` = one
spawned worker plus the caller), values above `VP9_MAX_WORKERS` (16) are
clamped, the per-frame count is also clamped to the frame's tile-column
count, and `0`/`1`, an unbound block, or a single-tile-column frame take the
sequential walk.

## Error and taint rules

- Every tile range in the frame is validated (`off >= 0`, `len >= 1`,
  `off + len <= frame slice`) before any worker starts. Workers repeat the
  check before touching a slice, so no worker can be handed an offset it has
  to trust.
- A worker stops at the first tile whose Boolean decoder, partition walk or
  exit check fails and records that tile's (row, column) in its job.
- After all work is complete, the failure with the smallest
  `(row, column)` — the earliest failing tile in the raster order the
  sequential walk uses — is returned. Scheduling cannot change which tile
  that is: tiles are deterministic functions of their columns' earlier data,
  a worker never reads another column, and a worker never reads a tile it
  did not decode itself. All tiles raster-before the earliest failure are
  therefore decoded with the same inputs the sequential walk would use.
- On failure `Vp9State.counts` is **not** updated with the per-worker
  totals, the loop filter, `_update_seg_map`, `_update_prev_mv`, adaptation
  and publication do not run (the caller returns before them), and the
  decode-target buffer is not published. The persistent state is tainted
  until the next key or intra-only frame, exactly as the sequential error
  path documents. The next header parse clears the counts again.
- Every spawned worker is joined before the frame returns, error or not, so
  a failed frame never leaves a thread reading the work block.

Two observable differences from the sequential path exist only for corrupt
streams: the threaded walk validates the whole tile table up front (the
sequential walk validates lazily, tile by tile), and a worker stops at the
first failure in its own columns rather than at the first failure in raster
order, so the returned code for a *multi-error* frame can differ. Both paths
return a negative code and leave the frame unpublished; no valid stream is
affected.

## Interactions with frame flags

- **`frame_parallel_decoding_mode`**: the flag only decides whether
  `refresh_probs` (8.4) adapts probabilities from the counts after the
  frame. Tile decode itself never adapts and never reads a shared counter,
  so the flag needs no special handling: per-worker counters are summed into
  `Vp9State.counts` in worker order and the sequential code path consumes
  the same total. With the flag set, `refresh_probs` ignores the totals,
  exactly as before.
- **`error_resilient_mode`**: forces parallel decoding in 6.2 and no
  probability save; the same reasoning applies. It also clears
  `PrevSegmentIds` before the tiles, which `_decode_slice` still does before
  the pool is touched.
- **Counts clearing**: `vp9_header_parse` clears `Vp9State.counts` for every
  coded frame (8.3); `vp9_threads_prepare` clears the per-worker counters at
  the same point in the frame, so the sum after the join equals the
  sequential accumulation.
- **Tile-size table**: `h.tiles` (the per-tile byte ranges parsed by
  `header.cst`) is read-only and validated up front by the threaded walk;
  `h.tiles.count` must equal `tile_cols * tile_rows`, as before.

## What remains sequential

Everything outside the tile columns: header parsing, superframe splitting,
reference validation and buffer selection, the coded-surface clear, the
`PrevSegmentIds` clear, probability adaptation, the loop filter of 8.8, the
8.1/8.10 segment-map and motion-field updates, reference publication,
show-existing output and the send/receive handoff. The loop filter is
deliberately not threaded: it spans tile edges and the whole frame must be
reconstructed first.

## Interface

New, additive; no existing signature changed except the internal
`tile.tile_decode_all` (which gains a pool pointer and is only called by
`decode.cst`):

| Function | Meaning |
|---|---|
| `vp9_threads_work_size(width, height, workers)` | Bytes for the worker block for frames up to `width x height`. `0` when `workers < 2` (no block needed), negative on a bad size. |
| `vp9_decoder_bind_threads(d, mem, cap, workers)` | Attach the worker block and request `workers` threads (calling thread included). `0`/`1` disables and needs no block. `I_BADPARAM` for a null/unaligned block with `workers > 1`. |
| `vp9_decoder_set_threads(d, workers)` | Change the count without touching the block; returns the effective count or `I_BADPARAM` if a block is missing. |

`vp9_decoder_send`, `vp9_decoder_receive`, `Vp9Output`, the work block and
the pixel-pool API are unchanged; the default remains the sequential path.
Enabling threads changes neither the packet handoff nor the output bytes.

## What is not done

- No row-mt (superblock-row jobs inside a tile), no work stealing, no
  tile-size-based ordering.
- No threaded loop filter, deblocking across tiles, or threaded adaptation.
- No persistent thread pool: spawning is once per frame, not once per
  decoder.
- No fps measurement, no profiling, no benchmark of any kind (policy for
  this stage).
- Lossless, profiles 1-3 and the pending table blobs are unchanged by this
  stage.

## UNCONFIRMED

- **Above-context sharing across tile rows.** This module relies on section
  2's note (a tile depends on the tile above) and on 7.4.1 clearing the
  above contexts only once before the first tile. libvpx
  (`decode_tiles_mt`) refuses tile threading when `log2_tile_rows != 0`,
  which is consistent with the note but does not by itself prove that above
  contexts carry entropy state across tile rows; FFmpeg's
  `decode_tiles_mt` decodes all tile rows per column with shared above
  arrays, which supports it. UNCONFIRMED: whether a conforming encoder may
  reset above contexts at a tile-row boundary (this decoder would then need
  a per-row clear under `frame_parallel_decoding_mode == 0`). Bit-exactness
  against libvpx/FFmpeg on a stream with `tile_rows_log2 > 0` settles it.
- **Chroma nonzero context columns.** The chroma above-nonzero index maps a
  luma mode-info column `c` to chroma 4x4 column `c` (`subsampling_x == 1`),
  a subrange of the luma columns `[2c, 2c+2)`. Tiles of different columns
  still occupy disjoint index ranges, so the column-ownership argument
  holds, but the exact chroma index mapping repeats the pre-existing
  `residual.cst`/`block.cst` convention and is not re-derived here.
- **Spawn refusal as "not an error".** Treating a failed `thread.spawn` as
  "decode this range on the caller" is a policy choice (it keeps output
  identical and avoids failing a frame for a resource shortage); no
  reference decoder was found that forces a different policy. UNCONFIRMED
  as a requirement.
- **Error code for multi-error corrupt frames.** The threaded path reports
  the earliest failing tile in raster order among the failures its workers
  recorded; it does not continue decoding after a failure to discover
  earlier-in-raster-order failures in *other* columns that its own worker
  never reached. Valid streams are unaffected; the exact code for a doubly
  corrupt frame is UNCONFIRMED against libvpx/FFmpeg.

## Checks for the test phase (NOT RUN)

- Decode the vp90-2-* suite twice per stream, once with
  `vp9_decoder_set_threads(d, 1)` and once with the thread block bound and
  `n = 2, 4, 8`, and compare output bytes/MD5 per frame; they must match
  each other and the single-threaded baseline.
- Repeat with a stream whose `tile_rows_log2 > 0` and with
  `tile_cols_log2 = 0` (fallback), and with a frame at each tile-grid
  corner (1x1, 64x1, 1x4, 64x4).
- Run under ThreadSanitizer or helgrind over a few full streams; no race
  reports on the work/reference arrays.
- Corrupt one tile in the middle of a frame and check that the returned
  code and the "not published" state match the sequential path for the same
  corruption.
- ffprobe/ffmpeg difference and `opus_compare` are unrelated to this stage;
  fps is explicitly not measured.

## Reference implementations read

Only for behaviour; no code was copied, ported or transcribed.

- libvpx `main`: `vp9/decoder/vp9_decodeframe.c` (the single-threaded
  `decode_tiles` raster order and `clear_above_context` placement, the
  `decode_tiles_mt`/`tile_worker_hook` column split and its
  `assert(log2_tile_rows == 0)`, `tile_data->xd.counts = NULL` when
  `frame_parallel_decoding_mode`, and the per-tile `left_context` clears);
  `vp9/decoder/vp9_decoder.c` (worker data allocation, `init_mt`);
  `vp9/common/vp9_onyxc_int.h` (`set_skip_context`'s global-column above
  indexing, `set_mi_row_col`'s tile-left availability);
  `vp9/common/vp9_blockd.h` (per-plane above/left context pointers);
  `vp9/common/vp9_loopfilter.c` (frame-level filter masks, why the filter is
  a whole-frame pass).
- libvpx `main`: `vp9/common/vp9_reconintra.c` (intra edge extension) and
  the `vp9_reconinter.c` entry points were skimmed for the above-pixel
  reads the column argument relies on.
- FFmpeg `master`: `libavcodec/vp9.c` (the `decode_tiles_mt` job-per-tile-
  column loop over all tile rows, `active_tile_cols`, per-tile
  `td->counts`, the post-join count summation, and `intra_pred_data`
  per-column saved rows); `libavcodec/pthread_slice.c` (job/result array,
  main thread participating); `libavcodec/pthread_frame.c` (thread context
  handoff, for the lifetime model); `libavcodec/vp9recon.c` (intra edge
  availability, `have_top`/`have_left` selection).
