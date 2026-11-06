#!/bin/sh
# Mini player: caustic-av decodes the video, mpv shows the frames.
#
#   tools/play.sh <file>        real time with audio, no seeking; needs the
#                               decoder to keep up with the video
#   tools/play.sh <file> live   frames shown as they are decoded, silent, for
#                               videos the decoder cannot keep up with
#   tools/play.sh <file> <N>    decode the first N seconds, then play them at
#                               normal speed with audio and seeking
#
# A pipe cannot seek, so only the last form can. Build first with:
#   caustic -O2 examples/decode.cst -o build/decode
set -eu
in=$1
secs=${2:-0}
dec=${DECODE:-build/decode}
threads=${THREADS:-4}
info=$(ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$in")
w=$(echo "$info" | cut -d, -f1)
h=$(echo "$info" | cut -d, -f2)
fps=$(echo "$info" | cut -d, -f3 | awk -F/ '{ printf "%.3f", $1 / ($2 ? $2 : 1) }')
raw="--demuxer=rawvideo --demuxer-rawvideo-w=$w --demuxer-rawvideo-h=$h
  --demuxer-rawvideo-mp-format=yuv420p --demuxer-rawvideo-fps=$fps
  --title=caustic-av"

if [ "$secs" = 0 ]; then
  "$dec" --yuv --threads "$threads" "$in" 2>/dev/null | mpv $raw \
    --audio-file="$in" ${MPV_ARGS:-} -
  exit
fi

if [ "$secs" = live ]; then
  "$dec" --yuv --threads "$threads" "$in" | mpv $raw --no-audio --cache=no \
    --cache-pause=no --untimed ${MPV_ARGS:-} -
  exit
fi

bytes=$(awk -v w="$w" -v h="$h" -v f="$fps" -v s="$secs" \
  'BEGIN { printf "%d", int(f * s) * (w * h + 2 * int((w + 1) / 2) * int((h + 1) / 2)) }')
tmp=$(mktemp "${TMPDIR:-.}/play.XXXXXX.yuv")
trap 'rm -f "$tmp"' EXIT INT TERM
echo "decoding $secs s..." >&2
"$dec" --yuv --threads "$threads" "$in" 2>/dev/null | head -c "$bytes" > "$tmp" || true
mpv $raw --audio-file="$in" ${MPV_ARGS:-} "$tmp"
