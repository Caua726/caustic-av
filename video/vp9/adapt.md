# VP9 backward probability adaptation (`adapt.cst`)

Status: WRITTEN, NOT VERIFIED. Nothing in this note or in `adapt.cst` has
been compiled or run.

Scope: the probability half of VP9 frame decoding, taken from the VP9
Bitstream & Decoding Process Specification v0.6/v0.7, §8.3 (clear counts),
§8.4–§8.4.4 (adaptation), §9.3 (trees), §7.1.2 (frame contexts) and §6.1.2 /
§6.2 (call order). It has no frame parsing, no tile decoding and no default
tables. Every entry point is a pure function over caller-owned structs and
returns 0 (or a non-negative count) on success and a negative i64 errno on a
bad argument or out-of-range dimension.

Files:

| File | Contents |
|---|---|
| `video/vp9/adapt.cst` | constants, `Vp9Probs`, `Vp9Counts`, `Vp9ProbContexts`, `Vp9Trees`, merge/adapt/clear/load/save/refresh functions |
| `video/vp9/adapt.md` | this note |

## 1. Probability layout

`struct Vp9Probs` holds one complete probability set. Fields use the spec's
names and dimension order; probabilities are `i64` in 1..255 (the working type
for arithmetic decoding is the caller's choice, but these structs are the
canonical copies).

| Field | Dimensions | Spec reference |
|---|---|---|
| `coef_probs` | `[TX_SIZES][BLOCK_TYPES][REF_TYPES][COEF_BANDS][PREV_COEF_CONTEXTS][UNCONSTRAINED_NODES]` = `[4][2][2][6][6][3]` | §8.4.3 |
| `is_inter_prob` | `[IS_INTER_CONTEXTS]` = `[4]` | §8.4.4 |
| `comp_mode_prob` | `[COMP_MODE_CONTEXTS]` = `[5]` | §8.4.4 |
| `comp_ref_prob` | `[REF_CONTEXTS]` = `[5]` | §8.4.4 |
| `single_ref_prob` | `[REF_CONTEXTS][2]` = `[5][2]` | §8.4.4 |
| `inter_mode_probs` | `[INTER_MODE_CONTEXTS][INTER_MODES-1]` = `[7][3]` | §8.4.4 |
| `y_mode_probs` | `[BLOCK_SIZE_GROUPS][INTRA_MODES-1]` = `[4][9]` | §8.4.4 |
| `uv_mode_probs` | `[INTRA_MODES][INTRA_MODES-1]` = `[10][9]` | §8.4.4 |
| `partition_probs` | `[PARTITION_CONTEXTS][PARTITION_TYPES-1]` = `[16][3]` | §8.4.4 |
| `skip_prob` | `[SKIP_CONTEXTS]` = `[3]` | §8.4.4 |
| `interp_filter_probs` | `[INTERP_FILTER_CONTEXTS][SWITCHABLE_FILTERS-1]` = `[4][2]` | §8.4.4 |
| `tx_probs` | `[TX_SIZES][TX_SIZE_CONTEXTS][TX_SIZES-1]` = `[4][2][3]` | §8.4.4 |
| `mv_joint_probs` | `[MV_JOINTS-1]` = `[3]` | §8.4.4 |
| `mv_sign_prob` | `[2]` | §8.4.4 |
| `mv_class_probs` | `[2][MV_CLASSES-1]` = `[2][10]` | §8.4.4 |
| `mv_class0_bit_prob` | `[2]` | §8.4.4 |
| `mv_bits_prob` | `[2][MV_OFFSET_BITS]` = `[2][10]` | §8.4.4 |
| `mv_class0_fr_probs` | `[2][CLASS0_SIZE][MV_FR_SIZE-1]` = `[2][2][3]` | §8.4.4 |
| `mv_fr_probs` | `[2][MV_FR_SIZE-1]` = `[2][3]` | §8.4.4 |
| `mv_class0_hp_prob` | `[2]` | §8.4.4 |
| `mv_hp_prob` | `[2]` | §8.4.4 |

The innermost dimension of every row is the number of internal nodes of the
tree that produced it (leaves minus one):

