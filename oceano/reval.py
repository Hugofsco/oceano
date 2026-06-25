"""Retrieval evaluation harness.

Measures how well embedding retrieval finds the RIGHT document for a query. It's pluggable: a
strategy is just `retrieve(query, corpus) -> ranked list of corpus indices`, so the same labelled
cases score any approach — plain dense, prefixed dense (nomic's search_query/search_document), and
later hybrid (BM25+dense) or a reranker. That makes "did this change help?" a number, not a vibe.

Run it:  python -m oceano.reval        (compares no-prefix vs nomic-prefixed on the seed set)

The seed cases below deliberately use LOW lexical overlap between query and target — so a keyword
match alone won't score; the retriever has to capture *meaning*. Each target doc is also a
distractor for every other query, so recall@1 is a real signal, not a freebie.
"""
from oceano import embeddings

# (terse query, the one prose document it should retrieve). Grouped into clusters of NEAR-NEIGHBOUR
# docs (same topic, different specifics) so the retriever must discriminate fine meaning, not just
# topic — and queries are short/keyword-ish vs prose docs, which is where the query/document prefix
# asymmetry earns its keep. Every doc is a distractor for every other query.
CASES = [
    # --- relational databases ---
    ("remove duplicate rows from a table", "Use SELECT DISTINCT or a GROUP BY to collapse repeated records in a result set."),
    ("make a slow query faster", "Adding an index on the filtered column lets the planner skip a full table scan."),
    ("combine rows from two tables", "A JOIN matches records across tables on a shared key to produce one combined output."),
    ("undo a change after it committed", "Once a transaction commits it cannot be rolled back; issue a compensating update instead."),
    # --- git ---
    ("throw away my uncommitted edits", "git restore discards local modifications in the working tree that were never staged."),
    ("fix the wording of my last commit", "git commit --amend rewrites the most recent commit, message included, before you push."),
    ("pull another branch's work into mine", "Merging or rebasing integrates the commits from one branch on top of another."),
    ("see changes I haven't staged yet", "Running diff with no arguments shows the unstaged edits sitting in your working copy."),
    # --- cooking ---
    ("stop my noodles clumping together", "Stir the pasta in the first minute and use plenty of boiling water so strands stay separate."),
    ("cook a steak to medium rare", "Sear the cut, then pull it near 54 degrees internal for a warm pink centre."),
    ("rescue a dish that's too salty", "Stir in a splash of cream or a peeled potato to mellow an over-seasoned sauce."),
    ("keep cut avocado from going brown", "Coat the exposed flesh with lime juice and press wrap right against the surface."),
    # --- personal finance ---
    ("spend less money each month", "Cancelling unused subscriptions and cooking at home trims a household budget quickly."),
    ("start putting money aside for retirement", "Contributing early to a tax-advantaged account lets compounding do the heavy lifting."),
    ("get out of credit card debt", "Paying off the highest-interest balance first minimises what you owe over time."),
    ("build a cushion for emergencies", "Set aside three to six months of expenses in an account you can reach instantly."),
    # --- weather ---
    ("will it rain tomorrow", "The outlook calls for scattered showers and grey skies through the next day."),
    ("is it safe to drive in fog", "Slow right down, use low beams, and leave extra distance when visibility drops to a few metres."),
    ("how hot will it get this week", "A heat dome pushes afternoon highs well past thirty degrees for several days running."),
    ("what causes thunder", "The rapid expansion of air superheated by a lightning bolt produces the sharp clap you hear."),
    # --- fitness ---
    ("get bigger arms", "Progressive overload on curls and presses, with enough protein, is what grows muscle."),
    ("run a quicker 5k", "Interval work near threshold pace, layered on easy mileage, sharpens race times."),
    ("loosen up tight hamstrings", "A daily forward fold, done after a warm-up, gradually lengthens the backs of the thighs."),
    ("exercise that's easy on the joints", "Swimming and cycling build strength with very little impact on knees and hips."),
    # --- programming languages ---
    ("language with manual memory control", "C hands the programmer direct pointers and explicit allocation with no garbage collector."),
    ("memory safety without a garbage collector", "Rust guarantees safety at compile time through its ownership and borrowing rules."),
    ("language that runs in the browser", "JavaScript executes inside the page and drives interactivity through an event loop."),
    ("quick scripting with dynamic typing", "Python favours readable code, automatic memory management, and types resolved at runtime."),
    # --- Oceano domain ---
    ("what does the thinking for Oceano", "Cognition is swappable: a local model or Claude via the CLI drives each turn while the body stays put."),
    ("where are the agent's memories kept", "Long-term facts live in a local SQLite store and are recalled by embedding similarity."),
    ("how does email reading stay safe", "A fetched message is fenced as untrusted, and reading one taints the turn so it can't send."),
    ("why won't the daemon start", "A wrong WorkingDirectory makes python -m fail to import the package and the unit crash-loops."),
]


def _embed(text):
    """Embed exactly `text` (caller decides any prefix) via the shared embed server, or None."""
    try:
        r = embeddings._client.embeddings.create(model=embeddings.EMBED_MODEL, input=text)
        return r.data[0].embedding
    except Exception:
        return None


def dense(doc_prefix="", query_prefix=""):
    """A retrieve(query, corpus) strategy: rank corpus by cosine to the query, each side optionally
    carrying a nomic instruction prefix. Pass prefixes to compare conventions."""
    def retrieve(query, corpus):
        dvs = [_embed(doc_prefix + d) for d in corpus]
        qv = _embed(query_prefix + query)
        if qv is None:
            return []
        sims = []
        for i, dv in enumerate(dvs):
            sims.append((embeddings.cosine(qv, dv) if dv else -1.0, i))
        sims.sort(reverse=True)
        return [i for _, i in sims]
    return retrieve


def evaluate(retrieve, cases=CASES, ks=(1, 3, 5)):
    """Run `retrieve` over the cases; return {recall@k..., mrr, n, ranks}. rank = 0-based position
    of the correct doc in the returned order (None if absent)."""
    corpus = [doc for _, doc in cases]
    ranks = []
    for i, (q, _) in enumerate(cases):
        order = retrieve(q, corpus)
        ranks.append(order.index(i) if i in order else None)
    n = len(cases)
    out = {f"recall@{k}": sum(1 for r in ranks if r is not None and r < k) / n for k in ks}
    out["mrr"] = sum((1.0 / (r + 1)) for r in ranks if r is not None) / n
    out["n"] = n
    out["ranks"] = ranks
    return out


def compare():
    """Score plain dense vs nomic-prefixed dense on the seed set and print a small table."""
    variants = {
        "no-prefix (current)": dense(),
        "nomic prefixes": dense(doc_prefix="search_document: ", query_prefix="search_query: "),
    }
    rows = {name: evaluate(rt) for name, rt in variants.items()}
    print(f"{'strategy':<24} {'recall@1':>9} {'recall@3':>9} {'recall@5':>9} {'MRR':>7}")
    for name, m in rows.items():
        print(f"{name:<24} {m['recall@1']:>9.3f} {m['recall@3']:>9.3f} {m['recall@5']:>9.3f} {m['mrr']:>7.3f}")
    return rows


if __name__ == "__main__":
    compare()
