"""Build and read the Enron email sample used by the async compliance scan.

The full CMU archive is ~420 MB compressed. `build_sample` streams it and stops after
`limit` messages, so nobody needs the whole thing on disk; the output (gzipped JSONL,
a few MB) is committed to the repo.

    python -m kestrel.ingest.enron --source https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz --limit 2000
"""

from __future__ import annotations

import argparse
import email
import email.policy
import gzip
import hashlib
import json
import re
import tarfile
import urllib.request
from pathlib import Path
from typing import IO, Iterator

DEFAULT_SOURCE = "https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "data" / "enron" / "enron_sample.jsonl.gz"
MAX_BODY = 8000
_WS = re.compile(r"\s+")


def parse_message(raw: bytes, path: str) -> dict | None:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    if msg.is_multipart():
        parts = [p for p in msg.walk() if p.get_content_type() == "text/plain"]
        payload = parts[0].get_payload(decode=True) if parts else b""
    else:
        payload = msg.get_payload(decode=True) or b""
    body = payload.decode("utf-8", errors="replace").strip()
    if len(body) < 40:  # drop empty / trivially short messages
        return None
    # folder path inside maildir: maildir/<user>/<folder...>/<n>.
    parts = path.split("/")
    user = parts[1] if len(parts) > 2 and parts[0] == "maildir" else ""
    folder = "/".join(parts[2:-1]) if len(parts) > 3 else ""
    return {
        "message_id": (msg.get("Message-ID") or hashlib.sha1(raw).hexdigest()).strip(),
        "date": msg.get("Date", ""),
        "sender": msg.get("From", ""),
        "recipients": _WS.sub(" ", " ".join(filter(None, [msg.get("To", ""), msg.get("Cc", "")]))).strip(),
        "subject": msg.get("Subject", ""),
        "user": user,
        "folder": folder,
        "body": body[:MAX_BODY],
    }


def iter_tar(stream: IO[bytes]) -> Iterator[tuple[str, bytes]]:
    with tarfile.open(fileobj=stream, mode="r|gz") as tar:
        for member in tar:
            if member.isfile():
                f = tar.extractfile(member)
                if f is not None:
                    yield member.name, f.read()


def build_sample(stream: IO[bytes], out: Path, limit: int, per_user: int = 200) -> int:
    seen_bodies: set[str] = set()
    per_user_count: dict[str, int] = {}
    n = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as w:
        for name, raw in iter_tar(stream):
            if "/sent" in name or "/deleted_items" in name:  # skip duplicates of other folders
                continue
            rec = parse_message(raw, name)
            if rec is None:
                continue
            key = hashlib.sha1(rec["body"].encode()).hexdigest()
            if key in seen_bodies or per_user_count.get(rec["user"], 0) >= per_user:
                continue
            seen_bodies.add(key)
            per_user_count[rec["user"]] = per_user_count.get(rec["user"], 0) + 1
            w.write(json.dumps(rec) + "\n")
            n += 1
            if n >= limit:
                break
    return n


def read_sample(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="URL or local path of the CMU tarball")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=2000)
    a = ap.parse_args()
    if a.source.startswith("http"):
        with urllib.request.urlopen(a.source) as resp:
            n = build_sample(resp, a.out, a.limit)
    else:
        with open(a.source, "rb") as f:
            n = build_sample(f, a.out, a.limit)
    print(f"wrote {n} messages to {a.out}")


if __name__ == "__main__":
    main()
