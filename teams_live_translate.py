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


async def _receiver(session, decode, caption, show_transcript: bool):
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
            if show_transcript:
                print(out_tx.text, end="", flush=True)
        if getattr(sc, "turn_complete", False):
            if caption is not None:
                caption.end_turn()
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
    if args.switch:
        lines.append("  switch: type a language code + Enter to change target, 'q' to quit")
    lines.append("Ctrl-C to stop.\n")
    print("\n".join(lines), flush=True)

    with contextlib.ExitStack() as stack:
        stack.enter_context(in_stream)
        if out_stream is not None:
            stack.enter_context(out_stream)

        # Reconnect loop: one iteration per Live session. A new session starts on
        # language switch or after the server closes the connection (~15-min cap).
        while not controller.stop.is_set():
            controller.switch.clear()
            target = controller.target
            _drain_capture_queue()
            config = _build_config(target, args.echo)
            try:
                async with client.aio.live.connect(model=MODEL, config=config) as session:
                    print(f"[connected -> {target}]", flush=True)
                    tasks = {
                        asyncio.create_task(_sender(session, encode)),
                        asyncio.create_task(
                            _receiver(session, decode, caption, args.transcript)
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
