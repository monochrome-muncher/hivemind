"""The restore refusals in their order (ADR 0124), including the two defensive chain rules
that no sequence of store operations reaches today."""

from __future__ import annotations

from hivemind.domain.entry_restore import (
    EntryNotWithdrawn,
    EntryRedacted,
    RestoreChainConflict,
    RestoreViaCleanup,
    chain_conflict_message,
    restore_refusal,
)

ENTRY = "e-restore"
ACTIVE = "e-active"
LINKER = "e-linker"


def test_no_chain_fact_means_no_conflict() -> None:
    assert chain_conflict_message(ENTRY, False, None, None) is None


def test_a_cut_link_wins_over_the_other_chain_facts() -> None:
    message = chain_conflict_message(ENTRY, True, LINKER, ACTIVE)
    assert message is not None and "link between them was cut" in message


def test_a_memory_put_back_in_its_place_is_named_before_a_stray_link() -> None:
    message = chain_conflict_message(ENTRY, False, LINKER, ACTIVE)
    assert message is not None and ACTIVE in message and LINKER not in message


def test_an_active_memory_still_linking_to_it_is_a_conflict() -> None:
    message = chain_conflict_message(ENTRY, False, LINKER, None)
    assert message is not None and LINKER in message


def test_the_refusals_come_in_their_documented_order() -> None:
    chain = chain_conflict_message(ENTRY, True, None, None)
    assert isinstance(restore_refusal(ENTRY, "active", None, "c1", chain, True), EntryRedacted)
    assert isinstance(restore_refusal(ENTRY, "active", None, "c1", chain), EntryNotWithdrawn)
    assert isinstance(restore_refusal(ENTRY, "withdrawn", None, "c1", chain), RestoreViaCleanup)
    assert isinstance(restore_refusal(ENTRY, "withdrawn", None, None, chain), RestoreChainConflict)
    assert restore_refusal(ENTRY, "withdrawn", "typo", None, None) is None
