"""Trust-aware portable skill packs (skill-pack v1).

A skill is reusable *guidance* — checklists, conventions, review
questions — composed into agent briefs. It is plain bounded Markdown
(SKILL.md-compatible: an HTML-comment metadata header followed by
instruction body), never executable content, never a plugin: this solves
a prompt-composition problem, and the module refuses to grow an
executable plugin system to do it.

Trust rules, enforced rather than assumed:

* **Imported skills are untrusted.** Every skill enters the store in
  state ``untrusted`` and becomes ``approved`` only through an explicit
  operator review decision (``aperio skill-review --approve``), or is
  ``revoked`` by one. Approval is recorded with the approver identity —
  a declaration, like every actor id in Aperio.
* **Operator requirements outrank skills.** Composed briefs render skill
  content under the quoted-context trust boundary; a skill's
  instructions can never become operator requirements and never gate
  approval or completion.
* **Conflicts are reported, not resolved.** A skill may declare
  ``conflicts``; selecting two skills that declare conflicts with each
  other raises an explicit finding. When two approved skills give
  different-versioned guidance for the same scope, the composition
  report records both and the higher version wins — deterministically,
  with the decision shown.
* **Private material never travels.** Skill export carries only the
  skill's own declared content; the store never mixes in memory,
  project state, or machine paths.

The store is one operator-named directory (like a memory store, never a
discovered or global location). Each skill lives in one subdirectory
containing ``SKILL.md``. Builtin packs ship inside the package with
provenance ``aperio-builtin-v1``
and enter a store as ``untrusted`` like anything else — installing the
package is not operator approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from aperio.errors import PathError, StateError, ValidationError

SKILL_STORE_FORMAT = "aperio-skill-store"
SKILL_STORE_SCHEMA_VERSION = 1

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
SKILL_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SKILL_SCOPES = (
    "general", "design", "ux", "design-systems", "accessibility",
    "frontend", "backend", "cms", "security", "testing", "release",
    "seo", "research", "writing", "business", "requirements", "planning",
)
SKILL_STATES = ("untrusted", "approved", "revoked")

MAX_SKILL_MD_BYTES = 64 * 1024
MAX_DEPENDENCIES = 12
MAX_CONFLICTS = 12
MAX_NAME_BYTES = 64
MAX_DESCRIPTION_BYTES = 300
MAX_STORE_SKILLS = 500

_HEADER_START = "<!-- aperio:skill"
_HEADER_END = "-->"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def _check_text(value: object, where: str, *, max_bytes: int,
                single_line: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where} must be non-empty text")
    if "\x00" in value:
        raise ValidationError(f"{where} must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{where} must be at most {max_bytes} bytes")
    if single_line and "\n" in value:
        raise ValidationError(f"{where} must be single-line")
    return value


def _parse_list_field(value: str, where: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if len(items) > MAX_DEPENDENCIES:
        raise ValidationError(
            f"{where} must list at most {MAX_DEPENDENCIES} entries"
        )
    for item in items:
        if not SKILL_NAME_RE.match(item):
            raise ValidationError(
                f"{where} entries must be skill names ({item!r} invalid)"
            )
    return items


def parse_skill_md(text: str, *, source_path: str = "SKILL.md") -> dict:
    """Parse and validate one SKILL.md document (skill-pack v1).

    The metadata header is an HTML comment block, so the file renders as
    ordinary Markdown in any SKILL.md-compatible reader:

        <!-- aperio:skill
        name: frontend-engineering
        version: 1.0.0
        description: ...
        scope: frontend
        depends_on: design-systems
        conflicts: legacy-css-guide
        provenance: aperio-builtin-v1
        -->
        ... instruction body (bounded Markdown) ...
    """

    text = _check_text(text, f"{source_path} content",
                       max_bytes=MAX_SKILL_MD_BYTES, single_line=False)
    if not text.lstrip().startswith(_HEADER_START):
        raise ValidationError(
            f"{source_path}: the metadata header must start with "
            f"{_HEADER_START!r} on the first line"
        )
    end = text.find(_HEADER_END)
    if end < 0:
        raise ValidationError(
            f"{source_path}: the metadata header is not closed with {_HEADER_END!r}"
        )
    header_text = text[len(_HEADER_START):end].strip()
    body = text[end + len(_HEADER_END):].strip()
    if not body:
        raise ValidationError(f"{source_path}: the instruction body is empty")

    fields: dict[str, str] = {}
    for line in header_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValidationError(
                f"{source_path}: header line {line!r} must be 'field: value'"
            )
        field, _, value = line.partition(":")
        field = field.strip().lower()
        if field in fields:
            raise ValidationError(
                f"{source_path}: duplicate header field {field!r}"
            )
        fields[field] = value.strip()

    known = {"name", "version", "description", "scope", "depends_on",
             "conflicts", "provenance"}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise ValidationError(
            f"{source_path}: unknown header field(s): " + ", ".join(unknown)
        )
    missing = sorted(known - {"depends_on", "conflicts"} - set(fields))
    if missing:
        raise ValidationError(
            f"{source_path}: missing header field(s): " + ", ".join(missing)
        )

    name = _check_text(fields["name"], f"{source_path}.name",
                       max_bytes=MAX_NAME_BYTES, single_line=True)
    if not SKILL_NAME_RE.match(name):
        raise ValidationError(
            f"{source_path}.name must match {SKILL_NAME_RE.pattern!r}"
        )
    version = _check_text(fields["version"], f"{source_path}.version",
                          max_bytes=20, single_line=True)
    if not SKILL_VERSION_RE.match(version):
        raise ValidationError(
            f"{source_path}.version must be semver-like (major.minor.patch)"
        )
    description = _check_text(fields["description"],
                              f"{source_path}.description",
                              max_bytes=MAX_DESCRIPTION_BYTES,
                              single_line=True)
    scope = _check_text(fields["scope"], f"{source_path}.scope",
                        max_bytes=40, single_line=True)
    if scope not in SKILL_SCOPES:
        raise ValidationError(
            f"{source_path}.scope must be one of: " + ", ".join(SKILL_SCOPES)
        )
    depends_on = _parse_list_field(fields.get("depends_on", ""),
                                   f"{source_path}.depends_on")
    if name in depends_on:
        raise ValidationError(
            f"{source_path}.depends_on must not include itself"
        )
    conflicts = _parse_list_field(fields.get("conflicts", ""),
                                  f"{source_path}.conflicts")
    provenance = _check_text(fields.get("provenance", "imported"),
                             f"{source_path}.provenance", max_bytes=200,
                             single_line=True)

    content_sha256 = hashlib.sha256(
        json.dumps(
            {"name": name, "version": version, "description": description,
             "scope": scope, "depends_on": depends_on,
             "conflicts": conflicts, "provenance": provenance,
             "body": body},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return {
        "name": name,
        "version": version,
        "description": description,
        "scope": scope,
        "depends_on": depends_on,
        "conflicts": conflicts,
        "provenance": provenance,
        "body": body,
        "content_sha256": content_sha256,
        "source_path": source_path,
    }


def render_skill_md(skill: dict) -> str:
    """Render a validated skill back to SKILL.md form (export/inspection)."""

    header = [
        "name: " + skill["name"],
        "version: " + skill["version"],
        "description: " + skill["description"],
        "scope: " + skill["scope"],
    ]
    if skill.get("depends_on"):
        header.append("depends_on: " + ", ".join(skill["depends_on"]))
    if skill.get("conflicts"):
        header.append("conflicts: " + ", ".join(skill["conflicts"]))
    header.append("provenance: " + skill.get("provenance", "imported"))
    return (
        f"{_HEADER_START}\n" + "\n".join(header) + f"\n{_HEADER_END}\n\n"
        + skill["body"] + "\n"
    )


def _skill_state_path(store_dir: Path, name: str) -> Path:
    return store_dir / name / "SKILL.md"


def _skill_state_file(store_dir: Path, name: str) -> Path:
    return store_dir / name / "state.json"


class SkillStore:
    """One operator-named directory of skill packs.

    Layout: ``<store>/<skill-name>/SKILL.md`` plus
    ``<store>/<skill-name>/state.json`` (state-schema v1: the review
    state and its recorded decisions). The directory is created
    explicitly by the operator; Aperia never scans for stores.
    """

    def __init__(self, store_dir: Path):
        self.store_dir = Path(store_dir)
        if ".aperio" in Path(os.path.realpath(self.store_dir)).parts:
            raise PathError(
                "refusing to use a skill store inside '.aperio/' project "
                "state; choose a path outside it"
            )

    def _validate_store_dir(self, *, create: bool) -> None:
        if self.store_dir.exists():
            if os.path.islink(self.store_dir):
                raise PathError(
                    f"refusing to follow symlink at {self.store_dir}"
                )
            if not self.store_dir.is_dir():
                raise PathError(
                    f"skill store {self.store_dir} is not a directory"
                )
        elif create:
            self.store_dir.mkdir(parents=True, exist_ok=False)
        else:
            raise StateError(
                f"no skill store at {self.store_dir}; import a skill with "
                "'aperio skill-import' or point --store at an existing "
                "store"
            )

    def list_skills(self) -> list[dict]:
        self._validate_store_dir(create=False)
        skills = []
        for entry in sorted(self.store_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                skills.append(self.read_skill(entry.name))
        if len(skills) > MAX_STORE_SKILLS:
            raise StateError(
                f"the skill store holds more than {MAX_STORE_SKILLS} "
                "skills; split it"
            )
        return skills

    def read_skill(self, name: str) -> dict:
        if not SKILL_NAME_RE.match(name):
            raise ValidationError(f"skill name {name!r} is invalid")
        path = _skill_state_path(self.store_dir, name)
        if os.path.islink(path):
            raise PathError(
                f"refusing to follow symlink at {path} for skill {name}"
            )
        if not path.is_file():
            raise StateError(f"no skill {name!r} in the store")
        skill = parse_skill_md(path.read_text(encoding="utf-8"),
                               source_path=str(path))
        skill["state"] = self._read_state(name)
        return skill

    def _read_state(self, name: str) -> dict:
        state_path = _skill_state_file(self.store_dir, name)
        default = {
            "state_schema_version": SKILL_STORE_SCHEMA_VERSION,
            "state": "untrusted",
            "reviews": [],
        }
        if not state_path.exists():
            return default
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(
                f"skill state for {name!r} is damaged ({exc}); repair it "
                "by hand — never auto-repaired"
            ) from None
        if state.get("state_schema_version") != SKILL_STORE_SCHEMA_VERSION:
            raise StateError(
                f"skill state for {name!r} has an unsupported schema "
                "version"
            )
        if state.get("state") not in SKILL_STATES:
            raise StateError(
                f"skill state for {name!r} is invalid: "
                f"{state.get('state')!r}"
            )
        return state

    def _write_state(self, name: str, state: dict) -> None:
        skill_dir = self.store_dir / name
        state_path = _skill_state_file(self.store_dir, name)
        tmp = state_path.with_name("state.json.tmp")
        if os.path.islink(tmp) or tmp.exists():
            raise StateError(
                f"{tmp.name} is occupied for skill {name!r}; resolve it "
                "explicitly"
            )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            data = (json.dumps(state, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n").encode("utf-8")
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, state_path)

    def import_skill_md(self, text: str, *, source: str) -> dict:
        """Import one skill as ``untrusted``. Existing content is never
        overwritten: re-importing the same content is a no-op reported
        as such, and different content under an existing name is
        refused."""

        self._validate_store_dir(create=True)
        skill = parse_skill_md(text, source_path=source)
        skill_dir = self.store_dir / skill["name"]
        path = _skill_state_path(self.store_dir, skill["name"])
        if path.exists():
            existing = parse_skill_md(path.read_text(encoding="utf-8"),
                                      source_path=str(path))
            if existing["content_sha256"] == skill["content_sha256"]:
                return {
                    "imported": False,
                    "reason": "identical skill already present",
                    "skill": {**existing,
                              "state": self._read_state(skill["name"])},
                }
            if existing["version"] == skill["version"]:
                raise ValidationError(
                    f"skill {skill['name']!r} v{skill['version']} already "
                    "exists with different content at the same version; "
                    "import it under a new name or bump the version — "
                    "nothing is overwritten"
                )
            # A different version is a new pack: content changed, so the
            # trust judgment must change with it. The new content enters
            # as untrusted even if the old version was approved.
            if os.path.islink(path):
                raise PathError(
                    f"refusing to follow symlink at {path}"
                )
            replaced_from = existing["version"]
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0))
            try:
                data = render_skill_md(skill).encode("utf-8")
                written = 0
                while written < len(data):
                    written += os.write(fd, data[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            self._write_state(skill["name"], {
                "state_schema_version": SKILL_STORE_SCHEMA_VERSION,
                "state": "untrusted",
                "reviews": [{
                    "action": "import",
                    "actor": None,
                    "at": _utc_now(),
                    "note": f"content changed v{replaced_from} -> "
                            f"v{skill['version']}; previous approval does "
                            "not carry over — review again",
                }],
            })
            return {
                "imported": True,
                "reason": (
                    f"content changed v{replaced_from} -> "
                    f"v{skill['version']}; state reset to untrusted"
                ),
                "skill": {**skill,
                          "state": self._read_state(skill["name"])},
            }
        if len(self.list_skills()) >= MAX_STORE_SKILLS:
            raise ValidationError("the skill store is full")
        if os.path.islink(skill_dir):
            raise PathError(
                f"refusing to follow symlink at {skill_dir}"
            )
        skill_dir.mkdir(exist_ok=False)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            data = render_skill_md(skill).encode("utf-8")
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        self._write_state(skill["name"], {
            "state_schema_version": SKILL_STORE_SCHEMA_VERSION,
            "state": "untrusted",
            "reviews": [{
                "action": "import",
                "actor": None,
                "at": _utc_now(),
                "note": f"imported from {source}; untrusted until "
                        "approved",
            }],
        })
        return {"imported": True, "reason": "imported as untrusted",
                "skill": {**skill,
                          "state": self._read_state(skill["name"])}}

    def review(self, name: str, *, action: str, actor: str,
               note: str | None = None) -> dict:
        if action not in ("approve", "revoke"):
            raise ValidationError(
                "skill review action must be 'approve' or 'revoke'"
            )
        actor = _check_text(actor, "skill-review actor", max_bytes=200,
                            single_line=True)
        if note is not None:
            note = _check_text(note, "skill-review note", max_bytes=1000,
                               single_line=False)
        skill = self.read_skill(name)
        state = skill["state"]
        if action == "approve" and state["state"] == "approved":
            raise ValidationError(f"skill {name!r} is already approved")
        if action == "revoke" and state["state"] == "untrusted":
            raise ValidationError(
                f"skill {name!r} is untrusted; revoking is meaningless — "
                "dismiss it by removing the pack"
            )
        new_state = "approved" if action == "approve" else "revoked"
        state["state"] = new_state
        state["reviews"].append({
            "action": action,
            "actor": actor,
            "at": _utc_now(),
            "note": note,
        })
        self._write_state(name, state)
        return {**skill, "state": state}


# ---------------------------------------------------------------------------
# selection and composition
# ---------------------------------------------------------------------------


def select_skills(skills: list[dict], *, scope: str,
                  required: list[str] | None = None) -> dict:
    """Select approved skills for one task scope, with explicit findings.

    Only ``approved`` skills are selected — ``untrusted`` ones are listed
    as rejected-with-reason, never composed. Declared conflicts between
    selected skills are reported; versioned duplication (same name,
    different versions) keeps the higher version deterministically.
    """

    approved = [s for s in skills if s["state"]["state"] == "approved"]
    rejected = [
        {"name": s["name"],
         "reason": f"skill state is {s['state']['state']!r}"}
        for s in skills if s["state"]["state"] != "approved"
    ]
    pool: dict[str, dict] = {}
    superseded: list[dict] = []
    for skill in approved:
        existing = pool.get(skill["name"])
        if existing is None or _version_key(skill["version"]) > _version_key(
            existing["version"]
        ):
            if existing is not None:
                superseded.append({
                    "name": existing["name"],
                    "superseded_version": existing["version"],
                    "kept_version": skill["version"],
                })
            pool[skill["name"]] = skill

    if required:
        unknown_required = sorted(
            set(required) - set(pool)
        )
        if unknown_required:
            raise ValidationError(
                "skill selection: required skill(s) not approved in this "
                "store: " + ", ".join(unknown_required)
            )

    selected = []
    for name in sorted(pool):
        skill = pool[name]
        scope_match = skill["scope"] == scope or skill["scope"] == "general"
        if required is not None and name in required:
            scope_match = True
        if scope_match:
            selected.append(skill)
        else:
            rejected.append({
                "name": name,
                "reason": f"scope {skill['scope']!r} does not match "
                          f"{scope!r}",
            })

    conflicts: list[dict] = []
    selected_names = {s["name"] for s in selected}
    for skill in selected:
        for conflicting in skill.get("conflicts") or []:
            if conflicting in selected_names:
                conflicts.append({
                    "between": sorted([skill["name"], conflicting]),
                    "resolution": (
                        "both declare a conflict with each other; the "
                        "operator must deselect one — nothing is "
                        "auto-resolved"
                    ),
                })
    return {
        "selected": selected,
        "rejected": rejected,
        "conflicts": conflicts,
        "superseded_versions": superseded,
    }


def _version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return (major, minor, patch)


def compose_skill_section(selection: dict) -> dict:
    """The brief-facing composition of selected skills, with provenance."""

    selected = selection["selected"]
    if selection["conflicts"]:
        raise ValidationError(
            "skill composition: declared conflicts between selected "
            "skills must be resolved by the operator first "
            + "; ".join(
                "/".join(pair["between"])
                for pair in selection["conflicts"]
            )
        )
    composed = []
    for skill in sorted(selected, key=lambda s: s["name"]):
        composed.append({
            "name": skill["name"],
            "version": skill["version"],
            "scope": skill["scope"],
            "provenance": skill.get("provenance", "imported"),
            "content_sha256": skill["content_sha256"],
            "body": skill["body"],
        })
    return {
        "note": (
            "selected skills are quoted guidance under the brief's trust "
            "boundary; operator requirements outrank them, and they never "
            "gate approval or completion"
        ),
        "skills": composed,
        "untrusted_rejected": selection["rejected"],
        "conflicts": selection["conflicts"],
        "superseded_versions": selection["superseded_versions"],
    }


# ---------------------------------------------------------------------------
# builtin packs (shipped with the package, still untrusted until approved)
# ---------------------------------------------------------------------------


def builtin_skill_packs() -> list[dict]:
    """The builtin skill packs shipped with the package."""

    import importlib.resources

    packs = []
    root = importlib.resources.files("aperio") / "skill_packs"
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.name.endswith(".md"):
            packs.append(parse_skill_md(entry.read_text(encoding="utf-8"),
                                        source_path=f"builtin:{entry.name}"))
    if not packs:
        raise StateError("the builtin skill-pack set is empty")
    return packs


def export_approved_skills(store_dir: Path) -> dict:
    """Portable export of APPROVED skills only (skill-pack export v1)."""

    store = SkillStore(store_dir)
    skills = [s for s in store.list_skills()
              if s["state"]["state"] == "approved"]
    return {
        "format": "aperio-skill-export",
        "format_version": 1,
        "created_at": _utc_now(),
        "skills": [
            {
                "name": s["name"],
                "version": s["version"],
                "description": s["description"],
                "scope": s["scope"],
                "depends_on": s["depends_on"],
                "conflicts": s["conflicts"],
                "provenance": s.get("provenance", "imported"),
                "content_sha256": s["content_sha256"],
                "body": s["body"],
            }
            for s in sorted(skills, key=lambda s: s["name"])
        ],
        "limitations": [
            "export carries only the skills' own declared content; "
            "memory, project state, and machine paths never travel with "
            "it",
            "untrusted and revoked skills are excluded",
        ],
    }


def import_skill_export(store_dir: Path, document: dict) -> dict:
    """Import a skill export: every skill enters as ``untrusted``."""

    if not isinstance(document, dict) or document.get("format") != \
            "aperio-skill-export":
        raise ValidationError(
            "skill import: expected an 'aperio-skill-export' document"
        )
    if document.get("format_version") != 1:
        raise ValidationError(
            "skill import: unsupported format_version"
        )
    store = SkillStore(store_dir)
    results = []
    for entry in document.get("skills", []):
        text = render_skill_md({
            "name": entry["name"],
            "version": entry["version"],
            "description": entry["description"],
            "scope": entry["scope"],
            "depends_on": entry.get("depends_on") or [],
            "conflicts": entry.get("conflicts") or [],
            "provenance": entry.get("provenance", "imported"),
            "body": entry["body"],
            "content_sha256": entry.get("content_sha256"),
        })
        parsed = parse_skill_md(text, source_path=f"export:{entry['name']}")
        if entry.get("content_sha256") and parsed["content_sha256"] != \
                entry["content_sha256"]:
            raise ValidationError(
                f"skill import: {entry['name']} content hash mismatch; "
                "the export was altered"
            )
        results.append(store.import_skill_md(
            text, source=f"export ({entry.get('provenance', 'imported')})"
        ))
    return {"imported": results}
