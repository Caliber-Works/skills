---
name: md-whispr
description: Read any text file out loud with a local neural voice — markdown docs, READMEs, PRDs, specs, code, config, PDFs. Use when the user says "read this to me", "read me X.md", "whispr this", "narrate this doc", "listen to this file", "read the CLAUDE.md out loud", "audiobook this", "turn this into audio", or invokes /md-whispr. Also use for playback control mid-read (pause, resume, skip, go back, stop, where am I), for rendering a document to an audio file, and for one-command setup ("/md-whispr setup", "set up md-whispr", "install the voice backend", "the TTS server isn't running") which installs mlx-audio into a venv, picks a free port, starts the server, and verifies audio end to end. Runs entirely locally on Apple Silicon via mlx-audio + Kokoro; handles very long documents efficiently by streaming chunk-by-chunk.
license: MIT
---

# md-whispr

Turn any readable file into speech, locally, with playback that starts in under a
second no matter how long the document is.

## The one thing to know first

**LM Studio cannot do text-to-speech.** Its OpenAI-compatible API exposes only
`/v1/models`, `/v1/responses`, `/v1/chat/completions`, `/v1/embeddings`, and
`/v1/completions` — there is no `/v1/audio/speech`. GGUF TTS models like Orpheus
will *load* in LM Studio but the audio endpoints return errors. It is an open
feature request, not a shipped feature.

This skill therefore drives **mlx-audio**, which is Apple-MLX-native (built for
M-series silicon) and *does* implement OpenAI-compatible `/v1/audio/speech`. If
LM Studio ever ships a TTS endpoint, point `MD_WHISPR_TTS_URL` at it and nothing
else changes. See `reference/backends.md`.

## How it works

```
file → smart strip → sentence chunks → [synth thread, 3 ahead] → [playback loop]
                                              ↓
                                       content-hash cache
```

The synth thread stays ahead of the playhead, so audio begins after the **first**
chunk renders (~0.5s on an M4), not after the whole document. Every chunk is
cached by content hash: re-reading a doc is instant, and editing a doc only
re-renders the sections that changed. A 30-minute doc costs the same time-to-first-word
as a 30-second one.

## Setup — `/md-whispr setup`

When the user asks to set up, install, or fix the backend — or when `doctor`
reports the server is down — run the setup script. It is idempotent, so running
it when everything already works is a safe no-op.

```bash
bash {skill_dir}/scripts/setup.sh
```

Useful variants:

| Command | When |
|---|---|
| `bash {skill_dir}/scripts/setup.sh --check` | Report state, change nothing. Faster than full setup for a health question. |
| `bash {skill_dir}/scripts/setup.sh --venv ~/main-venv` | User named a specific virtualenv |
| `bash {skill_dir}/scripts/setup.sh --port 8080` | User wants a specific port |
| `bash {skill_dir}/scripts/setup.sh --quiet` | Skip the audible test clip |
| `bash {skill_dir}/scripts/setup.sh --stop` | Shut the backend down |

**Reading the output.** Every stage prints `STAGE <name> <ok|warn|fail> <detail>`
and the final line is always:

```
RESULT ok port=8001 url=http://127.0.0.1:8001/v1/audio/speech
```

Parse that last line rather than the prose. If `RESULT fail`, the failing
`STAGE` line names the cause — report that specific line to the user, don't
paraphrase it into something vaguer.

**What it does:** verifies macOS/arm64 and `afplay` → resolves or creates the
venv (flag > `MD_WHISPR_VENV` > an existing `~/main-venv` > active venv >
`~/.md-whispr/venv`) → installs
`mlx-audio[server]` + `misaki` → finds a free port, naming the process holding
any busy one → starts the server (auto-detecting console-script vs `python -m`)
→ polls health for 60s → warms Kokoro and reports cold vs warm latency → writes
`~/.md-whispr/env`, installs `~/bin/md-whispr-server`, appends the env source
line to the shell rc → plays a test clip.

**Expect the first run to be slow.** Stage `warm` downloads Kokoro (~330MB).
Tell the user that up front so a two-minute pause doesn't look like a hang.

**Port drift matters.** If 8000 was busy, the server lands elsewhere and the URL
lives in `~/.md-whispr/env`. In a shell that hasn't sourced it, pass the URL
explicitly: `--url $(grep TTS_URL ~/.md-whispr/env | cut -d= -f2)`.

