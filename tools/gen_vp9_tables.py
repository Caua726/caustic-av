#!/usr/bin/env python3
"""Generate video/vp9/vp9_tables_data.cst from the VP9 specification text.

The specification is the only input. Nothing is copied from another decoder:
this script reads the published tables, checks their dimensions and value
ranges, and writes the byte blobs the decoder indexes.

Usage, from the repository root:

    python3 tools/gen_vp9_tables.py \
        --spec .work/vp9/spec/clean.txt \
        -o video/vp9/vp9_tables_data.cst

Running this script replaces video/vp9/vp9_tables_data.cst with the generated
blobs. Until it has been run, the committed file is the pending placeholder
described in video/vp9/vp9.md, and each blob is an empty string.

Blob layout (all values are one byte, in specification order):

  VP9_TBL_INV_MAP         inv_map_table[MAX_PROB]                   255
  VP9_TBL_KF_PROBS        kf_partition_probs                        48
                          kf_y_mode_probs                         900
                          kf_uv_mode_probs                         90
  VP9_TBL_DEFAULT_PROBS   default_partition_probs                   48
                          default_y_mode_probs                      36
                          default_uv_mode_probs                     90
                          default_skip_prob                          3
                          default_is_inter_prob                      4
                          default_comp_mode_prob                     5
                          default_comp_ref_prob                      5
                          default_single_ref_prob                   10
                          default_mv_sign_prob                       2
                          default_mv_bits_prob                      20
                          default_mv_class0_bit_prob                 2
                          default_tx_probs                          24
                          default_inter_mode_probs                  21
                          default_interp_filter_probs                8
                          default_mv_joint_probs                     3
                          default_mv_class_probs                    20
                          default_mv_class0_fr_probs                12
                          default_mv_class0_hp_prob                  2
                          default_mv_fr_probs                        6
                          default_mv_hp_prob                          2
                          default_coef_probs                      1728

The blob order is fixed; video/vp9/vp9_tables.cst unpacks it in the same
order. Changing the order requires changing both files together.
"""

import argparse
import hashlib
import re
import sys

# name, expected element count, "probability" or "index"
TABLES = [
    ("inv_map_table", 255, "index"),

    ("kf_partition_probs", 48, "prob"),
    ("kf_y_mode_probs", 900, "prob"),
    ("kf_uv_mode_probs", 90, "prob"),

    ("default_partition_probs", 48, "prob"),
    ("default_y_mode_probs", 36, "prob"),
    ("default_uv_mode_probs", 90, "prob"),
    ("default_skip_prob", 3, "prob"),
    ("default_is_inter_prob", 4, "prob"),
    ("default_comp_mode_prob", 5, "prob"),
    ("default_comp_ref_prob", 5, "prob"),
    ("default_single_ref_prob", 10, "prob"),
    ("default_mv_sign_prob", 2, "prob"),
    ("default_mv_bits_prob", 20, "prob"),
    ("default_mv_class0_bit_prob", 2, "prob"),
    ("default_tx_probs", 24, "prob"),
    ("default_inter_mode_probs", 21, "prob"),
    ("default_interp_filter_probs", 8, "prob"),
    ("default_mv_joint_probs", 3, "prob"),
    ("default_mv_class_probs", 20, "prob"),
    ("default_mv_class0_fr_probs", 12, "prob"),
    ("default_mv_class0_hp_prob", 2, "prob"),
    ("default_mv_fr_probs", 6, "prob"),
    ("default_mv_hp_prob", 2, "prob"),
    ("default_coef_probs", 1728, "prob"),
]

KF_ORDER = ["kf_partition_probs", "kf_y_mode_probs", "kf_uv_mode_probs"]
DEFAULT_ORDER = [name for name, _, _ in TABLES
                 if name.startswith("default_")]

LINE_COMMENT = re.compile(r"//[^\n]*")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def fail(message):
    sys.stderr.write("gen_vp9_tables: %s\n" % message)
    raise SystemExit(1)


def strip_comments(text):
    """Drop both comment forms so their digits are never read as values.

    The default coefficient table is annotated with /* block Type 0 */ and
    /* Coeff Band N */ comments; leaving those in would inject their numbers
    into the value list.
    """
    text = BLOCK_COMMENT.sub("", text)
    return LINE_COMMENT.sub("", text)


