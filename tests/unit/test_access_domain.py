"""TDD: the trust-level visibility matrix (SPEC §12.2, ADR 0011).

Tests the pure predicate ``entry_is_visible`` (and its ``Visibility``
wrapper) at the domain seam — no store, no I/O. Covers the full matrix:
each trust level x each entry scope, plus the admin bypass, the
"self is private even at L3" rule, and the "own fleet entries follow the
agent" rule (entries stay in the fleet they were written into).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hivemind.domain.access import (
    Agent,
    AgentStatus,
    TrustLevel,
    Visibility,
    entry_is_visible,
)
from hivemind.domain.entry import Entry, Kind, new_entry_id

NOW = datetime(2026, 6, 1, tzinfo=UTC)
FL_A = "fleet-a"
FL_B = "fleet-b"


def make_entry(
    *,
    scope: str = "org",
    author: str = "alice",
    agent: str = "alice",
    fleet_id: str | None = None,
    state: str = "active",
) -> Entry:
    """A minimal Entry for visibility tests (identity is a fresh UUID)."""
    return Entry(
        id=new_entry_id(),
        kind=Kind.FACT,
        summary="s",
        author=author,
        agent=agent,
        occurred_at=NOW,
        created_at=NOW,
        scope=scope,
        fleet_id=fleet_id,
    )


def vis(level: int, name: str, home: str | None = None, is_admin: bool = False) -> Visibility:
    return Visibility(level=TrustLevel(level), name=name, home_fleet_id=home, is_admin=is_admin)


# --- matrix: what each level may read ------------------------------------


class TestUntrusted:
    """Level 0 (untrusted): reads nothing. Everything is hidden."""

    @pytest.mark.parametrize("scope", ["self", "fleet", "org"])
    def test_untrusted_reads_nothing(self, scope: str) -> None:
        e = make_entry(scope=scope, author="alice", fleet_id=FL_A)
        assert entry_is_visible(e, vis(0, "bob", home=FL_A)) is False

    def test_untrusted_reads_own_self_entry(self) -> None:
        # Even one's own `self` entries are hidden at level 0 (dormant).
        e = make_entry(scope="self", author="alice")
        assert entry_is_visible(e, vis(0, "alice")) is False

    def test_untrusted_admin_bypass(self) -> None:
        # Admin is a bypass: an admin *acting at* level 0 still sees all.
        e = make_entry(scope="self", author="alice")
        assert entry_is_visible(e, vis(0, "admin", is_admin=True)) is True


class TestLurker:
    """Level 1 (lurker): own entries + home-fleet entries + legacy org."""

    def test_lurker_reads_own_self(self) -> None:
        e = make_entry(scope="self", author="alice")
        assert entry_is_visible(e, vis(1, "alice", home=FL_A)) is True

    def test_lurker_reads_own_fleet_entry(self) -> None:
        # My own fleet-scoped entry is visible to me (I wrote it).
        e = make_entry(scope="fleet", author="alice", fleet_id=FL_A)
        assert entry_is_visible(e, vis(1, "alice", home=FL_A)) is True

    def test_lurker_reads_home_fleet_entry(self) -> None:
        # A teammate's fleet-scoped entry in my home fleet is visible.
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_A)
        assert entry_is_visible(e, vis(1, "alice", home=FL_A)) is True

    def test_lurker_cannot_read_other_fleet(self) -> None:
        # fleet-B's entry is not in my home fleet (A) -> hidden.
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_B)
        assert entry_is_visible(e, vis(1, "alice", home=FL_A)) is False

    def test_lurker_reads_legacy_org(self) -> None:
        e = make_entry(scope="org", author="bob")
        assert entry_is_visible(e, vis(1, "alice", home=FL_A)) is True

    def test_lurker_cannot_read_other_self(self) -> None:
        # self is private to its author, even for a lurker.
        e = make_entry(scope="self", author="bob")
        assert entry_is_visible(e, vis(1, "alice", home=FL_A)) is False


class TestContributor:
    """Level 2 (contributor): same reads as lurker (writes are broader,
    but visibility is read-side; writes are enforced at the write seam)."""

    def test_contributor_reads_home_fleet(self) -> None:
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_A)
        assert entry_is_visible(e, vis(2, "alice", home=FL_A)) is True

    def test_contributor_cannot_read_other_fleet(self) -> None:
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_B)
        assert entry_is_visible(e, vis(2, "alice", home=FL_A)) is False

    def test_contributor_reads_own_fleet_entry(self) -> None:
        e = make_entry(scope="fleet", author="alice", fleet_id=FL_A)
        assert entry_is_visible(e, vis(2, "alice", home=FL_A)) is True


class TestPrivileged:
    """Level 3 (privileged): read-broad — every fleet — write-local."""

    def test_privileged_reads_other_fleets(self) -> None:
        # L3 reads fleet-B's entry even though B is not my home fleet.
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_B)
        assert entry_is_visible(e, vis(3, "alice", home=FL_A)) is True

    def test_privileged_still_reads_home_fleet(self) -> None:
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_A)
        assert entry_is_visible(e, vis(3, "alice", home=FL_A)) is True

    def test_privileged_cannot_read_other_self(self) -> None:
        # self is private to its author even at L3 (that's what `self` is for).
        e = make_entry(scope="self", author="bob")
        assert entry_is_visible(e, vis(3, "alice", home=FL_A)) is False

    def test_privileged_reads_legacy_org(self) -> None:
        e = make_entry(scope="org", author="bob")
        assert entry_is_visible(e, vis(3, "alice", home=FL_A)) is True


class TestOwnEntriesFollowAgent:
    """A fleet-scoped entry stays in the fleet it was written into: when
    an agent moves fleets, its earlier entries remain readable by the
    old fleet (and by the agent itself), not re-parented."""

    def test_agent_moved_still_sees_own_old_fleet_entries(self) -> None:
        # alice was in fleet-A (wrote a fleet entry there), now home=fleet-B.
        old = make_entry(scope="fleet", author="alice", fleet_id=FL_A)
        # alice's current home is B, but her own old-A entry is still visible
        # to her (it is *her* entry).
        assert entry_is_visible(old, vis(1, "alice", home=FL_B)) is True

    def test_agent_moved_cannot_read_new_fleet_by_level_alone(self) -> None:
        # A lurker in fleet-B cannot read fleet-C's entry (neither home nor own).
        e = make_entry(scope="fleet", author="carol", fleet_id="fleet-c")
        assert entry_is_visible(e, vis(1, "alice", home=FL_B)) is False

    def test_other_member_of_old_fleet_still_reads_it(self) -> None:
        # dave is still in fleet-A; alice's old-A fleet entry is visible to dave.
        e = make_entry(scope="fleet", author="alice", fleet_id=FL_A)
        assert entry_is_visible(e, vis(1, "dave", home=FL_A)) is True


class TestAdminBypass:
    def test_admin_reads_anything(self) -> None:
        for scope in ("self", "fleet", "org"):
            e = make_entry(scope=scope, author="bob", fleet_id=FL_B)
            assert entry_is_visible(e, vis(0, "root", is_admin=True)) is True


class TestVisibilityWrapper:
    def test_wrapper_delegates(self) -> None:
        v = vis(1, "alice", home=FL_A)
        e = make_entry(scope="fleet", author="bob", fleet_id=FL_A)
        assert v.entry_visible(e) is True
        e2 = make_entry(scope="fleet", author="bob", fleet_id=FL_B)
        assert v.entry_visible(e2) is False


# --- domain records ------------------------------------------------------


class TestDomainRecords:
    def test_fleet_record(self) -> None:
        f = Agent(name="alice", status=AgentStatus.ACTIVE, trust_level=TrustLevel.LURKER)
        assert f.name == "alice"
        assert f.status is AgentStatus.ACTIVE
        assert f.trust_level is TrustLevel.LURKER

    def test_agent_pending_defaults(self) -> None:
        a = Agent(name="new", status=AgentStatus.PENDING, trust_level=TrustLevel.UNTRUSTED)
        assert a.home_fleet_id is None
        assert a.trust_level is TrustLevel.UNTRUSTED

    def test_agent_active_with_fleet(self) -> None:
        a = Agent(
            name="alice",
            status=AgentStatus.ACTIVE,
            trust_level=TrustLevel.CONTRIBUTOR,
            home_fleet_id=FL_A,
            owner_alias="alice@example.com",
        )
        assert a.home_fleet_id == FL_A
        assert a.owner_alias == "alice@example.com"
        assert a.activated_at is None
