#!/usr/bin/env python3
"""Generate audio/mp3/mp3_tables_data.cst from the public MPEG Layer III text.

Usage:
    python3 tools/gen_mp3_tables.py \
        --spec .work/docs/mp3_bitstream.txt \
        -o audio/mp3/mp3_tables_data.cst

The input is the published bitstream description "MPEG-Layer3 Bitstream
Syntax and Decoding" (Sieler/Sperschneider), whose Tables 8, 9, 23, 24-41,
42-59 and 60 carry the values this generator emits.  Nothing is taken from a
decoder; the parser below only reads numbers and bit strings out of the
document and checks their dimensions and prefix freedom.

What is generated:
  * MP3_BITRATE_MPEG1/2, MP3_SAMPLE_RATE      (Tables 8 and 9)
  * MP3_SFB_LONG/SHORT                        (Tables 42-59, widths)
  * MP3_PRETAB                                (Table 23)
  * MP3_ALIAS_C_NANO                          (Table 60, x1e9)
  * MP3_SYNTH_WINDOW_NANO                     (D[0..511], x1e9)
  * MP3_HUFF_LINBITS, MP3_HUFF_*              (Tables 24-41)

The analytic IMDCT windows and DCT twiddles are not printed tables and stay
PENDING; MP3_WINDOW_TABLES_GENERATED is emitted as 0.  Stage 8b supplies
them from the formulas, not from this script.

The output format is the one audio/mp3/mp3_tables.cst reads: one
NUL-terminated string literal per table, comma-separated decimal integers.
Huffman entries are x,y,hlen,hcod quadruples in x-major, y-minor order.
"""

import argparse
import re
import sys

# Table 8: Layer III bitrate_index -> kbit/s. Index 0 is free format and
# index 15 is forbidden, both stored as 0.
BITRATE_MPEG1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224,
                 256, 320, 0]
BITRATE_MPEG2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128,
                 144, 160, 0]
# Table 8 marks indices 9..14 forbidden for MPEG-2.5; index 15 is always
# forbidden.  They are stored as 0 like free format.
BITRATE_MPEG25 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 0, 0, 0, 0, 0, 0, 0]

# Table 9: rows MPEG-1, MPEG-2, MPEG-2.5; index 3 reserved as 0.
SAMPLE_RATE = [44100, 48000, 32000, 0,
               22050, 24000, 16000, 0,
               11025, 12000, 8000, 0]

# Table 23, with the tail band's zero. The printed table has 21 bands.
PRETAB = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
          1, 1, 1, 1, 2, 2, 3, 3, 3, 2, 0]

# Tables 40 and 41 headings: tables 16-23 and 24-31 share two codeword
# tables but differ in linbits. Tables 4 and 14 have no codewords and linbits
# 0; table 0 is the all-zero table.
HUFF_LINBITS = [0] * 16 + [1, 2, 3, 4, 6, 8, 10, 13] + [4, 5, 6, 7, 8, 9, 11, 13]

# Table 16: slen1, slen2 per scalefac_compress, interleaved.
SLEN_MPEG1 = [0, 0, 0, 1, 0, 2, 0, 3, 3, 0, 1, 1, 1, 2, 1, 3,
              2, 1, 2, 2, 2, 3, 3, 1, 3, 2, 3, 3, 4, 2, 4, 3]

# MPEG-2/2.5 number of scalefactors per partition: six contexts x three
# block cases (long, short not mixed, short mixed) x four partitions.  The
# values are the ones the procedure after Table 16 assigns.
LSF_NSF = [
    6, 5, 5, 5,  9, 9, 9, 9,  6, 9, 9, 9,
    6, 5, 7, 3,  9, 9, 12, 6, 6, 9, 12, 6,
    11, 10, 0, 0, 18, 18, 0, 0, 15, 18, 0, 0,
    7, 7, 7, 0, 12, 12, 12, 0, 6, 15, 12, 0,
    6, 6, 6, 3, 12, 9, 9, 6, 6, 12, 9, 6,
    8, 8, 5, 0, 15, 12, 9, 0, 6, 18, 9, 0,
]

# The document numbers its Huffman tables 0,1,2,3,5..13,15 in Tables 26-39
# and prints the shared tables in 40 and 41.  Document table -> MPEG table.
HUFF_DOC_TABLE = {
    26: 0, 27: 1, 28: 2, 29: 3, 30: 5, 31: 6, 32: 7, 33: 8,
    34: 9, 35: 10, 36: 11, 37: 12, 38: 13, 39: 15,
}
SHARED_TABLE = {40: 16, 41: 24}

