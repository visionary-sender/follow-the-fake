"""Runs inside the image (python3 -I, see chromium_packages.sh): prints `package<TAB>licence` for each
package listed in /added.txt, taken from /usr/share/doc/<package>/copyright.

The licence is the set of DEP-5 `License:` names in the copyright file, joined by "; ". A free-form
copyright file (no `License:` field) gets the licence keywords it mentions. A missing copyright
file is UNDETERMINED. This is a keyword check, not a legal review.
"""
import pathlib
import re

# Notice phrases for free-form copyright files (no DEP-5 `License:` field).
PHRASES = [
    ("X11", r"X Consortium"),
    ("MIT", r"Permission is hereby granted"),
    ("HPND", r"sell this software"),
    ("GPL", r"GNU General Public License"),
    ("LGPL", r"GNU Lesser General Public License"),
    ("MPL", r"Mozilla Public License"),
    ("Apache", r"Apache License"),
    ("BSD", r"Redistribution and use in source and binary forms"),
    ("Bitstream font licence", r"Bitstream"),
    ("public domain", r"in the public domain"),
]
LICENCE_FIELD = re.compile(r"^License:[ \t]*(\S[^\n]*)$", re.MULTILINE)

for line in pathlib.Path("/added.txt").read_text().splitlines():
    name = line.strip()
    if not name:
        continue
    copyright_file = pathlib.Path("/usr/share/doc") / name / "copyright"
    if not copyright_file.exists():
        licence = "UNDETERMINED"
    else:
        text = copyright_file.read_text(errors="replace")
        fields = sorted({m.group(1).strip() for m in LICENCE_FIELD.finditer(text)})
        if fields:
            licence = "; ".join(fields)
        else:
            found = [label for label, pattern in PHRASES if re.search(pattern, text)]
            licence = "free-form: " + ", ".join(found) if found else "UNDETERMINED"
    print(f"{name}\t{licence}")
