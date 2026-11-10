"""Fleet pins: pin, unpin and list."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep
from hivemind.api.routes.shared import (
    pin_entry,
    unpin_entry,
)
from hivemind.api.schemas import (
    PinnedOut,
    PinOut,
    PinsOut,
    UnpinOut,
)

router = APIRouter(prefix=API_PREFIX)


@router.put("/entries/{entry_id}/pin", response_model=PinOut)
async def pin(entry_id: str, credential: CredentialDep, app: AppDep) -> PinOut:
    """Pin an active fleet entry to its fleet's briefing (ADR 0058): a
    contributor or privileged agent of that fleet or an admin. Idempotent.

    404 unknown entry, 403 not allowed, 409 inactive, 422 not a fleet
    entry, the fleet already has 10 pins or the agent 3.
    """
    return await pin_entry(app, credential, entry_id)


@router.delete("/entries/{entry_id}/pin", response_model=UnpinOut)
async def unpin(entry_id: str, credential: CredentialDep, app: AppDep) -> UnpinOut:
    """Unpin an entry (ADR 0058); ``removed`` says whether it was pinned."""
    return await unpin_entry(app, credential, entry_id)


@router.get("/pins", response_model=PinsOut)
async def pins(
    credential: CredentialDep, app: AppDep, fleet_id: Annotated[str | None, Query()] = None
) -> PinsOut:
    """A fleet's pinned entries, newest pin first (ADR 0058): the
    caller's home fleet unless ``fleet_id`` names another. Entries the
    caller may not read are left out."""
    pinned = await app.governance_service.pinned(credential, fleet_id)
    return PinsOut(
        fleet_id=fleet_id or credential.home_fleet_id,
        pins=[PinnedOut.from_pinned(p) for p in pinned],
    )
