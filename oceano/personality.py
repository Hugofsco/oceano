"""Oceano's personality: a single freeform block of prose, edited only by the user
(Brain -> Identity), describing who Oceano is and how it should sound. Injected first
in every turn's context (see agent._personality_note), ahead of memory and everything
else, so it frames how the rest is read. Deliberately user-only — no tool exposes this
to the model, unlike 'identity'-category memories, which the agent DOES write to itself
via remember() as it learns atomistic facts about itself and the user.
"""
import config
from oceano import atomicio

PATH = config.WORKSPACE.parent / "data" / "personality.txt"


def get():
    try:
        return PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save(text):
    atomicio.write_text(PATH, (text or "").strip())
    return get()
