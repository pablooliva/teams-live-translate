# Teams Live Translate

Translate whatever audio is coming through Microsoft Teams into a target
language of your choice, in near-real-time spoken audio, using Google's
**Gemini 3.5 Live Translate** model.

It has two modes:

- **Listen mode** (default) — translates what you *hear*. It captures Teams audio
  via a virtual device and plays the translation to your headphones. Listen-only
  and one-directional: it never touches your microphone or sends anything into the
  meeting.
- **Captions mode** (`--captions`) — translates what you *say*. It captures your
  microphone and writes the translated text (and optionally the original) to files
  that [OBS](https://obsproject.com/) reads, so you can present with live translated
  captions baked into your Teams video. See [Captions mode](#captions-mode-presenting-with-live-captions).

The source language is auto-detected (70+ languages); the target language is
configurable and can be **changed on the fly** while running (`--switch`).

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

## Captions mode (presenting with live captions)

Use this when *you* are presenting and want your audience to read live translated
captions of what you say. It captures your **microphone** (no device config needed
— it uses your default input), translates it, and writes the text to files that OBS
displays — which you then bring into your Teams call, either by screen-sharing an
OBS projector (recommended for presenting) or as a virtual camera.

Audio playback is **off** by default in this mode (you don't want to hear your own
translated voice, and it would feed back into the mic).

```sh
# Speak English, caption in Spanish, with on-the-fly language switching:
uv run --env-file .env teams-live-translate --captions --switch --target es

# Bilingual — also write the original (source) line:
uv run --env-file .env teams-live-translate --captions --bilingual --switch --target es
```

Caption text is written to `./captions/` (override with `--caption-dir`):

- `translation.txt` — the translated caption (always)
- `source.txt` — the original transcript (only with `--bilingual`)

### Switching language while running

With `--switch`, type a target language [BCP-47 code](https://en.wikipedia.org/wiki/IETF_language_tag)
(e.g. `de`, `fr`, `ja`) and press **Enter** to switch; type `q` to quit. Because the
Live API fixes the target language when the connection opens, a switch transparently
reconnects with the new language — a ~1-second gap, then captions resume. (That same
reconnect logic also rides through the API's ~15-minute session cap automatically.)

### OBS setup (one-time)

1. **Add a caption source.** In OBS: **Sources → + → Text (GDI+)** on Windows, or
   **Text (FreeType 2)** on macOS. Tick **"Read from file"** and point it at
   `captions/translation.txt`. Style the font, size, and outline. OBS re-reads the
   file automatically, so captions update live as you speak.
   - For `--bilingual`, add a **second** Text source pointing at `captions/source.txt`
     and position it above the translation.
2. **Pin the caption to a fixed-width column — this is what stops the jitter.**
   Why it matters: by default OBS resizes the text source to its content on *every*
   update, so a center/bottom-anchored caption re-positions on every word — constant
   drift. The script already keeps the *height* fixed (it always writes exactly
   `--caption-lines` rows, blank-padded) and pre-wraps text to `--caption-width`
   columns so finished lines never re-wrap; you just need OBS to stop resizing the
   *width*. The control differs by renderer:
   - **macOS — Text (FreeType 2):** set **"Custom text width"** to the pixel width
     you want the caption column to be (this reserves a fixed width and caps
     wrapping). Leave **"Word Wrap"** on. Leave **"Chat log mode"** *off* — it's
     OBS's own scrolling-caption feature and would compete with the roll-up the
     script produces.
   - **Windows — Text (GDI+):** enable **"Use custom text extents"** and set an
     explicit **Width** and **Height**, with **Alignment = Left**, vertical **Top**.
   The result either way: a roll-up caption that scrolls up one line at a time with
   a stable bottom line, instead of reflowing the whole block on every fragment.
   - **Tune the fit:** keep `--caption-width` (characters) small enough that one
     full line stays *narrower* than your box's pixel width at your font size — then
     OBS never adds a wrap of its own. If lines look double-wrapped, `--caption-width`
     is too large for the box; lower it (or widen the box). Set `--caption-lines`
     (and, on GDI+, the box Height) to the number of rows you want visible.
3. **Get OBS into the Teams call.** Two ways, depending on what you're presenting:
   - **Screen-share an OBS projector (recommended).** Right-click the OBS preview →
     **Windowed Projector (Preview)** (or **Fullscreen Projector (Preview)** if you
     have a spare display), then in Teams use **Share content** and pick that
     projector window/screen. Your whole OBS scene lands on Teams' large
     content-share stage, so what you're presenting *plus* the captions get full
     real estate. Your webcam tile is left untouched.
   - **Virtual camera (captions over your face).** OBS: **Controls → Start Virtual
     Camera**, then Teams → **Settings → Devices → Camera** = `OBS Virtual Camera`.
     The catch: your entire OBS scene *becomes* your camera feed, so it's confined
     to the small webcam tile — anything you're presenting inside OBS is shrunk down
     with it. Fine for captions-over-talking-head, too cramped for slides or a demo.

> Teams won't let you inject text into its own native caption bar, so OBS renders
> your captions instead — which also gives you full control over styling and
> placement.

## Flags

| Flag | Description |
|------|-------------|
| `--list-devices` | List audio devices and exit. |
| `--target <code>` | Target language BCP-47 code (env `TARGET_LANGUAGE`). Default `de`. |
| `--source <code>` | Source-language hint (env `SOURCE_LANGUAGE`). Informational only — the model auto-detects. |
| `--input-device <name\|index>` | Capture device (env `INPUT_DEVICE`). Unset = default mic (captions mode); set to BlackHole / `CABLE Output` (listen mode). |
| `--output-device <name\|index>` | Playback device (env `OUTPUT_DEVICE`) — your headphones. |
| `--transcript` | Print the translated transcript to stdout. |
| `--captions` | Captions mode: capture the mic and write transcripts to files for OBS. |
| `--caption-dir <dir>` | Directory for caption files (env `CAPTION_DIR`). Default `./captions`. |
| `--caption-width <n>` | Caption line width in characters (env `CAPTION_WIDTH`). Match it to your OBS text-box width / font size so a full line just fits. Default `42`. |
| `--caption-lines <n>` | Number of visible caption lines — the roll-up depth (env `CAPTION_LINES`). Default `3`. |
| `--bilingual` | Also write the original source transcript (`source.txt`). |
| `--switch` | Enable on-the-fly target-language switching (type a code + Enter; `q` to quit). |
| `--playback` / `--no-playback` | Force translated-audio playback on/off. Default: on in listen mode, off in captions mode. |
| `--echo` | Output audio even when the input already matches the target language (default on). |

## Notes & limits

- **Latency:** the model stays a few seconds behind the speaker by design (it
  buffers context for natural-sounding output). Great for presentations; feels
  laggy in rapid back-and-forth. The resampler adds only ~10–30 ms on top.
- **Cost:** ~$0.023/min of audio (~$1.38/hour).
- **Preview:** `gemini-3.5-live-translate-preview` is a public-preview model; the
  API surface may change.
- **Captions still synthesize audio:** the model is audio-only (text comes from its
  transcription side-channel), so captions mode generates translated speech it then
  discards. Harmless, but if you only ever want text, a dedicated streaming
  speech-to-text + translation path would be leaner.
- **15-minute sessions:** the API caps an audio session at ~15 minutes; the script
  reconnects automatically (a ~1-second gap), so long presentations just work.
- **Sample rates / channels:** handled automatically (auto-detect + soxr
  resampling), so you shouldn't hit format-mismatch errors on either OS.
