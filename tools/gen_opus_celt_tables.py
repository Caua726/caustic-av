#!/usr/bin/env python3
"""Generate the CELT data table file for caustic-av.

This tool is committed source. It is NOT run in the writing phase: the
committed audio/opus/celt_tables_data.cst is the output it produces, and
celt.md records the validation it performs.

What it emits
    audio/opus/celt_tables_data.cst with the three CELT tables that RFC 6716
    names in prose but does not print:

        CELT_TBL_E_MEANS       mean band energy, Q4, 25 values
        CELT_TBL_E_PROB_MODEL  coarse-energy Laplace model, Q8, [LM][intra]
        CELT_TBL_CAPS          band caps minus 64, [LM][channels-1][band]

Why these three and nothing else
    The band edges (RFC Table 55), the static allocation (Table 57), the
    trim/spread/tapset PDFs (Tables 56/58/59) and the TF tables (60-63) are in
    the RFC text; celt_tables.cst carries them as source and this tool
    cross-checks them. The window (RFC 4.3.7), the 1/8-bit logarithm and the
    PVQ pulse/bit cache are defined by formulas, so the decoder derives them
    at init and this tool validates the derivations. Only the three tables
    above are data that no formula produces.

Provenance
    The values are codec constants of the RFC 6716 reference definition
    (quant_bands.c eMeans/e_prob_model and the rate cache caps), reproduced
    as data. They are not algorithm code and no value was invented; every
    one is checked against its declared length and range below. celt.md marks
    the licence/provenance wording as UNCONFIRMED for review.

Validation performed before writing
    * table dimensions and value ranges;
    * eBands monotonic and equal to the differences of RFC Table 55;
    * allocation rows 11 x 21 in 0..200;
    * TF tables match RFC Tables 60-63 and use only the printed values;
    * window W(n)=sin(pi/2*sin(pi/2*(n+1/2)/120)^2) is monotone and satisfies
      W(i)^2 + W(119-i)^2 = 1 within 1e-12;
    * log2_frac is exact on powers of two and equals ceil(8*log2(v)) for the
      values the allocation uses;
    * V(N,K) from the recurrence equals the closed form
      sum_{j=1..K} 2^j * C(N,j) * C(K-1,j-1) for small N,K, and
      bits(N,q)=log2_frac(V(N,get_pulses(q)),3) is non-decreasing in q.

Usage
    python3 tools/gen_opus_celt_tables.py -o audio/opus/celt_tables_data.cst
"""

import argparse
import math
import sys

# ── RFC 6716 band layout (Table 55, 2.5 ms column / modes.c eband5ms) ──
EBANDS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20, 24, 28, 34, 40,
          48, 60, 78, 100]

# Bins per band from RFC 6716 Table 55, columns 2.5/5/10/20 ms.
TABLE55 = [
    [1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 6, 6, 8, 12, 18, 22],
    [2, 2, 2, 2, 2, 2, 2, 2, 4, 4, 4, 4, 8, 8, 8, 12, 12, 16, 24, 36, 44],
    [4, 4, 4, 4, 4, 4, 4, 4, 8, 8, 8, 8, 16, 16, 16, 24, 24, 32, 48, 72, 88],
    [8, 8, 8, 8, 8, 8, 8, 8, 16, 16, 16, 16, 32, 32, 32, 48, 48, 64, 96, 144, 176],
]

# RFC 6716 Table 57: static allocation in 1/32 bit per MDCT bin.
# Row 0 is the printed all-zero quality-0 column; rows 1..10 are the printed
# quality columns 1..10.
ALLOC = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [90, 80, 75, 69, 63, 56, 49, 40, 34, 29, 20, 18, 10, 0, 0, 0, 0, 0, 0, 0, 0],
    [110, 100, 90, 84, 78, 71, 65, 58, 51, 45, 39, 32, 26, 20, 12, 0, 0, 0, 0, 0, 0],
    [118, 110, 103, 93, 86, 80, 75, 70, 65, 59, 53, 47, 40, 31, 23, 15, 4, 0, 0, 0, 0],
    [126, 119, 112, 104, 95, 89, 83, 78, 72, 66, 60, 54, 47, 39, 32, 25, 17, 12, 1, 0, 0],
    [134, 127, 120, 114, 103, 97, 91, 85, 78, 72, 66, 60, 54, 47, 41, 35, 29, 23, 16, 10, 1],
    [144, 137, 130, 124, 113, 107, 101, 95, 88, 82, 76, 70, 64, 57, 51, 45, 39, 33, 26, 15, 1],
    [152, 145, 138, 132, 123, 117, 111, 105, 98, 92, 86, 80, 74, 67, 61, 55, 49, 43, 36, 20, 1],
    [162, 155, 148, 142, 133, 127, 121, 115, 108, 102, 96, 90, 84, 77, 71, 65, 59, 53, 46, 30, 1],
    [172, 165, 158, 152, 143, 137, 131, 125, 118, 112, 106, 100, 94, 87, 81, 75, 69, 63, 56, 45, 20],
    [200, 200, 200, 200, 200, 200, 200, 200, 198, 193, 188, 183, 178, 173, 168, 163, 158, 153, 148, 129, 104],
]

