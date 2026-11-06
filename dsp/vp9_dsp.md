# DSP: VP9 acceleration

Stage 9. This directory holds the VP9 kernels that are worth running as
machine code, the runtime table that selects them, and the scalar fallbacks
that stay in the codec modules. Everything here is **WRITTEN, NOT VERIFIED**:
nothing has been compiled or run, and no measurement exists. In particular
there is no fps number and no kernel-level benchmark: this note says what the
code is meant to do, never that it does it faster.

## Files

| File | Holds |
|---|---|
| `dsp/vp9_dsp_abi.cst` | The `Vp9Dsp` table, the `VP9_DSP_*` feature bits and the kernel ABI. Imports nothing, so the portable dispatcher and the architecture modules can both name it without a cycle. |
| `dsp/vp9_dsp.cst` | The table's storage, the one-time install (`vp9_dsp_init`), the accessor every hook calls (`vp9_dsp_table`) and `vp9_dsp_features`. |
| `dsp/x86_64/vp9_cpu.cst` | CPUID probe: SSE2, SSE4.1, AVX, AVX2 bits. |
| `dsp/x86_64/vp9_intra.cst` | SSE2 intra predictors: V, H, TM for 4/8/16/32. |
| `dsp/x86_64/vp9_idct.cst` | SSE2 residual add (8.6.2 step 4) for 4x4, 8x8, 16x16, 32x32. |
| `dsp/x86_64/vp9_interp.cst` | SSE2 compound average, `Round2(a + b, 1)`. |
| `dsp/x86_64/vp9_dsp_x86.cst` | The install pass: probe, then publish the kernels whose instruction set the CPU has. |

## Dispatch

```
video/vp9/predict.cst    V/H/TM  ──┐
video/vp9/transforms.cst residual ──┼──> dsp.vp9_dsp_table() ──> slot non-null?
video/vp9/inter.cst      compound ──┘                             ├ yes: call it
                                                                  └ no : scalar kernel,
                                                                         which stays
                                                                         in the codec
```

A null slot means **the codec's own scalar kernel**, not an error and not a
second implementation kept here. The scalar code stays where the process is
described (`predict.cst`, `transforms.cst`, `inter.cst`), remains the
reference the kernels are checked against, and is what runs on every target
and CPU without a kernel. Nothing in `dsp/` duplicates it, so there is exactly
one definition of each process.

The hooks are placed after the caller's validation and add no public
interface: no function signature in `video/vp9/` changed, no struct gained a
field, and the only internal change is that `predict.cst` now builds its
`aboveRow`/`leftCol` arrays as `u8` (they hold samples and the 127/129
substitutes, so their values are unchanged) so the kernels can take them
directly.

### Ownership, lifetime and races

- The table is one zeroed struct in `.bss` inside `dsp/vp9_dsp.cst`. Nothing
  is allocated and nothing is freed; the pointers point into the image.
- Installation is a **monotonic publish**: each slot is written once per
  install pass with a value that depends only on the machine (the CPUID
  result), so two threads installing at the same time store identical values.
  An 8-byte aligned pointer store does not tear, so a reader sees either the
  initial zero or a finished kernel pointer — never a half-built one. A reader
  that sees zero runs the scalar kernel, which is correct, so a lost race
  costs speed and nothing else.
- `g_dsp_state` is only a fast path that skips the CPUID probe. It is written
  with `atomic_store` and read with `atomic_load` (the language's atomics are
  sequentially consistent), so a thread that observes 2 also observes every
  slot written before it. A racing installer repeats the work rather than
  waiting, because waiting would need a futex this module must not own.
- Hooks read the table once per block, not once per sample: one atomic load
  and a null test per prediction, per residual block and per compound block.

## Feature detection

`vp9_cpu_probe_x86_64` is a `with naked` function: the body is asm only, CPUID
is the one instruction the assembler has a mnemonic for and the rest is test
and branch, so no compiler value is live across it.

| Bit | Source | Gates |
|---|---|---|
| `VP9_DSP_SSE2` (1) | leaf 1, EDX bit 26 | every kernel written so far |
| `VP9_DSP_SSE41` (2) | leaf 1, ECX bit 19 | nothing yet |
| `VP9_DSP_AVX` (4) | leaf 1, ECX bit 28 | nothing yet |
| `VP9_DSP_AVX2` (8) | leaf 7.0, EBX bit 5 | nothing yet |

