"""OCR of ad screenshots with Tesseract (pol+eng), keeping confidence-filtered lines.

Tesseract runs as a subprocess on a pre-processed copy (greyscale, upscaled when small), so a
hostile image only ever reaches Pillow's decoder and Tesseract, never anything that executes it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

LANGS = "pol+eng"
MIN_LINE_CONF = 60.0  # mean word confidence of a kept line (Tesseract scale 0-100)
MIN_WIDTH = 1400  # narrower images are upscaled: Tesseract reads small UI text poorly

# Lines that are platform UI chrome (Meta Ad Library, Facebook, Instagram), not ad copy.
# Kept in ocr_text; dropped from the text that feeds templates and embeddings.
_CHROME_PHRASE = (
    r"(?:szczegóły reklamy|reklama ma kilka wersji|sponsorowane|sponsored|identyfikator biblioteki"
    r"|transparentność według lokalizacji|informacje o reklamodawcy|reklamodawca i płatnik"
    r"|informacje o reklamach i wykorzystaniu danych|poznaj szczegóły|learn more|dowiedz się więcej"
    r"|lubię to|komentarz|udostępnij|wyślij wiadomość|zobacz więcej|zobacz tłumaczenie|like|comment"
    r"|share|send message|see more|zarejestruj się|sign up|shop now|kup teraz|aktywne|active"
    r"|biblioteka reklam|ad library|ad details|library id|data zamieszczenia|łączny czas aktywności"
    r"|platformy|platforms|zamknij|close|aktywna|inactive|nieaktywna|started running)"
)
# A line is chrome only when it is made of chrome phrases plus OCR debris (symbols, digits, tokens
# of at most 3 letters such as "Vv" or "x"); "Zarejestruj się teraz i odbierz 3000 zł" stays ad copy.
_CHROME = re.compile(rf"(?:\s*(?:{_CHROME_PHRASE}|[\W\d_]+|\b\w{{1,3}}\b))+\s*", re.I)


class OcrUnavailable(RuntimeError):
    pass


def threads() -> int:
    return int(os.environ.get("FTF_THREADS", min(4, os.cpu_count() or 1)))


def _prepare(src: Path, dst: Path) -> Path:
    """Greyscale, upscaled copy of the image; the original path if Pillow is not installed."""
    try:
        from PIL import Image
    except ImportError:
        return src
    with Image.open(src) as im:
        im.seek(0)  # first frame of animated images
        g = im.convert("L")
    if g.width < MIN_WIDTH:
        f = min(3.0, MIN_WIDTH / max(g.width, 1))
        g = g.resize((round(g.width * f), round(g.height * f)), Image.LANCZOS)
    g.save(dst, "PNG")
    return dst


def ocr_lines(path: Path, min_conf: float = MIN_LINE_CONF) -> list[str]:
    """Text lines of an image whose mean word confidence is at least ``min_conf``."""
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrUnavailable("tesseract is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        img = _prepare(Path(path), Path(tmp) / "in.png")
        env = dict(os.environ, OMP_THREAD_LIMIT=str(threads()))
        out = subprocess.run(
            [exe, str(img), "stdout", "-l", LANGS, "--psm", "3", "tsv"],
            capture_output=True, env=env, timeout=180, check=True,
        ).stdout.decode("utf-8", "replace")
    lines: dict[tuple[str, str, str], list[tuple[float, str]]] = {}
    for row in out.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        lines.setdefault((cols[2], cols[3], cols[4]), []).append((conf, cols[11].strip()))
    kept = []
    for words in lines.values():
        mean = sum(c for c, _ in words) / len(words)
        text = " ".join(w for _, w in words)
        if mean >= min_conf and re.search(r"\w{2,}", text):
            kept.append(text)
    return kept


def content_lines(lines: list[str]) -> list[str]:
    """Lines minus platform UI chrome: the ad copy that templates and embeddings use."""
    return [ln for ln in lines if not _CHROME.fullmatch(ln.strip())]