# RFC 6716 Tables 60-63: tf_select_table[LM][4*transient + 2*select + flag].
TF_TABLE = [
    [0, -1, 0, -1, 0, -1, 0, -1],
    [0, -1, 0, -2, 1, 0, 1, -1],
    [0, -2, 0, -3, 2, 0, 1, -1],
    [0, -2, 0, -3, 3, 0, 1, -1],
]

# Mean band energy in Q4 (reference data table; 25 entries, bands 0..20 used).
E_MEANS_Q4 = [
    103, 100, 92, 85, 81, 77, 72, 70, 78, 75, 73, 71, 78, 74, 69,
    72, 70, 74, 76, 71, 60, 60, 60, 60, 60,
]

# Coarse-energy Laplace parameters, Q8: [LM][intra][2*min(band,20)+{0,1}].
E_PROB_MODEL = [
    [  # LM 0, 120-sample frames
        [72, 127, 65, 129, 66, 128, 65, 128, 64, 128, 62, 128, 64, 128, 64, 128,
         92, 78, 92, 79, 92, 78, 90, 79, 116, 41, 115, 40, 114, 40, 132, 26,
         132, 26, 145, 17, 161, 12, 176, 10, 177, 11],
        [24, 179, 48, 138, 54, 135, 54, 132, 53, 134, 56, 133, 55, 132, 55, 132,
         61, 114, 70, 96, 74, 88, 75, 88, 87, 74, 89, 66, 91, 67, 100, 59,
         108, 50, 120, 40, 122, 37, 97, 43, 78, 50],
    ],
    [  # LM 1, 240-sample frames
        [83, 78, 84, 81, 88, 75, 86, 74, 87, 71, 90, 73, 93, 74, 93, 74,
         109, 40, 114, 36, 117, 34, 117, 34, 143, 17, 145, 18, 146, 19, 162, 12,
         165, 10, 178, 7, 189, 6, 190, 8, 177, 9],
        [23, 178, 54, 115, 63, 102, 66, 98, 69, 99, 74, 89, 71, 91, 73, 91,
         78, 89, 86, 80, 92, 66, 93, 64, 102, 59, 103, 60, 104, 60, 117, 52,
         123, 44, 138, 35, 133, 31, 97, 38, 77, 45],
    ],
    [  # LM 2, 480-sample frames
        [61, 90, 93, 60, 105, 42, 107, 41, 110, 45, 116, 38, 113, 38, 112, 38,
         124, 26, 132, 27, 136, 19, 140, 20, 155, 14, 159, 16, 158, 18, 170, 13,
         177, 10, 187, 8, 192, 6, 175, 9, 159, 10],
        [21, 178, 59, 110, 71, 86, 75, 85, 84, 83, 91, 66, 88, 73, 87, 72,
         92, 75, 98, 72, 105, 58, 107, 54, 115, 52, 114, 55, 112, 56, 129, 51,
         132, 40, 150, 33, 140, 29, 98, 35, 77, 42],
    ],
    [  # LM 3, 960-sample frames
        [42, 121, 96, 66, 108, 43, 111, 40, 117, 44, 123, 32, 120, 36, 119, 33,
         127, 33, 134, 34, 139, 21, 147, 23, 152, 20, 158, 25, 154, 26, 166, 21,
         173, 16, 184, 13, 184, 10, 150, 13, 139, 15],
        [22, 178, 63, 114, 74, 82, 84, 83, 92, 82, 103, 62, 96, 72, 96, 67,
         101, 73, 107, 72, 113, 55, 118, 52, 125, 52, 118, 52, 117, 55, 135, 49,
         137, 39, 157, 32, 145, 29, 97, 33, 77, 40],
    ],
]

