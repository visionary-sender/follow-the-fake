"""Suite-wide settings: tests never read the VM's brand stop-list (HACK-66), so results do not depend on data."""

import os
import urllib.error
import urllib.request

import pytest

os.environ["FTF_BRAND_STOPLIST"] = "off"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Tests never reach the network (HACK-91): urlopen refuses, and the offline/fetch switches start unset
    (vm.sh exports FTF_OFFLINE=1 on our VMs; each test sets the switch it needs)."""

    def refused(*args, **kwargs):
        raise urllib.error.URLError("network disabled in tests")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    monkeypatch.delenv("FTF_OFFLINE", raising=False)
    monkeypatch.delenv("FTF_FETCH", raising=False)
