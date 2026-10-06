"""Fails when the repository contains something that looks like a secret. Run in CI and before deploys."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    "Entra client secret": re.compile(r"\b[A-Za-z0-9_~.-]{3}\dQ~[A-Za-z0-9_~.-]{31,34}\b"),
    "storage or service key": re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)=[A-Za-z0-9+/=%]{20,}"),
    "access token": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
SKIP_PARTS = {".git", ".venv", ".dojo-data", "node_modules", "__pycache__"}


def files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True, check=True).stdout
        return [Path(p) for p in out.splitlines() if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in Path(".").rglob("*") if p.is_file() and not SKIP_PARTS.intersection(p.parts)]


def main() -> int:
    found = []
    for path in files():
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            for m in pattern.finditer(text):
                found.append(f"{path}:{text.count(chr(10), 0, m.start()) + 1}: looks like a {name}")
    if found:
        print("\n".join(found))
        print(f"{len(found)} possible secret(s) found.")
        return 1
    print("No secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
