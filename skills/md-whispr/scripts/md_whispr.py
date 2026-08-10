#!/usr/bin/env python3
"""
md-whispr — read any text file out loud, efficiently, on Apple Silicon.

Pipeline
--------
    file -> smart strip -> sentence chunks -> [synth thread] -> [playback loop]

The synth thread runs ahead of the playback loop, so chunk N+1 is already
rendered by the time chunk N finishes. Playback starts after the FIRST chunk
(~0.5s on an M4), not after the whole document. Every rendered chunk is cached
by content hash, so re-reading a doc is instant and edits only re-render the
sections that changed.

Backend: any OpenAI-compatible POST /v1/audio/speech. Default is mlx-audio
(Apple MLX-native, built for M-series). LM Studio is NOT supported as a TTS
backend — it has no audio endpoint. See reference/backends.md.

Usage
-----
    md_whispr.py doctor
    md_whispr.py read PATH [options]
    md_whispr.py pause | resume | skip | back | stop | status
    md_whispr.py render PATH -o out.mp3
    md_whispr.py voices
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textprep import UnreadableFile, build_document  # noqa: E402

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("MD_WHISPR_HOME", os.path.join(HOME, ".md-whispr"))
CACHE_DIR = os.path.join(STATE_DIR, "cache")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
CONTROL_FILE = os.path.join(STATE_DIR, "control")
BOOKMARKS_FILE = os.path.join(STATE_DIR, "bookmarks.json")
LOG_FILE = os.path.join(STATE_DIR, "whispr.log")

DEFAULT_URL = os.environ.get("MD_WHISPR_TTS_URL", "http://localhost:8000/v1/audio/speech")
DEFAULT_MODEL = os.environ.get("MD_WHISPR_MODEL", "mlx-community/Kokoro-82M-bf16")
DEFAULT_VOICE = os.environ.get("MD_WHISPR_VOICE", "af_heart")
DEFAULT_SPEED = float(os.environ.get("MD_WHISPR_SPEED", "1.15"))
DEFAULT_LANG = os.environ.get("MD_WHISPR_LANG", "a")
DEFAULT_FORMAT = "wav"          # wav decodes instantly; mp3 for render output
LOOKAHEAD = 3                   # chunks rendered ahead of the playhead
CACHE_MAX_MB = int(os.environ.get("MD_WHISPR_CACHE_MB", "512"))
SYNTH_TIMEOUT = 180


def ensure_dirs() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# Small IO helpers
# --------------------------------------------------------------------------


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    ensure_dirs()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def log(msg: str) -> None:
    ensure_dirs()
    stamp = time.strftime("%H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# --------------------------------------------------------------------------
# TTS backend
# --------------------------------------------------------------------------


class TTSError(Exception):
    pass


class SpeechBackend:
    """OpenAI-compatible /v1/audio/speech client with a macOS `say` fallback."""

    def __init__(self, url: str, model: str, voice: str, speed: float,
                 lang: str, engine: str = "auto", fmt: str = DEFAULT_FORMAT):
        self.url = url
        self.model = model
        self.voice = voice
        self.speed = speed
        self.lang = lang
        self.fmt = fmt
        self.engine = engine if engine != "auto" else ("mlx" if self.probe(url) else "say")
        if self.engine == "say" and shutil.which("say") is None:
            raise TTSError(
                f"No TTS backend available. The server at {url} is not reachable and "
                "macOS `say` is not on PATH.\n"
                "  Start the server:  mlx_audio.server --port 8000\n"
                "  Install it:        pip install 'mlx-audio[server]' misaki"
            )
        if self.engine == "say":
            self.fmt = "aiff"

    # -- health ------------------------------------------------------------

    @staticmethod
    def probe(url: str, timeout: float = 2.0) -> bool:
        base = url.rsplit("/v1/", 1)[0]
        for candidate in (f"{base}/v1/models", f"{base}/health", base):
            try:
                with urllib.request.urlopen(candidate, timeout=timeout) as resp:
                    if resp.status < 500:
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # -- synthesis ---------------------------------------------------------

    def cache_key(self, text: str) -> str:
        sig = f"{self.engine}|{self.model}|{self.voice}|{self.speed}|{self.lang}|{self.fmt}|{text}"
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:32]

    def cache_path(self, text: str) -> str:
        ext = "aiff" if self.engine == "say" else self.fmt
        return os.path.join(CACHE_DIR, f"{self.cache_key(text)}.{ext}")

    def synth(self, text: str) -> str:
        """Render `text` to an audio file, returning its path. Cached."""
        out = self.cache_path(text)
        if os.path.exists(out) and os.path.getsize(out) > 256:
            return out
        tmp = f"{out}.part"
        if self.engine == "say":
            self._synth_say(text, tmp)
        else:
            self._synth_http(text, tmp)
        os.replace(tmp, out)
        return out

    def _synth_http(self, text: str, dest: str) -> None:
        payload = json.dumps({
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "speed": self.speed,
            "lang_code": self.lang,
            "response_format": self.fmt,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer not-needed"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=SYNTH_TIMEOUT) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise TTSError(f"TTS server returned HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"TTS server unreachable at {self.url}: {exc}") from exc
        if len(data) < 256:
            raise TTSError(f"TTS server returned {len(data)} bytes — likely an error payload.")
        with open(dest, "wb") as fh:
            fh.write(data)

    def _synth_say(self, text: str, dest: str) -> None:
        voice = self.voice if not re.match(r"^[a-z]{2}_", self.voice) else "Samantha"
        rate = str(int(180 * self.speed))
        cmd = ["say", "-v", voice, "-r", rate, "-o", dest, "--data-format=LEF32@22050", text]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise TTSError("macOS `say` is not available on this system.") from exc
        if proc.returncode != 0:
            raise TTSError(f"`say` failed: {proc.stderr.strip()[:300]}")


def prune_cache(limit_mb: int = CACHE_MAX_MB) -> None:
    """LRU-ish prune by mtime once the cache exceeds the limit."""
    ensure_dirs()
    files = []
    total = 0
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, path))
        total += st.st_size
    limit = limit_mb * 1024 * 1024
    if total <= limit:
        return
    for _, size, path in sorted(files):
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass
        if total <= limit * 0.8:
            break


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------


class Player:
    """Wraps afplay. Pause is SIGSTOP, not a restart — position is preserved."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.paused = False

    def play(self, path: str, rate: float = 1.0) -> None:
        cmd = ["afplay", path]
        if abs(rate - 1.0) > 0.01:
            cmd = ["afplay", "-r", f"{rate:.2f}", path]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.paused = False

    def wait(self, poll: float = 0.15) -> bool:
        """Block until the clip ends. Returns False if it was killed (skip/stop)."""
        while self.proc and self.proc.poll() is None:
            time.sleep(poll)
        return bool(self.proc and self.proc.returncode == 0)

    def pause(self) -> None:
        if self.proc and self.proc.poll() is None and not self.paused:
            self.proc.send_signal(signal.SIGSTOP)
            self.paused = True

    def resume(self) -> None:
        if self.proc and self.proc.poll() is None and self.paused:
            self.proc.send_signal(signal.SIGCONT)
            self.paused = False

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            if self.paused:
                self.proc.send_signal(signal.SIGCONT)
            self.proc.kill()
        self.proc = None
        self.paused = False


