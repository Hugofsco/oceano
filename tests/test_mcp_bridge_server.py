import asyncio

import httpx

from oceano import mcp_bridge_server


class _Response:
    def raise_for_status(self):
        return None


class _FlakyClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            raise httpx.ReadError("response lost")
        return _Response()


def test_local_transport_retry_reuses_the_same_operation_header():
    client = _FlakyClient()
    headers = {"X-Oceano-Operation-ID": "request-7"}
    response = asyncio.run(
        mcp_bridge_server._post_tool(client, "write_file", {"path": "a.py"}, headers))
    assert isinstance(response, _Response)
    assert len(client.calls) == 2
    assert client.calls[0][1]["headers"] is headers
    assert client.calls[1][1]["headers"] is headers
