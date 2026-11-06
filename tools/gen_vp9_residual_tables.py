#!/usr/bin/env python3
"""Generate video/vp9/residual_tables_data.cst from the VP9 specification.

The specification is the only input. Nothing is copied from another decoder:
this script reads the published tables, checks their dimensions, value
ranges and internal structure, and writes the byte blobs the residual
decoder indexes.

Usage, from the repository root:

    python3 tools/gen_vp9_residual_tables.py \
        --spec .work/vp9/spec/clean.txt \
        -o video/vp9/residual_tables_data.cst

Running this script replaces video/vp9/residual_tables_data.cst with the
generated blobs. Until it has been run, the committed file is the pending
placeholder described in video/vp9/residual.md, and each blob is an empty
string.

Blob layout (fixed; video/vp9/residual_tables.cst unpacks it in the same
order):

  VP9_TBL_RES_SCAN      default_scan_4x4, col_scan_4x4, row_scan_4x4,
                        default_scan_8x8, col_scan_8x8, row_scan_8x8,
                        default_scan_16x16, col_scan_16x16, row_scan_16x16,
                        default_scan_32x32                                2032 values, 16-bit LE
  VP9_TBL_RES_BAND      coefband_4x4                                        16 bytes
                        coefband_8x8plus                                1024 bytes
  VP9_TBL_RES_TOKENS    token_tree                                          20 signed bytes
                        energy_class                                        12 bytes
                        extra_bits                                          33 bytes
                        cat_probs                                           98 bytes
                        pareto_table                                      1024 bytes
                        mode2txfm_map                                       14 bytes
  VP9_TBL_RES_QUANT     dc_qlookup row 0                                   256 16-bit LE
                        ac_qlookup row 0                                   256 16-bit LE

Only the first quantizer row is emitted: the decoder accepts profile 0 only,
where BitDepth is 8 and (BitDepth-8)>>1 is 0.
"""

import argparse
import hashlib
import re
import sys

SCAN_ORDER = [
    ("default_scan_4x4", 16),
    ("col_scan_4x4", 16),
    ("row_scan_4x4", 16),
    ("default_scan_8x8", 64),
    ("col_scan_8x8", 64),
    ("row_scan_8x8", 64),
    ("default_scan_16x16", 256),
    ("col_scan_16x16", 256),
    ("row_scan_16x16", 256),
    ("default_scan_32x32", 1024),
]

BAND_ORDER = [
    ("coefband_4x4", 16),
    ("coefband_8x8plus", 1024),
]

TOKEN_NAMES = {
    "ZERO_TOKEN": 0,
    "ONE_TOKEN": 1,
    "TWO_TOKEN": 2,
    "THREE_TOKEN": 3,
    "FOUR_TOKEN": 4,
    "DCT_VAL_CATEGORY1": 5,
    "DCT_VAL_CATEGORY2": 6,
    "DCT_VAL_CATEGORY3": 7,
    "DCT_VAL_CATEGORY4": 8,
    "DCT_VAL_CATEGORY5": 9,
    "DCT_VAL_CATEGORY6": 10,
}

TXFM_NAMES = {
    "DCT_DCT": 0,
    "ADST_DCT": 1,
    "DCT_ADST": 2,
    "ADST_ADST": 3,
}

LINE_COMMENT = re.compile(r"//[^\n]*")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def fail(message):
    sys.stderr.write("gen_vp9_residual_tables: %s\n" % message)
    raise SystemExit(1)


def strip_comments(text):
    """Drop both comment forms so their digits are never read as values."""
    text = BLOCK_COMMENT.sub("", text)
    return LINE_COMMENT.sub("", text)


def find_table_body(text, name):
    """Return the text between the outer braces of `name[ ... ] = { ... }`.

    The declaration must start at the beginning of a line (leading spaces
    allowed); a mention inside a sentence, or a use such as
    `cat_probs[ cat ][ e ]`, is skipped.
    """
    pos = 0
    while True:
        i = text.find(name, pos)
        if i < 0:
            fail("table %r not found in the specification" % name)
        line_start = text.rfind("\n", 0, i) + 1
        if text[line_start:i].strip() != "":
            pos = i + len(name)
            continue

        j = i + len(name)
        while j < len(text) and text[j].isspace():
            j += 1
        if j >= len(text) or text[j] != "[":
            pos = i + len(name)
            continue

        k = j
        while k < len(text) and text[k] not in "=;":
            k += 1
        if k >= len(text) or text[k] == ";":
            pos = i + len(name)
            continue
        k += 1
        while k < len(text) and text[k].isspace():
            k += 1
        if k >= len(text) or text[k] != "{":
            pos = i + len(name)
            continue

        depth = 0
        start = k
        while k < len(text):
            c = text[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1:k]
            k += 1
        fail("unterminated table %r" % name)