# --------------------------------------------------------------------------
# Control channel
# --------------------------------------------------------------------------


def send_command(cmd: str) -> bool:
    state = read_json(STATE_FILE, {})
    if not pid_alive(state.get("pid", 0)):
        return False
    ensure_dirs()
    with open(CONTROL_FILE, "a", encoding="utf-8") as fh:
        fh.write(cmd + "\n")
    return True


def clear_commands() -> None:
    """Drop any commands left behind by a previous session.

    Without this, a `stop` that the last worker never got around to reading
    would be picked up by the next one and kill it on startup.
    """
    try:
        if os.path.exists(CONTROL_FILE):
            os.remove(CONTROL_FILE)
    except OSError:
        pass


def drain_commands() -> list[str]:
    if not os.path.exists(CONTROL_FILE):
        return []
    try:
        with open(CONTROL_FILE, "r+", encoding="utf-8") as fh:
            cmds = [ln.strip() for ln in fh if ln.strip()]
            fh.seek(0)
            fh.truncate()
        return cmds
    except OSError:
        return []


# --------------------------------------------------------------------------
# Worker (the thing that actually reads to you)
# --------------------------------------------------------------------------


def _fail(msg: str, path: str = "") -> int:
    log(f"FATAL {msg}")
    write_json(STATE_FILE, {"pid": os.getpid(), "status": "error", "error": msg,
                            "path": path, "file": os.path.basename(path),
                            "chunk": 0, "total": 0, "percent": 0,
                            "updated": time.time()})
    return 2