| Tree | Leaves | Nodes | Probability row |
|---|---|---|---|
| `intra_mode` | 10 | 18 | 9 |
| `inter_mode` | 4 | 6 | 3 |
| `partition` | 4 | 6 | 3 |
| `interp_filter` | 3 | 4 | 2 |
| `tx_size_8` | 2 | 2 | 1 |
| `tx_size_16` | 3 | 4 | 2 |
| `tx_size_32` | 4 | 6 | 3 |
| `mv_joint` | 4 | 6 | 3 |
| `mv_class` | 11 | 20 | 10 |
| `mv_fr` | 4 | 6 | 3 |
| `small_token` | 3 (nodes 2,4) | 6 | 3 |
| `binary` | 2 | 2 | 1 |

`coef_probs[..][0]` is the `more_coefs` probability (`binary_tree` at node 0),
`[1]` and `[2]` are the token probabilities (`small_token_tree` from node 2:
`[1]` is `> ZERO_TOKEN`, `[2]` is `> ONE_TOKEN`).

`tx_probs` row 0 (`TX_4X4`) is never selected and holds no live probability;
rows `TX_8X8`, `TX_16X16` and `TX_32X32` are 1, 2 and 3 wide. The padding row
exists so `maxTxSize` indexes the struct directly, mirroring
`tx_probs[maxTxSize][ctx][node]` in §9.3.2.

All tree leaves are negative syntax-element values; `0` is the leaf for value
zero, so the §8.4.2 test `s <= 0` is a leaf test. `Vp9Trees` carries exactly
the trees adaptation walks (`vp9_trees_init`). A decoder may reuse or alias
those arrays for §9.3.3; the trees it additionally needs (`token_tree`,
`segment_tree`, `cols_partition_tree`, `rows_partition_tree`) are not part of
this slice.

## 2. Counts layout

`struct Vp9Counts` is exactly the §8.3 list, same dimension order:

| Field | Dimensions |
|---|---|
| `counts_intra_mode` | `[BLOCK_SIZE_GROUPS][INTRA_MODES]` = `[4][10]` |
| `counts_uv_mode` | `[INTRA_MODES][INTRA_MODES]` = `[10][10]` |
| `counts_partition` | `[PARTITION_CONTEXTS][PARTITION_TYPES]` = `[16][4]` |
| `counts_interp_filter` | `[INTERP_FILTER_CONTEXTS][SWITCHABLE_FILTERS]` = `[4][3]` |
| `counts_inter_mode` | `[INTER_MODE_CONTEXTS][INTER_MODES]` = `[7][4]` |
| `counts_tx_size` | `[TX_SIZES][TX_SIZE_CONTEXTS][TX_SIZES]` = `[4][2][4]` |
| `counts_is_inter` | `[IS_INTER_CONTEXTS][2]` = `[4][2]` |
| `counts_comp_mode` | `[COMP_MODE_CONTEXTS][2]` = `[5][2]` |
| `counts_single_ref` | `[REF_CONTEXTS][2][2]` = `[5][2][2]` |
| `counts_comp_ref` | `[REF_CONTEXTS][2]` = `[5][2]` |
| `counts_skip` | `[SKIP_CONTEXTS][2]` = `[3][2]` |
| `counts_mv_joint` | `[MV_JOINTS]` = `[4]` |
| `counts_mv_sign` | `[2][2]` |
| `counts_mv_class` | `[2][MV_CLASSES]` = `[2][11]` |
| `counts_mv_class0_bit` | `[2][CLASS0_SIZE]` = `[2][2]` |
| `counts_mv_class0_fr` | `[2][CLASS0_SIZE][MV_FR_SIZE]` = `[2][2][4]` |
| `counts_mv_class0_hp` | `[2][2]` |
| `counts_mv_bits` | `[2][MV_OFFSET_BITS][2]` = `[2][10][2]` |
| `counts_mv_fr` | `[2][MV_FR_SIZE]` = `[2][4]` |
| `counts_mv_hp` | `[2][2]` |
| `counts_token` | `[TX_SIZES][BLOCK_TYPES][REF_TYPES][COEF_BANDS][PREV_COEF_CONTEXTS][UNCONSTRAINED_NODES]` = `[4][2][2][6][6][3]` |
| `counts_more_coefs` | `[TX_SIZES][BLOCK_TYPES][REF_TYPES][COEF_BANDS][PREV_COEF_CONTEXTS][2]` = `[4][2][2][6][6][2]` |

