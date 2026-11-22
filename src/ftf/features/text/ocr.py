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
DEFAULT_TIMEOUT_S = 600  # per Tesseract call (FTF_OCR_TIMEOUT); a timed-out image is skipped

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
_CHROME = re.compile(rf"(?<!\w){_CHROME_PHRASE}(?!\w)", re.I)
# OCR debris left around chrome: symbols, digits and at most 3 letters per token ("Vv", "x", "©").
_DEBRIS = re.compile(r"[\W\d_]*(?:[^\W\d_]{1,3})?[\W\d_]*")


def is_chrome(line: str) -> bool:
    """True when a line holds at least one chrome phrase and nothing else but OCR debris.

    Token-wise checks keep the work linear: OCR output of an ad is attacker-controlled, so no
    pattern here may backtrack over a whole line. "Zarejestruj się teraz i odbierz 3000 zł" stays.
    """
    rest, n = _CHROME.subn(" ", line)
    return n > 0 and all(_DEBRIS.fullmatch(tok) for tok in rest.split())


class OcrUnavailable(RuntimeError):
    pass


def threads() -> int:
    return int(os.environ.get("FTF_THREADS", min(4, os.cpu_count() or 1)))


def ocr_timeout() -> int:
    """Seconds allowed for one Tesseract call (``FTF_OCR_TIMEOUT``, default 600).

    A value that is not a positive integer falls back to the default, so a typo never fails analysis.
    """
    try:
        v = int(os.environ.get("FTF_OCR_TIMEOUT", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return v if v >= 1 else DEFAULT_TIMEOUT_S


def _prepare(src: Path, dst: Path) -> Path:
    """Greyscale, upscaled copy of the image; the original path if Pillow is not installed."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return src
    with Image.open(src) as im:
        im.seek(0)  # first frame of animated images
        g = ImageOps.exif_transpose(im).convert("L")  # phone photos: honour EXIF rotation
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
            capture_output=True, env=env, timeout=ocr_timeout(), check=True,
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
    return [ln for ln in lines if not is_chrome(ln)]