def run_worker(args) -> int:
    ensure_dirs()
    try:
        doc = build_document(args.path, code_mode=args.code, target=args.chunk_chars)
    except UnreadableFile as exc:
        return _fail(str(exc), args.path)
    chunks = doc.chunks
    if not chunks:
        return _fail(f"Nothing readable in {args.path}", args.path)

    try:
        backend = SpeechBackend(args.url, args.model, args.voice, args.speed,
                                args.lang, engine=args.engine)
    except TTSError as exc:
        return _fail(str(exc), args.path)
    player = Player()

    start = max(0, min(args.start, len(chunks) - 1))
    playhead = {"i": start}
    stop_flag = threading.Event()
    synth_errors: Queue = Queue()

    # --- synth-ahead thread ------------------------------------------------
    def synth_loop() -> None:
        i = start
        while not stop_flag.is_set() and i < len(chunks):
            if i > playhead["i"] + LOOKAHEAD:
                time.sleep(0.1)
                continue
            if i < playhead["i"]:
                i = playhead["i"]
                continue
            try:
                backend.synth(chunks[i].text)
            except Exception as exc:  # noqa: BLE001 - the loop must never die silently
                synth_errors.put(str(exc))
                stop_flag.set()
                return
            i += 1

    worker = threading.Thread(target=synth_loop, daemon=True)
    worker.start()

    def save_state(status: str) -> None:
        write_json(STATE_FILE, {
            "pid": os.getpid(),
            "status": status,
            "path": doc.path,
            "file": os.path.basename(doc.path),
            "title": doc.title,
            "chunk": playhead["i"],
            "total": len(chunks),
            "label": chunks[min(playhead["i"], len(chunks) - 1)].label,
            "percent": round(100 * playhead["i"] / max(len(chunks), 1)),
            "voice": backend.voice,
            "speed": backend.speed,
            "engine": backend.engine,
            "est_minutes": doc.stats["est_minutes"],
            "updated": time.time(),
        })

    def save_bookmark() -> None:
        marks = read_json(BOOKMARKS_FILE, {})
        marks[doc.path] = {
            "chunk": playhead["i"], "total": len(chunks),
            "at": time.time(), "title": doc.title,
        }
        write_json(BOOKMARKS_FILE, marks)

    save_state("starting")
    log(f"reading {doc.path} ({len(chunks)} chunks, ~{doc.stats['est_minutes']}min, "
        f"engine={backend.engine}, voice={backend.voice})")

    paused = False
    try:
        while playhead["i"] < len(chunks):
            for cmd in drain_commands():
                if cmd == "pause":
                    paused = True
                    player.pause()
                elif cmd == "resume":
                    paused = False
                    player.resume()
                elif cmd == "stop":
                    raise KeyboardInterrupt
                elif cmd == "skip":
                    player.kill()
                    playhead["i"] = min(playhead["i"] + 1, len(chunks))
                elif cmd == "back":
                    player.kill()
                    playhead["i"] = max(playhead["i"] - 1, 0)
                elif cmd.startswith("goto:"):
                    player.kill()
                    try:
                        playhead["i"] = max(0, min(int(cmd[5:]), len(chunks) - 1))
                    except ValueError:
                        pass
                elif cmd.startswith("speed:"):
                    try:
                        backend.speed = float(cmd[6:])
                    except ValueError:
                        pass

            if paused:
                save_state("paused")
                time.sleep(0.2)
                continue

            if not synth_errors.empty():
                err = synth_errors.get()
                log(f"FATAL {err}")
                save_state("error")
                write_json(STATE_FILE, {**read_json(STATE_FILE, {}), "error": err})
                return 2

            i = playhead["i"]
            if i >= len(chunks):
                break
            ch = chunks[i]

            # Wait for the synth thread; render inline if it fell behind.
            path = backend.cache_path(ch.text)
            waited = 0.0
            while not os.path.exists(path) and waited < 2.0 and not stop_flag.is_set():
                time.sleep(0.05)
                waited += 0.05
            if not os.path.exists(path):
                try:
                    path = backend.synth(ch.text)
                except TTSError as exc:
                    log(f"FATAL {exc}")
                    write_json(STATE_FILE, {**read_json(STATE_FILE, {}),
                                            "status": "error", "error": str(exc)})
                    return 2

            save_state("playing")
            save_bookmark()
            if ch.is_section and i > start:
                time.sleep(0.35)  # a breath between sections
            player.play(path)
            while player.proc and player.proc.poll() is None:
                cmds = drain_commands()
                if not cmds:
                    time.sleep(0.12)
                    continue
                for cmd in cmds:
                    if cmd == "pause":
                        paused = True
                        player.pause()
                        save_state("paused")
                    elif cmd == "resume":
                        paused = False
                        player.resume()
                        save_state("playing")
                    elif cmd == "stop":
                        raise KeyboardInterrupt
                    elif cmd == "skip":
                        player.kill()
                    elif cmd == "back":
                        player.kill()
                        playhead["i"] = max(playhead["i"] - 2, -1)
                    elif cmd.startswith("goto:"):
                        player.kill()
                        try:
                            playhead["i"] = max(0, min(int(cmd[5:]), len(chunks) - 1)) - 1
                        except ValueError:
                            pass
                while paused and player.proc:
                    for cmd in drain_commands():
                        if cmd == "resume":
                            paused = False
                            player.resume()
                            save_state("playing")
                        elif cmd == "stop":
                            raise KeyboardInterrupt
                    time.sleep(0.15)

            playhead["i"] += 1

        playhead["i"] = len(chunks)
        save_state("done")
        marks = read_json(BOOKMARKS_FILE, {})
        marks.pop(doc.path, None)  # finished -> clear the bookmark
        write_json(BOOKMARKS_FILE, marks)
        log(f"finished {doc.path}")
        return 0

    except KeyboardInterrupt:
        save_state("stopped")
        save_bookmark()
        log(f"stopped at chunk {playhead['i']}")
        return 0
    finally:
        stop_flag.set()
        player.kill()
        prune_cache()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_doctor(args) -> int:
    ok = True
    print("md-whispr doctor")
    print("-" * 46)

    have_afplay = shutil.which("afplay") is not None
    print(f"{'PASS' if have_afplay else 'FAIL'}  afplay (macOS audio playback)")
    ok &= have_afplay

    reachable = SpeechBackend.probe(args.url)
    print(f"{'PASS' if reachable else 'FAIL'}  TTS server at {args.url}")
    if not reachable:
        ok = False
        print("        Start it with:  mlx_audio.server --port 8000")
        print("        Install:        pip install 'mlx-audio[server]' misaki")
        have_say = shutil.which("say") is not None
        print(f"        {'Fallback available: macOS `say`' if have_say else 'No fallback available'}")

    if reachable:
        try:
            base = args.url.rsplit("/v1/", 1)[0]
            with urllib.request.urlopen(f"{base}/v1/models", timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            loaded = json.dumps(body)[:300]
            hit = args.model.split("/")[-1].lower() in loaded.lower()
            print(f"{'PASS' if hit else 'WARN'}  model {args.model}"
                  f"{'' if hit else ' not loaded yet (loads on first request)'}")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN  could not list models: {exc}")

    ensure_dirs()
    n = len(os.listdir(CACHE_DIR))
    size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in os.listdir(CACHE_DIR))
    print(f"PASS  cache: {n} clips, {size / 1e6:.1f} MB at {CACHE_DIR}")

    state = read_json(STATE_FILE, {})
    if pid_alive(state.get("pid", 0)):
        print(f"INFO  currently {state.get('status')}: {state.get('file')} "
              f"({state.get('percent')}%)")
    print("-" * 46)
    print("READY" if ok else "NOT READY — fix the FAIL lines above")
    return 0 if ok else 1


