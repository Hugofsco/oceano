"""Retrieval evaluation harness.

Measures how well retrieval finds the RIGHT document for a query. A strategy is a *builder*:
`build(corpus) -> rank(query) -> ranked corpus indices`, so corpus-side work (embedding docs,
building the BM25 index) happens once. The same labelled cases score any approach — plain dense,
nomic-prefixed dense, BM25, and the hybrid (dense+BM25 fused with RRF) we actually ship. That makes
"did this change help?" a number, not a vibe.

Run it:  python -m oceano.reval

Two case sets, scored against ONE shared corpus (all docs are mutual distractors):
  PARAPHRASE — terse query, prose doc, deliberately LOW lexical overlap → tests *meaning* (dense's home turf).
  KEYWORD    — query carries a distinctive term (a command, errno, identifier, API name) that also
               appears in the target, with same-topic distractors → tests *exact match* (BM25's home turf).
A good hybrid should win KEYWORD clearly and not regress PARAPHRASE — i.e. be best overall.
"""
import re
import sqlite3

from oceano import embeddings


def _fts_match(query):
    """Free-text query -> a safe FTS5 MATCH expression: alphanumeric tokens OR'd, each quoted so
    punctuation can't break the parse. '' when there's no usable token."""
    toks = re.findall(r"\w+", query.lower())
    return " OR ".join(f'"{t}"' for t in toks) if toks else ""


def _rrf(*ranked_lists, k=60):
    """Reciprocal Rank Fusion: merge ranked id-lists by summing 1/(k+rank) across lists. Best-first."""
    score = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda c: score[c], reverse=True)

# --- (terse query, the one prose doc it should retrieve). Low lexical overlap on purpose. ---
PARAPHRASE = [
    ("remove duplicate rows from a table", "Use SELECT DISTINCT or a GROUP BY to collapse repeated records in a result set."),
    ("make a slow query faster", "Adding an index on the filtered column lets the planner skip a full table scan."),
    ("combine rows from two tables", "A JOIN matches records across tables on a shared key to produce one combined output."),
    ("undo a change after it committed", "Once a transaction commits it cannot be rolled back; issue a compensating update instead."),
    ("throw away my uncommitted edits", "Discarding local modifications in the working tree returns files to their last saved state."),
    ("see changes I haven't staged yet", "A plain diff shows the edits sitting in your working copy that were never added."),
    ("stop my noodles clumping together", "Stir the pasta in the first minute and use plenty of boiling water so strands stay separate."),
    ("cook a steak to medium rare", "Sear the cut, then pull it near 54 degrees internal for a warm pink centre."),
    ("rescue a dish that's too salty", "Stir in a splash of cream or a peeled potato to mellow an over-seasoned sauce."),
    ("keep cut avocado from going brown", "Coat the exposed flesh with lime juice and press wrap right against the surface."),
    ("spend less money each month", "Cancelling unused subscriptions and cooking at home trims a household budget quickly."),
    ("start putting money aside for retirement", "Contributing early to a tax-advantaged account lets compounding do the heavy lifting."),
    ("get out of credit card debt", "Paying off the highest-interest balance first minimises what you owe over time."),
    ("build a cushion for emergencies", "Set aside three to six months of expenses in an account you can reach instantly."),
    ("will it rain tomorrow", "The outlook calls for scattered showers and grey skies through the next day."),
    ("is it safe to drive in fog", "Slow right down, use low beams, and leave extra distance when visibility drops to a few metres."),
    ("how hot will it get this week", "A heat dome pushes afternoon highs well past thirty degrees for several days running."),
    ("what causes thunder", "The rapid expansion of air superheated by a lightning bolt produces the sharp clap you hear."),
    ("get bigger arms", "Progressive overload on curls and presses, with enough protein, is what grows muscle."),
    ("run a quicker 5k", "Interval work near threshold pace, layered on easy mileage, sharpens race times."),
    ("loosen up tight hamstrings", "A daily forward fold, done after a warm-up, gradually lengthens the backs of the thighs."),
    ("exercise that's easy on the joints", "Swimming and cycling build strength with very little impact on knees and hips."),
    ("language with manual memory control", "C hands the programmer direct pointers and explicit allocation with no garbage collector."),
    ("memory safety without a garbage collector", "Rust guarantees safety at compile time through its ownership and borrowing rules."),
    ("language that runs in the browser", "JavaScript executes inside the page and drives interactivity through an event loop."),
    ("quick scripting with dynamic typing", "Python favours readable code, automatic memory management, and types resolved at runtime."),
    ("what does the thinking for Oceano", "Cognition is swappable: a local model or Claude via the CLI drives each turn while the body stays put."),
    ("where are the agent's memories kept", "Long-term facts live in a local SQLite store and are recalled by embedding similarity."),
    ("how does email reading stay safe", "A fetched message is fenced as untrusted, and reading one taints the turn so it cannot send."),
    ("why won't the daemon come up", "A wrong working directory makes the module import fail and the service restart over and over."),
    ("keep my logins secure", "An encrypted vault that stores credentials guards against password reuse and breaches."),
    ("why is the daytime sky blue", "Shorter wavelengths of sunlight scatter most in the air, tinting the overhead view."),
]