## Workflow

### 1. Preflight (always, before the first read of a session)

```bash
python3 {skill_dir}/scripts/md_whispr.py doctor
```

If it reports NOT READY, offer to run setup rather than falling back silently —
never let the user think they're hearing Kokoro when they're hearing the system
`say` voice.

### 2. Preview before committing to a long read

For anything over ~10 minutes, run the dry run first and show the user the plan.
It synthesizes nothing.

```bash
python3 {skill_dir}/scripts/md_whispr.py read PATH --dry-run
```

This prints how much was stripped, the chunk count, the estimated duration, and
the first chunks of narration — so the user can catch a bad strip before
listening to 30 minutes of it.

### 3. Read

```bash
python3 {skill_dir}/scripts/md_whispr.py read PATH
```

Returns immediately; playback continues in a detached process. **Never use
`--foreground`** in an agent context — it blocks until the document ends.

Useful flags:

| Flag | Use it when |
|---|---|
| `--resume` | Continuing a doc the user stopped partway through |
| `--voice am_adam` | User wants a different voice (`voices` lists them) |
| `--speed 1.4` | User wants it faster; 1.15 is the default, 1.3–1.5 is a common skim speed |
| `--code skip` | Doc is code-heavy and the user only wants prose |
| `--code full` | User explicitly wants code read aloud line by line |
| `--start N` | Jumping to a known chunk |

### 4. Control mid-read

```bash
python3 {skill_dir}/scripts/md_whispr.py pause
python3 {skill_dir}/scripts/md_whispr.py resume
python3 {skill_dir}/scripts/md_whispr.py skip     # next chunk
python3 {skill_dir}/scripts/md_whispr.py back     # previous chunk
python3 {skill_dir}/scripts/md_whispr.py goto 40  # jump to chunk 40
python3 {skill_dir}/scripts/md_whispr.py stop     # stop and bookmark
python3 {skill_dir}/scripts/md_whispr.py status   # progress + current section
```

`pause` uses SIGSTOP, so it freezes mid-word and resumes exactly there — it does
not restart the chunk. `stop` writes a bookmark; `--resume` picks it up.

`status` reports the current **section name**, which is how you answer "where are
we?" or "what's it reading now?" without guessing.

### 5. Render to a file instead

When the user wants to listen later, on a phone, or without a live process:

```bash
python3 {skill_dir}/scripts/md_whispr.py render PATH -o brief.m4a
```

Requires `ffmpeg` to stitch chunks (`brew install ffmpeg`).

## Judgment calls

**Reading a whole directory.** The tool takes one file. If the user asks for a
folder, list the candidate files, propose an order, and read them in sequence —
starting each next file only after `status` reports `done`.

**Very long documents.** Over ~45 minutes, offer `render` instead of live
playback, or suggest a specific section via `--start`. Say the estimate out loud
before starting: "That's about 34 minutes — want the whole thing, or just the
deployment section?"

**Code-heavy repos docs.** Default `--code announce` says "shell block, 3 lines,
skipped" rather than reading the commands. That is almost always what someone
walking around wants. Only use `--code full` when explicitly asked.

**Non-markdown files.** Source files are narrated as comments + declarations
only (~85% of the file is skipped). Config files read the first 60 lines. PDFs
need `pdftotext` (`brew install poppler`) or `pypdf`. Binary files are rejected
with a clear message.

**Don't summarize unless asked.** The user asked to hear *their* document. If
they want a condensed version, they'll say so — then summarize to a temp `.md`
and read that instead.

## What the smart strip does

Frontmatter, HTML comments, badges, and images are dropped. Links keep their text
and lose the URL. Fenced code becomes an announcement. Tables flatten to spoken
rows (`Layer: E2E; Command: npm run test:e2e`). Headings become "Section: X" with
a breath before them. Emoji, the section-sign glyph, and smart quotes are
normalized away. `e.g.` becomes "for example". Checkboxes become "To do" / "Done".

Chunks never split a sentence and target ~380 characters — Kokoro's comfortable
window for natural prosody.

## Reference

- `reference/backends.md` — why not LM Studio, how to swap backends, env vars
- `reference/voices.md` — Kokoro voice presets and language codes
- `reference/troubleshooting.md` — every failure mode and its fix
