# MP3 Layer III decode path

Stage 8b. This note covers `mp3_decode.cst` and `mp3_synth.cst`: the front
end from one parsed `Mp3Frame` to requantized spectral values, and the
reconstruction/synthesis kernels that produce interleaved s16 PCM.

Status: **WRITTEN, NOT VERIFIED.** Nothing in this directory has been
compiled or run. The FFmpeg PCM-difference check named by the stage brief is
recorded below for the test phase and has not been executed.

> Stage 8a owns `mp3.cst`, `mp3_tables.cst`, the table data blobs and the
> table generator. This branch is rebased on stage 8a's approved tip
> (`stage8a-mp3`, ca4a564); the identifiers in the table contract below are
> the ones that tip exports, checked by reading, not by compiling.

## Files

| File | Holds |
|---|---|
| `audio/mp3/mp3_decode.cst` | `Mp3Decode`, main-data assembly and reservoir, MPEG-1 and LSF scalefactor decode, requantization gains, Huffman big_values/count1, MS/intensity stereo, frame driver. |
| `audio/mp3/mp3_synth.cst` | `Mp3Synth`, short-block reordering, alias reduction, 36/12-point IMDCT, windows and overlap, frequency inversion, 32-band polyphase synthesis, s16 output. |

There is no `mp3_decode_tables.cst`: the decode-only constants this stage
needs (count1 tables, MPEG-1 slen, LSF `nsf` partitions, pretab, scalefactor
band widths/offsets, alias coefficients, synthesis window) are all exposed by
stage 8a's `mp3_tables.cst`. The IMDCT windows and cos matrices are analytic
and are computed at init with `std/math.cst`; no pending blob is read.

## API

```c
struct Mp3Decode;   // caller owns one, typically embedded in a decoder object

i64 mp3_decode_init(Mp3Decode *m);
i64 mp3_decode_open(Mp3Decode *m, *u8 src, i64 len);   // init + mp3_open
i64 mp3_decode_reset(Mp3Decode *m);                    // clear history, keep stream
i64 mp3_decode_frame(Mp3Decode *m, Mp3Frame *f,
                     *u8 pcm, i64 cap);
```

- `mp3_decode_init` zeroes the object, builds the analytic tables and the
  intensity pan table. It must be called once before any other call.
- `mp3_decode_open` is the convenience entry: `mp3_decode_init` followed by
  `mp3_open(&m->stream, src, len)`.
- `mp3_decode_reset` clears reservoir, overlap, polyphase history and the
  per-frame scratch but keeps the computed tables and the open stream. It is
  the intended call after a seek or after a decode error.
- `mp3_decode_frame` consumes a frame already obtained with
  `mp3_next(&m->stream, &frame)` and writes
  `samples_per_frame * channels * 2` bytes of little-endian signed 16-bit
  PCM to `pcm`. `cap` is in bytes. It returns frames per channel (1152 for
  MPEG-1, 576 for MPEG-2/2.5), or a negative error.
- No allocation from untrusted sizes: the persistent buffers are fields of
  `Mp3Decode` and `Mp3Synth`, and the kernels use fixed-size stack arrays
  (`block[36]`, `p[24]`, `o[12]`, `u[512]`, `s_in[32]`, `out[32]`,
  `tmp[576]`, and the Huffman `code[256]`/`len[256]` plus count1
  `ccode[16]`/`clen[16]`) whose sizes are compile-time constants. Every
  stream offset, part length and band offset is checked before use.

The ticket shorthand `mp3_decode_frame(m, frame, pcm, cap)` is split in two
arguments here (`m` for persistent decode state, `f` for the parsed frame)
because stage 8a's `Mp3` carries the stream parser state, not the synthesis
history. `Mp3Decode` embeds both `Mp3` and `Mp3Synth`.

## Main data and reservoir

`Mp3Frame.main_data_offset` (absolute in `Mp3.src`) and `main_data_len`
locate the bytes after this frame's side information. `main_data_begin`
(`frame.side.main_data_begin`, equal to `frame.reservoir_back`) is how many
bytes to take from the end of the saved previous main-data tail.

```
logical_main = saved_tail[reservoir_len - back .. reservoir_len]
             + src[main_data_offset .. main_data_offset + main_data_len)
```

After the frame is decoded (success or error) the last 512 bytes of
`logical_main` replace the saved tail. That is exactly the window a later
backpointer can address; no byte copying beyond the one contiguous
`m->main` buffer is done. A backpointer larger than the saved tail is
`I_EOF`; the parser rejects one larger than the main data already returned,
so a clean stream starting at frame 0 has `main_data_begin == 0`.

