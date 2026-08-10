# Contributing

## Repo layout

One directory per skill under `skills/`:

```
skills/
  md-whispr/
    SKILL.md          # required — frontmatter + agent instructions
    LICENSE.txt
    scripts/          # anything the skill executes
    reference/        # loaded on demand, not up front
```

The `skills/` directory is one of the paths the [`skills`
CLI](https://github.com/vercel-labs/skills) discovers automatically, so a new directory here is
installable the moment it lands on `main`.

## SKILL.md rules

Frontmatter must have `name` and `description`.

- `name` — lowercase, hyphens, no leading/trailing hyphen, no `--`, and it must match the directory
  name.
- `description` — the whole trigger surface. This is the only text most agents see when deciding
  whether to load the skill, so it has to name the situations *and* the phrasings that should
  activate it. Keep it under 1024 characters; Claude Code rejects longer.

Keep `SKILL.md` itself focused on what the agent does. Push anything reference-shaped — voice
tables, troubleshooting matrices, backend notes — into `reference/` so it costs nothing until it's
needed.

## Before opening a PR

```bash
python3 scripts/validate.py
```

This checks frontmatter validity, name/directory agreement, description length, that referenced
`scripts/` and `reference/` paths exist, and that every shell and Python script parses.

Then confirm the CLI still sees the skill:

```bash
npx skills add . --list
```

## Bar for a new skill

We only ship skills we run ourselves. A skill belongs here if it works end to end on a clean
machine, fails with a message that says what to do next, and doesn't quietly degrade — a
text-to-speech skill that falls back to the system voice without saying so is worse than one that
errors.