`clear_counts` zeroes the whole struct (packed structs, no padding). Counting
belongs to the tile layer, per §9.3.4: one increment at
`[context][observed value]`. Two clamps matter:

* `token` increments `counts_token[txSz][plane>0][is_inter][band][ctx]
  [Min(2, syntax)]`, so every token value 2..10 lands in bucket 2 (the spec
  table states this explicitly).
* `more_coefs` increments `counts_more_coefs[...][syntax]`; see §7 of this
  note for the ambiguity.

## 3. Frame contexts (0..3)

`struct Vp9ProbContexts` is four `Vp9Probs` slots. Every probability table
lives in every slot — including `tx_probs` and `skip_prob`, which are simply
loaded/saved separately:

* `load_probs(ctxs, ctx, cur)` — copy every table **except** `tx_probs` and
  `skip_prob` from slot `ctx` into the working set. The working `tx_probs` and
  `skip_prob` are preserved (the function saves and restores them around the
  whole-struct copy).
* `load_probs2(ctxs, ctx, cur)` — copy only `tx_probs` and `skip_prob`.
* `save_probs(ctxs, ctx, cur)` — copy every table into slot `ctx`.
* `setup_past_independence_probs(cur, defaults)` — copy a fully initialised
  default set into the working set (probability part of
  `setup_past_independence`, §7.2).

§8.2 requires that a context be initialized (a prior `save_probs` on it)
before `load_probs`; this module cannot enforce that, so the header layer that
initializes all four contexts is responsible. Probabilities must already be in
1..255: `merge_prob` rejects 0 and values above 255 as `I_BADPARAM`, so a
zero-filled struct cannot silently adapt (this is deliberate — an uninitialized
table is a bug, not a probability of zero).

## 4. Call order

`frame(sz)` (§6.1):

```
uncompressed_header()
trailing_bits()
if (header_size_in_bytes == 0) return
load_probs(frame_context_idx)        // adapt.load_probs
load_probs2(frame_context_idx)       // adapt.load_probs2
clear_counts()                       // adapt.clear_counts
init_bool(); compressed_header(); exit_bool()
decode_tiles(...)                     // counts accumulate here
refresh_probs()                       // adapt.refresh_probs
```

`compressed_header()` (§6.3) applies the frame's **forward** updates
(`diff_update_prob`) to the working tables; they are used for this frame's
tile decode. `refresh_probs` (§6.1.2) is:

```
if (error_resilient_mode == 0 && frame_parallel_decoding_mode == 0) {
  load_probs(frame_context_idx)
  adapt_coef_probs()
  if (FrameIsIntra == 0) {
    load_probs2(frame_context_idx)
    adapt_noncoef_probs()
  }
}
if (refresh_frame_context)
  save_probs(frame_context_idx)
```

Consequences that the implementation makes visible:

* The adaptation prior is the **saved context**, not the forward-updated
  working tables: the `load_probs` / `load_probs2` calls inside
  `refresh_probs` replace them before adapting. Forward updates are therefore
  per-frame only; they do not propagate through adaptation.
* For an intra frame, `load_probs2` is not called, so the forward-updated
  `skip_prob` (from `read_skip_prob`, §6.3.8, which runs for intra frames too)
  and `tx_probs` (from `tx_mode_probs`, §6.3.2, when `tx_mode ==
  TX_MODE_SELECT`) stay in the working set and are saved by `save_probs`.
* `clear_counts` runs before the compressed header, so counts cover exactly
  the tiles of this frame.

`refresh_probs` takes all of the decisions as arguments (`error_resilient_mode`,
`frame_parallel_decoding_mode`, `frame_is_intra`, `refresh_frame_context`,
`last_frame_type`, `interpolation_filter`, `tx_mode`,
`allow_high_precision_mv`) plus `frame_context_idx` as it stands *after* the
§6.2 block. It returns a negative errno before touching anything for a bad
`error_resilient_mode`, `frame_parallel_decoding_mode`, `frame_is_intra`,
`refresh_frame_context` or `frame_context_idx`, but `allow_high_precision_mv`
is checked only inside `adapt_noncoef_probs`, i.e. after `load_probs` and
`adapt_coef_probs` have already modified `cur`, and it is never checked on an
intra frame: §6.2 parses `allow_high_precision_mv` (and `interpolation_filter`)
only in the non-intra branch, so those values are undefined for an intra frame
and must not be validated there. An error during adaptation may leave the
working set partially updated, and the caller is expected to abandon the
frame.