`part2_3_length` is a bit budget per granule/channel over that logical
stream. Each part is decoded in `[start, start+part2_3_length)`; padding is
skipped, overshoot is `I_CORRUPT`, and a short stream is `I_EOF`. MPEG-1
processes two granules per frame; MPEG-2/2.5 processes one.

## Decode flow

```
mp3_decode_frame
  validate header/layer/free-format/channels/lengths, cap
  assemble logical main data, BitsMsb over it
  per granule:
    per channel:
      validate block_type/window_switching
      scalefactors: MPEG-1 (slen1/slen2, scfsi) or LSF (mp3_lsf_nsf)
      fill per-line requantization gains in Huffman order
      Huffman big_values (tables 0..31, linbits/signs) then count1 (A/B)
      skip to the part2_3 boundary
    MS/intensity stereo over both channels
    per channel:
      reorder short blocks (in place), alias reduction
      IMDCT 36/12, window, overlap-add
      frequency inversion
      polyphase synthesis -> 576 PCM frames
  save reservoir tail
```

Important ordering choices:

- Stereo is applied **before** reordering, as in the reference decoders;
  reordering then maps the short-block Huffman order
  `[sfb][window][line]` to the subband/time order the IMDCT consumes.
- Alias reduction runs for long blocks (all 32 subbands), for the two (or
  four at 8 kHz) long subbands of a mixed block, and not for a pure short
  block.
- Frequency inversion is applied to the 18 overlap-added samples of every
  odd subband before synthesis.

## Numeric conventions

Everything after Huffman is `f64` in the natural units of ISO/IEC 11172-3
2.4.3.4.

- Requantization follows the spec formula exactly:
  `is^(4/3) * 2^((global_gain - 210)/4) * 2^(-scalefac_multiplier *
  (scalefac + pretab))`, with the short-block `subblock_gain` term.
  `scalefac_multiplier` is 0.5 for `scalefac_scale == 0` and 1 otherwise.
- IMDCT uses the analytical expression
  `x[i] = sum_k X[k] cos(pi/(2n) (2i+1+n/2)(2k+1))`, n = 36 or 12, with no
  extra `2/n` factor. Windows are the four spec shapes (normal, start, stop,
  short), computed at init.
- The 32-point synthesis matrix is
  `N[i][k] = cos((16+i)(2k+1)pi/64)`, i = 0..63, used unnormalised (its
  gain is 32) exactly as printed. No compensating division is applied.
- The synthesis window `D[0..511]` is Table 3-B.3 read through
  `mp3_synth_window(i)` as the dimensionless printed value (range about
  -1.145..+1.145), not the Q16 integer.
- PCM conversion multiplies the natural sample by 32768, clamps to
  [-32768, 32767] and rounds away from zero, then writes two little-endian
  bytes.

Requantization is the whole scaling chain. 2.4.3.4 states that "all
necessary scaling factors are contained within this formula", that the
constant 210 is a system constant, and that the synthesis filterbank is
implemented "according to the formulas below", with PCM output in [-1, 1].
So requant x unnormalised IMDCT x N x D is already full scale, and
`MP3_IMDCT_SCALE` is 1. Dividing the IMDCT window output again (for example
by 1/32 for the unnormalised matrix) double-counts and makes the output
about 30 dB too quiet. The absence of a `2/n` IMDCT factor and the unity
global scale are the two choices a PCM comparison can confirm; everything
else is the spec formula.

## Table contract with stage 8a

Identifiers checked against stage 8a's tip (all public in `mp3_tables.cst` /
`mp3.cst`; not every constant is referenced by name, several counts appear
as literals in the loops below):

- versions/modes: `MP3_VER_1`, `MP3_VER_2`, `MP3_VER_25`,
  `MP3_MODE_JOINT`; `mp3_is_mpeg1`, `mp3_sfb_row`.
- scalefactor bands: `mp3_sfb_long_width`, `mp3_sfb_long_offset`,
  `mp3_sfb_short_width`; `MP3_SFB_LONG_COUNT` (22, tail width included),
  `MP3_SFB_SHORT_COUNT` (13, tail width included).
- scalefactors: `mp3_slen1`, `mp3_slen2`, `mp3_lsf_nsf(context, block_case,
  partition)`, `mp3_pretab`.
