"""The embedded widget is served with Cache-Control: no-cache so a rebuilt bundle is always
picked up on a plain refresh instead of a browser serving the stale, cached script."""

import pytest
from fastapi.testclient import TestClient

from agentbridge.main import app, _WIDGET_DIST


@pytest.mark.skipif(not _WIDGET_DIST.is_dir(), reason="widget/dist not built")
def test_widget_bundle_served_no_cache():
    client = TestClient(app)
    resp = client.get("/widget/agentbridge-widget.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"
