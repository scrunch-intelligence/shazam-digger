"""Your taste by SOUND: does a track sound like the ones you kept, or like the ones you dropped?

Every 30 s preview is turned into a fingerprint (sound_fp.py, the Discogs-EffNet model from MTG / Essentia). For a
track, this finds the 3 Keeps it sounds most like and the 3 Drops it sounds most like; the score is how close the
Keeps are minus how close the Drops are. Nothing is built in: it only knows what you pressed.

Optional. Needs numpy, onnxruntime, ffmpeg and the model file (./setup.sh --sound). Without them the tool works as
before. Fingerprints are cached in data/sound/, previews are never kept.

Honest note: with only a few Keeps and Drops this is close to a coin toss (0.5), so treat it as promising, not proven.
`learn` prints the same check on your own picks.
Use one kind of audio only (the previews): fingerprints of other files of the same song do not match well.
"""
import os, tempfile, urllib.request

K = 3           # nearest Keeps / Drops compared
MIN_EACH = 5    # Keeps and Drops with sound needed before it scores anything


def available():
    try:
        import numpy, onnxruntime  # noqa: F401
        import sound_fp
        return bool(sound_fp.model_path())
    except Exception:
        return False


def fingerprint_track(t, cache_dir, key, ua="shazam-digger"):
    """The cached fingerprint of a track's preview, made once. None when there is no preview or it fails."""
    import numpy as np, sound_fp
    f = os.path.join(cache_dir, key + ".npz")
    if os.path.exists(f):
        try:
            return np.load(f)["emb"]
        except Exception:
            return None
    url = t.get("preview") or ""
    src, tmp = t.get("file") or "", None
    try:
        if url.startswith("http"):
            fd, tmp = tempfile.mkstemp(suffix=".audio"); os.close(fd)
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=20) as r, open(tmp, "wb") as o:
                o.write(r.read())
            src = tmp
        if not src or not os.path.exists(src):
            return None
        e = sound_fp.fingerprint(src)
        if e is not None:
            os.makedirs(cache_dir, exist_ok=True)
            np.savez(f, emb=e)
        return e
    except Exception:
        return None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


class Model:
    def __init__(self, keeps, drops, scale_from=None):
        """keeps, drops: lists of fingerprints. scale_from: every fingerprint you have (sets the scale)."""
        import numpy as np
        allv = np.array(list(scale_from) if scale_from is not None and len(scale_from) else list(keeps) + list(drops))
        self.mu, self.sd = allv.mean(0), allv.std(0) + 1e-6
        self.P, self.N = self._z(np.array(keeps)), self._z(np.array(drops))

    def _z(self, v):
        import numpy as np
        z = (np.asarray(v) - self.mu) / self.sd
        return z / (np.linalg.norm(z, axis=-1, keepdims=True) + 1e-9)

    def score(self, v):
        import numpy as np
        z = self._z(v)
        return float(np.sort(self.P @ z)[-K:].mean() - np.sort(self.N @ z)[-K:].mean())


def build(keeps, drops, scale_from=None):
    """A Model, or None with fewer than MIN_EACH Keeps or Drops that have sound."""
    return Model(keeps, drops, scale_from) if len(keeps) >= MIN_EACH and len(drops) >= MIN_EACH else None


def check(keeps, drops, scale_from=None):
    """Each decided track hidden in turn and scored by the rest: how often a Keep ranks above a Drop (0.5 = coin toss)."""
    rows = [(v, 1) for v in keeps] + [(v, 0) for v in drops]
    s = []
    for i, (v, y) in enumerate(rows):
        rest = rows[:i] + rows[i + 1:]
        m = build([a for a, b in rest if b], [a for a, b in rest if not b], scale_from)
        if m is None:
            return None
        s.append((m.score(v), y))
    pos, neg = [a for a, y in s if y], [a for a, y in s if not y]
    return round(sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg)), 3)
