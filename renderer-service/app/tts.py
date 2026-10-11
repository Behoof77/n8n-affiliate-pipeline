"""Text-to-speech with word-level timing.

Primary path (default): edge-tts streams WordBoundary events straight from the
Microsoft TTS engine — free, exact, and costs almost no RAM (important: the
host only has ~1 GB).

Optional path (RENDERER_STT=whisper): faster-whisper tiny/int8 transcribes the
generated audio with word timestamps, per the spec's alternative.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

DEFAULT_VOICE = os.getenv("RENDERER_VOICE", "hi-IN-SwaraNeural")
STT_MODE = os.getenv("RENDERER_STT", "edge").lower()


@dataclass
class Word:
    start: float  # seconds, relative to scene audio start
    end: float
    text: str


def audio_duration(path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


async def tts_scene(text: str, out_mp3: str, voice: str = DEFAULT_VOICE) -> tuple[list[Word], float]:
    """Synthesise one scene. Returns (words, duration_seconds).

    Raises RuntimeError when edge-tts fails (network / service change) — the
    caller converts it into an HTTP 502 so n8n surfaces the problem.
    """
    import edge_tts

    words: list[Word] = []
    communicate = edge_tts.Communicate(text.strip(), voice)
    try:
        with open(out_mp3, "wb") as fh:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    fh.write(chunk["data"])
                elif chunk.get("type") == "WordBoundary":
                    # offsets are 100-nanosecond ticks
                    start = float(chunk["offset"]) / 1e7
                    dur = float(chunk["duration"]) / 1e7
                    words.append(Word(start=start, end=start + dur, text=chunk["text"]))
    except Exception as exc:  # noqa: BLE001 - surface as service error upstream
        raise RuntimeError(f"edge-tts failed: {exc}") from exc

    dur = audio_duration(out_mp3)
    if dur <= 0:
        raise RuntimeError("edge-tts produced empty audio")

    if STT_MODE == "whisper" or not words:
        whisper_words = _whisper_words(out_mp3)
        if whisper_words:
            words = whisper_words
    if not words:
        words = _proportional_words(text, dur)
    return words, dur


def _whisper_words(audio_path: str, language: str = "hi") -> list[Word]:
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            audio_path, language=language, word_timestamps=True, vad_filter=False
        )
        words: list[Word] = []
        for seg in segments:
            for w in seg.words or []:
                words.append(Word(start=w.start, end=w.end, text=w.word.strip()))
        return [w for w in words if w.text]
    except Exception:
        return []


def _proportional_words(text: str, duration: float) -> list[Word]:
    """Last-resort timing: spread tokens evenly across the clip."""
    tokens = text.split()
    if not tokens or duration <= 0:
        return []
    step = duration / len(tokens)
    return [
        Word(start=i * step, end=(i + 1) * step, text=t)
        for i, t in enumerate(tokens)
    ]
