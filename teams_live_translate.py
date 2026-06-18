#!/usr/bin/env python3
"""Live-translate incoming Teams audio into a target language.

Captures audio from a (virtual) input device — typically BlackHole (macOS) or
VB-CABLE (Windows), which you set as Teams' speaker output — streams it to
Google's Gemini 3.5 Live Translate model over a WebSocket, and plays the
translated speech back to your real headphones.

One-directional and listen-only: it translates what you HEAR. It does not touch
your microphone or inject anything back into the meeting.

The Live Translate API requires a fixed audio format (16 kHz mono in, 24 kHz
mono out). Real devices rarely run at those rates — virtual cables are usually
44.1/48 kHz stereo. So this script auto-detects each device's native rate and
channel count, downmixes to mono, and resamples to/from the API rates with soxr.
That makes it robust across macOS (CoreAudio) and Windows (WASAPI/MME), where
sample-rate handling differs.

Config is read from the environment (see .env.example) and can be overridden on
the command line. Run with --list-devices first to find your device names.
"""

import argparse
import asyncio
import os
import queue
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
# Mic callback pushes captured (native-rate, native-channel) bytes here.
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


async def _sender(session, encode: CaptureEncoder):
    """Pump captured mic chunks into the Live session (downmixed + resampled)."""
    loop = asyncio.get_running_loop()
    while True:
        raw = await loop.run_in_executor(None, _capture_q.get)
        chunk = encode(raw)
        if chunk:
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={IN_RATE}")
            )


async def _receiver(session, decode: PlaybackDecoder, show_transcript: bool):
    """Receive translated audio, resample for the device, and queue it for playback."""
    while True:
        async for response in session.receive():
            sc = getattr(response, "server_content", None)
            if not sc:
                continue
            if sc.model_turn:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        pcm = decode(part.inline_data.data)
                        with _play_lock:
                            _play_buf.extend(pcm)
            if show_transcript and sc.output_transcription and sc.output_transcription.text:
                print(sc.output_transcription.text, end="", flush=True)


def _device_format(device, kind: str):
    """Return (native_samplerate, mono-capped channel count) for a device.

    `device` may be None (system default), an int index, or a name substring.
    Channels are capped at 2 to keep downmix/fan-out simple and to match the
    typical mix format of virtual cables and headphones.
    """
    info = sd.query_devices(device, kind)
    rate = int(round(info["default_samplerate"]))
    ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
    channels = max(1, min(int(info[ch_key]), 2))
    return rate, channels


async def run(args):
    # genai.Client() automatically reads GEMINI_API_KEY or GOOGLE_API_KEY.
    client = genai.Client()

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            # Target language is fully configurable (BCP-47 code, e.g. "de", "es").
            target_language_code=args.target,
            # Source language is AUTO-DETECTED by the model (70+ languages, even
            # mixed within one meeting). The preview API exposes no source-language
            # override, so args.source is informational only — kept so the wiring
            # is ready if Google adds the field later.
            echo_target_language=args.echo,
        ),
    )

    in_rate, in_ch = _device_format(args.input_device, "input")
    out_rate, out_ch = _device_format(args.output_device, "output")
    encode = CaptureEncoder(in_rate, in_ch)
    decode = PlaybackDecoder(out_rate, out_ch)

    in_stream = sd.RawInputStream(
        samplerate=in_rate,
        blocksize=in_rate // 10,  # ~100 ms chunks
        dtype=DTYPE,
        channels=in_ch,
        device=args.input_device,
        callback=_in_callback,
    )
    out_stream = sd.RawOutputStream(
        samplerate=out_rate,
        blocksize=0,
        dtype=DTYPE,
        channels=out_ch,
        device=args.output_device,
        callback=_out_callback,
    )

    src = args.source or "auto-detect"
    print(
        f"Translating {src} -> {args.target}\n"
        f"  in : {args.input_device or 'default'} @ {in_rate} Hz / {in_ch}ch -> {IN_RATE} Hz mono\n"
        f"  out: {args.output_device or 'default'} @ {OUT_RATE} Hz mono -> {out_rate} Hz / {out_ch}ch\n"
        f"Ctrl-C to stop.\n",
        flush=True,
    )

    with in_stream, out_stream:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            await asyncio.gather(
                _sender(session, encode),
                _receiver(session, decode, show_transcript=args.transcript),
            )


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
        help="Capture device name or index (env INPUT_DEVICE). "
        "Set to BlackHole (macOS) or 'CABLE Output' (Windows).",
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
    return p.parse_args()


def main():
    args = parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return
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
