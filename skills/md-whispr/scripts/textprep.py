"""
textprep.py — turn any readable file into speech-ready chunks.

Two jobs:
  1. extract()  : file path -> plain narration text (markdown-aware "smart strip")
  2. chunk()    : narration text -> list of Chunk(text, label, is_section)

Design notes
------------
* Smart strip is tuned for LISTENING to engineering docs while walking, not for
  fidelity. Code blocks are announced, not read. URLs are dropped. Tables are
  flattened to prose. Headings become spoken section markers.
* Chunking never splits a sentence. Chunks target ~380 chars, which is Kokoro's
  comfortable window: long enough to keep prosody natural, short enough that the
  first chunk starts playing in well under a second on an M-series chip.
* Pure stdlib. No third-party imports.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Chunk model
# --------------------------------------------------------------------------


@dataclass
class Chunk:
    """One synthesis + playback unit."""

    text: str
    label: str = ""          # human-facing hint, e.g. "Deployment workflows"
    is_section: bool = True  # True -> insert a short breath before playing
    index: int = 0

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.text)


@dataclass
class Document:
    path: str
    title: str
    narration: str
    chunks: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# File type routing
# --------------------------------------------------------------------------

MARKDOWN_EXT = {".md", ".mdx", ".markdown", ".mdown", ".qmd"}
PLAIN_EXT = {".txt", ".text", ".rst", ".adoc", ".org", ".log", ""}
CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".sh", ".bash", ".zsh",
    ".sql", ".css", ".scss", ".html", ".vue", ".svelte", ".php", ".lua",
}
DATA_EXT = {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".env", ".csv", ".tsv"}

BINARY_SNIFF_BYTES = 4096


class UnreadableFile(Exception):
    pass


def looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(BINARY_SNIFF_BYTES)
    except OSError as exc:
        raise UnreadableFile(str(exc)) from exc
    if b"\x00" in head:
        return True
    # Heuristic: >30% non-text bytes
    if not head:
        return False
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return (printable / len(head)) < 0.70


def read_source(path: str) -> tuple[str, str]:
    """Return (raw_text, kind). Kind ∈ markdown|plain|code|data|pdf."""
    if not os.path.exists(path):
        raise UnreadableFile(f"No such file: {path}")
    if os.path.isdir(path):
        raise UnreadableFile(f"Path is a directory, not a file: {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return _read_pdf(path), "pdf"

    if looks_binary(path):
        raise UnreadableFile(
            f"{os.path.basename(path)} looks like a binary file. "
            "md-whispr reads text: markdown, plain text, code, config, and PDF."
        )

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    if ext in MARKDOWN_EXT:
        kind = "markdown"
    elif ext in CODE_EXT:
        kind = "code"
    elif ext in DATA_EXT:
        kind = "data"
    else:
        kind = "plain"
    return raw, kind


def _read_pdf(path: str) -> str:
    """Best-effort PDF text via pdftotext, then pypdf. Neither is required."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(path)
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise UnreadableFile(
            "Could not extract text from the PDF. Install one of: "
            "`brew install poppler` (pdftotext) or `pip install pypdf`."
        ) from exc


# --------------------------------------------------------------------------
# Smart strip
# --------------------------------------------------------------------------

RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
RE_TOML_FRONTMATTER = re.compile(r"\A\+\+\+\n.*?\n\+\+\+\n", re.DOTALL)
RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
RE_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})([^\n]*)\n(.*?)(?:^[ \t]*\2[ \t]*$|\Z)",
                      re.DOTALL | re.MULTILINE)
RE_INDENT_CODE = re.compile(r"^(?: {4}|\t)\S.*$", re.MULTILINE)
RE_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
RE_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
RE_LINK_DEF = re.compile(r"^\s*\[[^\]]+\]:\s+\S+.*$", re.MULTILINE)
RE_BARE_URL = re.compile(r"<?\b(?:https?://|www\.)[^\s>)\]]+>?")
RE_AUTOLINK = re.compile(r"<(https?://[^>]+)>")
RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
RE_SETEXT = re.compile(r"^(?!\s*$)(.+)\n(=|-){3,}\s*$", re.MULTILINE)
RE_HR = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.MULTILINE)
RE_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
RE_LIST_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
RE_LIST_NUM = re.compile(r"^(\s*)(\d+)[.)]\s+", re.MULTILINE)
RE_TASK = re.compile(r"^(\s*)(?:[-*+]\s+)?\[( |x|X)\]\s+", re.MULTILINE)
RE_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?])")
RE_DOUBLE_PUNCT = re.compile(r"([.!?])\1{1,}")
RE_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|_|~~)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
RE_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$")
RE_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
RE_FOOTNOTE = re.compile(r"\[\^[^\]]+\]")
RE_MULTI_BLANK = re.compile(r"\n{3,}")
RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")