SSE2 is part of the x86_64 baseline, so on any x86_64 CPU the check passes
and all kernels are published; the check exists because the kernels still
declare what they need, and a later kernel gated on SSE4.1 or AVX2 must test
its own bit. AVX and AVX2 are reported from CPUID alone: the XCR0 test
(OSXSAVE, `XGETBV`) that a VEX-encoded kernel would also require is **not**
implemented, because no kernel here uses one.

Targets: the x86_64 module is imported only under
`__target_is_x86_64 - __target_is_windows == 1`. Each statement that names it
is guarded by `abi.VP9_X86_SYSV`, an `imut` global that folds to 1 exactly on
x86_64 System V, so on every other target the branch is dead and the name of
the module that was not loaded is never resolved. The install pass itself
aborts the build with `__compile_error` if it is reached with
`VP9_X86_SYSV == 0`, which catches a direct import of `dsp/x86_64/`. The
kernels read the System V argument registers (rdi, rsi, rdx, rcx, r8, r9) and
use only caller-saved registers, so a Windows x86_64 build keeps the scalar
path. AArch64 installs nothing (there is no NEON kernel).

## Kernels

Every kernel is `with naked`: no prologue, no epilogue, arguments read
straight from the ABI registers, `ret` written in the asm. All of them return
nothing, take integer/pointer arguments only, and write their block directly
into the caller's plane. `dsp/vp9_dsp_abi.cst` carries the exact ABI;
repeated here for review:

```
intra:     void fn(u8 *dst, i64 stride, const u8 *above, const u8 *left, i64 size)
residual:  void fn(u8 *dst, i64 stride, const i32 *dq, i64 n0)
compound:  void fn(u8 *a, i64 a_stride, const u8 *b, i64 b_stride, i64 w, i64 h)
```

`above` points at `aboveRow[0]`, so `above[-1]` is the top-left sample; `left`
points at `leftCol[0]`. Both are already substituted for an unavailable side
(127/129), so a kernel knows nothing about availability.

### `vp9_x86_pred_v` (V_PRED, 8.5.1)

Copies the above row into every output row: one dword (4x4), one qword (8x8)
or one/two `movdqu` (16x16, 32x32) per row. Reads `2*size` bytes of the above
buffer at most, which the caller guarantees.

### `vp9_x86_pred_h` (H_PRED, 8.5.1)

Row i is `leftCol[i]` repeated. The repeated byte is built in a general
register by shift-and-or (byte → 2 → 4 → 8 bytes), which needs no shuffle
instruction, then stored once per 8 samples (16x16 twice, 32x32 four times).
Reads only `left[0..size-1]`.

### `vp9_x86_pred_tm` (TM_PRED, 8.5.1)

`Clip1(above[j] + left[i] - above[-1])`, sixteen samples at a time:

1. `punpcklbw` against a zero register widens the low eight above samples to
   16-bit lanes; `psrldq` by 8 moves the high eight down and the same widening
   produces the second group.
2. `left[i] - above[-1]` is computed in a general register, reduced to 16
   bits, duplicated inside a dword and broadcast with `pshufd`, giving the
   offset in all eight 16-bit lanes.
3. `paddw` adds it. It cannot wrap: a sample is 0..255 and the offset is
   -255..255, so the sum stays inside a signed 16-bit lane.
4. `packuswb` maps the 16-bit lanes back to bytes with **unsigned
   saturation**, which is precisely section 4.6's `Clip1`.

For size 32 the second sixteen samples repeat steps 1-4. For size 4 and 8 the
full 16 bytes are computed and only `size` bytes are stored, so the lanes fed
by bytes outside the block cannot reach the frame.

### `vp9_x86_add_residual` (8.6.2 step 4)

Adds the transformed residual to the prediction and clips. Eight samples per
iteration (four for 4x4):

1. two `movdqu` loads take eight i32 coefficients;
2. `packssdw` packs them to eight 16-bit lanes;
3. the eight predicted bytes are loaded with `movsd` (zeroing the rest of the
   register) and zero-extended to lanes with `punpcklbw` against zero;
4. `paddsw` adds the prediction **with signed saturation**;
5. `packuswb` clips to bytes.

Why this is bit-identical to the scalar i64 loop for *any* i32 residual:

- `packssdw` saturates a coefficient to [-32768, 32767]. A coefficient outside
  that range can only push the result to 0 or 255, which is where it clamps
  anyway, so the saturation is order-preserving.