# --- query carries a distinctive token that also appears in the target; same-topic distractors. ---
KEYWORD = [
    ("how to git rebase onto main", "git rebase main replays your branch's commits on top of the latest main."),
    ("git cherry-pick one commit", "git cherry-pick copies a single commit from another branch onto the current one."),
    ("git stash my uncommitted work", "git stash shelves uncommitted changes so the working tree is clean to switch branches."),
    ("git bisect to find the bad commit", "git bisect binary-searches history to find which commit introduced a regression."),
    ("EROFS when writing a file", "EROFS means the filesystem is mounted read-only; remount it read-write to fix it."),
    ("ECONNREFUSED connecting to a port", "ECONNREFUSED means nothing is listening on that port; start the service first."),
    ("ENOSPC error on disk", "ENOSPC means the disk is out of space; free some or enlarge the volume."),
    ("EADDRINUSE address already in use", "EADDRINUSE means another process already bound the port; stop it or choose another."),
    ("what does OCEANO_WEB_HOST control", "Set OCEANO_WEB_HOST to 127.0.0.1 to bind the web UI to localhost instead of 0.0.0.0."),
    ("what is the search_document prefix for", "The search_document prefix tags stored content for nomic so it matches a search_query."),
    ("what does the ubatch flag do for embeddings", "serve-embeddings.sh sets -ub 2048 so the embed server processes a long input in one batch."),
    ("pandas groupby then aggregate", "In pandas, groupby followed by agg summarises rows within each group."),
    ("numpy argsort for ranking", "numpy argsort returns the indices that would sort an array, useful for ranking."),
    ("json.dumps to serialize a dict", "json.dumps converts a Python object into a JSON-formatted string."),
    ("asyncio gather coroutines", "asyncio.gather runs several coroutines concurrently and returns their results together."),
    ("HTTP 429 too many requests", "A 429 status means the client is rate-limited and should retry after a delay."),
    ("CORS preflight OPTIONS request", "A CORS preflight sends an OPTIONS request to check whether a cross-origin call is allowed."),
    ("what does NoNewPrivileges do in systemd", "NoNewPrivileges stops a service and its children from gaining privileges via setuid binaries."),
]


def _embed(text):
    """Embed exactly `text` (caller controls any prefix) via the shared embed server, or None."""
    try:
        r = embeddings._client.embeddings.create(model=embeddings.EMBED_MODEL, input=text)
        return r.data[0].embedding
    except Exception:
        return None


def dense(doc_prefix="search_document: ", query_prefix="search_query: "):
    def build(corpus):
        dvs = [_embed(doc_prefix + d) for d in corpus]
        def rank(q):
            qv = _embed(query_prefix + q)
            if qv is None:
                return []
            sims = sorted(((embeddings.cosine(qv, dv) if dv else -1.0, i) for i, dv in enumerate(dvs)), reverse=True)
            return [i for _, i in sims]
        return rank
    return build


def bm25():
    def build(corpus):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE d USING fts5(t, tokenize='unicode61')")
        con.executemany("INSERT INTO d(rowid, t) VALUES (?, ?)", list(enumerate(corpus)))
        def rank(q):
            expr = _fts_match(q)
            if not expr:
                return []
            return [r[0] for r in con.execute(
                "SELECT rowid FROM d WHERE d MATCH ? ORDER BY bm25(d) LIMIT 100", (expr,)).fetchall()]
        return rank
    return build


def hybrid(pool=50):
    dbuild, bbuild = dense(), bm25()
    def build(corpus):
        drank, brank = dbuild(corpus), bbuild(corpus)
        def rank(q):
            d, l = drank(q)[:pool], brank(q)[:pool]
            return _rrf(d, l) if (d and l) else (d or l)
        return rank
    return build


def _score(rank, cases, which, ks=(1, 3, 5)):
    ranks = []
    for i in which:
        order = rank(cases[i][0])
        ranks.append(order.index(i) if i in order else None)
    n = len(which)
    out = {f"r@{k}": sum(1 for r in ranks if r is not None and r < k) / n for k in ks}
    out["mrr"] = sum(1.0 / (r + 1) for r in ranks if r is not None) / n
    return out


def compare():
    """dense vs bm25 vs hybrid, on one shared corpus, reported per query-type and overall."""
    cases = PARAPHRASE + KEYWORD
    corpus = [d for _, d in cases]
    npar = len(PARAPHRASE)
    subsets = {"paraphrase": list(range(npar)),
               "keyword": list(range(npar, len(cases))),
               "ALL": list(range(len(cases)))}
    built = {name: build(corpus) for name, build in {"dense": dense(), "bm25": bm25(), "hybrid": hybrid()}.items()}
    for sname, which in subsets.items():
        print(f"\n== {sname} (n={len(which)}, corpus={len(corpus)}) ==")
        print(f"{'strategy':<9}{'r@1':>8}{'r@3':>8}{'r@5':>8}{'MRR':>8}")
        for st, rank in built.items():
            m = _score(rank, cases, which)
            print(f"{st:<9}{m['r@1']:>8.3f}{m['r@3']:>8.3f}{m['r@5']:>8.3f}{m['mrr']:>8.3f}")


if __name__ == "__main__":
    compare()
