# VP9: headers, superframes and the Boolean decoder

Stage 3 of the VP9 decoder. It turns a packet into everything the frame
decoder needs except the pixels: the frame's uncompressed and compressed
headers, the reference and segmentation state that survives between frames,
the probability contexts, and the byte ranges of the tiles. It does not
decode a tile, reconstruct a frame, or filter one.

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
- Frame size with refs reads `found_ref` until one is set; a reference slot
  that was never published, or whose profile/bit depth/subsampling/color
  space differs from this frame, is `I_CORRUPT`. Inter frames inherit the
  color configuration of the last coded frame, as they have no color config
  of their own.
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

## Errata

All three items are **UNCONFIRMED** until an external reference run confirms
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

## Limitations and deferred work

- Profile 0 only; other profiles return `I_UNSUPPORTED`.
- No tile or frame decoding. `decode_tiles`, partition decoding, residual
  decoding, loop filtering and reference reconstruction belong to stages 4-6.
- `refresh_probs` / `adapt_coef_probs` / `adapt_noncoef_probs` are stage 5:
  stage 3 parses the updates into the working context and leaves adaptation
  after the tiles to the next stage.
- The three probability blobs are pending (above). Until they are generated,
  tile decoding is blocked by design.
- The planning brief's stage-3 test file (`tests/test_vp9_headers.cst`, its
  IVF/raw fixtures and the `trace_headers` oracle commands) was **not
  written**: the coordinator removed the test phase and instructed that no
  new test suites or oracle scripts be added. `tests/run.cst` is untouched.

## Reference implementations read

Only for behaviour and to settle the two errata; no code was copied, ported
or transcribed.

- libvpx `main`: `vp9/decoder/vp9_decodeframe.c` (uncompressed and compressed
  header parsing, tile sizes), `vp9/decoder/vp9_dsubexp.c` (term subexp /
  inverse remap), `vp9/decoder/vp9_detokenize.c` (coefficient counting),
  `vp9/common/vp9_entropymode.c` (defaults, `setup_past_independence`),
  `vp9/common/vp9_onyxc_int.h` (`set_partition_probs`,
  `vp9_init_macroblockd`), `vp9/encoder/vp9_tokenize.c` (encoder-side branch
  counts), `vp9/vp9_cx_iface.c` and `vp9/decoder/vp9_decoder.c`
  (superframe index writer and parser), `vpx_dsp/bitreader.h`,
  `vpx_dsp/bitreader.c` (Boolean decoder window).
- FFmpeg `master`: `libavcodec/vp9.c` (`decode_frame_header`, compressed
  header updates, keyframe partition-table selection in `decode_sb`),
  `libavcodec/vp9block.c` (block-level prob/count usage).
- FFmpeg `n6.1`: `libavcodec/vp9_superframe_split_bsf.c` (superframe index
  byte order and split checks).
- SerenityOS `master`, a third, from-scratch VP9 decoder:
  `Userland/Libraries/LibMedia/Video/VP9/Parser.cpp` (the token loop),
  `TreeParser.cpp` (counting `counts_more_coefs` once per read, by value)
  and `SyntaxElementCounter.h`/`SyntaxElementCounter.cpp` (counter sizes).
  Read to confirm the E2 rule; no code was copied.

## Status

WRITTEN, NOT VERIFIED. Nothing in this directory has been compiled or run.
The MD5 and `trace_headers` checks above are recorded for the test phase and
have not been executed.
