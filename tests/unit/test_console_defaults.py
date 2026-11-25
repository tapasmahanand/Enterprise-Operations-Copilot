"""The console must start every call unverified (KB-SOP-IDV level 0)."""

import re
from html.parser import HTMLParser

from kestrel.config import ROOT

UI = (ROOT / "kestrel" / "ui" / "index.html").read_text(encoding="utf-8")


class _Select(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside, self.options = False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select":
            self.inside = a.get("id") == "idv"
        elif tag == "option" and self.inside:
            self.options.append((a.get("value"), "selected" in a))

    def handle_endtag(self, tag):
        if tag == "select":
            self.inside = False


def test_idv_select_defaults_to_level_0():
    p = _Select(); p.feed(UI)
    assert [v for v, _ in p.options] == ["0", "1", "2", "3"]
    assert [v for v, sel in p.options if sel] == ["0"], "exactly one preselected option, and it is level 0"


def test_new_call_resets_verification_to_0():
    body = re.search(r'\$\("newcall"\)\.onclick = \(\) => \{(.*?)\n\};', UI, re.S)
    assert body and '$("idv").value = "0"' in body.group(1) and "session = null" in body.group(1)


def test_api_default_is_level_0():
    from kestrel.api.main import ChatIn
    assert ChatIn(message="hello there").idv_level == 0