# Things that read badly out loud.
NOISE_LINE = re.compile(
    r"^\s*(?:\[!\[|<img|<br|<p align|<div align|<!\[)",
    re.IGNORECASE,
)

ABBREV_SPOKEN = {
    r"\be\.g\.": "for example,",
    r"\bi\.e\.": "that is,",
    r"\betc\.": "et cetera",
    r"\bvs\.?\b": "versus",
    r"\bw/\b": "with",
    r"\bw/o\b": "without",
    r"\baka\b": "also known as",
    r"\bTODO\b": "to do",
    r"\bFIXME\b": "fix me",
    r"\bWIP\b": "work in progress",
    r"\bAKA\b": "also known as",
    r"\bIIRC\b": "if I recall correctly",
    r"\bASAP\b": "as soon as possible",
    r"->": " to ",
    r"=>": " gives ",
    r"&&": " and ",
    r"\|\|": " or ",
    r"\s&\s": " and ",
    r"\+\+": " plus plus ",
}

# The section-sign glyph is stripped, never spoken. Written as an escape so the
# character itself never appears literally in this repo.
BANNED_GLYPHS = "\u00a7"

# Punctuation that TTS engines either skip or mispronounce.
PUNCT_MAP = {
    "·": ", ",   # middle dot, common in nav breadcrumbs
    "•": ", ",   # bullet
    "–": " - ",  # en dash
    "—": " - ",  # em dash
    "…": "...",
    "“": '"', "”": '"',
    "‘": "'", "’": "'",
    " ": " ",
    "→": " to ",
    "←": " from ",
    "✓": "check", "✔": "check",
    "✗": "cross", "✘": "cross",
}


def _normalize_punct(text: str) -> str:
    for src, dst in PUNCT_MAP.items():
        text = text.replace(src, dst)
    return text


def _strip_emoji(text: str) -> str:
    out = []
    for ch in text:
        if ch in BANNED_GLYPHS:
            continue
        cat = unicodedata.category(ch)
        # So (other symbols) covers most emoji + dingbats; Cf covers ZWJ/VS.
        if cat in ("So", "Cf", "Cs", "Co"):
            continue
        out.append(ch)
    return "".join(out)


def _describe_code(lang: str, body: str, mode: str) -> str:
    lines = [ln for ln in body.splitlines() if ln.strip()]
    n = len(lines)
    lang = (lang or "").strip().split()[0] if lang.strip() else ""
    name = {"": "code", "sh": "shell", "bash": "shell", "zsh": "shell",
            "js": "JavaScript", "ts": "TypeScript", "tsx": "TypeScript",
            "jsx": "JavaScript", "py": "Python", "sql": "SQL",
            "jsonc": "JSON", "yml": "YAML"}.get(lang.lower(), lang or "code")

    if mode == "skip":
        return ""
    if mode == "full":
        return f"{name} block. {body}\nEnd of {name} block."
    plural = "line" if n == 1 else "lines"
    return f"[{name} block, {n} {plural}, skipped.]"