- `paddsw`, not `paddw`: 255 + 32767 would wrap to -33014 in a plain 16-bit
  add and `packuswb` would then produce 0 where the scalar path produces 255.
  Signed saturation keeps the sign and the clamp.
- `packuswb` is `Clip1`.

The coefficient loads read `n0*n0*4` bytes in total and never cross the block:
the last group of the last row ends exactly at the last coefficient.

### `vp9_x86_compound_avg` (8.5.2 compound)

`pavgb` is `(a + b + 1) >> 1` per byte, which is `Round2(a + b, 1)` for the
two already-clipped predictions — no separate clip is needed, exactly as the
scalar loop states. Sixteen bytes per iteration; the result replaces the first
prediction in place. The hook requires `w` to be a multiple of 16 (the widths
are 4/8/16/32/64), so 4 and 8 wide compound blocks keep the scalar loop.

## Why the kernels are `.byte`

caustic's assembler has no mnemonic for packed byte or word arithmetic, for a
shuffle, or for any VEX-encoded instruction: its instruction table
(`caustic-assembler/lexer/lexer.cst`, `init_ht`) holds 118 instruction ids,
and the only packed-integer operations among them are `paddd`, `paddq`,
`psubd`, `psubq`, `pxor` and `psrldq`, plus the 128-bit loads/stores
(`movdqu`, `movaps`, `movupd`, `movd`, `movq`) and the scalar float forms.
`punpcklbw`, `paddw`, `paddsw`, `packuswb`, `packssdw`, `pshufd` and `pavgb`
are all outside it, so the instructions these kernels need cannot be written
by name.

They are therefore emitted through the assembler's documented escape hatch:
`.byte` with the encoding from the Intel SDM, register-only forms (modrm =
`11 ddd sss`), one instruction per `asm(".byte ...")` call, with the mnemonic,
the opcode map and the modrm it encodes on the line above. `.byte` exists for
exactly this (`caustic-as`'s `.ripdisp` note calls it "an instruction written
out with .byte"), and the compiler's own AVX2 backend is emitted the same way
(`src/codegen/emit_vec.cst` emits VEX forms as `.byte` sequences).

The consequence is a hard boundary for this stage: **no AVX2 kernel can be
written today**, because AVX2 has neither mnemonics nor a reviewable way to
hand-encode a few dozen VEX instructions inside a write-only phase. The probe
reports the AVX2 bit so a later stage can gate a kernel on it.

Encodings used (legacy `66` prefix, 128-bit operands, `modrm` in the comment
above each line):

| Instruction | Encoding |
|---|---|
| `punpcklbw` | `66 0F 60 /r` |
| `paddw` | `66 0F FD /r` |
| `paddsw` | `66 0F ED /r` |
| `packuswb` | `66 0F 67 /r` |
| `packssdw` | `66 0F 6B /r` |
| `pshufd` | `66 0F 70 /r ib` |
| `pavgb` | `66 0F E0 /r` |

## Alignment, strides and buffers

- **No alignment requirement.** Every wide access is `movdqu`, `movss` or
  `movsd`; the kernels never use an aligned form (`movdqa`, `movaps`) and
  never assume a 16-byte boundary on a plane pointer or a stride.
- `stride` is a byte stride and must be at least the block width; the kernels
  advance the row pointer by it and never read or write outside
  `[row, row + size)` for each of the `size` rows.
- The intra kernels may read up to 32 bytes of the above row starting at
  `above[0]` (two 16-byte loads for 32x32; one for every other size, including
  4x4 and 8x8 where only `size` bytes are used). `predict.cst` builds that row
  in a 65-entry buffer and passes a pointer one past its first entry, so the
  read stays inside the buffer for every size.
- The residual kernel reads `n0*n0` i32 coefficients from `dq` and exactly
  `n0` bytes of each destination row.
- The compound kernel reads and writes exactly `w` bytes per row for `h` rows
  in both buffers.

## What is still scalar

Every one of these runs the existing scalar kernel on every target; the
column says why a kernel is not here yet.

| Process | Why scalar |
|---|---|
| DC_PRED (8.5.1) | Expressible with `psadbw` (SSE2) in a straight line, but the per-block work is one sum and a shift while the cost is the store, so the win is small; no kernel written rather than a crowded one. |
| D45/D63 (8.5.1) | Both are a 1-D 3-tap (or 2-tap) filter of the above row followed by row slices of the filtered array, so a vector form exists. It needs two prefiltered arrays and a scalar tail per size, and it was not written in this stage. |
| D117/D135/D153/D207 | Diagonal propagation: row `i` is a shifted copy of row `i-2` (or `i-1`) plus a column seed. A kernel has to write into the plane row by row and seed columns separately; not written. |
| Inverse DCT/ADST 4..32, WHT | Needs 16-bit multiplies (`pmullw`/`pmaddwd`), lane shuffles and lane-wise arithmetic shifts, all outside the assembler's table, plus a transpose. High instruction count per kernel and no way to check a hand-encoded form in this phase. The 32x32 DCT is the clearest future win. |
| Dequantisation (8.6.2 steps 1-2) | Needs a lane multiply of 32-bit coefficients by a quantiser; no `pmulld` in the assembler. |
| 8-tap interp H/V (8.5.2.4) | The sample-parallel form exists only when the reference is unscaled (`step_x == 16`); a scaled reference gives every sample its own phase and needs a per-sample path. Neither was written. |
| Loop filter (8.8.5, 8.8.6) | Per-edge with data-dependent masks and a transpose for the horizontal pass; needs `pminub`/`pmaxub`/`pcmpeqb`/`psubusb`-class instructions. Not written. |

`lf_filter_plane` therefore has no hook: there is no kernel to call, and an
empty dispatch would be dead code.

The compiler's own SIMD backend (`--cpu=auto`, SSE2/AVX2 chosen at run time by
the Caustic compiler) may vectorise some of these scalar loops. That is not
claimed here and has not been measured; it is mentioned because it is the
other reason the scalar kernels were kept as plain loops over contiguous
arrays.