## 5. updateFactor (§8.4.3)

| Condition | updateFactor |
|---|---|
| `FrameIsIntra == 1` | 112 |
| otherwise `LastFrameType == KEY_FRAME` | 128 |
| otherwise | 112 |

`LastFrameType` is the previous frame's `frame_type` (`KEY_FRAME == 0`,
`NON_KEY_FRAME == 1`) and is not consulted for intra frames, matching the
§7.2 note that it is undefined for the first frame (which is intra).
`adapt_coef_probs` validates it only when `FrameIsIntra == 0`.

## 6. Adaptation details

### 6.1 `merge_prob` (§8.4.1)

```
den    = ct0 + ct1
prob   = den == 0 ? 128 : Clip3(1, 255, (ct0 * 256 + (den >> 1)) / den)
count  = Min(den, count_sat)
factor = max_update_factor * count / count_sat
out    = Round2(pre_prob * (256 - factor) + prob * factor, 8)
```

`Round2(x, 8) = (x + 128) >> 8`; all divisions have positive operands, so the
integer division is the spec's truncating one. `den == 0` gives `count = 0`,
`factor = 0` and returns `pre_prob` unchanged. No floating point is used.

Arguments are validated: `pre_prob` in 1..255, counts in 0..2^40,
`count_sat` in 1..2^40, `max_update_factor` in 0..128. The 2^40 ceiling keeps
`ct0 * 256` and `pre_prob * 256` inside i64 for any caller input; a valid
per-frame count is far below it, so exceeding it is a caller bug and returns
`I_DIMS`. The result is in 1..255 and is never clipped again.

### 6.2 `merge_probs` (§8.4.2)

Recursive in the same shape as the spec:

```
s = tree[i]
leftCount  = s <= 0 ? counts[-s] : merge_probs(tree, s, ...)
r = tree[i + 1]
rightCount = r <= 0 ? counts[-r] : merge_probs(tree, r, ...)
probs[i >> 1] = merge_prob(probs[i >> 1], leftCount, rightCount, ...)
return leftCount + rightCount
```

It is in-place: children have larger node indices than the parent, hence
larger probability indices, so `probs[i >> 1]` is read before any child
update can touch it. The return value is the total count under the node
(0..2^40) or a negative errno from a bad node/leaf/length.

The public signature carries the declared lengths (`tree_len`, `probs_len`,
`counts_len`) because Caustic arrays do not carry bounds. `probs_len` must be
`tree_len / 2`; every visited positive node must be even and inside the tree;
every leaf `-s` must be inside the counts row. This is what turns an
integration slip (wrong tree for a row, wrong counts row) into `I_DIMS`
instead of memory corruption.

`adapt_prob(prob, ct0, ct1)` is `merge_prob` with `COUNT_SAT = 20` and
`MAX_UPDATE_FACTOR = 128`; `adapt_probs(tree, ...)` is `merge_probs` from node
0 with the same constants.

### 6.3 `adapt_coef_probs` (§8.4.3)

Loops `t < 4`, `i < 2`, `j < 2`, `k < 6`, `l < maxL`, with `maxL = 3` for
band `k == 0` and 6 otherwise. Each `(t,i,j,k,l)` row runs:

* `merge_probs(small_token_tree, 2, coef_probs[t][i][j][k][l],
  counts_token[t][i][j][k][l], 24, updateFactor)` — updates entries 1 and 2;
* `merge_probs(binary_tree, 0, coef_probs[t][i][j][k][l],
  counts_more_coefs[t][i][j][k][l], 24, updateFactor)` — updates entry 0.

Count saturation is **24** here (not 20); the maximum update factor is the
`updateFactor` above.

### 6.4 `adapt_noncoef_probs` (§8.4.4)

Order and arrays, all with `COUNT_SAT = 20`, `MAX_UPDATE_FACTOR = 128`:

