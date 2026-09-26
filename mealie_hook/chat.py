"""Editing the rules by describing the change to Qwen.

The model never writes files. It returns a structured proposal -- vocabulary operations,
exact find/replace edits to the prompts, and suggested organizers to create in Mealie --
which is applied to a copy of the rules, validated and shown as a diff. Only an explicit
Apply writes it, as a new version in the history.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .llm import LLM, LLMError
from .rules import (FILES, ROLE_DOCS, VOCAB_FILE, Entry, Kind, Role, Rules, RulesError,
                    Vocabulary, unified)

log = logging.getLogger(__name__)

EDITOR_PROMPT = Path(__file__).with_name("editor.md")


class VocabOp(BaseModel):
    op: Literal["set", "delete"]
    kind: Kind
    name: str = Field(min_length=1, max_length=60)
    guidance: str | None = None
    roles: list[Role] | None = None


class PromptEdit(BaseModel):
    file: Literal["ingredients.md", "classify.md", "labels.md"]
    find: str = Field(min_length=1)
    replace: str


class CreateOrganizer(BaseModel):
    kind: Kind
    name: str = Field(min_length=1, max_length=60)


class EditProposal(BaseModel):
    reply: str = Field(max_length=800)
    vocab_ops: list[VocabOp] = []
    prompt_edits: list[PromptEdit] = []
    create_in_mealie: list[CreateOrganizer] = []


@dataclass
class Applied:
    texts: dict[str, str]                       # the full new set of files
    changed: list[str]
    errors: list[str] = field(default_factory=list)      # block applying
    warnings: list[str] = field(default_factory=list)    # shown, and trigger one retry


def apply_proposal(texts: dict[str, str], p: EditProposal) -> Applied:
    """Apply a proposal to a copy of the files. Pure; never raises for a bad proposal."""
    new, errors = dict(texts), []
    if p.vocab_ops:
        try:
            vocab = Vocabulary.parse_toml(texts[VOCAB_FILE])
        except Exception as e:
            return Applied(new, [], [f"current {VOCAB_FILE} is invalid: {e}"])
        for op in p.vocab_ops:
            section = vocab.section(op.kind)
            if op.op == "delete":
                if op.name not in section:
                    errors.append(f"cannot delete {op.kind}.{op.name!r}: not in the vocabulary")
                else:
                    del section[op.name]
                continue
            entry = section.get(op.name) or Entry()
            if op.guidance is not None:
                entry.guidance = op.guidance.strip()
            if op.roles is not None:
                entry.roles = list(dict.fromkeys(op.roles))
            section[op.name] = entry
        try:
            new[VOCAB_FILE] = Vocabulary.model_validate(vocab.model_dump()).to_toml()
        except ValueError as e:
            errors.append(str(e).splitlines()[-1] if str(e) else "invalid vocabulary")
    for edit in p.prompt_edits:
        n = new[edit.file].count(edit.find)
        if n != 1:
            errors.append(f"{edit.file}: the text to replace was found {n} times "
                          f"(must be exactly once): {edit.find[:80]!r}")
            continue
        new[edit.file] = new[edit.file].replace(edit.find, edit.replace)
    if not errors:
        try:
            Rules.from_texts(new)
        except RulesError as e:
            errors.append(str(e))
    changed = [f for f in FILES if new[f] != texts[f]]
    # Creating a name in Mealie without describing it would leave it with no roles or
    # guidance -- a "Duck" tag that is never treated as a protein.
    described = {(op.kind, op.name) for op in p.vocab_ops if op.op == "set"}
    try:
        existing = Vocabulary.parse_toml(new[VOCAB_FILE])
    except Exception:
        existing = Vocabulary()
    warnings = [f"{c.name!r} would be created in Mealie but has no entry in the vocabulary; "
                f"add a vocab_ops set for {c.kind}.{c.name} with its roles and guidance"
                for c in p.create_in_mealie
                if (c.kind, c.name) not in described and c.name not in existing.section(c.kind)]
    return Applied(new, changed, errors, warnings)


def _context(texts: dict[str, str], live: dict[str, list[str]]) -> str:
    parts = [EDITOR_PROMPT.read_text().strip(), "", "## Names that exist in Mealie now"]
    parts += [f"- {kind}: {', '.join(names) or '(none)'}" for kind, names in live.items()]
    parts += ["", "## Roles", *[f"- {r} ({k}): {d}" for r, (k, d) in ROLE_DOCS.items()]]
    for name in FILES:
        parts += ["", f"## Current {name}", "```", texts[name].strip(), "```"]
    return "\n".join(parts)


def propose(llm: LLM, texts: dict[str, str], live: dict[str, list[str]],
            conversation: list[dict]) -> tuple[EditProposal, Applied]:
    """One chat turn. Retries once, telling the model what was wrong, if its edits fail."""
    messages = [{"role": "system", "content": _context(texts, live)}]
    messages += [{"role": m["role"], "content": m["content"]} for m in conversation[-12:]
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    proposal = llm.structured(messages, EditProposal, name="rules-edit", temperature=0.2)
    applied = apply_proposal(texts, proposal)
    problems = applied.errors + applied.warnings
    if problems:
        messages += [{"role": "assistant", "content": proposal.model_dump_json()},
                     {"role": "user", "content": "That proposal has problems:\n- "
                      + "\n- ".join(problems)
                      + "\nAnswer my request again from scratch with all of them fixed. Copy "
                        "`find` text exactly from the current file. Write `reply` as if this "
                        "were your first answer; do not mention this correction."}]
        try:
            retry = llm.structured(messages, EditProposal, name="rules-edit", temperature=0.2)
        except LLMError:
            return proposal, applied
        retry_applied = apply_proposal(texts, retry)
        score = lambda a: (len(a.errors), len(a.warnings))
        if score(retry_applied) < score(applied):
            proposal, applied = retry, retry_applied
    return proposal, applied


def suggested_creates(p: EditProposal, live: dict[str, list[str]]) -> list[dict]:
    """Organizers to offer creating: the model's list plus any vocabulary name Mealie lacks."""
    out, seen = [], set()
    items = [(c.kind, c.name) for c in p.create_in_mealie]
    items += [(op.kind, op.name) for op in p.vocab_ops if op.op == "set"]
    for kind, name in items:
        if name not in live.get(kind, []) and (kind, name) not in seen:
            seen.add((kind, name))
            out.append({"kind": kind, "name": name})
    return out