- Huffman: `mp3_huff_table_valid`, `mp3_huff_linbits`, `mp3_huff_load`,
  `MP3_HUFF_MAX_CODE_BITS`; `mp3_count1_load`, `mp3_count1_table_count`.
- reconstruction: `mp3_alias_c`, `mp3_synth_window`.
- parser types: `Mp3`, `Mp3Header`, `Mp3Granule`, `Mp3SideInfo`,
  `Mp3Frame`, `mp3_open`.

`mp3_lsf_nsf`'s `block_case` is 0 long, 1 pure short, 2 mixed; `context` is
the 0..5 LSF scalefac_compress range. `mp3_huff_load` indexes code/len by
`x*16+y`; `mp3_count1_load` by `(v<<3)|(w<<2)|(x<<1)|y`, matching the
decoder's symbol masks.

The stage 8a IMDCT/window blob placeholders and `mp3_window_tables_ready()`
are deliberately not consumed: the analytic windows/matrices are built at
init, and the only tabulated reconstruction vector is Table 3-B.3 through
`mp3_synth_window`. If 8a later exposes its own IMDCT tables, the init code
can read them instead; the kernel layout is unchanged.

## What is supported

- MPEG-1 Layer III at 32/44.1/48 kHz, mono/stereo/dual/joint, CBR and VBR
  frame headers (VBR is just a sequence of headers; Xing/VBRI fields belong
  to the parser).
- MPEG-2 LSF at 16/22.05/24 kHz and MPEG-2.5 at 8/11.025/12 kHz, including
  the LSF four-partition scalefactors and implied preflag.
- MS stereo, MPEG-1 intensity stereo, LSF intensity stereo, mixed blocks,
  start/stop/short windows, all 32 Huffman tables and both count1 tables.
- Free-format bitrate, Layers I/II and non-Layer-III headers return
  `I_UNSUPPORTED` rather than guessing.

Not implemented here: CRC verification (the parser locates the frame; a bad
CRC is not used to mute), error concealment/PLC, emphasis/de-emphasis
filtering, and gapless trimming (see below). A decode error returns a
negative code and leaves the caller to call `mp3_decode_reset` if it wants
to continue.

## Gapless trimming

Neither stage trims audio. Stage 8a's parser reads the Xing/LAME and VBRI
fields into `Mp3` (`encoder_delay`, `encoder_padding`, `lame_present`,
`vbri_delay`) and `mp3.cst` states that no audio is trimmed;
`mp3_decode_frame` returns every decoded frame. Dropping the encoder
delay and padding belongs to the layer that owns the stream (the caller of
`mp3_next`/`mp3_decode_frame`), because only that layer knows the first and
last frame of a track and can drop samples across frame boundaries.
`mp3.md` (stage 8a) is aligned to this policy in this branch: no stage owns
gapless trimming, and the parser fields are the interface for whoever does.

## Pending and unconfirmed

- **UNCONFIRMED (normalisation).** The global scale is unity because
  2.4.3.4 states that the requantization formula contains all scaling
  factors and that the printed synthesis yields PCM in [-1, 1], and because
  the IMDCT is the unnormalised expression. The value is not confirmed by a
  run: an FFmpeg PCM comparison is still the check, and `MP3_IMDCT_SCALE`
  in `mp3_synth.cst` is the single place to adjust it. A further division,
  such as the 1/32 an earlier draft applied for the unnormalised matrix,
  makes the output about 30 dB too quiet.
