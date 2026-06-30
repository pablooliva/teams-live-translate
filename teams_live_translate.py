#!/usr/bin/env python3
"""Live-translate audio into a target language, as speech and/or as captions.

Two modes, sharing one Gemini 3.5 Live Translate pipeline:

* Listen mode (default): captures audio from a (virtual) input device — typically
  BlackHole (macOS) or VB-CABLE (Windows) set as Teams' speaker output — streams
  it to Google's Live Translate model, and plays the translated speech back to your
  headphones. Listen-only: it translates what you HEAR.

* Captions mode (--captions): captures your MICROPHONE, and writes the translated
  text (and optionally the original source text) to files that OBS can read with a
  "Text (read from file)" source. Point OBS's Virtual Camera at Teams and your
  audience sees live captions of what you say. Audio playback is off by default in
  this mode (you don't want to hear your own translated voice).

The target language can be changed on the fly with --switch: the Live API config
is immutable per connection, so a language change cleanly closes the session and
reopens it with the new target. That same reconnect loop also rides through the
API's ~15-minute audio-session cap automatically.

The Live Translate API requires a fixed audio format (16 kHz mono in, 24 kHz mono
out). Real devices rarely run at those rates, so this script auto-detects each
device's native rate/channels, downmixes to mono, and resamples with soxr — robust
across macOS (CoreAudio) and Windows (WASAPI/MME).

Config is read from the environment (see .env.example) and can be overridden on the
command line. Run with --list-devices first to find your device names.
"""

import argparse
import asyncio
import contextlib
import json
import os
import queue
import sys
import threading

import numpy as np
import sounddevice as sd
import soxr
from google import genai
from google.genai import types

# --- Audio format required by the Live Translate API ---------------------
# Input:  raw 16-bit PCM, 16 kHz, mono, little-endian
# Output: raw 16-bit PCM, 24 kHz, mono, little-endian
IN_RATE = 16000
OUT_RATE = 24000
DTYPE = "int16"
SAMPLE_BYTES = 2

MODEL = "gemini-3.5-live-translate-preview"

# --- Shared buffers between the audio threads and the asyncio loop --------
# Input callback pushes captured (native-rate, native-channel) bytes here.
_capture_q: "queue.Queue[bytes]" = queue.Queue()
# Receiver appends playback-ready (native-rate, native-channel) bytes here.
_play_buf = bytearray()
_play_lock = threading.Lock()


class CaptureEncoder:
    """Native device audio -> 16 kHz mono int16 PCM for the API.

    Downmixes to mono and resamples with a stateful soxr stream (so chunk
    boundaries stay seamless). Becomes a no-op when the device already matches.
    """

    def __init__(self, native_rate: int, native_channels: int):
        self.channels = native_channels
        self.resampler = (
            None if native_rate == IN_RATE
            else soxr.ResampleStream(native_rate, IN_RATE, 1, dtype="int16")
        )

    def __call__(self, raw: bytes) -> bytes:
        a = np.frombuffer(raw, dtype=np.int16)
        if self.channels > 1:
            a = a.reshape(-1, self.channels).astype(np.int32).mean(axis=1).astype(np.int16)
        if self.resampler is not None:
            a = self.resampler.resample_chunk(a)
        return a.tobytes()


class PlaybackDecoder:
    """24 kHz mono int16 PCM from the API -> native device rate/channels.

    Resamples (stateful soxr) then fans mono out to the device's channel count.
    """

    def __init__(self, native_rate: int, native_channels: int):
        self.channels = native_channels
        self.resampler = (
            None if native_rate == OUT_RATE
            else soxr.ResampleStream(OUT_RATE, native_rate, 1, dtype="int16")
        )

    def __call__(self, raw: bytes) -> bytes:
        a = np.frombuffer(raw, dtype=np.int16)
        if self.resampler is not None:
            a = self.resampler.resample_chunk(a)
        if self.channels > 1:
            a = np.repeat(a[:, None], self.channels, axis=1).reshape(-1)
        return a.tobytes()


