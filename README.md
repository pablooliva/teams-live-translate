# Teams Live Translate

Translate whatever audio is coming through Microsoft Teams into a target
language of your choice, in near-real-time spoken audio, using Google's
**Gemini 3.5 Live Translate** model.

Listen-only and one-directional: it translates what you *hear*. It never touches
your microphone or sends anything into the meeting. The source language is
auto-detected (70+ languages); the target language is configurable.

Runs on **macOS and Windows** — same script, same commands; only the virtual
audio device differs.

## How it works

```
Teams ──(output device)──> virtual cable ──> this script ──(WebSocket)──> Gemini Live
                                                                              │
                your headphones <──(translated 24 kHz PCM)──── this script <──┘
```

The one non-obvious trick: **Teams lets you pick its speaker output device
independently**, so by pointing Teams at a virtual audio device, *only* Teams
audio gets captured — not Spotify, notifications, or anything else.

The script auto-detects each device's native sample rate and channel count,
downmixes to mono, and resamples to/from the API's required 16 kHz/24 kHz with
[soxr](https://github.com/dofuuz/python-soxr). So you don't need to hand-tune
audio formats — it adapts to whatever the device reports, on either OS.

## Common setup (both platforms)

1. **Python deps** (managed by [uv](https://docs.astral.sh/uv/)):

   ```sh
   uv sync
   ```

   Creates `.venv` and installs the locked dependencies from `uv.lock`. No need
   to activate it — use `uv run` (below) and uv handles the rest.

2. **API key** — get one at https://aistudio.google.com/apikey, then copy
   `.env.example` to `.env` and paste your key into `GEMINI_API_KEY`.

Then follow the virtual-device steps for your OS below.

## macOS

1. **Virtual audio device** — [BlackHole](https://github.com/ExistentialAudio/BlackHole):

   ```sh
   brew install blackhole-2ch
   ```

   (Reboot or log out/in if it doesn't appear in the device list.)

2. **Point Teams at it** — Teams → Settings → Devices → **Speaker** = `BlackHole 2ch`.

3. Set `INPUT_DEVICE=BlackHole` in `.env` (the default already is).

> Hear *both* original and translation: create a **Multi-Output Device**
> (BlackHole + your headphones) in *Audio MIDI Setup* and select that as Teams'
> speaker instead.

## Windows

Pick one of three routes:

### Route 1 — VB-CABLE (recommended, the BlackHole equivalent)

1. Install [VB-CABLE](https://vb-audio.com/Cable/) (free). It adds a playback
   device **"CABLE Input"** and a recording device **"CABLE Output."**
2. **Point Teams at it** — Teams → Settings → Devices → **Speaker** = `CABLE Input`.
3. Set `INPUT_DEVICE=CABLE Output` in `.env`, and `OUTPUT_DEVICE` to your headphones.

   Like the simple macOS setup, you'll hear *only* the translation this way.

### Route 2 — VoiceMeeter (if you want to hear original + translation)

[VoiceMeeter](https://vb-audio.com/Voicemeeter/) (free) is a virtual mixer. Route
Teams' output to *both* your headphones (original) and a virtual bus (captured by
the script). More setup, but it's the Windows answer to macOS's Multi-Output
Device. Set `INPUT_DEVICE` to the VoiceMeeter output bus.

### Route 3 — WASAPI loopback (no extra software)

Windows can capture a device's playback directly without a virtual cable. Combine
with Windows 11 per-app output routing (Settings → System → Sound → Volume mixer)
to send Teams to a dedicated output device and loopback-capture only that one.
Zero installs, but more fiddly — ask if you want the script wired for this.

> **Tip:** on Windows, `--list-devices` shows the same physical device several
> times (once per host API: MME, DirectSound, WASAPI). Any of them works thanks
> to the built-in resampling; pick by the clearest name.

## Run

Find your device names/indices first:

```sh
uv run teams-live-translate --list-devices
```

Then run (uv loads `.env` automatically via `--env-file`):

```sh
uv run --env-file .env teams-live-translate --transcript
```

Override anything on the CLI:

```sh
# Translate into Spanish, explicit devices, show transcript
uv run --env-file .env teams-live-translate \
  --target es --input-device "CABLE Output" --output-device "Headphones" --transcript
```

> On Windows PowerShell the `\` line-continuations above won't work — put it all
> on one line, or use a backtick (`` ` ``) for continuation.

## Notes & limits

- **Latency:** the model stays a few seconds behind the speaker by design (it
  buffers context for natural-sounding output). Great for presentations; feels
  laggy in rapid back-and-forth. The resampler adds only ~10–30 ms on top.
- **Cost:** ~$0.023/min of audio (~$1.38/hour).
- **Preview:** `gemini-3.5-live-translate-preview` is a public-preview model; the
  API surface may change.
- **Sample rates / channels:** handled automatically (auto-detect + soxr
  resampling), so you shouldn't hit format-mismatch errors on either OS.
