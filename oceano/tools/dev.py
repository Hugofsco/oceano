"""Dev tools: git, code search, test running, and read-only SQL over data files."""
import subprocess
import sys

import config
from oceano.tools.core import _resolve, _ws, tool

# ============================ dev: git · code_search · run_tests ============================
_GIT_OK = {"status", "diff", "log", "show", "branch", "add", "commit", "blame", "stash",
           "rev-parse", "ls-files", "shortlog", "tag"}


@tool({
    "type": "function",
    "function": {
        "name": "git",
        "description": "Run a read/local git command in the workspace (status, diff, log, show, add, "
                       "commit, blame, …). Pass the subcommand and its args as one string, e.g. "
                       "'log --oneline -10' or 'commit -m \"msg\"'. Remote/push operations are refused "
                       "— use run_shell if you really need them.",
        "parameters": {"type": "object", "properties": {
            "args": {"type": "string", "description": "git subcommand + args, e.g. 'status' or 'diff HEAD~1'"},
        }, "required": ["args"]},
    },
})
def git(args):
    import shlex
    try:
        parts = shlex.split(args or "")
    except ValueError as e:
        return f"ERROR: couldn't parse args: {e}"
    if not parts:
        return "ERROR: pass a git subcommand, e.g. 'status' or 'log --oneline -5'"
    if parts[0] not in _GIT_OK:
        return f"ERROR: '{parts[0]}' isn't allowed here (allowed: {', '.join(sorted(_GIT_OK))}). Use run_shell for anything else."
    try:
        r = subprocess.run(["git", *parts], cwd=str(_ws()), capture_output=True, text=True,
                           timeout=config.SHELL_TIMEOUT)
    except FileNotFoundError:
        return "ERROR: git is not installed"
    except subprocess.TimeoutExpired:
        return "ERROR: git command timed out"
    out = (r.stdout + r.stderr).strip()
    return f"(exit {r.returncode})\n{out}"[:8000] if out else f"(exit {r.returncode}, no output)"


@tool({
    "type": "function",
    "function": {
        "name": "code_search",
        "description": "Fast text/regex search across workspace files (ripgrep). Returns matching "
                       "lines with file:line. Use this to find where something is defined or used.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "text or regex to search for"},
            "path": {"type": "string", "description": "subdir to search, default whole workspace"},
            "glob": {"type": "string", "description": "optional filter like '*.py' or '!*.min.js'"},
        }, "required": ["query"]},
    },
})
def code_search(query, path=".", glob=""):
    base = _resolve(path)
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "-S", "--max-count", "50"]
    if glob:
        cmd += ["--glob", glob]
    cmd += ["--", query, str(base)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=config.SHELL_TIMEOUT)
    except FileNotFoundError:
        return "ERROR: ripgrep (rg) is not installed — `apt install ripgrep`"
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out"
    out = r.stdout.strip()
    if not out:
        return f"(no matches for {query!r})"
    lines = out.splitlines()
    extra = f"\n… ({len(lines) - 200} more lines)" if len(lines) > 200 else ""
    return ("\n".join(lines[:200]) + extra)[:8000]


@tool({
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Detect and run the project's test suite in the workspace (pytest / npm test / "
                       "cargo test / make test) and return the result. Use after writing or editing "
                       "code to check it works.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "project subdir, default the workspace root"},
        }},
    },
})
def run_tests(path="."):
    base = _resolve(path)
    d = base if base.is_dir() else base.parent
    if (d / "pyproject.toml").exists() or (d / "pytest.ini").exists() or (d / "tests").is_dir() or list(d.glob("test_*.py")):
        cmd = [sys.executable, "-m", "pytest", "-q"]
    elif (d / "package.json").exists():
        cmd = ["npm", "test", "--silent"]
    elif (d / "Cargo.toml").exists():
        cmd = ["cargo", "test", "-q"]
    elif (d / "Makefile").exists():
        cmd = ["make", "test"]
    else:
        return "(no test suite detected — looked for pytest, package.json, Cargo.toml, Makefile)"
    try:
        r = subprocess.run(cmd, cwd=str(d), capture_output=True, text=True, timeout=max(config.SHELL_TIMEOUT, 300))
    except FileNotFoundError as e:
        return f"ERROR: test runner not installed: {e}"
    except subprocess.TimeoutExpired:
        return "ERROR: tests timed out"
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-60:])
    return f"(exit {r.returncode}) {' '.join(cmd)}\n{tail}"[:8000]


@tool({
    "type": "function",
    "function": {
        "name": "sql_query",
        "description": "Run a read-only SQL query over a data file in the workspace (CSV / TSV / "
                       "Parquet / JSON) using DuckDB — for quick data analysis. Reference the file as "
                       "the table `data` (e.g. SELECT category, count(*) FROM data GROUP BY 1).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "a SELECT query; the file is the table `data`"},
            "path": {"type": "string", "description": "workspace path to the CSV/TSV/Parquet/JSON file"},
        }, "required": ["query"]},
    },
})
def sql_query(query, path=""):
    try:
        import duckdb
    except ImportError:
        return "ERROR: duckdb not installed — `pip install duckdb`"
    q = (query or "").strip()
    if not q:
        return "ERROR: provide a SQL SELECT query"
    con = duckdb.connect(":memory:")
    try:
        if path:
            p = _resolve(path)
            if not p.is_file():
                return f"(no such file: {path})"
            reader = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet",
                      ".pq": "read_parquet", ".json": "read_json_auto"}.get(p.suffix.lower())
            if not reader:
                return f"(unsupported file type {p.suffix}; use csv/tsv/parquet/json)"
            con.execute(f"CREATE TABLE data AS SELECT * FROM {reader}(?)", [str(p)])
        con.execute("SET enable_external_access=false")   # sandbox the user query: no fs/network/COPY
        cur = con.execute(q)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(200)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    finally:
        con.close()
    if not cols:
        return "(query ran; no result set)"
    out = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
    tail = f"\n… (first {len(rows)} rows)" if len(rows) >= 200 else ""
    return ("\n".join(out) + tail)[:8000]