class _RollUp:
    """A fixed-width, fixed-height roll-up window for one caption file.

    Streamed transcript fragments are appended with add(); the buffer greedily
    word-wraps them to `width` columns. A line is *committed* (frozen) the instant
    it fills, so it never re-wraps afterward — only the bottom line is still
    "live" and growing. render() always returns exactly `height` rows, newest at
    the bottom and blank-padded at the top.

    The point of the constant width + constant height: OBS re-reads and re-renders
    the whole file on every fragment. If the text could re-wrap or change line
    count, OBS would reflow the entire block and the caption would jitter with no
    stable reference point. Here, finished lines never move and the block is always
    the same size, so the live line sits at a fixed bottom position and completed
    lines scroll up exactly one row at a time — broadcast/TV-caption style.
    """

    def __init__(self, width: int, height: int):
        self.width = max(1, width)
        self.height = max(1, height)
        self.committed: list[str] = []  # frozen, already-wrapped lines
        self.current = ""               # the live bottom line, still growing

    def add(self, text: str) -> None:
        # The model occasionally emits its own newlines/tabs mid-stream; fold them
        # into spaces so they don't defeat our wrapping. (We keep the ordinary
        # single spaces between fragments — those are what separate the words.)
        self.current += text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        self._wrap()

    def end_turn(self) -> None:
        # An utterance just ended. Don't break the line — the roll-up flow simply
        # continues — but make sure the next utterance can't glue onto the last
        # word if its first fragment lacks a leading space.
        if self.current and not self.current.endswith(" "):
            self.current += " "

    def _wrap(self) -> None:
        while len(self.current) > self.width:
            cut = self.current.rfind(" ", 0, self.width + 1)
            if cut <= 0:  # a single word longer than a line: hard-break it
                head, self.current = self.current[: self.width], self.current[self.width :]
            else:
                head, self.current = self.current[:cut], self.current[cut + 1 :]
            self.committed.append(head.rstrip())
        # We only ever display the last `height` rows, so keep memory bounded.
        if len(self.committed) > self.height:
            self.committed = self.committed[-self.height :]

    def render(self) -> str:
        rows = (self.committed + [self.current])[-self.height :]
        # Pad the top with blank rows so the block is always exactly `height` lines
        # tall and the live line stays pinned to the bottom. A single space (not an
        # empty string) guarantees OBS gives each pad row a full line of height.
        rows = [" "] * (self.height - len(rows)) + rows
        return "\n".join(rows)


class CaptionWriter:
    """Writes live transcripts to plain text files for OBS "read from file" sources.

    Two files in `directory`:
      translation.txt - the translated caption (always)
      source.txt      - the original-language transcript (only when bilingual)

    Each file is a roll-up window (see _RollUp): fragments are word-wrapped to a
    fixed column width and shown as a fixed number of rows, newest at the bottom.
    Finished lines never re-wrap and the block is always the same height, so OBS
    renders a stable caption that scrolls up one line at a time as you speak,
    instead of reflowing the whole block on every fragment.
    """

    TRANSLATION_FILE = "translation.txt"
    SOURCE_FILE = "source.txt"
    # Default roll-up geometry; override per run via the width/lines args
    # (env CAPTION_WIDTH / CAPTION_LINES, or --caption-width / --caption-lines).
    #   width = characters per line — match your OBS text-box width / font size
    #   lines = number of visible rows (the roll-up depth)
    DEFAULT_WIDTH = 42
    DEFAULT_LINES = 3

    def __init__(
        self,
        directory: str,
        bilingual: bool,
        width: int = DEFAULT_WIDTH,
        lines: int = DEFAULT_LINES,
    ):
        self.bilingual = bilingual
        os.makedirs(directory, exist_ok=True)
        self.dst_path = os.path.join(directory, self.TRANSLATION_FILE)
        self.src_path = os.path.join(directory, self.SOURCE_FILE)
        self.dst = _RollUp(width, lines)
        self.src = _RollUp(width, lines)
        self._write(self.dst_path, self.dst.render())
        if bilingual:
            self._write(self.src_path, self.src.render())

    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def add_source(self, text: str) -> None:
        if not self.bilingual:
            return
        self.src.add(text)
        self._write(self.src_path, self.src.render())

    def add_translation(self, text: str) -> None:
        self.dst.add(text)
        self._write(self.dst_path, self.dst.render())

    def end_turn(self) -> None:
        self.dst.end_turn()
        if self.bilingual:
            self.src.end_turn()