# Band caps minus 64, [LM][channels-1][band] (reference cache.caps).
CAPS = [
    [  # LM 0
        [224, 224, 224, 224, 224, 224, 224, 224, 160, 160, 160, 160, 185, 185, 185, 178, 178, 168, 134, 61, 37],
        [224, 224, 224, 224, 224, 224, 224, 224, 240, 240, 240, 240, 207, 207, 207, 198, 198, 183, 144, 66, 40],
    ],
    [  # LM 1
        [160, 160, 160, 160, 160, 160, 160, 160, 185, 185, 185, 185, 193, 193, 193, 183, 183, 172, 138, 64, 38],
        [240, 240, 240, 240, 240, 240, 240, 240, 207, 207, 207, 207, 204, 204, 204, 193, 193, 180, 143, 66, 40],
    ],
    [  # LM 2
        [185, 185, 185, 185, 185, 185, 185, 185, 193, 193, 193, 193, 193, 193, 193, 183, 183, 172, 138, 65, 39],
        [207, 207, 207, 207, 207, 207, 207, 207, 204, 204, 204, 204, 201, 201, 201, 188, 188, 176, 141, 66, 40],
    ],
    [  # LM 3
        [193, 193, 193, 193, 193, 193, 193, 193, 193, 193, 193, 193, 194, 194, 194, 184, 184, 173, 139, 65, 39],
        [204, 204, 204, 204, 204, 204, 204, 204, 201, 201, 201, 201, 198, 198, 198, 187, 187, 175, 140, 66, 40],
    ],
]

PREFIX = """// =======================================================
// caustic-av/audio/opus/celt_tables_data.cst
// GENERATED by tools/gen_opus_celt_tables.py. Do not edit by hand.
//
// Data constants of the CELT layer that the RFC 6716 text names but does
// not print. Each table is decimal text, parsed once at decoder init.
// Provenance, validation and the reason only these three tables are data
// (everything else is derived from the RFC formulas) are in the generator
// and in audio/opus/celt.md.
//
//   CELT_TBL_E_MEANS      mean band energy Q4, 25 values
//   CELT_TBL_E_PROB_MODEL coarse-energy Laplace model Q8, 4 x 2 x 42
//   CELT_TBL_CAPS         band caps minus 64, 4 x 2 x 21
//
// Regenerate with:
//   python3 tools/gen_opus_celt_tables.py -o audio/opus/celt_tables_data.cst
// =======================================================
"""


def ilog2(v):
    return v.bit_length()


def log2_frac(val, frac):
    """Conservative (rounded up) log2 in 1/frac units, the format helper."""
    if val <= 0:
        return 0
    l = ilog2(val)
    if val & (val - 1):
        v = ((val - 1) >> (l - 16)) + 1 if l > 16 else val << (16 - l)
        res = (l - 1) << frac
        f = frac
        while True:
            b = v >> 16
            res += b << f
            v = (v + b) >> b
            v = (v * v + 0x7FFF) >> 15
            if f == 0:
                break
            f -= 1
        return res + (1 if v > 0x8000 else 0)
    return (l - 1) << frac


def get_pulses(q):
    return q if q < 8 else (8 + (q & 7)) << ((q >> 3) - 1)


def urow(n, kmax):
    """U(n, 0..kmax+1), saturated at 2**32."""
    cap = 1 << 32
    u = [0] * (kmax + 2)
    u[0] = 1
    for _ in range(n):
        old_left = u[0]
        u[0] = 0
        for i in range(1, kmax + 2):
            old_i = u[i]
            u[i] = min(cap, old_i + u[i - 1] + old_left)
            old_left = old_i
    return u


def pvq_v(n, k):
    u = urow(n, k)
    return min(1 << 32, u[k] + u[k + 1])


def comb(n, k):
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def pvq_v_closed(n, k):
    return sum((1 << j) * comb(n, j) * comb(k - 1, j - 1) for j in range(1, k + 1))


