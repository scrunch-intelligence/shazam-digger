#!/usr/bin/env bash
# One-command setup for Mac and Linux: ./setup.sh
#   --screenshots  the optional screenshot feature
#   --sound        the optional sound taste (numpy, onnxruntime and an 18 MB model file; needs ffmpeg installed)
set -e
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  PIP="uv pip install --python .venv/bin/python"
else
  PY=$(command -v python3.12 || command -v python3.13 || command -v python3.11 || true)
  [ -z "$PY" ] && { echo "Install Python 3.11+ (or uv) first."; exit 1; }
  "$PY" -m venv .venv
  PIP=".venv/bin/python -m pip install"
fi
$PIP -r requirements.txt
for a in "$@"; do
  [ "$a" = "--screenshots" ] && $PIP -r requirements-screenshots.txt
  if [ "$a" = "--sound" ]; then
    $PIP -r requirements-sound.txt
    mkdir -p models
    M=models/discogs-effnet-bsdynamic-1.onnx
    # Discogs-EffNet by the Music Technology Group (MTG), licence CC BY-NC-SA 4.0. Downloaded from its makers, not shipped here.
    [ -f "$M" ] || curl -L --fail -o "$M" https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx
    command -v ffmpeg >/dev/null 2>&1 || echo "Install ffmpeg for the sound taste (Mac: brew install ffmpeg)."
  fi
done
[ -f config.toml ] || cp config.example.toml config.toml
.venv/bin/python -m unittest -q test_digger
echo
echo "Ready. Try:  .venv/bin/python digger.py list sample/shazams.txt"