def table_rows(body):
    """Split a table body into its brace-delimited rows.

    The specification pads short rows implicitly (cat_probs[0] is written
    `{ 0}`); a table with no inner braces is a single row.
    """
    rows = re.findall(r"\{([^{}]*)\}", body)
    if rows:
        return rows
    return [body]


def numeric_rows(text, name, width=None):
    """Read a table as rows of integers, right-padding short rows with 0."""
    body = find_table_body(text, name)
    rows = []
    for row in table_rows(body):
        values = [int(v) for v in re.findall(r"-?\d+", row)]
        if width is not None:
            if len(values) > width:
                fail("%s: a row has %d values, at most %d expected" %
                     (name, len(values), width))
            values.extend([0] * (width - len(values)))
        rows.append(values)
    return rows


def name_row(text, name, names):
    """Read a table written with symbolic values (token_tree, tx types)."""
    body = find_table_body(text, name)
    out = []
    for item in body.split(","):
        s = item.strip()
        if not s:
            continue
        neg = s.startswith("-")
        if neg:
            s = s[1:].strip()
        if s in names:
            value = names[s]
        elif s.isdigit():
            value = int(s)
        else:
            fail("%s: unknown value %r" % (name, s))
        out.append(-value if neg else value)
    return out


def one_row(text, name, count):
    rows = numeric_rows(text, name)
    flat = [v for row in rows for v in row]
    if len(flat) != count:
        fail("%s: expected %d values, read %d" % (name, count, len(flat)))
    return flat


def check_permutation(name, values, size):
    if sorted(values) != list(range(size)):
        fail("%s is not a permutation of 0..%d" % (name, size - 1))


def blob8(values):
    return "".join("\\x%02x" % (v & 0xFF) for v in values)


def blob16(values):
    out = []
    for v in values:
        out.append("\\x%02x" % (v & 0xFF))
        out.append("\\x%02x" % ((v >> 8) & 0xFF))
    return "".join(out)


def sha(values, wide):
    if wide:
        return hashlib.sha256(bytes(
            b for v in values for b in (v & 0xFF, (v >> 8) & 0xFF)
        )).hexdigest()
    return hashlib.sha256(bytes(v & 0xFF for v in values)).hexdigest()


def read_scans(text):
    scans = []
    for name, size in SCAN_ORDER:
        values = one_row(text, name, size)
        check_permutation(name, values, size)
        scans.extend(values)
    if len(scans) != 2032:
        fail("scan blob: %d values, expected 2032" % len(scans))
    return scans


def read_bands(text):
    bands = []
    for name, size in BAND_ORDER:
        values = one_row(text, name, size)
        for i, v in enumerate(values):
            if v < 0 or v > 5:
                fail("%s[%d] = %d outside 0..5" % (name, i, v))
        bands.extend(values)
    if len(bands) != 1040:
        fail("band blob: %d values, expected 1040" % len(bands))
    return bands


def read_tokens(text):
    tree = name_row(text, "token_tree", TOKEN_NAMES)
    if len(tree) != 20:
        fail("token_tree: expected 20 entries, read %d" % len(tree))
    for i, v in enumerate(tree):
        if v <= 0:
            if -v > 10:
                fail("token_tree[%d] = %d is not a token" % (i, v))
        else:
            if v >= 20 or (v & 1) != 0:
                fail("token_tree[%d] = %d is not a node index" % (i, v))

    energy = one_row(text, "energy_class", 12)
    for i, v in enumerate(energy):
        if v < 0 or v > 5:
            fail("energy_class[%d] = %d outside 0..5" % (i, v))
    if energy[0] != 0:
        fail("energy_class[0] = %d, expected 0" % energy[0])

    extra_rows = []
    for row in numeric_rows(text, "extra_bits", 3):
        if len(extra_rows) >= 11:
            fail("extra_bits: more than 11 rows")
        extra_rows.append(row)
    if len(extra_rows) != 11:
        fail("extra_bits: expected 11 rows, read %d" % len(extra_rows))
    for token, (cat, num_extra, base) in enumerate(extra_rows):
        if token < 5:
            expect = (0, 0, token)
        elif token < 10:
            c = token - 4
            expect = (c, c, 5 + (1 << c) - 2)
        else:
            expect = (6, 14, 67)
        if (cat, num_extra, base) != expect:
            fail("extra_bits[%d] = %r, expected %r" %
                 (token, (cat, num_extra, base), expect))

    cat_probs = []
    rows = numeric_rows(text, "cat_probs", 14)
    if len(rows) != 7:
        fail("cat_probs: expected 7 rows, read %d" % len(rows))
    if rows[0] != [0] * 14:
        fail("cat_probs[0] is not the unused all-zero row")
    for cat in range(1, 7):
        for e, v in enumerate(rows[cat]):
            if v < 1 or v > 255:
                fail("cat_probs[%d][%d] = %d outside 1..255" % (cat, e, v))
    for row in rows:
        cat_probs.extend(row)

    pareto = one_row(text, "pareto_table", 1024)
    for i, v in enumerate(pareto):
        if v < 1 or v > 255:
            fail("pareto_table[%d] = %d outside 1..255" % (i, v))

    txfm = name_row(text, "mode2txfm_map", TXFM_NAMES)
    if len(txfm) != 14:
        fail("mode2txfm_map: expected 14 entries, read %d" % len(txfm))
    for i, v in enumerate(txfm):
        if v < 0 or v > 3:
            fail("mode2txfm_map[%d] = %d outside 0..3" % (i, v))

    tokens = list(tree) + energy + \
        [v for row in extra_rows for v in row] + cat_probs + pareto + txfm
    if len(tokens) != 1201:
        fail("token blob: %d bytes, expected 1201" % len(tokens))
    return tokens