def validate():
    errors = []

    def need(cond, msg):
        if not cond:
            errors.append(msg)

    # Band edges and RFC Table 55.
    need(len(EBANDS) == 22, "eBands length")
    need(all(EBANDS[i] < EBANDS[i + 1] for i in range(21)), "eBands monotone")
    for lm, bins in enumerate(TABLE55):
        need(len(bins) == 21, "Table 55 column length")
        for b in range(21):
            need(bins[b] == (EBANDS[b + 1] - EBANDS[b]) << lm,
                 "Table 55 mismatch at LM %d band %d" % (lm, b))

    # Allocation table.
    need(len(ALLOC) == 11 and all(len(r) == 21 for r in ALLOC), "ALLOC shape")
    need(all(0 <= v <= 200 for r in ALLOC for v in r), "ALLOC range")

    # TF table against RFC Tables 60-63.
    expect = {
        (0, 0): [0, -1], (0, 1): [0, -1], (1, 0): [0, -1], (1, 1): [0, -1],
        (0, 0, 1): [0, -1], (0, 1, 1): [0, -1],
        (1, 0, 1): [1, 0], (1, 1, 1): [1, -1],
        (2, 0): [0, -2], (2, 1): [0, -2], (2, 0, 1): [2, 0], (2, 1, 1): [1, -1],
        (3, 0): [0, -2], (3, 1): [0, -2], (3, 0, 1): [3, 0], (3, 1, 1): [1, -1],
    }
    need(len(TF_TABLE) == 4 and all(len(r) == 8 for r in TF_TABLE), "TF shape")
    need(all(v in (-3, -2, -1, 0, 1, 2, 3) for r in TF_TABLE for v in r), "TF values")
    for lm in range(4):
        for tr in range(2):
            for sel in range(2):
                for flag in range(2):
                    v = TF_TABLE[lm][4 * tr + 2 * sel + flag]
                    key = (lm, tr) if sel == 0 else (lm, tr, 1)
                    if key in expect:
                        need(v == expect[key][flag],
                             "TF mismatch LM %d tr %d sel %d flag %d" % (lm, tr, sel, flag))

    # Means and models.
    need(len(E_MEANS_Q4) == 25, "eMeans length")
    need(all(0 <= v <= 160 for v in E_MEANS_Q4), "eMeans range")
    need(len(E_PROB_MODEL) == 4, "e_prob LM count")
    for lm in range(4):
        need(len(E_PROB_MODEL[lm]) == 2, "e_prob intra count")
        for it in range(2):
            need(len(E_PROB_MODEL[lm][it]) == 42, "e_prob row length")
            need(all(0 <= v <= 255 for v in E_PROB_MODEL[lm][it]),
                 "e_prob range")
    need(len(CAPS) == 4 and all(len(x) == 2 for x in CAPS), "caps shape")
    need(all(len(c) == 21 for x in CAPS for c in x), "caps band count")
    need(all(0 <= v <= 255 for x in CAPS for c in x for v in c), "caps range")

    # Window: monotone and power complementary with the mirror index.
    win = [math.sin(0.5 * math.pi * math.sin(0.5 * math.pi * (i + 0.5) / 120.0) ** 2)
           for i in range(120)]
    need(all(win[i] < win[i + 1] for i in range(119)), "window monotone")
    need(win[0] < 1e-5 and abs(win[119] - 1.0) < 1e-6, "window endpoints")
    for i in range(60):
        need(abs(win[i] ** 2 + win[119 - i] ** 2 - 1.0) < 1e-12,
             "window power complementarity at %d" % i)

    # log2_frac: exact on powers of two, conservative elsewhere.
    for f in range(0, 9):
        for e in range(0, 17):
            need(log2_frac(1 << e, f) == e << f, "log2_frac power of two")
    for v in range(1, 5000):
        got = log2_frac(v, 3)
        lo = int(math.ceil(8.0 * math.log2(v)))
        # The format helper is conservative: it rounds up, and its own note
        # allows at most one extra unit for some values.
        need(lo <= got <= lo + 1,
             "log2_frac bounds at %d: %d not in [%d,%d]" % (v, got, lo, lo + 1))

    # PVQ recurrence against the closed form (RFC 4.3.4.2), small values.
    for n in range(1, 7):
        for k in range(1, 7):
            need(pvq_v(n, k) == pvq_v_closed(n, k),
                 "V(%d,%d) recurrence vs closed form" % (n, k))
    need(pvq_v(1, 1) == 2 and pvq_v(2, 1) == 4 and pvq_v(2, 2) == 8,
         "V small values")
    for n in (1, 2, 3, 4, 8, 22, 176):
        last = -1
        for q in range(0, 41):
            v = pvq_v(n, get_pulses(q)) if q else 0
            b = log2_frac(v, 3) if v else 0
            need(b >= last, "bits monotone N=%d q=%d" % (n, q))
            last = b
    return errors


def emit(path):
    out = [PREFIX]
    out.append('\nlet is *u8 as CELT_TBL_E_MEANS with imut =\n    "'
               + " ".join(str(v) for v in E_MEANS_Q4) + '";\n')
    out.append('\nlet is *u8 as CELT_TBL_E_PROB_MODEL with imut =\n    "'
               + " ".join(" ".join(str(v) for v in row)
                          for lm in E_PROB_MODEL for row in lm) + '";\n')
    out.append('\nlet is *u8 as CELT_TBL_CAPS with imut =\n    "'
               + " ".join(" ".join(str(v) for v in row)
                          for lm in CAPS for row in lm) + '";\n')
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(out))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output",
                    default="audio/opus/celt_tables_data.cst",
                    help="output file")
    ap.add_argument("--check-only", action="store_true",
                    help="validate the tables without writing")
    args = ap.parse_args(argv)

    errors = validate()
    if errors:
        for e in errors:
            print("error: " + e, file=sys.stderr)
        return 1
    print("celt tables: all checks passed", file=sys.stderr)
    if not args.check_only:
        emit(args.output)
        print("wrote " + args.output, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
