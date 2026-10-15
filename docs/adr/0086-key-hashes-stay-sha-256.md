# Key hashes stay SHA-256 (the KDF upgrade is held)

`key_hash` (`store/auth.py`) stores raw API keys as a SHA-256 hex digest, and
every stored `key_hash` row is one. Code-scanning alert #5 (CodeQL "use of a
broken or weak cryptographic hashing algorithm on sensitive data") wants a KDF
(PBKDF2/argon2) there. A shippable fix existed — PR #179: PBKDF2-HMAC-SHA256
at 100,000 iterations with a deployment pepper, config- and doc-integrated —
and was held back on operational cost, not on technical grounds.

## Why the cost is real

Raw keys are returned to the caller exactly once and never stored (SPEC §8.1).
The stored hash is a one-way commitment: change the KDF and no stored hash
matches any raw key again, with no migration path that does not re-issue
keys. Adopting a KDF is therefore a fleet-wide key-rotation event in every
deployment (admin, org and every agent key).

## Why the security benefit is small

API keys are `hm_` + 32 bytes of `secrets.token_urlsafe` — 256 bits of
uniform randomness. Against such a keyspace:

* **Brute force** is infeasible at any practical hash rate; a KDF's
  iteration multiplier only makes "infeasible" more infeasible.
* **Precomputation** (rainbow tables) needs structure in the keyspace —
  wordlists, short tokens, low entropy. Uniform 256-bit random has none, so
  tables do not apply.

CodeQL's rule is tuned for password-like secrets, where both attacks are
real. It over-flags here.

## Decision

1. **SHA-256 stays the stored key hash.** The fingerprint (first 12 hex
   characters; SPEC §12.5, ADR 0027) is unchanged.
2. **Reopen when key entropy ever drops** — keys derived from low-entropy
   input, shortened keys, or a structured keyspace. Then adopt a KDF together
   with a key-rotation plan (re-issue admin/org/agent keys; agents pick up
   their new key on next issuance).
3. **Alert #5 is an accepted finding.** PR #179 is closed unmerged; the
   code-scanning alert is dismissed with a reference to this ADR so Copilot
   Autofix does not re-open it as a draft.

## Consequences

* No rotation; deployments are untouched.
* The docs on `main` (SPEC.md, `keys.py`, `audit.py`, ops-runbook) say
  "SHA-256", which is accurate under this decision; no doc change needed.
  (The "stored key hash" rewording that shipped on PR #179's branch is
  moot with it closed.)
* If a future audit asks "why not a KDF?", this ADR is the answer: the
  rotation cost is real, the benefit for 256-bit random keys is not.