def _flatten_tables(text: str, max_rows: int = 12) -> str:
    """Turn pipe tables into short spoken rows: 'header: value; header: value.'"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        is_row = line.count("|") >= 2 and line.strip().startswith(("|", " |")) or (
            line.count("|") >= 2 and not line.strip().startswith("|") and "|" in line
        )
        if not is_row:
            out.append(line)
            i += 1
            continue

        block = []
        while i < len(lines) and lines[i].count("|") >= 2:
            block.append(lines[i])
            i += 1
        if len(block) < 2:
            out.extend(block)
            continue

        def cells(row: str) -> list[str]:
            return [c.strip() for c in row.strip().strip("|").split("|")]

        headers = cells(block[0])
        body = [r for r in block[1:] if not RE_TABLE_SEP.match(r)]
        out.append(f"Table with {len(body)} rows.")
        for row in body[:max_rows]:
            vals = cells(row)
            pairs = [
                f"{h}: {v}"
                for h, v in zip(headers, vals)
                if v and v not in {"-", "--", "n/a", "N/A"}
            ]
            if pairs:
                out.append("; ".join(pairs) + ".")
        if len(body) > max_rows:
            out.append(f"...and {len(body) - max_rows} more rows.")
        out.append("End of table.")
    return "\n".join(out)


def smart_strip(raw: str, *, code_mode: str = "announce", speak_headings: bool = True) -> str:
    """Markdown (and markdown-ish) -> narration text."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    text = RE_FRONTMATTER.sub("", text)
    text = RE_TOML_FRONTMATTER.sub("", text)
    text = RE_HTML_COMMENT.sub(" ", text)

    # Fenced code first, so its contents escape every later rule.
    def _fence_sub(m: re.Match) -> str:
        return "\n" + _describe_code(m.group(3), m.group(4), code_mode) + "\n"

    text = RE_FENCE.sub(_fence_sub, text)

    text = RE_LINK_DEF.sub("", text)
    text = RE_IMAGE.sub(lambda m: (f"Image: {m.group(1)}." if m.group(1).strip() else ""), text)
    text = RE_AUTOLINK.sub(" link ", text)
    text = RE_LINK.sub(r"\1", text)
    text = RE_REF_LINK.sub(r"\1", text)
    text = RE_BARE_URL.sub(" link ", text)
    text = RE_FOOTNOTE.sub("", text)

    text = _flatten_tables(text)

    # Headings -> spoken markers with a sentence terminator so chunking sees them.
    def _heading_sub(m: re.Match) -> str:
        level, title = len(m.group(1)), m.group(2).strip().rstrip(":")
        if not title:
            return ""
        if not speak_headings:
            return f"\n\n{title}.\n"
        lead = "Section" if level <= 2 else "Subsection"
        return f"\n\n{lead}: {title}.\n"

    text = RE_HEADING.sub(_heading_sub, text)
    text = RE_SETEXT.sub(lambda m: f"\n\nSection: {m.group(1).strip()}.\n", text)

    text = RE_HR.sub("\n", text)
    text = RE_BLOCKQUOTE.sub("", text)
    text = RE_TASK.sub(lambda m: f"{m.group(1)}" + ("Done. " if m.group(2) in "xX" else "To do. "), text)
    text = RE_LIST_BULLET.sub(r"\1", text)
    text = RE_LIST_NUM.sub(r"\1\2. ", text)
    text = RE_INLINE_CODE.sub(r"\1", text)
    text = RE_EMPHASIS.sub(r"\2", text)
    text = RE_HTML_TAG.sub(" ", text)
    text = html.unescape(text)

    lines = [ln for ln in text.splitlines() if not NOISE_LINE.match(ln)]
    text = "\n".join(lines)

    text = _normalize_punct(text)
    text = _strip_emoji(text)
    for pat, rep in ABBREV_SPOKEN.items():
        text = re.sub(pat, rep, text)

    # A list item without terminal punctuation runs into the next one. Fix that,
    # but never double up on a line that already ends in punctuation or a bracket.
    text = "\n".join(
        (ln.rstrip() + ".")
        if (ln.strip() and ln.rstrip()[-1] not in ".!?:;,)]}\"'")
        else ln
        for ln in text.splitlines()
    )

    text = RE_SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = RE_DOUBLE_PUNCT.sub(r"\1", text)
    text = RE_MULTI_SPACE.sub(" ", text)
    text = re.sub(r"^[ \t]+$", "", text, flags=re.MULTILINE)
    text = RE_MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def prep_code(raw: str, path: str) -> str:
    """Source files: read comments and declarations, skip dense bodies."""
    name = os.path.basename(path)
    lines = raw.splitlines()
    out = [f"Source file {name}, {len(lines)} lines."]
    decl = re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?"
        r"(?:def|class|function|const|let|var|type|interface|enum|struct|impl|fn|func|public|private)\b"
    )
    comment = re.compile(r"^\s*(?://|#|\*|/\*\*?)\s?(.*)$")
    divider = re.compile(r"^[\s\-=_*#~/]+$")
    for ln in lines:
        if decl.match(ln):
            out.append(ln.strip().rstrip("{(:,").strip() + ".")
            continue
        m = comment.match(ln)
        if not m:
            continue
        body = m.group(1).strip()
        # Skip ASCII-art dividers and stub comments — they read as noise.
        if divider.match(body) or len(re.sub(r"[^A-Za-z0-9]", "", body)) < 4:
            continue
        out.append(body if body.endswith((".", "!", "?", ":")) else body + ".")
    if len(out) == 1:
        out.append("No comments or declarations found to narrate.")
    return "\n".join(out)


