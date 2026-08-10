#!/usr/bin/env python3
"""Validate every skill in skills/ before it ships.

Checks what actually breaks installs: malformed frontmatter, a name the CLI will
reject, a description too long for Claude Code, references to files that were
never committed, and scripts that don't parse.

    python3 scripts/validate.py

Exit code 0 if every skill is clean, 1 otherwise. Stdlib only — CI shouldn't
need a package install to tell you a YAML key is missing.
"""

from __future__ import annotations

import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"

# The CLI's own rule: lowercase alphanumerics and single internal hyphens.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_DESCRIPTION = 1024

# Paths a SKILL.md points the agent at: {skill_dir}/scripts/x.sh, `reference/y.md`.
PATH_PATTERNS = (
    re.compile(r"\{skill_dir\}/([A-Za-z0-9_./-]+)"),
    re.compile(r"`((?:scripts|reference)/[A-Za-z0-9_./-]+)`"),
)


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Return top-level scalar keys from the leading --- block, or None if absent."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fields: dict[str, str] = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" and key:  # folded continuation of the previous value
            fields[key] += " " + line.strip()
            continue
        if ":" not in line:
            return None
        key, _, value = line.partition(":")
        key = key.strip()
        fields[key] = value.strip()
    return fields


def check_skill(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    rel = skill_dir.relative_to(ROOT)
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.is_file():
        return [f"{rel}: no SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)
    if fields is None:
        return [f"{rel}/SKILL.md: missing or unterminated YAML frontmatter"]

    name = fields.get("name", "")
    description = fields.get("description", "")

    if not name:
        errors.append(f"{rel}/SKILL.md: frontmatter has no 'name'")
    else:
        if not NAME_RE.match(name):
            errors.append(
                f"{rel}/SKILL.md: name '{name}' must be lowercase alphanumerics "
                "separated by single hyphens"
            )
        if name != skill_dir.name:
            errors.append(
                f"{rel}/SKILL.md: name '{name}' does not match directory '{skill_dir.name}'"
            )

    if not description:
        errors.append(f"{rel}/SKILL.md: frontmatter has no 'description'")
    elif len(description) > MAX_DESCRIPTION:
        errors.append(
            f"{rel}/SKILL.md: description is {len(description)} chars, "
            f"over the {MAX_DESCRIPTION} limit Claude Code enforces"
        )

    for pattern in PATH_PATTERNS:
        for referenced in set(pattern.findall(text)):
            if not (skill_dir / referenced).exists():
                errors.append(f"{rel}/SKILL.md: references missing file '{referenced}'")

    for script in sorted(skill_dir.rglob("*.sh")):
        proc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        if proc.returncode != 0:
            errors.append(
                f"{script.relative_to(ROOT)}: syntax error — {proc.stderr.strip()}"
            )

    for module in sorted(skill_dir.rglob("*.py")):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pyc") as out:
                py_compile.compile(str(module), cfile=out.name, doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{module.relative_to(ROOT)}: syntax error — {exc.msg.strip()}")

    return errors


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"no skills/ directory at {SKILLS_DIR}", file=sys.stderr)
        return 1

    skill_dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not skill_dirs:
        print("skills/ contains no skills", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    for skill_dir in skill_dirs:
        errors = check_skill(skill_dir)
        status = "FAIL" if errors else "ok"
        print(f"{status:>4}  {skill_dir.relative_to(ROOT)}")
        all_errors.extend(errors)

    if all_errors:
        print()
        for error in all_errors:
            print(f"  - {error}")
        print(f"\n{len(all_errors)} problem(s) in {len(skill_dirs)} skill(s)")
        return 1

    print(f"\n{len(skill_dirs)} skill(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