1. `is_inter_prob[i]` for `i < 4`
2. `comp_mode_prob[i]` for `i < 5`
3. `comp_ref_prob[i]` for `i < 5`
4. `single_ref_prob[i][j]` for `i < 5`, `j < 2`
5. `inter_mode_probs[i]` via `inter_mode_tree`, `i < 7`
6. `y_mode_probs[i]` via `intra_mode_tree`, `i < 4`
7. `uv_mode_probs[i]` via `intra_mode_tree`, `i < 10`
8. `partition_probs[i]` via `partition_tree`, `i < 16`
9. `skip_prob[i]` for `i < 3`
10. `interp_filter_probs[i]` via `interp_filter_tree`, `i < 4`, only when
    `interpolation_filter == SWITCHABLE (4)`
11. `tx_probs[TX_8X8/16X16/32X32][i]` via the three tx trees, `i < 2`, only
    when `tx_mode == TX_MODE_SELECT (4)`
12. `mv_joint_probs` via `mv_joint_tree`
13. for `comp = 0..1`: `mv_sign_prob[comp]`, `mv_class_probs[comp]` via
    `mv_class_tree`, `mv_class0_bit_prob[comp]`, `mv_bits_prob[comp][j]`
    (`j < 10`), `mv_class0_fr_probs[comp][j]` via `mv_fr_tree` (`j < 2`),
    `mv_fr_probs[comp]` via `mv_fr_tree`, and — only when
    `allow_high_precision_mv == 1` — `mv_class0_hp_prob[comp]` and
    `mv_hp_prob[comp]`

## 7. `more_coefs` counting ambiguity — UNCONFIRMED

§9.3.4 says: "There is one special case (for more_coefs) that is described at
the end of this section." The paragraph is **absent** from both the v0.6 and
v0.7 text available here: the table row
`more_coefs -> counts_more_coefs[txSz][plane>0][is_inter][band][ctx][syntax]`
is followed directly by §10 "Additional tables". The token row's
`Min(2,syntax)` clamp is printed in the table, so the missing paragraph is not
that clamp; the uncovered behaviour is when a `more_coefs` is *not* decoded.

Rule adopted, marked **UNCONFIRMED** because the normative paragraph is
missing:

* Increment `counts_more_coefs[...][syntax]` once for every `more_coefs`
  syntax element actually decoded, using the observed 0/1 value.
* §6.4.24 `tokens()` reads `more_coefs` only while `checkEob == 1`, i.e. at
  the first scan position and after every non-zero token. Positions skipped
  after a `ZERO_TOKEN` (`checkEob = 0`) do **not** increment
  `counts_more_coefs`.
* When `more_coefs == 0` the block ends and no `token` is decoded, so
  `counts_token` is not incremented for that position; only
  `counts_more_coefs[...][0]` is.

The libvpx detokenizer read for this module increments its equivalent
end-of-block count only when that bit is decoded (it does not read the bit
again after a zero token). FFmpeg's coefficient adaptation merges exactly the
three token buckets into the token tree and keeps the end-of-block decision in
the separate `more_coefs` probability. That agrees with the rule above, but
the spec paragraph that would settle it is missing, so the rule stays
UNCONFIRMED until an authoritative erratum is consulted.

## 8. Integration notes for stage 3 (header state)

The module deliberately does **not** parse or own frame-header state. The
header layer must drive it in this order.

**At sequence/decoder start.** Call `vp9_trees_init(&trees)` once; the trees
do not change. Keep one `Vp9Probs cur`, one `Vp9Counts counts` and one
`Vp9ProbContexts ctxs` in the decoder state (about 26 KiB for counts and
16 KiB per context; all caller-owned, nothing allocated here).

**In `uncompressed_header`, where §6.2 calls `setup_past_independence()`**
(`FrameIsIntra || error_resilient_mode`), after the non-probability resets
done by the header layer:

```
adapt.setup_past_independence_probs(&cur, &defaults)
if (frame_type == KEY_FRAME || error_resilient_mode == 1 || reset_frame_context == 3) {
    for i = 0..3: adapt.save_probs(&ctxs, i, &cur)
} else if (reset_frame_context == 2) {
    adapt.save_probs(&ctxs, frame_context_idx, &cur)
}
frame_context_idx = 0
```

`defaults` must be a fully initialised `Vp9Probs` holding the §10.5 default
probability tables (coefficient and non-coefficient). Those tables are not in
this module and are not yet written anywhere in this worktree: **pending**,
owned by the table/header layer. Until they exist, no caller can perform a
legal `setup_past_independence`.

