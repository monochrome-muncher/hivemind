"""Admin fleet management: create, rename, list, merge, move and undo."""

from __future__ import annotations

from fastapi import APIRouter

from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep, NameParam
from hivemind.api.routes.shared import (
    require_admin,
)
from hivemind.api.schemas import (
    CreateFleetRequest,
    FleetMergeOut,
    FleetMergePreviewOut,
    FleetMergeRecordOut,
    FleetMergeRequest,
    FleetMoveOut,
    FleetMovePreviewOut,
    FleetMoveRequest,
    FleetOut,
    FleetUnmergeOut,
    RenameFleetRequest,
)

router = APIRouter(prefix=API_PREFIX)


@router.post("/admin/fleets", response_model=FleetOut, status_code=201)
async def create_fleet(
    payload: CreateFleetRequest, credential: CredentialDep, app: AppDep
) -> FleetOut:
    """Create a named fleet (admin-gated, ADR 0012)."""
    fleet = await app.access_service.create_fleet(payload.name, credential)
    return FleetOut.from_fleet(fleet)


@router.patch("/admin/fleets/{fleet_id}", response_model=FleetOut)
async def rename_fleet(
    fleet_id: NameParam, payload: RenameFleetRequest, credential: CredentialDep, app: AppDep
) -> FleetOut:
    """Rename a fleet (admin-gated, audited ``fleet.rename``, ADR 0109).
    The id, pins, counters and entries are unchanged; a merged fleet can
    be renamed; the current name again is a 200 with no audit row. 404
    unknown fleet, 409 ``fleet_exists`` when another fleet has the name,
    422 for a name a new fleet could not have."""
    fleet = await app.access_service.rename_fleet(fleet_id, payload.name, credential)
    return FleetOut.from_fleet(fleet)


@router.get("/admin/fleets", response_model=list[FleetOut])
async def list_fleets(
    credential: CredentialDep, app: AppDep, include_merged: bool = False
) -> list[FleetOut]:
    """List fleets (admin-gated, ADR 0012). Fleets merged into another
    are left out unless ``?include_merged=true`` (ADR 0107)."""
    fleets = await app.access_service.list_fleets(credential, include_merged=include_merged)
    return [FleetOut.from_fleet(f) for f in fleets]


@router.post(
    "/admin/fleets/{source_id}/merge",
    response_model=FleetMergePreviewOut | FleetMergeOut,
)
async def merge_fleet(
    source_id: NameParam, payload: FleetMergeRequest, credential: CredentialDep, app: AppDep
) -> FleetMergePreviewOut | FleetMergeOut:
    """Merge a fleet into another or into a new one (admin-gated, audited
    ``fleet.merge``, ADR 0107): its memories, agents and kept pins move
    in one transaction, with no key change. Name exactly one of
    ``target_fleet_id`` and ``new_fleet_name``. ``dry_run: true`` changes
    nothing and answers the preview (counts, both fleets' pins, likely
    duplicate pairs). More than 10 pins across the two fleets need
    ``keep_pin_ids`` (422); ``expected_pin_ids`` (the pins the preview showed)
    refuses the merge when they changed since (409 ``merge_refused``). 404 unknown fleet, 409 ``fleet_merged`` /
    ``merge_refused`` / ``fleet_exists``."""
    require_admin(credential, "merging fleets")
    merges = app.access_service.merges
    if payload.dry_run:
        preview = await merges.preview(
            source_id,
            credential,
            target_fleet_id=payload.target_fleet_id,
            new_fleet_name=payload.new_fleet_name,
        )
        return FleetMergePreviewOut.from_preview(preview)
    outcome = await merges.merge(
        source_id,
        credential,
        target_fleet_id=payload.target_fleet_id,
        new_fleet_name=payload.new_fleet_name,
        keep_pin_ids=payload.keep_pin_ids,
        expected_pin_ids=payload.expected_pin_ids,
    )
    return FleetMergeOut.from_outcome(outcome)


@router.post(
    "/admin/fleets/{source_id}/move",
    response_model=FleetMovePreviewOut | FleetMoveOut,
)
async def move_fleet_agents(
    source_id: NameParam, payload: FleetMoveRequest, credential: CredentialDep, app: AppDep
) -> FleetMovePreviewOut | FleetMoveOut:
    """Move agents and the ``fleet`` memories they wrote there to another
    fleet or a new one (admin-gated, audited ``fleet.move_agents``, ADR
    0112), in one transaction and with no key change. Name exactly one of
    ``target_fleet_id`` and ``new_fleet_name``. A memory moves when its
    author is a mover and the head of its supersession chain moves too;
    its pins follow. ``dry_run: true`` changes nothing and answers how
    many memories leave, who stays and loses access, and the pins in play.
    The pin fields work as in a merge (422 over 10 pins without
    ``keep_pin_ids``; ``expected_pin_ids`` refuses when they changed).
    404 unknown fleet, 409 ``fleet_merged`` / ``move_refused`` (also an
    agent that is not in the fleet, named in the message) /
    ``fleet_exists``. Undo is the merge undo route."""
    require_admin(credential, "moving agents between fleets")
    merges = app.access_service.merges
    if payload.dry_run:
        preview = await merges.preview_move(
            source_id,
            payload.agents,
            credential,
            target_fleet_id=payload.target_fleet_id,
            new_fleet_name=payload.new_fleet_name,
        )
        return FleetMovePreviewOut.from_preview(preview)
    outcome = await merges.move(
        source_id,
        payload.agents,
        credential,
        target_fleet_id=payload.target_fleet_id,
        new_fleet_name=payload.new_fleet_name,
        keep_pin_ids=payload.keep_pin_ids,
        expected_pin_ids=payload.expected_pin_ids,
    )
    return FleetMoveOut.from_outcome(outcome)


@router.get("/admin/fleet-merges", response_model=list[FleetMergeRecordOut])
async def list_fleet_merges(credential: CredentialDep, app: AppDep) -> list[FleetMergeRecordOut]:
    """Recorded fleet merges and moves (``kind``), newest first
    (admin-gated, ADR 0107, 0112), each with whether it can be undone now."""
    require_admin(credential, "the fleet merges")
    views = await app.access_service.merges.list_merges(credential)
    return [FleetMergeRecordOut.from_view(v) for v in views]


@router.post("/admin/fleet-merges/{merge_id}/undo", response_model=FleetUnmergeOut)
async def undo_fleet_merge(
    merge_id: NameParam, credential: CredentialDep, app: AppDep
) -> FleetUnmergeOut:
    """Undo a fleet merge or move (admin-gated, audited ``fleet.unmerge`` /
    ``fleet.move_undo``, ADR 0107, 0112): the recorded entries and agents
    still in the target go back, dropped pins are restored where they fit.
    Only the newest standing merge or move involving the target (and, for
    a move, its source) can be undone (409 ``merge_refused`` /
    ``move_refused``)."""
    require_admin(credential, "undoing a fleet merge")
    outcome = await app.access_service.merges.undo(merge_id, credential)
    return FleetUnmergeOut.from_outcome(outcome)
