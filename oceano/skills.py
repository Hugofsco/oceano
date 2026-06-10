"""Skills: reusable instruction packets the agent loads on demand.

Each skill is  skills/<name>/SKILL.md  with frontmatter + a body of instructions:

    ---
    name: research-report
    description: when & how to write a cited research report
    ---
    <step-by-step instructions the agent should follow>

The agent sees only the short descriptions by default (list_skills); it pulls the
full body into context only when it decides a skill is relevant (load_skill).
That keeps the prompt small for small models.
"""
import config

SKILLS_DIR = config.WORKSPACE.parent / "skills"


def _parse(text):
    """Split '---' frontmatter from body. Returns (meta dict, body str)."""
    if text.startswith("---"):
        try:
            _, fm, body = text.split("---", 2)
        except ValueError:
            return {}, text
        meta = {}
        for line in fm.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        return meta, body.strip()
    return {}, text


def list_skills():
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta, _ = _parse(sk.read_text(encoding="utf-8"))
        out.append(f"- {meta.get('name', sk.parent.name)}: {meta.get('description', '')}")
    return "\n".join(out) or "(no skills yet — create skills/<name>/SKILL.md)"


def load_skill(name):
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        return f"(no such skill: {name}). Use list_skills to see what's available."
    _, body = _parse(path.read_text(encoding="utf-8"))
    return body


def all_skills():
    """Full skill objects (name, description, body, dir) for the UI."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta, body = _parse(sk.read_text(encoding="utf-8"))
        out.append({"dir": sk.parent.name, "name": meta.get("name", sk.parent.name),
                    "description": meta.get("description", ""), "body": body})
    return out


def save_skill(name, description, body, dir=None):
    """Create or update a skill. Returns its directory slug."""
    slug = dir or ("".join(c for c in name.lower() if c.isalnum() or c in "-_") or "skill")
    d = SKILLS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body.strip()}\n", encoding="utf-8")
    return slug


def delete_skill(dir):
    import shutil
    d = (SKILLS_DIR / dir).resolve()
    if d.is_dir() and d.parent == SKILLS_DIR:
        shutil.rmtree(d)
        return True
    return False
