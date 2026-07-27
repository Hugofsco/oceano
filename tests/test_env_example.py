"""Keep oceano.env.example authoritative as new configuration knobs are added."""
import pathlib
import re


ROOT = pathlib.Path(__file__).parent.parent
EXAMPLE = ROOT / "oceano.env.example"


def test_env_example_documents_every_runtime_environment_read():
    sources = [ROOT / "config.py", ROOT / "cli.py", *sorted((ROOT / "oceano").rglob("*.py"))]
    getter = re.compile(r'''(?:os\.environ\.get|os\.getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']''')
    used = set()
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="ignore")
        used.update(getter.findall(text))
    documented = set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", EXAMPLE.read_text(encoding="utf-8")))
    missing = used - documented
    assert not missing, f"environment variables missing from oceano.env.example: {sorted(missing)}"


def test_env_example_documents_installer_and_build_variables():
    text = EXAMPLE.read_text(encoding="utf-8")
    for name in ("OCEANO_LLAMA_SWAP_BIN", "OCEANO_BACKEND"):
        assert name in text


def test_dynamic_routing_is_documented_as_disabled_by_default():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "OCEANO_DYNAMIC_TOOLS=0" in text
    for name in ("OCEANO_DYNAMIC_TOOL_LIMIT", "OCEANO_DYNAMIC_TOOL_MODELS",
                 "OCEANO_DYNAMIC_TOOL_EXCLUDE_MODELS", "OCEANO_DYNAMIC_TOOL_TELEMETRY"):
        assert name in text