@dataclass
class Proposal:
    id: str
    base_version: str
    request: str
    proposal: EditProposal
    applied: Applied
    creates: list[dict]
    at: float = field(default_factory=time.time)

    def view(self, before: dict[str, str]) -> dict:
        return {
            "id": self.id,
            "reply": self.proposal.reply,
            "errors": self.applied.errors,
            "warnings": self.applied.warnings,
            "changed": self.applied.changed,
            "diffs": {f: unified(before[f], self.applied.texts[f], f) for f in self.applied.changed},
            "creates": self.creates,
            "applicable": bool(self.applied.changed) and not self.applied.errors,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        bits = [f"{op.op} {op.kind}.{op.name}" for op in self.proposal.vocab_ops]
        bits += [f"edit {e.file}" for e in self.proposal.prompt_edits]
        return "; ".join(bits)


class ProposalStore:
    """Recent proposals, in memory: they are cheap to regenerate and stale after an apply."""

    def __init__(self, keep: int = 30):
        self.keep = keep
        self._items: dict[str, Proposal] = {}
        self._lock = threading.Lock()

    def add(self, **kw) -> Proposal:
        p = Proposal(id=uuid.uuid4().hex[:12], **kw)
        with self._lock:
            self._items[p.id] = p
            for old in sorted(self._items.values(), key=lambda x: x.at)[:-self.keep]:
                del self._items[old.id]
        return p

    def get(self, pid: str) -> Proposal | None:
        return self._items.get(pid)


def history_line(p: Proposal) -> str:
    """What the browser keeps as the assistant's turn, so later turns have context."""
    s = p.proposal.reply
    if p.summary():
        s += f"\n[proposed: {p.summary()}]"
    return s