def read_quant(text):
    values = []
    for name in ("dc_qlookup", "ac_qlookup"):
        rows = numeric_rows(text, name)
        if len(rows) != 3:
            fail("%s: expected 3 rows, read %d" % (name, len(rows)))
        for r, row in enumerate(rows):
            if len(row) != 256:
                fail("%s[%d]: expected 256 values, read %d" %
                     (name, r, len(row)))
        row = rows[0]
        for i in range(256):
            if row[i] < 1 or row[i] > 65535:
                fail("%s[0][%d] = %d outside 1..65535" % (name, i, row[i]))
            if i > 0 and row[i] < row[i - 1]:
                fail("%s[0] is not nondecreasing at %d" % (name, i))
        values.extend(row)
    if len(values) != 512:
        fail("quant blob: %d values, expected 512" % len(values))
    return values


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", required=True,
                    help="VP9 specification text (clean.txt)")
    ap.add_argument("-o", "--out", required=True,
                    help="output .cst file")
    args = ap.parse_args()

    try:
        with open(args.spec, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as exc:
        fail("cannot read %s: %s" % (args.spec, exc))

    text = strip_comments(raw)

    scans = read_scans(text)
    bands = read_bands(text)
    tokens = read_tokens(text)
    quant = read_quant(text)

    out = []
    out.append("// GENERATED by tools/gen_vp9_residual_tables.py from the")
    out.append("// VP9 specification text. DO NOT EDIT.")
    out.append("//")
    out.append("// Regenerate from the repository root:")
    out.append("//   python3 tools/gen_vp9_residual_tables.py --spec <clean.txt> \\")
    out.append("//       -o video/vp9/residual_tables_data.cst")
    out.append("//")
    out.append("// Blob layout and sizes are documented in video/vp9/residual.md")
    out.append("// and in the header of video/vp9/residual_tables.cst.")
    out.append("")
    out.append("let is i64 as VP9_RESIDUAL_TABLES_GENERATED with imut = 1;")
    out.append("")
    for ident, desc, values, wide in (
        ("VP9_TBL_RES_SCAN", "scan orders (spec 10.1)", scans, True),
        ("VP9_TBL_RES_BAND", "coefficient bands (spec 10.1)", bands, False),
        ("VP9_TBL_RES_TOKENS",
         "token tree, energy classes, extra bits, category probabilities, "
         "pareto table, mode-to-transform map (spec 9.3.2, 10.2, 10.3)",
         tokens, False),
        ("VP9_TBL_RES_QUANT",
         "dc_qlookup row 0 and ac_qlookup row 0 (spec 8.6.1)",
         quant, True),
    ):
        size = len(values) * (2 if wide else 1)
        out.append("// %s: %s, %d bytes, sha256 %s" %
                   (ident, desc, size, sha(values, wide)))
        out.append('let is *u8 as %s with imut = "%s";' %
                   (ident, blob16(values) if wide else blob8(values)))
        out.append("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    sys.stdout.write("wrote %s: scan %d, band %d, token %d, quant %d values\n" %
                     (args.out, len(scans), len(bands), len(tokens),
                      len(quant)))


if __name__ == "__main__":
    main()
