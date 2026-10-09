"""String similarity in pure Python (no dependency). Every function returns a value in [0, 1], 1 = identical.

- `edit`: 1 − Levenshtein distance / length of the longer string.
- `jaro_winkler`: Jaro similarity with the Winkler prefix bonus (p = 0.1, prefix ≤ 4).
- `token_jaccard`: |A ∩ B| / |A ∪ B| over word sets (exact words; reported, not averaged).
- `soft_token`: like a Dice coefficient over words, where two words also count as shared when their edit
  similarity is ≥ the spelling-variant threshold ("alloy" ~ "alloys", "ulvane" ~ "ulvanee").
- `token_set`: the token-set ratio. Compare the shared words with each side's shared + remaining words (edit
  similarity), take the best; word order and repeated words do not matter.
- `trigram`: Jaccard over padded character trigrams (robust to small spelling changes inside words).

`name_components` reports all six on the core names; `name_similarity` is the mean of edit, Jaro-Winkler,
soft-token, token-set and trigram. No single metric decides anything; the decision rules (`matching`) read the
components and the word-level differences.
"""

from functools import lru_cache


@lru_cache(maxsize=200_000)
def edit(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / len(a)


@lru_cache(maxsize=200_000)
def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(len(a), len(b)) // 2 - 1
    window = max(window, 0)
    ma = [False] * len(a)
    mb = [False] * len(b)
    matches = 0
    for i, ca in enumerate(a):
        lo, hi = max(0, i - window), min(len(b), i + window + 1)
        for j in range(lo, hi):
            if not mb[j] and b[j] == ca:
                ma[i] = mb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    sa = [c for c, m in zip(a, ma, strict=True) if m]
    sb = [c for c, m in zip(b, mb, strict=True) if m]
    transpositions = sum(x != y for x, y in zip(sa, sb, strict=True)) / 2
    jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4], strict=False):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def token_jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def soft_token(a: tuple[str, ...], b: tuple[str, ...], typo: float) -> float:
    if not a or not b:
        return 0.0
    rest = list(b)
    shared = 0
    for t in a:
        best, best_s = -1, 0.0
        for j, u in enumerate(rest):
            sc = 1.0 if t == u else edit(t, u)
            if sc > best_s:
                best, best_s = j, sc
        if best >= 0 and best_s >= typo:
            shared += 1
            rest.pop(best)
    return 2 * shared / (len(a) + len(b))


def token_set(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = " ".join(sorted(sa & sb))
    ra = " ".join(sorted(sa - sb))
    rb = " ".join(sorted(sb - sa))
    ca = (inter + " " + ra).strip()
    cb = (inter + " " + rb).strip()
    if not inter:
        return edit(ca, cb)
    return max(edit(inter, ca), edit(inter, cb), edit(ca, cb))


@lru_cache(maxsize=200_000)
def _grams(s: str) -> frozenset[str]:
    p = f"  {s} "
    return frozenset(p[i : i + 3] for i in range(len(p) - 2))


def trigram(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / len(ga | gb)


AVERAGED = ("edit", "jaro_winkler", "soft_token", "token_set", "trigram")


def name_components(
    core_a: str, core_b: str, tok_a: tuple[str, ...], tok_b: tuple[str, ...], typo: float
) -> dict:
    return {
        "edit": round(edit(core_a, core_b), 6),
        "jaro_winkler": round(jaro_winkler(core_a, core_b), 6),
        "token_jaccard": round(token_jaccard(tok_a, tok_b), 6),
        "soft_token": round(soft_token(tok_a, tok_b, typo), 6),
        "token_set": round(token_set(tok_a, tok_b), 6),
        "trigram": round(trigram(core_a, core_b), 6),
    }


def mean(components: dict) -> float:
    return round(sum(components[k] for k in AVERAGED) / len(AVERAGED), 6)


def token_differences(a: tuple[str, ...], b: tuple[str, ...], typo: float) -> dict:
    """Words in one name only, paired with their closest word in the other.

    A pair whose edit similarity is ≥ `typo` is a spelling variant ("alloy"/"alloys", "ulvane"/"ulvanee");
    any other unmatched word is *distinguishing* ("trading"/"traders" at 0.57, "steel", "power"): the names
    describe different things, which caps a name-only decision.
    """
    only_a = [t for t in a if t not in b]
    only_b = [t for t in b if t not in a]
    variants, distinguishing = [], []
    used = set()
    for t in only_a:
        best, best_s = None, 0.0
        for j, u in enumerate(only_b):
            if j in used:
                continue
            s = edit(t, u)
            if s > best_s or (s == best_s and best is not None and u < only_b[best]):
                best, best_s = j, s
        if best is not None and best_s >= typo:
            used.add(best)
            variants.append([t, only_b[best], round(best_s, 4)])
        else:
            distinguishing.append(t)
    distinguishing += [u for j, u in enumerate(only_b) if j not in used]
    return {"spelling_variants": variants, "distinguishing_words": sorted(distinguishing)}
