"""Against a real hosted MCP endpoint: HIVEMIND_LIVE=1 with HIVEMIND_MCP_URL and HIVEMIND_API_KEY."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from hivemind_memory import Hivemind, HivemindError, replies
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


def _assert_shape(reply: Mapping[str, Any], shape: Any) -> None:
    keys = set(reply)
    assert shape.__required_keys__ <= keys, shape.__required_keys__ - keys
    assert keys <= shape.__required_keys__ | shape.__optional_keys__, keys - (
        shape.__required_keys__ | shape.__optional_keys__
    )


async def test_replies_have_the_typed_shapes() -> None:
    summary = f"hivemind-memory live shapes {uuid.uuid4().hex}"
    async with Hivemind.connect() as hive:
        _assert_shape(hive.whoami, replies.WhoAmI)
        _assert_shape(await hive.write("fact", summary, check=True), replies.CheckReply)

        written = await hive.write("fact", summary, scope="fleet", tags=["live-test"])
        _assert_shape(written, replies.WriteReply)

        found = await hive.search(summary)
        _assert_shape(found, replies.SearchReply)
        for hit in found["hits"]:
            _assert_shape(hit, replies.Hit)

        entry = await hive.get(written["id"], include_history=True)
        _assert_shape(entry, replies.EntryDetail)
        _assert_shape(entry["feedback"], replies.Feedback)
        many = await hive.get([written["id"], str(uuid.uuid4())])
        _assert_shape(many, replies.Entries)

        listed = await hive.list(tags=["live-test"], limit=5)
        _assert_shape(listed, replies.ListReply)
        for item in listed["entries"]:
            _assert_shape(item, replies.ListItem)

        _assert_shape(await hive.pinned(), replies.PinnedReply)
        _assert_shape(await hive.withdraw(written["id"], "live test cleanup"), replies.Entry)