# Sampling-rate order used by the nine sfb rows, matching mp3_tables.cst.
SFB_ORDER = [44100, 48000, 32000, 22050, 24000, 16000, 11025, 12000, 8000]
SFB_LONG_DOC = {42: 8000, 44: 11025, 46: 12000, 48: 16000, 50: 22050,
                52: 24000, 54: 32000, 56: 44100, 58: 48000}
SFB_SHORT_DOC = {43: 8000, 45: 11025, 47: 12000, 49: 16000, 51: 22050,
                 53: 24000, 55: 32000, 57: 44100, 59: 48000}


def fail(message):
    print("gen_mp3_tables: " + message, file=sys.stderr)
    raise SystemExit(1)


def load_sections(text):
    """Map table number -> lines between that standalone heading and the next."""
    lines = text.splitlines()
    heads = {}
    for i, line in enumerate(lines):
        m = re.match(r"^Table (\d+)\s*$", line.strip())
        if m:
            heads[int(m.group(1))] = i
    if not heads:
        fail("no 'Table N' headings found in the specification text")
    numbers = sorted(heads)
    sections = {}
    for n in numbers:
        start = heads[n]
        later = [x for x in numbers if x > n]
        end = heads[later[0]] if later else len(lines)
        sections[n] = lines[start + 1:end]
    return sections, text


def parse_sfb(section):
    """Read (width, start, end) rows of one Table 42..59."""
    rows = []
    for line in section:
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", line)
        if not m:
            continue
        sfb, width, start, end = map(int, m.groups())
        if sfb != len(rows):
            fail("scalefactor band row %d out of order" % sfb)
        rows.append((width, start, end))
    if len(rows) not in (12, 21):
        fail("scalefactor band table has %d rows, expected 12 or 21" % len(rows))
    off = 0
    for i, (width, start, end) in enumerate(rows):
        if width <= 0:
            fail("sfb %d has non-positive width" % i)
        if start != off or end != off + width - 1:
            fail("sfb %d is not contiguous (start %d end %d, expected %d..%d)"
                 % (i, start, end, off, off + width - 1))
        off += width
    return [w for w, _, _ in rows]


def parse_pretab(section):
    """Read Table 23.  Bands 0..9 are printed as one two-digit token."""
    values = {}
    for line in section:
        s = line.strip()
        if not s or "scalefactor band" in s:
            continue
        m = re.match(r"^(\d+)\s+(\d+)$", s)
        if m:
            sfb, val = int(m.group(1)), int(m.group(2))
            if sfb > 20:
                fail("pretab band %d out of range" % sfb)
            values[sfb] = val
            continue
        if re.fullmatch(r"\d\d", s):
            sfb, val = int(s[0]), int(s[1])
            if sfb > 20:
                fail("pretab band %d out of range" % sfb)
            values[sfb] = val
    if sorted(values) != list(range(21)):
        fail("pretab does not contain bands 0..20")
    return [values[i] for i in range(21)] + [0]


def parse_alias_c(section):
    """Read Table 60: eight printed negative coefficients."""
    out = []
    for line in section:
        m = re.match(r"^\s*([0-7])\s+(-0\.\d+)\s*$", line)
        if m:
            if int(m.group(1)) != len(out):
                fail("alias coefficient %s out of order" % m.group(1))
            out.append(m.group(2))
    if len(out) != 8:
        fail("alias coefficient table has %d entries, expected 8" % len(out))
    return [decimal_nano(v) for v in out]


def decimal_nano(s):
    neg = s.startswith("-")
    body = s[1:] if neg else s
    if "." not in body:
        fail("expected a decimal number, got %r" % s)
    whole, frac = body.split(".", 1)
    frac = (frac + "000000000")[:9]
    value = int(whole) * 1000000000 + int(frac)
    return -value if neg else value


def parse_d_window(text):
    """Read the 512 D[i] values printed just before Table 22."""
    found = {}
    for m in re.finditer(r"D\[\s*(\d+)\]\s*=\s*(-?\d+),(\d+)", text):
        i = int(m.group(1))
        if i in found:
            fail("duplicate D[%d]" % i)
        neg = m.group(2).startswith("-")
        whole = int(m.group(2).lstrip("-"))
        frac = m.group(3)
        if len(frac) != 9:
            fail("D[%d] fraction has %d digits, expected 9" % (i, len(frac)))
        found[i] = -(whole * 1000000000 + int(frac)) if neg \
            else whole * 1000000000 + int(frac)
    if sorted(found) != list(range(512)):
        fail("D table does not contain exactly indices 0..511")
    return [found[i] for i in range(512)]


