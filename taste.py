"""Learn your taste from your own data (no hand-set genre weights).

A small, explainable model you can read: a logistic regression over words that describe each track.

    model = train(keeps, drops, library=[], unlabelled=[])   # rows are {"artist", "title"[, "genre", "label"]}
    score(model, row)    -> 0..1, how likely you keep it (compare tracks with each other; 0.5 = no opinion)
    reasons(model, row)  -> the strongest learned words behind that score
    check(keeps, drops)  -> honest leave-one-out numbers: is it better than chance yet?
    time_split(rows, day) -> train on decisions before `day`, test on the ones after, with a 90% interval
    seeds(keeps, library) / seed_match(row, seeds) -> the artists, remixers and labels of your own tracks

Two traps this avoids (learned the hard way):
  * Library rows (tracks you play) usually carry a folder name, not a shop genre, and no mix name. If those words were
    features the model would only learn "looks like my library". So library rows only give artists, remixers and the
    label; genre words and mix type are learned only from rows that came from the same shop/pool list as the tracks
    being scored (your Keeps/Drops and optional unlabelled pool rows). A 'src:pool' word soaks up "came from the pool".
  * A random sample of the pool you never judged ("unlabelled") is a weak "probably not", never a Drop.
'I have it' picks arrive here as Keeps (digger.py maps them), so owning a track teaches the same as keeping it.
Plain Python, no extra packages. Retrain whenever you have new decisions: `python digger.py learn picks.csv`.
"""
import math, random, re

# How strongly each kind of evidence is held back (bigger = it needs more decisions before it counts).
# Artists and labels usually appear only once or twice, so they are held back hard.
PRIOR = {"g": 1.0, "G": 2.0, "mix": 1.0, "lbl": 6.0, "art": 6.0, "bias": 0.01, "src": 0.01}
LIBRARY_VOTE = 1.0        # each library track counts as one Keep (artists and label only)
UNLABELLED_SHARE = 0.5    # all unlabelled pool rows together weigh this share of the positives
GENERIC = {"original", "mix", "extended", "edit", "remix", "radio", "club", "version", "remastered", "rework", "dub",
           "feat", "ft", "the", "and", "vip", "instrumental", "mixed", "rmx", "vocal", "single", "short", "piano", "peak",
           "main", "festival", "acoustic", "reprise", "bootleg", "dj", "full", "length", "live", "special"}
NOT_A_NAME = {"various artists", "various", "unknown", "unknown artist", "id", "unreleased", "va"}


def mix_type(title):
    t = " ".join(re.findall(r"\(([^)]*)\)", title.lower()))
    if not t:
        return "plain"
    for k, pat in (("radio", r"radio|short"), ("extended", r"extended"), ("original", r"original"), ("club", r"club"),
                   ("dub", r"\bdub\b"), ("remix", r"remix|rmx|rework|edit\b|bootleg|mix")):
        if re.search(pat, t):
            return k
    return "other"


def people(artist, title):
    names = re.split(r",|&| feat\.?| ft\.?| x | vs\.? ", artist.lower())
    for br in re.findall(r"\(([^)]*)\)", title.lower()):  # remixers: '(Someone Extended Remix)'
        if re.search(r"remix|rmx|rework|mix\b|edit", br) and not re.fullmatch(r"\s*(original|extended|club|radio)( mix| edit)?\s*", br):
            br = re.sub(r"\b(" + "|".join(GENERIC) + r")\b", " ", br)
            names += re.split(r",|&| x ", br)
    names = [n.strip() for n in names]
    return [n for n in names if len(n) > 1 and n not in NOT_A_NAME]


def tokens(t, pool=True):
    """Words that describe a track. pool=False (library rows): artists, remixers and label only."""
    out = ["art:" + n for n in people(t.get("artist", ""), t.get("title", ""))]
    if t.get("label"):
        out.append("lbl:" + t["label"].lower().strip())
    if pool:
        g = re.sub(r"[/()&-]", " ", (t.get("genre") or "").lower()).split()
        out += ["src:pool"] + ["g:" + w for w in dict.fromkeys(g)] + ["G:" + a + "_" + b for a, b in zip(g, g[1:])]
        out.append("mix:" + mix_type(t.get("title", "")))
    return list(dict.fromkeys(out)) + ["bias"]


def _fit(rows, iters=200, damp=0.5):
    """rows: [(tokens, y, weight)]. L2-regularised logistic regression, damped per-word Newton steps."""
    w = {}
    for _ in range(iters):
        grad, hess = {}, {}
        for toks, y, wt in rows:
            z = sum(w.get(k, 0.0) for k in toks)
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            for k in toks:
                grad[k] = grad.get(k, 0.0) + wt * (p - y)
                hess[k] = hess.get(k, 0.0) + wt * p * (1 - p)
        biggest = 0.0
        for k, gk in grad.items():
            lam = PRIOR.get(k.split(":")[0], 1.0)
            step = damp * (gk + lam * w.get(k, 0.0)) / (hess[k] + lam)
            w[k] = w.get(k, 0.0) - step
            biggest = max(biggest, abs(step))
        if biggest < 1e-5:
            break
    return w


