"""Turn LLM token streams into speakable sentences as early as possible (PRD §4.3 step 6)."""
from __future__ import annotations

import re

_BOUNDARY = re.compile(r"([.!?।]+[\"')\]]*)(\s+)")
_MARKDOWN = re.compile(r"[*_`#>]+")
_SPACES = re.compile(r"\s+")
# Emoji / pictographs / dingbats + joiners and variation selectors. Small local models add these
# despite the prompt, and a TTS voice either skips them or reads out garbage.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF"
    "‍︎️⃣]+"
)


def clean_for_speech(text: str) -> str:
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)  # markdown links -> label
    text = _EMOJI.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


class SentenceChunker:
    """Feed deltas, get complete sentences. The first sentence is released eagerly (even if short)
    to cut time-to-first-audio; later ones are merged up to ``min_chars`` to save TTS round trips."""

    def __init__(self, min_chars: int = 40, first_min_chars: int = 8):
        self.buf = ""
        self.min_chars = min_chars
        self.first_min_chars = first_min_chars
        self.emitted = 0

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            threshold = self.first_min_chars if self.emitted == 0 else self.min_chars
            cut = None
            for m in _BOUNDARY.finditer(self.buf):
                if m.end(1) >= threshold:
                    cut = m.end(1)
                    break
            if cut is None:
                break
            sentence, self.buf = self.buf[:cut], self.buf[cut:].lstrip()
            sentence = clean_for_speech(sentence)
            if sentence:
                out.append(sentence)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        rest = clean_for_speech(self.buf)
        self.buf = ""
        if rest:
            self.emitted += 1
            return [rest]
        return []