## UNCONFIRMED

1. **`.byte` encodings against caustic-as.** The opcodes are from the Intel
   SDM and each line states its encoding, but no assembler has been run on
   them in this phase. Whether caustic-as object output and the linker accept
   every `.byte` sequence unchanged is unverified.
2. **`with naked` argument registers.** The kernels assume the SysV argument
   registers exactly as `std/thread.cst`'s `_clone_thread` does. That a
   `fn_ptr` call through `call()` reaches a naked function with arguments in
   rdi/rsi/... is verified by reading the compiler's call lowering, not by
   execution.
3. **AVX2 OS support.** The AVX/AVX2 bits are raw CPUID bits; the XCR0 test
   is absent. No kernel depends on it yet.
4. **Wide loads on small blocks.** Every intra kernel loads 16 bytes at a
   time, so a 4x4 or 8x8 prediction reads above-row bytes beyond the block
   width; those lanes are computed and discarded (the store is `movss` or
   `movsd`). libvpx and FFmpeg keep the same kind of headroom around their
   above arrays; here it is `predict.cst`'s 65-entry buffer, argued by the
   geometry above but not checked by any test in this phase.
5. **Reference agreement.** libvpx and FFmpeg substitute the edge arrays and
   index the left column in their own (inverted) order before calling their
   kernels; the kernels here take the spec's order (leftCol[i] is row i, as
   `predict.cst` builds it). The equivalence is argued, not compared.

## Pending work

1. Generate nothing: these kernels need no table and no blob.
2. AVX2 kernels, when the assembler can encode them, or a decision to
   hand-encode VEX forms in a phase where they can be assembled and compared.
3. The DC, D45/D63, D117/D135/D153/D207, DCT/ADST, 8-tap interp and loop
   filter kernels listed above, in that order of expected value.
4. A size policy for the small blocks: a 4x4 kernel call may cost more than
   the scalar loop it replaces. Nothing has been measured, so the table is
   size-agnostic on purpose.
5. AArch64 NEON: nothing written, nothing installed, scalar everywhere.

## Verification (all NOT RUN)

The checks stage 9 would need before it can claim anything: a build through
`caustic-mk` at `-O1` and at the optimizing backend, the scalar-versus-kernel
byte comparison for every mode and size (a temporary harness that calls both
and compares, which does not exist), the VP9 conformance vectors through
`trace_headers`/MD5, and an fps measurement against the scalar build. None of
these was executed in this phase.

One gap worth stating plainly: no target in `Causticfile` imports the video
tree at all (`tests/run.cst` compiles the core and Matroska cases only), so
neither these kernels nor the stage 3-6 decoder are compiled by
`caustic-mk run test`. Compiling them at all requires a driver that imports
`video/vp9/decode.cst`, which does not exist yet (stage 10). Every construct
used here was checked by reading against the existing users in this repository
and in `std/`; none has been through the compiler in this phase.
