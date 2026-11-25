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


def test_space_joined_recipients_do_not_hide_external_domain():
    from kestrel.jobs.enron_scan import scan_message
    msg = {"subject": "Notice", "recipients": "a@enron.com x@caiso.com y@enron.com",
           "body": "As with all notices, please treat this as confidential. Thanks."}
    assert [f["rule_id"] for f in scan_message(msg)] == ["CS-EXT-CONF"]


def test_footer_confidential_is_not_a_marking():
    from kestrel.jobs.enron_scan import scan_message
    msg = {"subject": "Re: game", "recipients": "jim@milchev.com",
           "body": "Thanks Jim.\nThis message may contain confidential information that is protected by privilege."}
    assert scan_message(msg) == []


def test_kb_ids_need_valid_checksums_and_no_url_context():
    from kestrel.jobs.enron_scan import _kb_ids
    assert _kb_ids("Order#  020147542092 shipped") == []                 # leading 0: not an Aadhaar
    assert _kb_ids("see http://x.com/docs/2000042807462411493.html") == []
    assert _kb_ids("card 4111 1111 1111 1111 was used") == ["card"]     # Luhn-valid
    assert _kb_ids("card 4111 1111 1111 1112 was used") == []
    assert _kb_ids("Aadhaar 2341 2341 2346") == ["aadhaar"]              # Verhoeff-valid
