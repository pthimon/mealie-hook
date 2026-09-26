"""The editable rules: a vocabulary of Mealie's organizers plus three prompt templates.

They live in DATA_DIR/rules and are read on every use, so an edit from the rules page takes
effect on the next recipe without a redeploy. The repo's rules/ directory holds the shipped
defaults, copied in on first start.

Mealie itself supplies the NAMES (the response schemas are built from its live lists); this
file supplies what the names MEAN -- guidance rendered into the prompts, and roles that the
rules in code key on instead of hardcoded names.
"""

import difflib
import hashlib
import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError, model_validator

Kind = Literal["categories", "tags", "tools", "labels"]
KINDS: tuple[str, ...] = ("categories", "tags", "tools", "labels")

ROLE_DOCS: dict[str, tuple[str, str]] = {
    "main": ("categories", "main-dish slot: not-on-mains tags are dropped, new_protein is considered"),
    "protein": ("tags", "listed as a protein tag"),
    "season": ("tags", "listed as a season tag"),
    "summer": ("tags", "never kept together with a winter tag"),
    "winter": ("tags", "added to anything that uses a slow-cooker tool"),
    "character": ("tags", "listed as a character tag"),
    "not-on-mains": ("tags", "never kept on a main category"),
    "provenance": ("tags", "records where a recipe came from; never chosen by the model"),
    "slow-cooker": ("tools", "added when the recipe says \"slow cooker\"; implies winter"),
    "herbs": ("labels", "\"sage leaves\" folds onto an existing herb food in this aisle"),
}
Role = Literal["main", "protein", "season", "summer", "winter", "character", "not-on-mains",
               "provenance", "slow-cooker", "herbs"]

PROMPTS = ("ingredients.md", "classify.md", "labels.md")
VOCAB_FILE = "vocabulary.toml"
FILES = (VOCAB_FILE, *PROMPTS)
# Placeholders each template must keep; the rendered vocabulary goes there.
PLACEHOLDERS = {"ingredients.md": (), "classify.md": ("{{categories}}", "{{tags}}", "{{tools}}"),
                "labels.md": ("{{labels}}",)}


class Entry(BaseModel):
    guidance: str = ""
    roles: list[Role] = []


class Vocabulary(BaseModel):
    categories: dict[str, Entry] = {}
    tags: dict[str, Entry] = {}
    tools: dict[str, Entry] = {}
    labels: dict[str, Entry] = {}

    @model_validator(mode="after")
    def _roles_fit_kind(self):
        for kind in KINDS:
            for name, entry in getattr(self, kind).items():
                for role in entry.roles:
                    if ROLE_DOCS[role][0] != kind:
                        raise ValueError(f"role {role!r} belongs to {ROLE_DOCS[role][0]}, "
                                         f"not {kind} ({name!r})")
        return self

    def section(self, kind: str) -> dict[str, Entry]:
        return getattr(self, kind)

    def with_role(self, kind: str, role: str) -> list[str]:
        return [n for n, e in self.section(kind).items() if role in e.roles]

    def guidance(self, kind: str, name: str) -> str:
        e = self.section(kind).get(name)
        return e.guidance if e else ""

    @classmethod
    def parse_toml(cls, text: str) -> "Vocabulary":
        return cls.model_validate(tomllib.loads(text))

    def to_toml(self) -> str:
        """Canonical text: one line per field, arrays inline, sections in a fixed order.

        Written by hand rather than with a TOML library so that a one-field change is a
        one-line diff on the rules page. The shipped default is in this exact form.
        """
        out = ["# Guidance and roles for Mealie's organizers. Names must match Mealie exactly;",
               "# a name in Mealie but not here is still offered to the model, with no guidance.",
               "# Edited from the rules page (/rules/); this header is regenerated on save.",
               "#", "# Roles:"]
        out += [f"#   {kind:<11} {role:<13} {doc}" for role, (kind, doc) in ROLE_DOCS.items()]
        for kind in KINDS:
            out += ["", f"# {'-' * 20} {kind}"]
            for name, e in self.section(kind).items():
                out += ["", f"[{kind}.{_toml_key(name)}]"]
                if e.roles:
                    out.append("roles = [" + ", ".join(_toml_str(r) for r in e.roles) + "]")
                if e.guidance:
                    out.append(f"guidance = {_toml_str(e.guidance)}")
        return "\n".join(out) + "\n"


