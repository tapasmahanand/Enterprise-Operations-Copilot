import gzip
import io
import tarfile

from kestrel.ingest.enron import build_sample, read_sample

MSG = b"""Message-ID: <{n}.JavaMail@thyme>
Date: Mon, 14 May 2001 16:39:00 -0700 (PDT)
From: phillip.allen@enron.com
To: tim.belden@enron.com
Subject: Forecast {n}

Here is our forecast for next week, including the account 1234-5678-9012-3456 reference {n}.
"""


def _tarball(files: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_build_sample_parses_dedupes_skips_sent_and_stops_at_limit(tmp_path):
    files = {f"maildir/allen-p/inbox/{i}.": MSG.replace(b"{n}", str(i).encode()) for i in range(5)}
    files["maildir/allen-p/inbox/dup."] = files["maildir/allen-p/inbox/0."]
    files["maildir/allen-p/sent/9."] = MSG.replace(b"{n}", b"99")
    out = tmp_path / "s.jsonl.gz"
    n = build_sample(_tarball(files), out, limit=3)
    rows = list(read_sample(out))
    assert n == 3 == len(rows)
    assert rows[0]["user"] == "allen-p" and rows[0]["folder"] == "inbox"
    assert rows[0]["subject"].startswith("Forecast")
    assert all("99" not in r["subject"] for r in rows)
    assert len({r["body"] for r in rows}) == 3
