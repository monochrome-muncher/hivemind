"""Against a real hosted MCP endpoint: HIVEMIND_LIVE=1 with HIVEMIND_MCP_URL and HIVEMIND_API_KEY."""

from __future__ import annotations

import json
import os
import uuid

import pytest

from hivemind_memory import Hivemind, HivemindError
from hivemind_memory.langchain import langchain_tools

pytestmark = pytest.mark.skipif(
    os.environ.get("HIVEMIND_LIVE") != "1", reason="set HIVEMIND_LIVE=1"
)


async def test_write_search_get_rate_and_withdraw() -> None:
    summary = f"hivemind-memory live test {uuid.uuid4().hex}"
    async with Hivemind.connect() as hive:
        assert hive.whoami["key_kind"] == "agent"

        written = await hive.write("fact", summary, scope="fleet")
        entry_id = written["id"]

        found = await hive.search(summary)
        assert entry_id in [hit["id"] for hit in found["hits"]]

        entry = await hive.get(entry_id)
        assert entry["summary"] == summary

        with pytest.raises(HivemindError) as refused:
            await hive.feedback(entry_id, "helpful")
        assert refused.value.code == "permission_denied"

        withdrawn = await hive.withdraw(entry_id, "live test cleanup")
        assert withdrawn["id"] == entry_id

        search_tool = next(t for t in langchain_tools(hive) if t.name == "hive_search")
        reply = await search_tool.ainvoke({"query": summary})
        assert "hits" in json.loads(reply)