def find_table_body(text, name):
    """Return the text between the outer braces of `name[ ... ] = { ... }`.

    The declaration must start at the beginning of a line (leading spaces
    allowed) and the bracket list may span lines, as it does for
    default_coef_probs. A semicolon before the opening brace means this is a
    mention, not the definition.
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


def read_table(text, name):
    body = find_table_body(text, name)
    return [int(v) for v in re.findall(r"-?\d+", body)]


def validate(name, values, kind):
    expected = dict((n, c) for n, c, _ in TABLES)[name]
    if len(values) != expected:
        fail("%s: expected %d values, read %d" %
             (name, expected, len(values)))

    if kind == "index":
        # inv_map_table holds a deltaProb -> mapped value permutation.
        for i, v in enumerate(values):
            if v < 1 or v > 254:
                fail("%s[%d] = %d is outside 1..254" % (name, i, v))
        return

    # A coded probability is 1..255. The only zeros allowed are slots the
    # specification itself marks unused.
    for i, v in enumerate(values):
        if v < 0 or v > 255:
            fail("%s[%d] = %d is outside 0..255" % (name, i, v))
        if v == 0 and not unused_slot(name, i):
            fail("%s[%d] = 0, but that slot is used" % (name, i))

    if name == "default_tx_probs":
        # [tx][ctx][3]; only node < tx is ever read.
        for tx in range(4):
            for ctx in range(2):
                for node in range(3):
                    v = values[(tx * 2 + ctx) * 3 + node]
                    if node < tx:
                        if v == 0:
                            fail("default_tx_probs[%d][%d][%d] = 0" %
                                 (tx, ctx, node))
                    elif v != 0:
                        fail("default_tx_probs[%d][%d][%d] = %d, expected 0"
                             % (tx, ctx, node, v))

    if name == "default_coef_probs":
        # [tx][block][ref][band][prev_ctx][node]; band 0 has only three
        # previous-coefficient contexts, so [0][3..5][*] are unused.
        for tx in range(4):
            for blk in range(2):
                for ref in range(2):
                    for band in range(6):
                        for ctx in range(6):
                            for node in range(3):
                                idx = ((((tx * 2 + blk) * 2 + ref) * 6 + band) * 6 + ctx) * 3 + node
                                if band == 0 and ctx >= 3:
                                    if values[idx] != 0:
                                        fail("default_coef_probs[%d][%d][%d][0][%d][%d] = %d, expected 0"
                                             % (tx, blk, ref, ctx, node,
                                                values[idx]))
                                elif values[idx] == 0:
                                    fail("default_coef_probs[%d][%d][%d][%d][%d][%d] = 0"
                                         % (tx, blk, ref, band, ctx, node))


def unused_slot(name, i):
    """True for the zero slots the specification marks unused."""
    if name == "default_tx_probs":
        tx, rest = divmod(i, 6)
        node = rest % 3
        return node >= tx
    if name == "default_coef_probs":
        tx, rest = divmod(i, 432)          # 2*2*6*6*3 per transform size
        blk, rest = divmod(rest, 216)      # 2*6*6*3
        ref, rest = divmod(rest, 108)      # 6*6*3
        band, rest = divmod(rest, 18)      # 6*3
        ctx, node = divmod(rest, 3)
        return band == 0 and ctx >= 3
    return False


def blob(values):
    return "".join("\\x%02x" % (v & 0xFF) for v in values)


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

    tables = {}
    for name, _, kind in TABLES:
        values = read_table(text, name)
        validate(name, values, kind)
        tables[name] = values

    inv = tables["inv_map_table"]
    kf = []
    for name in KF_ORDER:
        kf.extend(tables[name])
    default = []
    for name in DEFAULT_ORDER:
        default.extend(tables[name])

    def sha(values):
        return hashlib.sha256(bytes(v & 0xFF for v in values)).hexdigest()

    out = []
    out.append("// GENERATED by tools/gen_vp9_tables.py from the VP9")
    out.append("// specification text. DO NOT EDIT.")
    out.append("//")
    out.append("// Regenerate from the repository root:")
    out.append("//   python3 tools/gen_vp9_tables.py --spec <clean.txt> \\")
    out.append("//       -o video/vp9/vp9_tables_data.cst")
    out.append("//")
    out.append("// Blob layout and sizes are documented in video/vp9/vp9.md.")
    out.append("")
    out.append("let is i64 as VP9_TABLES_GENERATED with imut = 1;")
    out.append("")
    for ident, name, values in (
        ("VP9_TBL_INV_MAP", "inv_map_table", inv),
        ("VP9_TBL_KF_PROBS", "kf probability tables", kf),
        ("VP9_TBL_DEFAULT_PROBS", "default probability tables", default),
    ):
        out.append("// %s: %d bytes, sha256 %s" %
                   (name, len(values), sha(values)))
        out.append('let is *u8 as %s with imut = "%s";' %
                   (ident, blob(values)))
        out.append("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    sys.stdout.write("wrote %s: inv %d, kf %d, default %d bytes\n" %
                     (args.out, len(inv), len(kf), len(default)))


if __name__ == "__main__":
    main()