def huffman_codes(section):
    """Collect the binary hcod token at the end of each data row."""
    codes = []
    for line in section:
        s = line.strip()
        if not s or "Continuation of Table" in s or s.startswith("Huffman code table"):
            continue
        code = None
        for token in reversed(s.split()):
            if re.fullmatch(r"[01]+", token):
                code = token
                break
        if code is None:
            continue
        codes.append(code)
    return codes


def check_prefix_free(codes):
    for i, (a, la) in enumerate(codes):
        for j, (b, lb) in enumerate(codes):
            if i == j:
                continue
            m = min(la, lb)
            if a[:m] == b[:m]:
                fail("Huffman codes %d and %d share a prefix" % (i, j))


def huffman_entries(section, expected_count):
    """Return (x, y, hlen, hcod) entries in x-major, y-minor order."""
    codes = huffman_codes(section)
    if len(codes) != expected_count:
        fail("Huffman table has %d entries, expected %d"
             % (len(codes), expected_count))
    dim = int(expected_count ** 0.5)
    if dim * dim != expected_count:
        fail("Huffman table size %d is not a square" % expected_count)
    out = []
    used = []
    for idx, code in enumerate(codes):
        x, y = divmod(idx, dim)
        length = len(code)
        if length > 19:
            fail("Huffman code longer than 19 bits")
        value = int(code, 2) if length else 0
        if length and value >= (1 << length):
            fail("Huffman codeword does not fit its length")
        out.append((x, y, length, value))
        if length:
            used.append((code, length))
    check_prefix_free(used)
    return out


def count1_entries(section, name):
    codes = huffman_codes(section)
    if len(codes) != 16:
        fail("%s has %d entries, expected 16" % (name, len(codes)))
    used = []
    out = []
    for symbol, code in enumerate(codes):
        length = len(code)
        if length < 1 or length > 6:
            fail("%s code length %d out of range" % (name, length))
        value = int(code, 2)
        out.extend([symbol, length, value])
        used.append((code, length))
    check_prefix_free(used)
    return out


def blob(name, values):
    return 'let is *u8 as %s with imut = "%s";\n' % (
        name, ",".join(str(v) for v in values))