def prep_data(raw: str, path: str) -> str:
    name = os.path.basename(path)
    lines = raw.splitlines()
    head = "\n".join(lines[:60])
    tail = f"\n...and {len(lines) - 60} more lines." if len(lines) > 60 else ""
    return f"Config file {name}, {len(lines)} lines.\n{head}{tail}"


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

# Don't break on these trailing tokens.
_ABBREV_GUARD = (
    r"(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bSt)(?<!\bvs)(?<!\bNo)(?<!\bFig)"
    r"(?<!\bInc)(?<!\bLtd)(?<!\bJr)(?<!\bSr)(?<!\bcf)(?<!\bal)"
)
RE_SENTENCE = re.compile(rf"{_ABBREV_GUARD}(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")

TARGET_CHARS = 380
MAX_CHARS = 620
MIN_CHARS = 60


def split_sentences(paragraph: str) -> list[str]:
    parts = [p.strip() for p in RE_SENTENCE.split(paragraph) if p.strip()]
    out: list[str] = []
    for part in parts:
        # Hard-wrap anything monstrous (minified data, giant one-liners).
        while len(part) > MAX_CHARS:
            cut = part.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MIN_CHARS else MAX_CHARS
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            out.append(part)
    return out


def chunk(narration: str, target: int = TARGET_CHARS) -> list[Chunk]:
    """Group sentences into ~target-sized chunks, never splitting a sentence."""
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    pending_label = ""
    section_start = True

    def flush(label: str, is_section: bool) -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        text = " ".join(buf).strip()
        if text:
            chunks.append(Chunk(text=text, label=label, is_section=is_section,
                                index=len(chunks)))
        buf, buf_len = [], 0

    for para in re.split(r"\n\s*\n", narration):
        para = " ".join(para.split())
        if not para:
            continue

        heading = re.match(r"^(?:Section|Subsection): (.+?)\.$", para)
        if heading:
            flush(pending_label, section_start)
            pending_label = heading.group(1)[:60]
            section_start = True
            buf, buf_len = [para], len(para)
            continue

        for sent in split_sentences(para):
            if buf_len and buf_len + len(sent) + 1 > target:
                flush(pending_label, section_start)
                section_start = False
            buf.append(sent)
            buf_len += len(sent) + 1
            if buf_len >= target:
                flush(pending_label, section_start)
                section_start = False

    flush(pending_label, section_start)
    return chunks


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def build_document(
    path: str,
    *,
    code_mode: str = "announce",
    target: int = TARGET_CHARS,
    speak_title: bool = True,
) -> Document:
    raw, kind = read_source(path)
    name = os.path.basename(path)

    if kind in ("markdown", "pdf", "plain"):
        narration = smart_strip(raw, code_mode=code_mode)
    elif kind == "code":
        narration = prep_code(raw, path)
    else:
        narration = prep_data(raw, path)

    title = name
    m = re.search(r"^\s*Section: (.+?)\.\s*$", narration, re.MULTILINE)
    if m:
        title = m.group(1).strip()

    if speak_title:
        # "CLAUDE.md" spoken as "CLAUDE. m d" is grating; announce the title instead.
        spoken = title if m else re.sub(r"[-_]+", " ", os.path.splitext(name)[0])
        if m and narration.lstrip().startswith(m.group(0).strip()):
            # Don't say "Reading X. Section: X." — drop the duplicate marker.
            narration = narration.lstrip()[len(m.group(0).strip()):].lstrip()
        narration = f"Reading {spoken}.\n\n{narration}"

    chunks = chunk(narration, target=target)
    words = len(narration.split())
    return Document(
        path=os.path.abspath(path),
        title=title,
        narration=narration,
        chunks=chunks,
        stats={
            "kind": kind,
            "raw_chars": len(raw),
            "narration_chars": len(narration),
            "words": words,
            "chunks": len(chunks),
            # ~165 wpm is Kokoro at speed 1.0.
            "est_minutes": round(words / 165.0, 1),
            "reduction_pct": round(100 * (1 - len(narration) / max(len(raw), 1))),
        },
    )
