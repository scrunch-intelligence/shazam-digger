"""Sound fingerprint of a clip: Discogs-EffNet (MTG / Essentia model, ONNX), with the mel step in plain numpy.
Needs only numpy, onnxruntime and ffmpeg (no essentia, no torch). Same numbers as Essentia's own input step
(checked against fingerprints made with essentia: identical).
    fingerprint(audio_path) -> 1280 numbers, unit length (or None)
    python sound_fp.py < jobs.json      # [[audio path, out .npz path], ...]
Model: discogs-effnet-bsdynamic-1.onnx from https://essentia.upf.edu/models/feature-extractors/discogs-effnet/
(CC BY-NC-SA 4.0, MTG). Looked for in models/ next to this file, then SOUND_MODEL in the environment."""
import json, os, shutil, subprocess, sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODEL_URL = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx"
SR, FRAME, HOP, BANDS, PATCH, PATCH_HOP = 16000, 512, 256, 96, 128, 62
_sess = _fb = None


def model_path():
    for p in (os.environ.get("SOUND_MODEL"), HERE / "models" / "discogs-effnet-bsdynamic-1.onnx"):
        if p and Path(p).exists():
            return str(p)
    return None


def ffmpeg():
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def _mel_filters():
    """96 triangles on the Slaney mel scale, 0-8000 Hz, each scaled to unit area (Essentia MelBands: slaneyMel,
    linear weighting, unit_tri)."""
    def hz2mel(f):
        f = np.asarray(f, float)
        return np.where(f < 1000, f / (200 / 3), 15 + np.log(np.maximum(f, 1e-9) / 1000) / (np.log(6.4) / 27))
    def mel2hz(m):
        m = np.asarray(m, float)
        return np.where(m < 15, m * (200 / 3), 1000 * np.exp((np.log(6.4) / 27) * (m - 15)))
    edges = mel2hz(np.linspace(hz2mel(0), hz2mel(SR / 2), BANDS + 2))
    freqs = np.arange(FRAME // 2 + 1) * SR / FRAME
    fb = np.zeros((BANDS, len(freqs)))
    for i in range(BANDS):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        up, down = (freqs - lo) / (mid - lo), (hi - freqs) / (hi - mid)
        fb[i] = np.maximum(0, np.minimum(up, down)) * 2 / (hi - lo)
    return fb


def mel(audio):
    """audio: mono float32 at 16 kHz -> [frames, 96] log-mel, as Essentia's TensorflowInputMusiCNN."""
    global _fb
    if _fb is None:
        _fb = _mel_filters()
    n = 1 + max(0, (len(audio) - FRAME)) // HOP if len(audio) >= FRAME else 0
    if n <= 0:
        return np.zeros((0, BANDS), np.float32)
    idx = np.arange(FRAME)[None] + HOP * np.arange(n)[:, None]
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME) / (FRAME - 1))
    spec = np.abs(np.fft.rfft(audio[idx] * win, axis=1)) ** 2
    return np.log10(1 + 10000 * (spec @ _fb.T)).astype(np.float32)


def fingerprint(path):
    global _sess
    mp = model_path()
    if mp is None:
        return None
    if _sess is None:
        import onnxruntime as ort
        _sess = ort.InferenceSession(mp)
    raw = subprocess.run([ffmpeg(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    frames = mel(np.frombuffer(raw, np.float32))
    if len(frames) < PATCH:
        return None
    patches = np.array([frames[i:i + PATCH] for i in range(0, len(frames) - PATCH + 1, PATCH_HOP)], np.float32)
    emb = _sess.run(["embeddings"], {"melspectrogram": patches})[0].mean(0)
    return emb / (np.linalg.norm(emb) + 1e-9)


if __name__ == "__main__":
    for src, dst in json.load(sys.stdin):
        try:
            e = fingerprint(src)
            if e is not None:
                np.savez(dst, emb=e)
        except Exception as ex:
            print("fingerprint failed", src, ex, file=sys.stderr)