HEADER = """// =======================================================
// caustic-av/audio/mp3/mp3_tables_data.cst
// Blob data for audio/mp3/mp3_tables.cst, generated by
// tools/gen_mp3_tables.py from the public Layer III bitstream description.
// Do not edit by hand.
//
// Blob format: comma-separated decimal integers, NUL terminated by the
// string literal.  Huffman entries are x,y,hlen,hcod quadruples in
// x-major, y-minor order.  Tables 16-23 and 24-31 share their codeword
// tables.  Table 0 is the all-zero table and has no codewords; tables 4
// and 14 are unused.  Count1 entries are symbol,hlen,hcod triples.  The
// MPEG-1 slen and MPEG-2/2.5 LSF partition arrays follow Table 16.
//
// The scalefactor-band blobs store 22 long and 13 short bands per row in
// the order 44.1, 48, 32, 22.05, 24, 16, 11.025, 12, 8 kHz.  The published
// table gives the scalefactor bands; the last entry of each row is the
// remaining lines to 576 or 192.
//
// PENDING: the analytic IMDCT windows and DCT twiddles are not printed
// tables.  MP3_WINDOW_* stay empty and MP3_WINDOW_TABLES_GENERATED is 0
// until stage 8b supplies them; this generator does not invent them.
// =======================================================

// 0: the analytic IMDCT window/twiddle blobs below are still pending.
let is i64 as MP3_WINDOW_TABLES_GENERATED with imut = 0;

// PENDING: 36 long-block (normal, block_type 0) IMDCT window values.
let is *u8 as MP3_WINDOW_LONG with imut = "";
// PENDING: 36 start-block (block_type 1) window values.
let is *u8 as MP3_WINDOW_START with imut = "";
// PENDING: 36 stop-block (block_type 3) window values.
let is *u8 as MP3_WINDOW_STOP with imut = "";
// PENDING: 12 short-block (block_type 2) window values.
let is *u8 as MP3_WINDOW_SHORT with imut = "";
// PENDING: DCT/IMDCT twiddles, count fixed by stage 8b's transform.
let is *u8 as MP3_DCT_TWIDDLE with imut = "";

"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True,
                    help="mp3_bitstream.txt from the public documentation")
    ap.add_argument("-o", "--out", required=True,
                    help="path of mp3_tables_data.cst to write")
    args = ap.parse_args()

    text = open(args.spec, encoding="utf-8", errors="replace").read()
    sections, text = load_sections(text)

    # Scalefactor-band rows, ordered as mp3_tables.cst indexes them.
    long_rows = {}
    short_rows = {}
    for doc, rate in SFB_LONG_DOC.items():
        if doc not in sections:
            fail("missing Table %d" % doc)
        long_rows[rate] = parse_sfb(sections[doc])
    for doc, rate in SFB_SHORT_DOC.items():
        if doc not in sections:
            fail("missing Table %d" % doc)
        short_rows[rate] = parse_sfb(sections[doc])

    long_blob = []
    short_blob = []
    for rate in SFB_ORDER:
        widths = long_rows[rate]
        tail = 576 - sum(widths)
        if tail < 0:
            fail("long bands at %d Hz exceed 576 lines" % rate)
        long_blob.extend(widths + [tail])
        widths = short_rows[rate]
        tail = 192 - sum(widths)
        if tail < 0:
            fail("short bands at %d Hz exceed 192 lines" % rate)
        short_blob.extend(widths + [tail])

    if 23 not in sections:
        fail("missing Table 23 (pretab)")
    pretab = parse_pretab(sections[23])
    for i, value in enumerate(pretab):
        if value < 0 or value > 3:
            fail("pretab[%d] = %d out of range" % (i, value))

    if 60 not in sections:
        fail("missing Table 60 (alias coefficients)")
    alias = parse_alias_c(sections[60])

    if 24 not in sections:
        fail("missing Table 24 (count1 table A)")
    if 25 not in sections:
        fail("missing Table 25 (count1 table B)")
    count1_a = count1_entries(sections[24], "count1 table A")
    count1_b = count1_entries(sections[25], "count1 table B")

    if len(SLEN_MPEG1) != 32:
        fail("MPEG-1 slen table is not 16 pairs")
    if len(LSF_NSF) != 72:
        fail("LSF nsf table is not 6x3x4")

    d_window = parse_d_window(text)

    # Huffman tables.
    hunts = {}
    for doc, table in HUFF_DOC_TABLE.items():
        if doc not in sections:
            fail("missing Table %d" % doc)
        # Table 26 (table 0) prints a single hlen with no hcod; it is the
        # all-zero table and is handled without a blob.
        if table == 0:
            continue
        expected = {
            1: 4, 2: 9, 3: 9, 5: 16, 6: 16, 7: 36, 8: 36, 9: 36,
            10: 64, 11: 64, 12: 64, 13: 256, 15: 256,
        }[table]
        hunts[table] = huffman_entries(sections[doc], expected)
    for doc, table in SHARED_TABLE.items():
        if doc not in sections:
            fail("missing Table %d" % doc)
        hunts[table] = huffman_entries(sections[doc], 256)

    for table, to in ((16, 23), (24, 31)):
        for other in range(table + 1, to + 1):
            hunts[other] = hunts[table]

    if len(HUFF_LINBITS) != 32:
        fail("linbits array is not 32 entries")
    for i, value in enumerate(HUFF_LINBITS):
        if value < 0 or value > 13:
            fail("linbits[%d] = %d out of range" % (i, value))

    out = [HEADER]
    out.append(blob("MP3_BITRATE_MPEG1", BITRATE_MPEG1))
    out.append(blob("MP3_BITRATE_MPEG2", BITRATE_MPEG2))
    out.append(blob("MP3_BITRATE_MPEG25", BITRATE_MPEG25))
    out.append(blob("MP3_SAMPLE_RATE", SAMPLE_RATE))
    out.append(blob("MP3_SFB_LONG", long_blob))
    out.append(blob("MP3_SFB_SHORT", short_blob))
    out.append(blob("MP3_PRETAB", pretab))
    out.append(blob("MP3_ALIAS_C_NANO", alias))
    out.append(blob("MP3_SYNTH_WINDOW_NANO", d_window))
    out.append(blob("MP3_HUFF_LINBITS", HUFF_LINBITS))
    out.append(blob("MP3_COUNT1_A", count1_a))
    out.append(blob("MP3_COUNT1_B", count1_b))
    out.append(blob("MP3_SLEN_MPEG1", SLEN_MPEG1))
    out.append(blob("MP3_LSF_NSF", LSF_NSF))
    for table in [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15]:
        flat = [v for entry in hunts[table] for v in entry]
        out.append(blob("MP3_HUFF_%d" % table, flat))
    out.append(blob("MP3_HUFF_16_23",
                    [v for entry in hunts[16] for v in entry]))
    out.append(blob("MP3_HUFF_24_31",
                    [v for entry in hunts[24] for v in entry]))

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("".join(out))
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
