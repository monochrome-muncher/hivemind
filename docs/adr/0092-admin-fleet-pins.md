# The admin panel shows each fleet's pins

> **Status note:** [ADR 0096](0096-contributors-pin.md): a withdrawn entry leaves the briefing, so the listing no longer shows withdrawn pins and none needs review for that.

Extends [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin-only endpoints the panel's proxy forwards) and builds on [ADR 0058](0058-pinned-fleet-briefing.md) and [ADR 0076](0076-pin-integrity.md).

Status: Accepted. Admin-only and read-only.

## Context

Agents pin up to 10 must-read entries per fleet, pins and unpins are audited, and a pin carries why it changed since it was pinned (superseded or flagged, ADR 0076). The admin panel never showed them: an operator could see "entry.pin" rows in the audit log but not what a fleet currently pins, nor which pins have gone stale. `GET /v1/pins` takes a `fleet_id`, but it is a data-plane route, which the panel's proxy must never forward (ADR 0029).

## Decision

1. **`GET /v1/admin/fleets/{fleet_id}/pins`**, admin key only (403 otherwise), answers what `GET /v1/pins?fleet_id=…` answers for the admin key: the fleet's pins, newest first, each as its current version with `changed_since_pin` and `change_reason`. An unknown or malformed fleet id is 404. The panel's proxy forwards it.
2. **The Fleets page** shows a pin count per fleet, with how many need review (superseded, flagged or withdrawn since pinned), and a "Pinned memories" card per fleet, fleets needing review first. A memory opens in the drawer.
3. **Read-only.** The admin does not pin or unpin from the panel: pins are a fleet's own choice (ADR 0058). Withdrawing a pinned entry from the drawer works as before.

## Consequences

- The page reads one request per fleet. Fleets are few and hold at most 10 pins each, so this stays small; a pool with many fleets would want a batched read.
- The pin row is a shared front-end piece (`lists.js` `pinRow`, ADR 0091), so an agent-key app could show its home fleet's pins over `GET /v1/pins` with the same row.