def cmd_read(args) -> int:
    ensure_dirs()

    try:
        doc = build_document(args.path, code_mode=args.code, target=args.chunk_chars)
    except UnreadableFile as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        s = doc.stats
        print(f"file      {doc.path}")
        print(f"kind      {s['kind']}")
        print(f"stripped  {s['raw_chars']:,} -> {s['narration_chars']:,} chars "
              f"({s['reduction_pct']}% removed)")
        print(f"chunks    {s['chunks']}  (~{s['est_minutes']} min at speed {args.speed})")
        print(f"voice     {args.voice} via {args.model}")
        print()
        for ch in doc.chunks[: args.preview]:
            head = f"[{ch.index:>3}] " + (f"({ch.label}) " if ch.label else "")
            print(head + ch.text[:200] + ("..." if len(ch.text) > 200 else ""))
        if len(doc.chunks) > args.preview:
            print(f"... {len(doc.chunks) - args.preview} more chunks")
        return 0

    start = args.start
    if args.resume:
        marks = read_json(BOOKMARKS_FILE, {})
        mark = marks.get(os.path.abspath(args.path))
        if mark:
            start = mark["chunk"]
            print(f"resuming at chunk {start}/{mark['total']}")

    # One reader at a time.
    prev = read_json(STATE_FILE, {})
    if pid_alive(prev.get("pid", 0)):
        send_command("stop")
        for _ in range(20):
            time.sleep(0.1)
            if not pid_alive(prev.get("pid", 0)):
                break
    clear_commands()

    if args.foreground:
        args.start = start
        return run_worker(args)

    child = [sys.executable, os.path.abspath(__file__), "_worker", args.path,
             "--start", str(start), "--voice", args.voice, "--speed", str(args.speed),
             "--model", args.model, "--url", args.url, "--lang", args.lang,
             "--code", args.code, "--engine", args.engine,
             "--chunk-chars", str(args.chunk_chars)]
    with open(LOG_FILE, "a") as logfh:
        proc = subprocess.Popen(child, stdout=logfh, stderr=logfh,
                                start_new_session=True)

    # Give the worker a moment to either start playing or fail loudly.
    deadline = time.time() + 8.0
    state = {}
    while time.time() < deadline:
        time.sleep(0.25)
        state = read_json(STATE_FILE, {})
        if state.get("status") in ("playing", "error"):
            break
        if proc.poll() is not None:
            break

    s = doc.stats
    if state.get("status") == "error":
        print(f"error: {state.get('error')}", file=sys.stderr)
        return 2
    if proc.poll() is not None and state.get("status") != "done":
        print(f"error: reader exited immediately (code {proc.returncode}). "
              f"See {LOG_FILE}", file=sys.stderr)
        return 2
    print(f"reading {os.path.basename(doc.path)} — {s['chunks']} chunks, "
          f"~{s['est_minutes']} min, voice {args.voice} (pid {proc.pid})")
    print("controls: md_whispr.py pause | resume | skip | back | stop | status")
    return 0


