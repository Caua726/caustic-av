#!/bin/sh
# Mini player: caustic-av decodes the video, mpv shows the frames and plays the
# file's own audio. Usage: tools/play.sh <file.webm> [path/to/decode]
set -eu
in=$1
dec=${2:-build/decode}
info=$(ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$in")
w=$(echo "$info" | cut -d, -f1)
h=$(echo "$info" | cut -d, -f2)
fps=$(echo "$info" | cut -d, -f3 | awk -F/ '{ printf "%.3f", $1 / ($2 ? $2 : 1) }')
"$dec" --yuv "$in" | mpv --demuxer=rawvideo \
  --demuxer-rawvideo-w="$w" --demuxer-rawvideo-h="$h" \
  --demuxer-rawvideo-mp-format=yuv420p --demuxer-rawvideo-fps="$fps" \
  --audio-file="$in" --title=caustic-av ${MPV_ARGS:-} -
