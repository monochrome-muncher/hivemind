"""ADR 0082: a dev-mode credential sits at trust level 0 when stored keys
are required.

A *dev-mode* credential is one that is not a stored key — not an admin
key, not an org key, not an active agent's key: a hand-built identity such
as the dev runner's fixed ``dev``. When ``HIVEMIND_REQUIRE_STORED_KEYS`` is
on, such a credential gets exactly what a pending agent gets (SPEC §12.2):
reads return empty / not-found, writes are permission denied. Off by
default, so the keyless dev runner and local scripts are unchanged.
"""

from __future__ import annotations

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Verdict
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.errors import EntryNotFound
from hivemind.services.feedback import FeedbackService
from hivemind.services.wiring import build_services
from hivemind.services.write import WriteService
from tests.fakes import make_embedder, make_search_config, make_store


def dev_credential() -> Credential:
    """The dev runner's fixed identity: not stored, not access-controlled."""
    return Credential(user_id="dev", agent_id="hivemind-mcp")


async def _write_entry(credential: Credential) -> tuple[MemoryStore, str]:
    """One entry written by ``credential`` (a dev credential sees all)."""
    store = make_store()
    draft = EntryDraft(kind=Kind.FACT, summary="s", author=credential.user_id, agent="a")
    written = await WriteService(store, make_embedder()).write_and_relate(
        draft, writer=credential.visibility()
    )
    return store, written.entry.id


# -- the Credential predicate and the downgrade ---------------------------


@pytest.mark.parametrize(
    ("credential", "stored"),
    [
        (dev_credential(), False),
        (Credential(user_id="admin", is_admin=True), True),
        (Credential(user_id="org", is_org=True, access_controlled=True), True),
        (
            Credential(
                user_id="alice",
                agent_name="alice",
                access_controlled=True,
                trust_level=TrustLevel.LURKER,
            ),
            True,
        ),
    ],
)
def test_is_stored(credential: Credential, stored: bool) -> None:
    assert credential.is_stored is stored


def test_effective_downgrades_a_dev_mode_credential() -> None:
    original = dev_credential()
    downgraded = original.effective(require_stored_keys=True)
    assert downgraded is not original
    assert downgraded.user_id == "dev"
    assert downgraded.access_controlled is True
    assert downgraded.trust_level is TrustLevel.UNTRUSTED
    assert downgraded.is_admin is False
    assert downgraded.is_org is False
    assert downgraded.agent_name is None
    assert downgraded.agent_id == "hivemind-mcp"  # attribution survives
    # The stand-in sees nothing: level 0 on the trust matrix.
    assert downgraded.visibility().level is TrustLevel.UNTRUSTED


def test_effective_is_identity_when_off() -> None:
    credential = dev_credential()
    assert credential.effective(require_stored_keys=False) is credential


@pytest.mark.parametrize(
    "credential",
    [
        Credential(user_id="admin", is_admin=True),
        Credential(user_id="org", is_org=True, access_controlled=True),
        Credential(
            user_id="alice",
            agent_name="alice",
            access_controlled=True,
            trust_level=TrustLevel.PRIVILEGED,
        ),
    ],
)
def test_effective_keeps_stored_credentials(credential: Credential) -> None:
    assert credential.effective(require_stored_keys=True) is credential


# -- the service seam (build_services) ------------------------------------


async def test_dev_mode_feedback_is_not_found_when_stored_keys_required() -> None:
    store, entry_id = await _write_entry(dev_credential())
    service = FeedbackService(store, make_search_config(), require_stored_keys=True)
    with pytest.raises(EntryNotFound):
        await service.record_feedback(dev_credential(), entry_id, Verdict.HELPFUL)


async def test_dev_mode_feedback_works_when_flag_off() -> None:
    store, entry_id = await _write_entry(dev_credential())
    service = FeedbackService(store, make_search_config())
    # A second dev credential: the entry's own author cannot rate it (ADR 0065).
    outcome = await service.record_feedback(
        Credential(user_id="dev2", agent_id="other"), entry_id, Verdict.HELPFUL
    )
    assert outcome.feedback.entry_id == entry_id


async def test_build_services_propagates_the_flag() -> None:
    store, entry_id = await _write_entry(dev_credential())
    services = build_services(
        store, make_embedder(), make_search_config(), require_stored_keys=True
    )
    with pytest.raises(EntryNotFound):
        await services.governance_service.record_feedback(
            dev_credential(), entry_id, Verdict.HELPFUL
        )