def cmd_render(args) -> int:
    ensure_dirs()
    try:
        doc = build_document(args.path, code_mode=args.code, target=args.chunk_chars)
    except UnreadableFile as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    backend = SpeechBackend(args.url, args.model, args.voice, args.speed,
                            args.lang, engine=args.engine, fmt="wav")
    parts = []
    for i, ch in enumerate(doc.chunks):
        try:
            parts.append(backend.synth(ch.text))
        except TTSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        pct = round(100 * (i + 1) / len(doc.chunks))
        print(f"\rrendering {i + 1}/{len(doc.chunks)} ({pct}%)", end="", flush=True)
    print()

    out = args.output or os.path.splitext(os.path.basename(doc.path))[0] + ".m4a"
    listfile = os.path.join(STATE_DIR, "concat.txt")
    with open(listfile, "w", encoding="utf-8") as fh:
        for p in parts:
            fh.write(f"file '{p}'\n")

    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
               "-c:a", "aac", "-b:a", "64k", out]
    else:
        # macOS ships afconvert; concat via sox is not guaranteed, so require ffmpeg.
        print("error: ffmpeg is required to stitch chunks. `brew install ffmpeg`",
              file=sys.stderr)
        return 1
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"error: ffmpeg failed: {proc.stderr[-400:]}", file=sys.stderr)
        return 1
    print(f"wrote {out} (~{doc.stats['est_minutes']} min)")
    return 0


def cmd_status(args) -> int:
    state = read_json(STATE_FILE, {})
    if not state:
        print("idle — nothing has been read yet")
        return 0
    alive = pid_alive(state.get("pid", 0))
    status = state.get("status", "unknown")
    if not alive and status not in ("done", "stopped", "error"):
        status = "dead"
    bar_w = 24
    filled = int(bar_w * state.get("chunk", 0) / max(state.get("total", 1), 1))
    bar = "#" * filled + "." * (bar_w - filled)
    print(f"{status:>8}  {state.get('file', '?')}")
    print(f"          [{bar}] {state.get('chunk', 0)}/{state.get('total', 0)} "
          f"({state.get('percent', 0)}%)")
    if state.get("label"):
        print(f"          section: {state['label']}")
    print(f"          voice {state.get('voice')} @ {state.get('speed')}x "
          f"via {state.get('engine')}")
    if state.get("error"):
        print(f"          error: {state['error']}")

    marks = read_json(BOOKMARKS_FILE, {})
    if marks:
        print("\nbookmarks (resume with: read PATH --resume)")
        for path, m in sorted(marks.items(), key=lambda kv: -kv[1]["at"])[:5]:
            pct = round(100 * m["chunk"] / max(m["total"], 1))
            print(f"  {pct:>3}%  {path}")
    return 0


