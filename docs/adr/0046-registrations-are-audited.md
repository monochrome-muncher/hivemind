# Registrations are audited

Amends ADR 0027 (the audit log): its decision not to audit registration.

## Context

ADR 0027 left `POST /v1/agents` and `hive_register` out of the audit log:
registration grants no privilege (activation does, and is audited). The
2.0.0 review kept this as an open follow-up (ROADMAP §3.13): the admin
activates a pending agent with nothing on record about when it was
registered, with which key, or which owner alias it claimed, and confirms
the requester out of band.

That record is what an admin needs when a pending agent looks wrong (a
name that imitates another, an alias nobody recognises) and what an
investigation needs later (who asked for this identity, and when).

## Decision

1. **A new registration writes one audit row**: action `agent.register`,
   target the agent name, detail `{"owner_alias": ...}` when an alias was
   given. A re-registration (the idempotent `already_registered` answer)
   and a refused name (`name_conflict`, `invalid_input`) change nothing
   and write no row.

2. **A new actor kind, `org_key`**, for a registration made with the
   shared org key; the actor is `org:<key fingerprint>`. The fingerprint
   says which org key was used (useful across rotations), not which person
   or agent: every unregistered agent holds that key. A registration made
   with an admin key is an ordinary `admin_key` row (`admin:<fingerprint>`).

3. **The owner alias stays unverified.** It is the registrant's claim. The
   row records it so the admin can see what was claimed; confirming it is
   still the admin's out-of-band step (ADR 0039).

4. **Same guarantee as the other app-side rows**: the row is written after
   the agent record and not atomically with it, and a failed audit write
   fails the request (ADR 0027). The pending record then stands, and
   registering again with the same alias answers `already_registered`.

5. **Migration `0009.audit-register`** widens the two named CHECKs on
   `audit_log` (expand only). Its rollback deletes the `agent.register`
   rows, which the pre-0009 schema cannot hold, and restores the narrow
   CHECKs.

## Consequences

- The audit log answers "who registered this agent, with which key, for
  which claimed owner" without a schema change to `agents`.
- An older sibling pod that lists the audit log during the rollout does
  not know `org_key` / `agent.register` (the one-rollout window ADR 0028
  also accepted).
- The org key's holder population is unchanged; this records use, it does
  not limit it. Rate limiting registration stays at the ingress.
