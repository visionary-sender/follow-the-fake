"""Suite-wide settings: tests never read the VM's brand stop-list, so results do not depend on data."""

import os
import urllib.error
import urllib.request

import pytest

os.environ["FTF_BRAND_STOPLIST"] = "off"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """urlopen refuses, so the Python tests make no HTTP requests. In-process DNS and Chromium
    are not guarded (tests/fetch/test_browser.py resolves a .invalid name). The offline/fetch switches
    start unset; vm.sh exports FTF_OFFLINE=1 on our VMs and each test sets what it needs."""

    def refused(*args, **kwargs):
        raise urllib.error.URLError("network disabled in tests")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    monkeypatch.delenv("FTF_OFFLINE", raising=False)
    monkeypatch.delenv("FTF_FETCH", raising=False)
