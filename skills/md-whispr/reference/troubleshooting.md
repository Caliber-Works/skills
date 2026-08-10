# Troubleshooting

Start with `md_whispr.py doctor`. It checks playback, server reachability, model
availability, and cache state in one pass.

## Nothing plays, no error

`status` will tell you which of these it is.

- **`status` says `dead`** — the worker crashed. Read `~/.md-whispr/whispr.log`;
  the traceback is at the end.
- **`status` says `paused`** — a `pause` was never resumed. Run `resume`.
- **`status` says `playing` but you hear nothing** — check system volume and
  output device. Verify audio works at all: `afplay /System/Library/Sounds/Glass.aiff`.
- **`status` says `idle`** — the read never started. Re-run without
  `--dry-run`.

## "No TTS backend available"

The server isn't running and `say` isn't on PATH (the latter should be impossible
on macOS). Start the server:

```bash
mlx_audio.server --port 8000
```

Confirm it's up: `curl http://localhost:8000/v1/models`

## "TTS server returned HTTP 500"

Usually a model/voice/lang mismatch. Check that:

- the voice prefix matches `--lang` (`bf_emma` needs `--lang b`)
- `misaki` is installed — Kokoro needs it for text processing in every language
- the language pack is installed for `ja` / `zh`

The server's own log has the real traceback; md-whispr only sees the HTTP status.

## First read is slow, later ones are instant

Expected. The first request downloads and loads Kokoro (~330MB). Keep
`mlx_audio.server` running between sessions to avoid paying it repeatedly.

## It's reading garbage — URLs, backticks, table pipes

Run `--dry-run` and look at the actual narration. If the strip is wrong for a
particular file, the fastest fix is `--code skip` (drops code entirely) or
copying the prose you want into a temp `.md` and reading that.

The strip is tuned for markdown. A `.txt` file full of ASCII tables or a
minified `.json` will narrate poorly by nature — that's a content problem, not a
bug.

## Pronunciation is wrong on names or acronyms

Kokoro has no lexicon override. Workarounds:

- Spell it phonetically in the source doc where it matters
- Accept it — acronym mangling is the normal cost of local TTS
- For a doc you'll replay often, `render` it once and edit the audio

## Playback stutters or gaps between chunks

The synth thread fell behind the playhead. Causes:

- Something else is saturating the GPU (another MLX job, a heavy build)
- `--speed` is high enough that playback outruns synthesis
- Chunks are too small — raise `--chunk-chars 500` so each render covers more
  playback time

## Two readers fighting over the speakers

`read` stops any existing session before starting. If a worker was force-killed
and left a stale `state.json`, clear it:

```bash
rm ~/.md-whispr/state.json ~/.md-whispr/control
```

## Cache is eating disk

It self-prunes at 512MB by mtime. Change the ceiling with
`MD_WHISPR_CACHE_MB=2048`, or wipe it:

```bash
rm -rf ~/.md-whispr/cache
```

Wiping only costs re-render time; nothing is lost.

## `render` fails

Needs ffmpeg to concatenate chunks: `brew install ffmpeg`.

## PDFs don't read

Install a text extractor: `brew install poppler` (gives `pdftotext`) or
`pip install pypdf`. Scanned PDFs with no text layer need OCR first — md-whispr
won't do that.

## Resume jumped to the wrong place

Bookmarks are keyed by absolute path and chunk index. Editing the document
shifts chunk boundaries, so a bookmark taken before an edit lands approximately,
not exactly. Use `goto N` after checking `--dry-run` output if precision matters.