def cmd_control(args) -> int:
    if not send_command(args.command):
        print("nothing is playing", file=sys.stderr)
        return 1
    if args.command == "stop":
        time.sleep(0.3)
    print(args.command)
    return 0


def cmd_voices(args) -> int:
    print("Kokoro voice presets (mlx-community/Kokoro-82M-bf16)\n")
    table = [
        ("American English  lang_code a", [
            ("af_heart", "warm, natural — best default for long docs"),
            ("af_bella", "brighter, more energetic"),
            ("af_nova", "crisp, newsreader"),
            ("af_sky", "light, youthful"),
            ("am_adam", "deep male, steady"),
            ("am_echo", "neutral male"),
        ]),
        ("British English   lang_code b", [
            ("bf_alice", "clear British female"),
            ("bf_emma", "softer British female"),
            ("bm_daniel", "British male, measured"),
            ("bm_george", "British male, deeper"),
        ]),
        ("Other", [
            ("ff_siwis", "French female — lang_code f"),
            ("ef_dora", "Spanish female — lang_code e"),
            ("jf_alpha", "Japanese female — lang_code j"),
            ("zf_xiaobei", "Mandarin female — lang_code z"),
        ]),
    ]
    for group, voices in table:
        print(group)
        for name, note in voices:
            print(f"  {name:<12} {note}")
        print()
    print("Kokoro ships 54 presets; the full list is on the model card.")
    print("Set a default:  export MD_WHISPR_VOICE=af_heart")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_synth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--lang", default=DEFAULT_LANG)
    p.add_argument("--engine", default="auto", choices=["auto", "mlx", "say"])
    p.add_argument("--code", default="announce", choices=["announce", "skip", "full"],
                   help="how to handle fenced code blocks")
    p.add_argument("--chunk-chars", type=int, default=380,
                   help="target characters per synthesis chunk")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="md_whispr", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_read = sub.add_parser("read", help="read a file out loud (backgrounded)")
    p_read.add_argument("path")
    p_read.add_argument("--start", type=int, default=0, help="start at chunk N")
    p_read.add_argument("--resume", action="store_true", help="resume from bookmark")
    p_read.add_argument("--foreground", action="store_true")
    p_read.add_argument("--dry-run", action="store_true",
                        help="show the stripped text and chunk plan, synthesize nothing")
    p_read.add_argument("--preview", type=int, default=8)
    add_synth_args(p_read)
    p_read.set_defaults(func=cmd_read)

    p_worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p_worker.add_argument("path")
    p_worker.add_argument("--start", type=int, default=0)
    add_synth_args(p_worker)
    p_worker.set_defaults(func=run_worker)

    p_render = sub.add_parser("render", help="render to an audio file instead of playing")
    p_render.add_argument("path")
    p_render.add_argument("-o", "--output")
    add_synth_args(p_render)
    p_render.set_defaults(func=cmd_render)

    p_doctor = sub.add_parser("doctor", help="check the setup")
    p_doctor.add_argument("--url", default=DEFAULT_URL)
    p_doctor.add_argument("--model", default=DEFAULT_MODEL)
    p_doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("status", help="what is playing").set_defaults(func=cmd_status)
    sub.add_parser("voices", help="list Kokoro voices").set_defaults(func=cmd_voices)

    for name, helptext in [
        ("pause", "pause playback"), ("resume", "resume playback"),
        ("skip", "skip to the next chunk"), ("back", "go back one chunk"),
        ("stop", "stop and bookmark"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.set_defaults(func=cmd_control, command=name)

    p_goto = sub.add_parser("goto", help="jump to chunk N")
    p_goto.add_argument("n", type=int)
    p_goto.set_defaults(func=lambda a: cmd_control(
        argparse.Namespace(command=f"goto:{a.n}")))

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UnreadableFile as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