`setup_past_independence` itself still owes the non-probability resets
(FeatureData/FeatureEnabled, `segmentation_abs_or_delta_update`,
PrevSegmentIds, loop-filter ref/mode deltas, `ref_frame_sign_bias`); those
belong to the header/recon state, not here.

**After `uncompressed_header` returns and before tile decode**, run the §6.1
sequence: `load_probs`, `load_probs2`, `clear_counts`, then the compressed
header. The compressed-header forward updates operate directly on `cur`;
this module does not implement `diff_update_prob` (out of slice).

**After tile decode**, call:

```
adapt.refresh_probs(&cur, &counts, &ctxs, &trees,
                    frame_context_idx,
                    error_resilient_mode,
                    frame_parallel_decoding_mode,
                    FrameIsIntra,
                    refresh_frame_context,
                    LastFrameType,
                    interpolation_filter,
                    tx_mode,
                    allow_high_precision_mv)
```

with `frame_context_idx` as set by the §6.2 block (0 after
`setup_past_independence`). `LastFrameType` is `frame_type` of the previous
frame; it is read only for inter frames.

**Counts ownership.** The tile layer increments exactly the arrays in §2 from
its own `Vp9Counts`, using the context values from §9.3.2. It must count
`token` with `Min(2, syntax)`, must not count a token when `more_coefs == 0`,
and must count `more_coefs` only when it was decoded (§7 above). If tiles are
decoded out of order or in parallel, the caller must sum the per-tile counts
before calling `refresh_probs`; the arithmetic here assumes one tally per
frame.

**Aliasing.** Stage 3/4 may keep their own probability representation and only
call `merge_prob`/`merge_probs`; the `Vp9Probs`/`Vp9Counts` layouts are the
canonical ones in this file, and the constants (`VP9_TX_SIZES`, ...) are
exported for use as array lengths and loop bounds. `Vp9Trees` is reusable for
the §9.3.3 decode process; if a separate tree table module appears, adapt may
be switched to alias it without changing the arithmetic.

## 9. Not verified

Nothing here has been compiled, run, or compared against a decoder. Checks
that a later phase must run: compile the module; drive it from the frame loop
and compare probability tables and decoded frames against the VP9 conformance
vectors (MD5 at the decoder output, ffprobe frame hashes), and validate the
`clear_counts`/adaptation state through `trace_headers` or a table dump.

## Pending review notes

The read-only review that approved this slice (commit `60e5b7d`) left five
low-severity PENDING notes. They are reproduced here verbatim; the file:line
references are those of that commit.

**PENDING (low, non-blocking):**

1. `adapt.cst:467` — comment claims "Recursion depth is the tree depth (<= 5 here)". Deepest §9.3 path is 7 nested `merge_probs` frames (`mv_class` 0→2→4→8→12→14→16; `intra_mode` 0→2→4→6→12→14→16). Comment only; trivial stack use.
2. `adapt.md:198-201` — "returns a negative errno without touching anything if a flag is outside 0/1 or an index is outside 0..3". True for `error_resilient_mode`, `frame_parallel_decoding_mode`, `frame_is_intra`, `refresh_frame_context`, `frame_context_idx`; not for `allow_high_precision_mv` (checked at `adapt.cst:626`, i.e. after `load_probs`/`adapt_coef_probs` mutated `cur`, and never on intra frames). **Recommend fixing the sentence, not the code**: §6.2 parses `allow_high_precision_mv` *and* `interpolation_filter` only in the non-intra branch (`clean.txt:1159-1163`), so they are undefined for intra frames; adding up-front validation would reject legal intra frames that pass an undefined value.
3. `adapt.md:366-369` (and the comment at `adapt.cst:948-952`) — the list of `setup_past_independence` non-probability resets omits `loop_filter_delta_enabled = 1` (`clean.txt:2563`). A header implementer following the note would miss a required reset.
4. `adapt.cst:42` — `VP9_UNCONSTRAINED_NODES` is exported but used only in comments; the row width is written literally in the type (`160`) and in the calls (`559`, `583`).
5. `adapt.cst:493, 507` — child-node validation checks evenness and `+1 < tree_len` but not `child > i`; a caller-built cyclic tree would recurse until the stack dies. Unreachable through `Vp9Trees` produced by `vp9_trees_init`, so it cannot affect valid streams; a cheap `l > i` check would complete the stated "mismatched call is an `I_DIMS`, not corruption" promise.
