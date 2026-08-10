# Caliber Works Skills

Open-source [agent skills](https://agentskills.io) from [Caliber Works](https://caliberworks.co) — the
team behind [justrepl.com](https://justrepl.com), Espas, and agentctl. These are the skills we
actually use, extracted and made installable.

Install them into any skills-aware coding agent — Claude Code, Codex, Cursor, Amp, opencode, Gemini
CLI, Copilot, and [70+ others](https://github.com/vercel-labs/skills#supported-agents):

```bash
npx skills add Caliber-Works/skills
```

## Skills

| Skill | What it does | Requires |
| --- | --- | --- |
| [md-whispr](skills/md-whispr) | Reads any text file out loud with a local neural voice. Markdown, code, config, PDFs. Playback starts in under a second regardless of document length. | macOS on Apple Silicon |

## md-whispr

Point it at a file and listen. It strips the parts nobody wants read aloud (frontmatter, badges,
URLs, fenced code), chunks the rest on sentence boundaries, and streams synthesis a few chunks ahead
of the playhead — so a 30-minute document starts speaking as fast as a 30-second one. Every chunk is
cached by content hash, so re-reading is instant and editing a doc only re-renders what changed.

Everything runs locally on [mlx-audio](https://github.com/Blaizzy/mlx-audio) + Kokoro. No API keys,
no audio leaves the machine.

```
you: read me the PRD
agent: That's about 34 minutes — the whole thing, or just the deployment section?
you: skip ahead / pause / where are we?
```

**Install and set up:**

```bash
npx skills add Caliber-Works/skills --skill md-whispr -g
```

Then ask your agent to `set up md-whispr`, or run the setup directly:

```bash
bash ~/.claude/skills/md-whispr/scripts/setup.sh
```

Setup is idempotent and prints a machine-readable `STAGE`/`RESULT` line per step, so an agent can
run it and parse the outcome. First run downloads the Kokoro model (~330 MB) — expect a couple of
minutes.

**Requirements**

- macOS on Apple Silicon (mlx-audio is MLX-native; there is no CUDA or CPU fallback)
- Python 3.10+
- `afplay` (ships with macOS)
- Optional: `ffmpeg` for `render`, `poppler` for PDFs

**What setup touches** — all of it reversible:

- `~/.md-whispr/` — state, cache, logs, and the virtualenv if it creates one
- `~/bin/md-whispr-server` — a `start|stop|status` launcher
- One sourcing line appended to your shell rc, so `MD_WHISPR_TTS_URL` is set in new shells

To undo: `bash scripts/setup.sh --stop`, then remove `~/.md-whispr`, `~/bin/md-whispr-server`, and
the shell rc line.

**Note on LM Studio:** it cannot serve TTS. Its OpenAI-compatible API has no `/v1/audio/speech`
endpoint, so GGUF voice models load but produce errors. That's why this skill drives mlx-audio.
Details in [reference/backends.md](skills/md-whispr/reference/backends.md).

## Installing

```bash
# everything, into every agent the CLI detects
npx skills add Caliber-Works/skills

# one skill, globally, into Claude Code only
npx skills add Caliber-Works/skills --skill md-whispr -g -a claude-code

# see what's in here without installing
npx skills add Caliber-Works/skills --list
```

Project scope (the default) installs into `./.claude/skills/` and friends so the skill is committed
with your repo. `-g` installs to your home directory instead, available everywhere.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Every skill here has to earn its
place by being one we use ourselves.

## License

MIT © Caliber Works. See [LICENSE](LICENSE).