def train(keeps, drops, library=(), unlabelled=(), weight=None):
    """keeps/drops: your decisions. library: tracks you play (Keeps, artists/label only). unlabelled: pool rows you
    never judged (a weak 'probably not'). weight(row) -> per-decision weight, e.g. 0.5 for clicks from a biased week."""
    wt = weight or (lambda r: 1.0)
    rows = [(tokens(t), 1, wt(t)) for t in keeps] + [(tokens(t), 0, wt(t)) for t in drops] + \
           [(tokens(t, pool=False), 1, LIBRARY_VOTE) for t in library]
    pos = sum(r[2] for r in rows if r[1] == 1)
    unlabelled = list(unlabelled)
    if unlabelled:
        u = UNLABELLED_SHARE * pos / len(unlabelled)
        rows += [(tokens(t), 0, u) for t in unlabelled]
    return {"weights": _fit(rows), "trained_on": {"keeps": len(keeps), "drops": len(drops), "library": len(library),
                                                  "unlabelled": len(unlabelled)}}


def score(model, t):
    w = model.get("weights", {})
    z = sum(w.get(k, 0.0) for k in tokens(t))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def reasons(model, t, n=3):
    w = model.get("weights", {})
    ws = sorted(((w[k], k) for k in tokens(t) if k in w and k not in ("bias", "src:pool")), key=lambda x: -abs(x[0]))[:n]
    return [("+ " if x > 0 else "- ") + k for x, k in ws]


def strongest(model, n=10):
    """The words it learned most strongly, for and against."""
    ws = sorted(((v, k) for k, v in model.get("weights", {}).items() if k not in ("bias", "src:pool")), key=lambda x: x[0])
    return {"for": [(k, round(v, 2)) for v, k in ws[::-1][:n] if v > 0], "against": [(k, round(v, 2)) for v, k in ws[:n] if v < 0]}


# ---------------------------------------------------------------- seeds: the names on your own tracks

def seeds(keeps, library=(), drops=()):
    """{'names': {name: count}, 'labels': {label: count}} from your Keeps and library, minus names you mostly drop."""
    S = {"names": {}, "labels": {}}
    for rows, sign in ((keeps, 1), (library, 1), (drops, -1)):
        for t in rows:
            for n in people(t.get("artist", ""), t.get("title", "")):
                S["names"][n] = S["names"].get(n, 0) + sign
            if t.get("label"):
                k = t["label"].lower().strip()
                S["labels"][k] = S["labels"].get(k, 0) + sign
    return {kind: {k: c for k, c in d.items() if c > 0} for kind, d in S.items()}


def seed_match(t, S):
    """The seed names/labels this track shares with your own tracks."""
    out = [n for n in people(t.get("artist", ""), t.get("title", "")) if n in S["names"]]
    lb = (t.get("label") or "").lower().strip()
    return out + ([lb] if lb in S["labels"] else [])


# ---------------------------------------------------------------- honest checks

def _song(t):
    return re.split(r",|&", t.get("artist", "").lower())[0].strip() + "|" + t.get("title", "").lower().split("(")[0].strip()


def _auc(scores):
    pos = [s for s, y in scores if y]
    neg = [s for s, y in scores if not y]
    if not pos or not neg:
        return None
    return sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))


def check(keeps, drops, library=()):
    """Leave-one-out: hide one song (every version of it), train on the rest, score the hidden one.
    Returns plain numbers: auc (0.5 = coin toss, 1 = perfect) and how many keeps land in the top places."""
    rows = [(t, 1) for t in keeps] + [(t, 0) for t in drops]
    scores = []
    for s in {_song(t) for t, _ in rows}:
        k = [t for t, y in rows if y and _song(t) != s]
        d = [t for t, y in rows if not y and _song(t) != s]
        m = train(k, d, [t for t in library if _song(t) != s])
        scores += [(score(m, t), y) for t, y in rows if _song(t) == s]
    a = _auc(scores)
    if a is None:
        return {"auc": None, "keeps": sum(y for _, y in scores), "drops": sum(1 - y for _, y in scores)}
    pos = sum(y for _, y in scores)
    top = sorted(scores, key=lambda x: -x[0])[:pos]
    return {"auc": round(a, 3), "keeps": pos, "drops": len(scores) - pos, "keeps_in_top": sum(y for _, y in top)}


def time_split(rows, day, library=(), unlabelled=(), boots=500, seed=0):
    """rows: decisions with 'pick' ('keep'/'drop') and 'at' (YYYY-MM-DD...). Train on those before `day` (plus the
    library, minus any test song), test on those from `day`. Returns the AUC with a 90% bootstrap interval."""
    test = [r for r in rows if (r.get("at") or "")[:10] >= day]
    tsongs = {_song(r) for r in test}
    train_rows = [r for r in rows if (r.get("at") or "")[:10] < day and _song(r) not in tsongs]
    m = train([r for r in train_rows if r["pick"] == "keep"], [r for r in train_rows if r["pick"] == "drop"],
              [t for t in library if _song(t) not in tsongs], unlabelled)
    sc = [(score(m, r), int(r["pick"] == "keep")) for r in test]
    a = _auc(sc)
    if a is None:
        return {"auc": None, "test": len(test), "note": "need keeps and drops on both sides of " + day}
    rng, vals = random.Random(seed), []
    for _ in range(boots):
        b = _auc([sc[rng.randrange(len(sc))] for _ in sc])
        if b is not None:
            vals.append(b)
    vals.sort()
    return {"auc": round(a, 3), "interval_90": [round(vals[int(0.05 * len(vals))], 3), round(vals[int(0.95 * len(vals)) - 1], 3)],
            "test_keeps": sum(y for _, y in sc), "test": len(sc), "trained_on": len(train_rows)}