def _toml_key(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9_-]+", s) else _toml_str(s)


def _toml_str(s: str) -> str:
    # A JSON string is a valid TOML basic string: same escapes, and ensure_ascii=False keeps
    # "comté" readable.
    return json.dumps(s, ensure_ascii=False)


class RulesError(ValueError):
    pass


@dataclass
class Rules:
    """One consistent set of the four files, parsed."""
    vocab: Vocabulary
    prompts: dict[str, str]
    texts: dict[str, str] = field(default_factory=dict)   # raw file contents, for diffs

    @classmethod
    def from_texts(cls, texts: dict[str, str]) -> "Rules":
        try:
            vocab = Vocabulary.parse_toml(texts[VOCAB_FILE])
        except (tomllib.TOMLDecodeError, ValidationError, ValueError) as e:
            raise RulesError(f"{VOCAB_FILE}: {e}") from None
        for name in PROMPTS:
            missing = [p for p in PLACEHOLDERS[name] if p not in texts[name]]
            if missing:
                raise RulesError(f"{name}: placeholder(s) {', '.join(missing)} removed")
        return cls(vocab, {n: texts[n] for n in PROMPTS}, dict(texts))

    # ----------------------------------------------------------------- rendering

    def render(self, name: str, live: dict[str, list[str]], hidden_tags: set[str] = frozenset()) -> str:
        """Template `name` with the vocabulary sections filled from Mealie's live names."""
        text = self.prompts[name]
        subs = {
            "{{categories}}": self._bullets("categories", live.get("categories", [])),
            "{{tools}}": self._bullets("tools", live.get("tools", [])) or "(none)",
            "{{labels}}": self._bullets("labels", live.get("labels", [])),
            "{{tags}}": self._tags(live.get("tags", []), hidden_tags),
        }
        for key, value in subs.items():
            text = text.replace(key, value)
        return text.strip()

    def _bullets(self, kind: str, names: list[str]) -> str:
        out = []
        for n in names:
            g = self.vocab.guidance(kind, n)
            out.append(f"- **{n}**" + (f" — {g}" if g else ""))
        return "\n".join(out)

    def _tags(self, names: list[str], hidden: set[str]) -> str:
        v = self.vocab
        offered = [n for n in names if n not in hidden
                   and "provenance" not in (v.tags.get(n) or Entry()).roles]
        groups = [("Protein tags", "protein"), ("Season tags", "season"),
                  ("Character tags", "character")]
        lines, placed = [], set()
        not_mains = set(v.with_role("tags", "not-on-mains"))
        for title, role in groups:
            members = [n for n in offered if role in (v.tags.get(n) or Entry()).roles]
            if not members:
                continue
            lines.append(f"{title}:")
            for n in members:
                g = v.guidance("tags", n)
                extra = " (not on mains)" if role == "character" and n in not_mains else ""
                lines.append(f"- **{n}**{extra}" + (f" — {g}" if g else ""))
            placed |= set(members)
        rest = [n for n in offered if n not in placed]
        if rest:
            lines.append("Other tags:")
            lines += [f"- **{n}**" + (f" — {v.guidance('tags', n)}" if v.guidance("tags", n) else "")
                      for n in rest]
        return "\n".join(lines)

    # ------------------------------------------------------------------- drift

    def drift(self, live: dict[str, list[str]]) -> list[dict]:
        """Where the vocabulary and Mealie disagree, and which code rules that disables."""
        out = []
        singular = {"categories": "category", "tags": "tag", "tools": "tool", "labels": "aisle label"}
        for kind in KINDS:
            have = set(live.get(kind, []))
            for name in self.vocab.section(kind):
                if name not in have:
                    out.append({"level": "warn", "kind": kind, "name": name, "create": True,
                                "message": f"{singular[kind]} {name!r} is in the vocabulary "
                                           f"but not in Mealie"})
            for name in live.get(kind, []):
                if name not in self.vocab.section(kind):
                    out.append({"level": "info", "kind": kind, "name": name,
                                "message": f"{singular[kind]} {name!r} has no guidance"})
        rules = [
            ("slow-cooker ⇒ winter", [("tools", "slow-cooker"), ("tags", "winter")]),
            ("never summer and winter together", [("tags", "summer"), ("tags", "winter")]),
            ("not-on-mains tags dropped from mains", [("categories", "main"),
                                                      ("tags", "not-on-mains")]),
            ("herb leaves fold onto the herb", [("labels", "herbs")]),
        ]
        for rule, needs in rules:
            gaps = [f"{role} {singular[kind]}" for kind, role in needs
                    if not set(self.vocab.with_role(kind, role)) & set(live.get(kind, []))]
            if gaps:
                out.append({"level": "warn", "kind": "rule", "name": rule,
                            "message": f"rule '{rule}' is disabled: no {' or '.join(gaps)} "
                                       f"in Mealie"})
        return out