- **UNCONFIRMED (top-band intensity position).** The tail band (long sfb
  21; short sfb 12 for each window) has no transmitted scalefactor, hence no
  is_pos. This decoder reuses the previous band's position in that window,
  as FFmpeg does (`k = (i == 21) ? 20 : i`, and the last short band shares
  the previous band's k). minimp3 instead substitutes its neutral default
  (3 for MPEG-1, 0 for LSF) when the band below carries data and the
  previous position otherwise; the ISO text does not cover the tail band.
  Only frames with left-only data in the tail band differ.
- **UNCONFIRMED (LSF intensity maximum).** ISO 11172-3 says the maximum
  MPEG-1 intensity position (7) is illegal. For LSF this note treats 0..15
  as legal (matching the reference table), rather than declaring 15
  illegal; the spec wording in the bitstream note is ambiguous. Recorded,
  not run.
- **UNCONFIRMED (LSF scalefactor tables).** `mp3_lsf_nsf` values are stage
  8a's; the LSF four-partition split and counts in `_scf_lsf` follow the
  pseudocode in the bitstream note 4.2.4 and ISO/IEC 13818-3. They have not
  been checked against a real LSF stream.

The Table 3-B.3 synthesis window D is committed as stage 8a's blob
(`MP3_SYNTH_WINDOW_NANO` in `mp3_tables_data.cst`) and `syn_init` reads it
through `mp3_synth_window`; no path here depends on an unwritten blob. The
stage 8a `MP3_WINDOW_*` IMDCT-window placeholders stay unused: this stage
builds those windows analytically at init.

## Reference reading

These were read to understand behaviour only; no code was copied, ported or
transcribed. Public docs are the primary source:

- `lieff/minimp3`, `minimp3.h` — Layer III decode order, intensity
  top-band handling, IMDCT/window scaling.
- `FFmpeg/FFmpeg`, `libavcodec/mpegaudiodec_template.c` — decode order,
  MS/intensity stereo including the last-band scale-factor reuse, the
  short-block IMDCT layout.
- `FFmpeg/FFmpeg`, `libavcodec/mpegaudiodec_common.c`, `mpegaudiodata.c`,
  `mpegaudiodsp_template.c`, `mpegaudio.h`, `mpegaudiodsp.c`,
  `dct32_template.c` — table dimensions, window/`FRAC_BITS` scaling and the
  synthesis kernels.
- `.work/mp3/docs/iso11172-3.pdf`, `mp3_bitstream.pdf`, `mp3_theory.pdf` —
  the 3-B.3 window and 3-B.9 alias text, count1 tables, sfb tables,
  side-info syntax, requantization and stereo sections; plus
  `.work/docs/iso11172-3.txt` and `mp3_bitstream.txt` for the extracted
  requantization and intensity-stereo text quoted above.

## Oracle check for the test phase (NOT RUN)

The brief's original criterion is the maximum absolute difference from
FFmpeg on the same decoded frames. No MP3 vector is committed in this
branch (`tests/vectors/small/` holds Opus and VP9 files only), and there is
no `tests/test_mp3.cst`; both still have to be added in the test phase.
Suggested method, to be executed only in the test phase:

1. Obtain or cut MPEG-1 44.1 stereo CBR, MPEG-2 22.05 mono, MPEG-2.5
   11.025 stereo and VBR Xing/LAME vectors; no vector is committed today.
2. Decode the same span with this decoder and with `ffmpeg -f s16le`.
   Neither stage trims, so compare untrimmed frame-aligned output; if the
   FFmpeg demuxer has already dropped the LAME delay/padding, drop the same
   sample counts from ours using the parser's `encoder_delay` /
   `encoder_padding`. The first decoded frame is frame 0 on both sides.
3. Record `max(abs(ours - ffmpeg))` per rate/channel/mode and use it as the
   tolerance of a future `tests/test_mp3.cst`.

No command above has been run.

## Pending review notes

Notes on this stage's files from the read-only review of stage 8b. None
affects valid-stream output or memory safety. The first three were
corrected in the fix round; the last two are open and recorded for the next
reader.

1. **Fixed (P1).** The note and `mp3_synth.cst` described the Table 3-B.3
   synthesis window as a pending, zero-filled blob whose silence only the
   oracle would catch. The blob is committed in `mp3_tables_data.cst`
   (`MP3_SYNTH_WINDOW_NANO`); the comment and this note now say so. The
   scalar accessor still returns 0 for a malformed blob (stage 8a review
   note 4), which is only reachable with a damaged in-tree blob.
2. **Fixed (P2).** "All buffers are fields of `Mp3Decode`" was false: the
   kernels use fixed-size stack arrays. The API section now lists them and
   states the real property (no size comes from the stream).
3. **Fixed (P3).** The oracle step decoded "the committed small vectors"
   and named `tests/test_mp3.cst`; neither exists. The step now says the
   vectors and the test have to be obtained or written in the test phase.
4. **Open (naming).** The tail-band intensity substitution in `_stereo`
   uses the literal indices 21/20 and 12/11. They follow
   `MP3_SFB_LONG_SCALEFAC_COUNT` (21) and `MP3_SFB_SHORT_SCALEFAC_COUNT`
   (12) but are not written in terms of them; the surrounding band loops
   use the same literals, so a count change has to touch all of them
   together.
5. **Open (wording).** `_intensity_gains` clamps an MPEG-1 position to
   0..6 although `_illegal_pos` already rejects 7 and above before the call;
   the clamp only guards a negative value, which no caller reaches.