# Self-contained transcript page: inlined HTML/CSS/JS so the server has no static
# assets to ship. It opens an SSE stream to /events and renders fragments live.
_WEB_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Translation</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    background: #0f1115; color: #e8eaed;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header {
    flex: 0 0 auto; display: flex; align-items: center; gap: .6rem;
    padding: .7rem 1rem; border-bottom: 1px solid #23262d; background: #14171c;
  }
  header h1 { font-size: 1rem; font-weight: 600; margin: 0; color: #cfd3d8; }
  .badge {
    font-size: .8rem; padding: .15rem .55rem; border-radius: 999px;
    background: #1f6feb22; color: #6ea8fe; border: 1px solid #1f6feb55;
    text-transform: uppercase; letter-spacing: .04em;
  }
  .spacer { flex: 1; }
  .dot { width: .65rem; height: .65rem; border-radius: 50%; background: #555; transition: background .3s; }
  .dot.on { background: #2ea043; box-shadow: 0 0 6px #2ea043aa; }
  .dot.off { background: #d29922; }
  #feed {
    flex: 1; overflow-y: auto; padding: 1.2rem clamp(1rem, 6vw, 6rem);
    font-size: clamp(1.2rem, 2.6vw, 2rem); line-height: 1.5;
  }
  .line { margin: 0 0 .6rem; white-space: pre-wrap; word-break: break-word; color: #c2c6cc; }
  .line.live { color: #fff; }
  .line.live::after {
    content: "\\258B"; margin-left: .1em; color: #6ea8fe;
    animation: blink 1s steps(1) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  footer { flex: 0 0 auto; padding: .4rem 1rem; font-size: .75rem; color: #6b7178; border-top: 1px solid #23262d; }
</style>
</head>
<body>
  <header>
    <h1>Live Translation</h1>
    <span class="badge" id="lang">\\2026</span>
    <span class="spacer"></span>
    <span class="dot" id="status" title="connection"></span>
  </header>
  <div id="feed"></div>
  <footer>Auto-scrolling. Scroll up to read back; new lines resume auto-scroll.</footer>
  <script>
    const feed = document.getElementById('feed');
    const langEl = document.getElementById('lang');
    const statusEl = document.getElementById('status');
    let liveEl = null;
    let autoscroll = true;

    feed.addEventListener('scroll', () => {
      autoscroll = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    });
    function toBottom() { if (autoscroll) feed.scrollTop = feed.scrollHeight; }

    function ensureLive() {
      if (!liveEl) {
        liveEl = document.createElement('div');
        liveEl.className = 'line live';
        feed.appendChild(liveEl);
      }
      return liveEl;
    }
    function commitLive() {
      if (!liveEl) return;
      const t = liveEl.textContent.trim();
      if (t) { liveEl.textContent = t; liveEl.className = 'line'; }
      else { liveEl.remove(); }
      liveEl = null;
    }

    const es = new EventSource('/events');
    es.onopen = () => { statusEl.className = 'dot on'; };
    es.onerror = () => { statusEl.className = 'dot off'; };
    es.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'snapshot') {
        feed.innerHTML = '';
        liveEl = null;
        (msg.history || []).forEach(line => {
          const d = document.createElement('div');
          d.className = 'line';
          d.textContent = line;
          feed.appendChild(d);
        });
        if (msg.current) ensureLive().textContent = msg.current;
        if (msg.lang) langEl.textContent = msg.lang;
        autoscroll = true;
        feed.scrollTop = feed.scrollHeight;
      } else if (msg.type === 'frag') {
        ensureLive().textContent += msg.text;
        toBottom();
      } else if (msg.type === 'turn') {
        commitLive();
        toBottom();
      } else if (msg.type === 'lang') {
        langEl.textContent = msg.lang;
      }
    };
  </script>
</body>
</html>
"""


class WebBroadcaster:
    """Serves a live transcript webpage and streams fragments to it over SSE.

    A second sink alongside the OBS file: the embedded aiohttp server runs as a
    task on the main asyncio loop, and publish() fans each fragment out to every
    connected browser through its own asyncio.Queue. The put is non-blocking, so
    a stalled browser is dropped rather than ever back-pressuring translation.

    Unlike the OBS roll-up (a fixed 3-line block, tuned to stop OBS reflow
    jitter), the browser receives the *raw* fragment stream and builds a full,
    auto-scrolling transcript client-side — so the page isn't bound to that
    caption geometry. A bounded backlog of finished lines (plus the in-progress
    line) is replayed to each newly connected browser as a one-off "snapshot",
    so someone who joins mid-presentation sees recent context, not a blank page.
    """

    MAX_HISTORY = 500  # finished lines retained for the late-joiner snapshot

    def __init__(self, host: str, port: int, lang: str):
        self.host = host
        self.port = port
        self.lang = lang
        self.history: list[str] = []  # finished lines (bounded)
        self.current = ""             # the in-progress line, still growing
        self._clients: "set[asyncio.Queue[str]]" = set()
        self._runner = None

    async def start(self) -> None:
        # Lazy import so only --web requires aiohttp to be installed.
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/events", self._handle_events)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_index(self, request):
        from aiohttp import web

        return web.Response(text=_WEB_PAGE, content_type="text/html")

    async def _handle_events(self, request):
        from aiohttp import web

        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # tell proxies not to buffer the stream
            }
        )
        await resp.prepare(request)
        q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=2000)
        # Register the queue and capture the snapshot with NO await in between, so
        # the snapshot and the live queue partition the event stream exactly: any
        # fragment after this instant goes to the queue, everything before is in
        # the snapshot — no gap, no duplicate.
        self._clients.add(q)
        snapshot = json.dumps(
            {"type": "snapshot", "lang": self.lang, "history": self.history, "current": self.current},
            ensure_ascii=False,
        )
        try:
            await resp.write(f"data: {snapshot}\n\n".encode("utf-8"))
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {data}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")  # heartbeat: keep idle proxies open
        except (asyncio.CancelledError, OSError, RuntimeError):
            pass  # browser navigated away / connection dropped
        finally:
            self._clients.discard(q)
        return resp

    def _publish(self, event: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(event, ensure_ascii=False)
        for q in self._clients:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow/dead client: drop rather than block translation

    def set_language(self, lang: str) -> None:
        if lang == self.lang:
            return
        self.lang = lang
        self._publish({"type": "lang", "lang": lang})

    def add_translation(self, text: str) -> None:
        # Fold stray newlines/tabs to spaces (the model emits them mid-stream),
        # matching what the OBS roll-up does, so a fragment stays one logical line.
        clean = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        self.current += clean
        self._publish({"type": "frag", "text": clean})

    def end_turn(self) -> None:
        line = self.current.strip()
        if line:
            self.history.append(line)
            if len(self.history) > self.MAX_HISTORY:
                self.history = self.history[-self.MAX_HISTORY :]
        self.current = ""
        self._publish({"type": "turn"})


class Controller:
    """Shared control state between the stdin reader thread and the asyncio loop."""

    def __init__(self, target: str):
        self.target = target
        self.switch = asyncio.Event()  # set when the target language changes
        self.stop = asyncio.Event()    # set to quit

    def request_switch(self, new_target: str) -> None:
        self.target = new_target
        self.switch.set()

    def request_stop(self) -> None:
        self.stop.set()


def _in_callback(indata, frames, time_info, status):
    """PortAudio thread: copy captured native PCM into the capture queue."""
    if status:
        print(f"[input status] {status}", flush=True)
    _capture_q.put(bytes(indata))


def _out_callback(outdata, frames, time_info, status):
    """PortAudio thread: fill the output buffer from translated audio, pad with silence."""
    if status:
        print(f"[output status] {status}", flush=True)
    need = len(outdata)
    with _play_lock:
        avail = min(need, len(_play_buf))
        outdata[:avail] = _play_buf[:avail]
        del _play_buf[:avail]
    if avail < need:
        outdata[avail:] = b"\x00" * (need - avail)


def _drain_capture_queue() -> None:
    """Discard backlogged audio so a fresh session doesn't start seconds behind."""
    try:
        while True:
            _capture_q.get_nowait()
    except queue.Empty:
        pass


async def _sender(session, encode: CaptureEncoder):
    """Pump captured chunks into the Live session (downmixed + resampled).

    The queue read uses a short timeout so the executor thread returns promptly:
    that keeps cancellation clean on reconnect and prevents orphaned threads from
    accumulating and stealing audio across sessions.
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            raw = await loop.run_in_executor(None, _capture_q.get, True, 0.1)
        except queue.Empty:
            continue
        chunk = encode(raw)
        if chunk:
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={IN_RATE}")
            )


async def _receiver(session, decode, caption, broadcaster, show_transcript: bool):
    """Receive translated audio + transcripts; play audio and/or write captions.

    Returns when the session ends (server close / 15-min cap), which the run loop
    treats as a signal to reconnect.
    """
    async for response in session.receive():
        sc = getattr(response, "server_content", None)
        if not sc:
            continue
        if decode is not None and sc.model_turn:
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    pcm = decode(part.inline_data.data)
                    with _play_lock:
                        _play_buf.extend(pcm)
        # The source-transcript field has been seen under both names across API
        # versions; accept either so bilingual mode is resilient.
        in_tx = getattr(sc, "input_transcription", None) or getattr(
            sc, "input_audio_transcription", None
        )
        if in_tx and in_tx.text and caption is not None:
            caption.add_source(in_tx.text)
        out_tx = getattr(sc, "output_transcription", None)
        if out_tx and out_tx.text:
            if caption is not None:
                caption.add_translation(out_tx.text)
            if broadcaster is not None:
                broadcaster.add_translation(out_tx.text)
            if show_transcript:
                print(out_tx.text, end="", flush=True)
        if getattr(sc, "turn_complete", False):
            if caption is not None:
                caption.end_turn()
            if broadcaster is not None:
                broadcaster.end_turn()
            if show_transcript:
                print(flush=True)


def _device_format(device, kind: str):
    """Return (native_samplerate, mono-capped channel count) for a device.

    `device` may be None (system default), an int index, or a name substring.
    Channels are capped at 2 to keep downmix/fan-out simple and to match the
    typical mix format of mics, virtual cables and headphones.
    """
    info = sd.query_devices(device, kind)
    rate = int(round(info["default_samplerate"]))
    ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
    channels = max(1, min(int(info[ch_key]), 2))
    return rate, channels


def _build_config(target: str, echo: bool) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],  # Live Translate is audio-only; text comes via transcription.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            # Target language is fully configurable (BCP-47 code, e.g. "de", "es").
            target_language_code=target,
            # Source language is AUTO-DETECTED by the model (70+ languages). The
            # preview API exposes no source-language override, so args.source is
            # informational only — kept so the wiring is ready if Google adds it.
            echo_target_language=echo,
        ),
    )


def _start_control_thread(loop: asyncio.AbstractEventLoop, controller: Controller) -> None:
    """Daemon thread: read language-switch commands from stdin.

    Type a BCP-47 code (e.g. "es") + Enter to switch target language; "q" to quit.
    Daemon so it never blocks process exit while parked on a blocking read.
    """
    def reader():
        for line in sys.stdin:
            cmd = line.strip()
            if not cmd:
                continue
            if cmd.lower() in ("q", "quit", "exit"):
                loop.call_soon_threadsafe(controller.request_stop)
                return
            loop.call_soon_threadsafe(controller.request_switch, cmd)
            print(f"[switching -> {cmd}]", flush=True)

    threading.Thread(target=reader, daemon=True).start()


async def run(args):
    # genai.Client() automatically reads GEMINI_API_KEY or GOOGLE_API_KEY.
    client = genai.Client()
    controller = Controller(args.target)

    in_rate, in_ch = _device_format(args.input_device, "input")
    encode = CaptureEncoder(in_rate, in_ch)

    decode = None
    out_stream = None
    out_rate = out_ch = None
    if args.playback:
        out_rate, out_ch = _device_format(args.output_device, "output")
        decode = PlaybackDecoder(out_rate, out_ch)

    caption = (
        CaptionWriter(
            args.caption_dir, args.bilingual, args.caption_width, args.caption_lines
        )
        if args.captions
        else None
    )

    broadcaster = (
        WebBroadcaster(args.web_host, args.web_port, args.target) if args.web else None
    )

    in_stream = sd.RawInputStream(
        samplerate=in_rate,
        blocksize=in_rate // 10,  # ~100 ms chunks
        dtype=DTYPE,
        channels=in_ch,
        device=args.input_device,
        callback=_in_callback,
    )
    if args.playback:
        out_stream = sd.RawOutputStream(
            samplerate=out_rate,
            blocksize=0,
            dtype=DTYPE,
            channels=out_ch,
            device=args.output_device,
            callback=_out_callback,
        )

    if args.switch:
        _start_control_thread(asyncio.get_running_loop(), controller)

    src = args.source or "auto-detect"
    mode = "captions" if args.captions else "listen"
    lines = [
        f"Mode: {mode}   Translating {src} -> {args.target}",
        f"  in : {args.input_device or 'default (mic)'} @ {in_rate} Hz / {in_ch}ch -> {IN_RATE} Hz mono",
    ]
    if args.playback:
        lines.append(
            f"  out: {args.output_device or 'default'} @ {OUT_RATE} Hz mono -> {out_rate} Hz / {out_ch}ch"
        )
    else:
        lines.append("  out: audio playback OFF")
    if caption is not None:
        kind = "bilingual (source + translation)" if args.bilingual else "translation only"
        lines.append(f"  captions: {os.path.abspath(args.caption_dir)}  ({kind})")
    if broadcaster is not None:
        lines.append(
            f"  web: http://{args.web_host}:{args.web_port}  "
            "(open in a browser; share with others via a tunnel — see README)"
        )
    if args.switch:
        lines.append("  switch: type a language code + Enter to change target, 'q' to quit")
    lines.append("Ctrl-C to stop.\n")
    print("\n".join(lines), flush=True)

    async with contextlib.AsyncExitStack() as stack:
        if broadcaster is not None:
            await broadcaster.start()
            stack.push_async_callback(broadcaster.stop)
        stack.enter_context(in_stream)
        if out_stream is not None:
            stack.enter_context(out_stream)

        # Reconnect loop: one iteration per Live session. A new session starts on
        # language switch or after the server closes the connection (~15-min cap).
        while not controller.stop.is_set():
            controller.switch.clear()
            target = controller.target
            if broadcaster is not None:
                broadcaster.set_language(target)
            _drain_capture_queue()
            config = _build_config(target, args.echo)
            try:
                async with client.aio.live.connect(model=MODEL, config=config) as session:
                    print(f"[connected -> {target}]", flush=True)
                    tasks = {
                        asyncio.create_task(_sender(session, encode)),
                        asyncio.create_task(
                            _receiver(session, decode, caption, broadcaster, args.transcript)
                        ),
                        asyncio.create_task(controller.switch.wait()),
                        asyncio.create_task(controller.stop.wait()),
                    }
                    try:
                        done, _ = await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_COMPLETED
                        )
                    finally:
                        # Tear down this session's tasks on every exit path — normal
                        # turnover, language switch, reconnect, and Ctrl-C. Cancelling
                        # while the socket is still open lets the receiver end on a
                        # clean CancelledError instead of racing the websocket close
                        # into an APIError; gathering (return_exceptions=True) retrieves
                        # every result so a normal stop never surfaces as an
                        # "exception was never retrieved" traceback.
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    for t in done:
                        exc = t.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            print(f"[session ended: {exc!r}]", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any connection error -> reconnect
                if controller.stop.is_set():
                    break
                print(f"[reconnecting after error: {exc!r}]", flush=True)
                await asyncio.sleep(0.5)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    p.add_argument(
        "--target",
        default=os.environ.get("TARGET_LANGUAGE", "de"),
        help="Target language BCP-47 code (env TARGET_LANGUAGE). Default: de",
    )
    p.add_argument(
        "--source",
        default=os.environ.get("SOURCE_LANGUAGE", ""),
        help="Source language hint (env SOURCE_LANGUAGE). Informational only — "
        "the model auto-detects. Leave blank for auto.",
    )
    p.add_argument(
        "--input-device",
        default=os.environ.get("INPUT_DEVICE") or None,
        help="Capture device name or index (env INPUT_DEVICE). Leave unset to use "
        "your default mic (captions mode), or set to BlackHole / 'CABLE Output' (listen mode).",
    )
    p.add_argument(
        "--output-device",
        default=os.environ.get("OUTPUT_DEVICE") or None,
        help="Playback device name or index (env OUTPUT_DEVICE). Set to your headphones.",
    )
    p.add_argument(
        "--echo",
        action="store_true",
        default=os.environ.get("ECHO_TARGET", "1") not in ("0", "false", "False", ""),
        help="Output audio even when input already matches the target language (default on).",
    )
    p.add_argument("--transcript", action="store_true", help="Print the translated transcript to stdout")
    # --- Captions mode -----------------------------------------------------
    p.add_argument(
        "--captions",
        action="store_true",
        help="Write live transcripts to text files for an OBS 'read from file' source.",
    )
    p.add_argument(
        "--caption-dir",
        default=os.environ.get("CAPTION_DIR", "captions"),
        help="Directory for caption files (env CAPTION_DIR). Default: ./captions",
    )
    p.add_argument(
        "--caption-width",
        type=int,
        default=int(os.environ.get("CAPTION_WIDTH") or CaptionWriter.DEFAULT_WIDTH),
        help="Caption line width in characters (env CAPTION_WIDTH). Match it to your "
        "OBS text-box width / font size so a full line just fits. Default: 42",
    )
    p.add_argument(
        "--caption-lines",
        type=int,
        default=int(os.environ.get("CAPTION_LINES") or CaptionWriter.DEFAULT_LINES),
        help="Number of visible caption lines, i.e. the roll-up depth (env "
        "CAPTION_LINES). Default: 3",
    )
    p.add_argument(
        "--bilingual",
        action="store_true",
        help="Also write the original source transcript (source.txt) alongside translation.txt.",
    )
    # Playback default: ON in listen mode, OFF in captions mode (resolved in main()).
    p.add_argument(
        "--playback", dest="playback", action="store_true", default=None,
        help="Play translated audio (default: on in listen mode, off in captions mode).",
    )
    p.add_argument(
        "--no-playback", dest="playback", action="store_false",
        help="Disable translated-audio playback.",
    )
    p.add_argument(
        "--switch",
        action="store_true",
        help="Enable on-the-fly target-language switching: type a code + Enter (reconnects).",
    )
    # --- Web mode ----------------------------------------------------------
    p.add_argument(
        "--web",
        action="store_true",
        help="Serve a live transcript webpage (SSE) several viewers can open at once. "
        "Independent of --captions; pair with a tunnel to share with remote participants.",
    )
    p.add_argument(
        "--web-host",
        default=os.environ.get("WEB_HOST", "127.0.0.1"),
        help="Web server bind address (env WEB_HOST). Default 127.0.0.1 (localhost only — "
        "pair with a tunnel). Set 0.0.0.0 to expose directly on your LAN.",
    )
    p.add_argument(
        "--web-port",
        type=int,
        default=int(os.environ.get("WEB_PORT") or 8080),
        help="Web server port (env WEB_PORT). Default 8080.",
    )
    return p.parse_args()


def main():
    args = parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return
    # Playback defaults to off in captions mode, on otherwise, unless set explicitly.
    if args.playback is None:
        args.playback = not args.captions
    # Coerce numeric device strings ("3") to int indices, which sounddevice prefers.
    for attr in ("input_device", "output_device"):
        val = getattr(args, attr)
        if isinstance(val, str) and val.isdigit():
            setattr(args, attr, int(val))
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
