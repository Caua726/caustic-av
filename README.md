# caustic-av

**Audio and video containers and codecs for [Caustic](https://github.com/Caua726/Caustic), written from the specifications: the job FFmpeg and GStreamer do for C, with neither of them inside.**

![status](https://img.shields.io/badge/status-planning-lightgrey)
![license](https://img.shields.io/badge/license-MIT-blue)

> **Nothing is implemented yet.** This file is the plan: every format, module,
> command and call below describes what the library is meant to become. The
> **State** columns say what exists, and today they all say *planned*.

## What it is

A library that opens media files, separates their tracks, and decodes and
encodes what is inside them.

It is not a wrapper. FFmpeg, GStreamer and every other C codec library stay out
of the code and out of the build: each container and codec here is implemented
in Caustic, from its specification. The only dependencies are other Caustic
libraries, fetched by `caustic-mk`.

FFmpeg appears in exactly one place, the tests, as an independent program to
compare results against. No FFmpeg source is copied or ported, because that
would make this library a derivative of LGPL/GPL code.

## Formats

### Containers

| Format | Demux | Mux | State |
|---|---|---|---|
| WAV | yes | yes | planned |
| Ogg | yes | yes | planned |
| Matroska | yes | yes | planned |
| WebM | yes | yes | planned |
| MP4 | yes | yes | planned |

WebM is a restricted form of Matroska, so the two share one implementation over
a common EBML reader. Compressed Matroska tracks and compressed MP4 headers are
inflated with `caustic-compact`.

### Audio

| Codec | Decode | Encode | State |
|---|---|---|---|
| PCM | yes | yes | planned |
| FLAC | yes | yes | planned |
| Vorbis | yes | yes | planned |
| MP3 | yes | yes | planned |
| Opus | yes | yes | planned |

### Video

| Codec | Decode | Encode | State |
|---|---|---|---|
| VP8 | yes | yes | planned |
| VP9 | yes | yes | planned |
| AV1 | yes | yes | planned |

### Left out on purpose

**H.264, H.265 and AAC** are not planned, because their patents are licensed
through active paid pools (see [Patents](#patents)). A container holding one of
these tracks still opens and lists it, with its codec identified; asking to
decode that track returns `UNSUPPORTED`.

This has a real cost: many `.mp4` and `.mkv` files found in the wild use these
codecs, and their video or audio will not play.

## Layout

```
caustic-av/
  Causticfile
  caustic_av.cst        the hub
  demux.cst  mux.cst    container detection by content, then dispatch
  decode.cst encode.cst codec id to implementation

  core/
    errno  buf  bits (MSB and LSB first, Exp-Golomb)  sniff  codec (ids)
    packet  frame (YUV planes)  samples  time (timebase, timestamps)

  container/            demux and mux of a format in one file
    ebml  matroska (and WebM)  ogg  mp4  wav

  audio/                one directory per codec: hub, design note, parts
    pcm/  flac/  vorbis/  mp3/  opus/
  video/
    vp8/  vp9/  av1/

  dsp/                  what codecs share, and what gains from SIMD
    mdct  idct  interp  deblock
  convert/              YUV and RGB, scaling, resampling, sample formats

  tools/                table generators
  tests/                run.cst, test_*.cst, vectors/
  examples/             probe.cst, decode.cst
```

Each codec directory carries a design note beside its code, such as
`video/vp9/vp9.md`, recording what it holds and why.

## The API

Pull-based, with the conventions of the sibling libraries: a negative `i64` is
an error.

```
demux.open(src, len, &d)                  // bytes in; the library opens no files
demux.next(&d, &pkt)                      // one packet of one track
decode.open(d.tracks[i].codec, &dec)
decode.send(&dec, &pkt)
decode.receive(&dec, &frame)              // one packet may yield 0..N frames
```

Frames and samples are all it hands out. It knows nothing of windows or
speakers: `caustic-media` depends on this library, never the reverse, and shows
or plays the results.

## Dependencies

| Library | Used for |
|---|---|
| [caustic-compact](https://github.com/Caua726/caustic-compact) | zlib for compressed Matroska tracks and MP4 headers, CRC |

Codecs need most-significant-bit-first reading, which `caustic-compact`'s
least-significant-bit-first reader does not do, so that part lives in
`core/bits`.

## Roadmap

1. **Foundation:** `core/`, the five containers, and a `probe` example listing a
   file's tracks.
2. **Audio decoding:** PCM, FLAC, Vorbis, MP3, then Opus.
3. **Video decoding:** VP8, VP9, then AV1, with `dsp/` and `convert/` growing as
   they need to.
4. **Encoding:** PCM and FLAC first, then the other audio codecs, then video.

The open risk is speed. The compiler offers inline `asm(...)` and `--cpu=auto`
(AVX2 chosen at run time), and the standard library has threads, but nothing has
been measured. High-resolution video in real time will probably depend on SIMD
in `dsp/`, which is why those routines are kept apart from the codecs.

## Testing

- `caustic-mk run test`: every suite.
- `caustic-mk run test-opt`: the same suite through the optimizing backend, so a
  failure that appears only there points at the compiler.

Decoders are checked against an independent decoder. FFmpeg generates test files
and the checksum of each decoded frame, and small vectors are committed under
`tests/vectors/`. Conformance suites published for each format are used where
they exist, such as the libvpx VP9 vectors and the Opus vectors from RFC 6716.

## Patents

The licence of this code covers this code. **It grants no patent rights.**

The formats implemented here are published for royalty-free use, but that does
not guarantee that no third party claims patents on them: Sisvel runs pools
claiming patents on VP9 and AV1, Access Advance runs another for AV1, and AV1 is
the subject of patent litigation. Anyone shipping a product built on this library
should check their own position. Nothing here is legal advice.

## Licence

MIT.