def unified(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"a/{name}", f"b/{name}", n=2))


class RulesStore:
    """DATA_DIR/rules: the live files, plus history/ with a snapshot before each change."""

    def __init__(self, directory: Path, defaults: Path):
        self.dir = Path(directory)
        self.defaults = Path(defaults)
        self.history = self.dir / "history"
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            if not (self.dir / name).exists():
                shutil.copy(self.defaults / name, self.dir / name)

    def texts(self) -> dict[str, str]:
        return {n: (self.dir / n).read_text() for n in FILES}

    def load(self) -> Rules:
        return Rules.from_texts(self.texts())

    def version(self) -> str:
        """Changes whenever any file changes; guards against applying a stale proposal."""
        blob = json.dumps(self.texts(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def save(self, new_texts: dict[str, str], summary: str, who: str = "") -> str:
        """Validate, snapshot the current files, then write. Returns the snapshot id."""
        merged = {**self.texts(), **new_texts}
        Rules.from_texts(merged)                    # raises RulesError if invalid
        snap = self._snapshot(summary, who)
        for name, text in new_texts.items():
            if name not in FILES:
                raise RulesError(f"unknown rules file {name!r}")
            tmp = self.dir / f".{name}.tmp"
            tmp.write_text(text)
            tmp.replace(self.dir / name)
        return snap

    def _snapshot(self, summary: str, who: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        d = self.history / stamp
        d.mkdir(parents=True)
        for name in FILES:
            shutil.copy(self.dir / name, d / name)
        (d / "meta.json").write_text(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "summary": summary, "who": who}))
        return stamp

    def list_history(self) -> list[dict]:
        out = []
        for d in sorted(self.history.glob("*"), reverse=True) if self.history.exists() else []:
            meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
            out.append({"id": d.name, **meta})
        return out

    def snapshot_texts(self, snap_id: str) -> dict[str, str]:
        if not re.fullmatch(r"\d{8}T\d{12}", snap_id):
            raise RulesError("bad snapshot id")
        d = self.history / snap_id
        if not d.exists():
            raise RulesError(f"no snapshot {snap_id}")
        return {n: (d / n).read_text() for n in FILES if (d / n).exists()}

    def revert(self, snap_id: str, who: str = "") -> str:
        """Restore the files as they were BEFORE the change recorded by `snap_id`."""
        return self.save(self.snapshot_texts(snap_id), f"revert to before {snap_id}", who)
