# `HIVEMIND_RECENCY_FLOOR` gets an explicit spelling for "no floor"

**Amends ADR 0022.** ADR 0022 turned the SPEC §6.4 recency floor on by
default (`0.8`) and stated, as a deliberate consequence: *"there is
deliberately no env spelling for 'no floor': the unbounded form is the
defect this ADR fixes... An operator who needs the old behaviour back
sets a negligible floor (`1e-9`)."*

That consequence is the gap this ADR closes. `SearchConfig.recency_floor`
is `float | None`, and `None` is a real, tested, load-bearing value — it
is the exact baseline every ADR 0022 measurement is taken against — but
there was no environment spelling that produced it. An empty
`HIVEMIND_RECENCY_FLOOR=` raised a `pydantic.ValidationError`
(`Input should be a valid number`), so an operator reverting the floor
on a real deployment had no honest way to say "no floor" and had to
approximate it with `1e-9`.

An approximation is not the same thing as the value it approximates, and
this codebase does not otherwise ask operators to fake `None` with a
magic number: `HIVEMIND_EXTRACTOR_ENDPOINT=` (ADR 0016) is exactly
"empty means off," `HIVEMIND_EMBEDDING_API_KEY=` is exactly "empty means
no key." `recency_floor` was the one `Settings` field where the falsy
spelling was rejected instead of accepted.

## Decision

`Settings.recency_floor` gains a `field_validator(mode="before")` that
maps two spellings to `None`, case-insensitively and whitespace-trimmed:

- the **empty string** (`HIVEMIND_RECENCY_FLOOR=`) — consistent with the
  rest of `Settings`' "empty means off/none" fields;
- the literal **`none`** (`HIVEMIND_RECENCY_FLOOR=none`) — the exact word
  CONTEXT.md's glossary entry and the code comments already use for this
  value, so an operator reading either can spell it back.

Anything else — including out-of-range numbers — is left to pydantic's
normal float parsing and to `SearchConfig`'s existing `(0, 1]` check
(triggered when `Settings.search_config()` builds a `SearchConfig`).
`"null"`, `"off"`, `"nil"`, and typos are **not** accepted spellings:
picking exactly two, both already meaningful in this codebase, keeps the
grammar small instead of accumulating synonyms.

`SearchConfig` itself is unchanged — it is a plain dataclass constructed
from Python values (tests, sweeps), and `None` was always a valid literal
there. This is purely about what the *environment* is allowed to say.

## Why this doesn't reopen ADR 0022

ADR 0022's actual decision — the floor ships **on**, at **0.8**, and it
is a band not a slider — is untouched. What ADR 0022 got wrong was a
side consequence about the *environment's grammar*, not the retrieval
math. Nothing here changes `SearchConfig`'s default, the `(0, 1]`
validation for real values, or any scoring behavior; it only makes an
already-legal value ( `None` ) reachable from the deployment surface that
every other optional-off knob in `Settings` already supports.

## Interaction with the unknown-`HIVEMIND_*`-variable check (ADR 0024)

ADR 0024, landed alongside this change, rejects any `HIVEMIND_*`
environment variable that is neither a `Settings` field nor on its
exemption list. That check operates on variable **names**; this change
is about the **value grammar of one already-known field**
(`HIVEMIND_RECENCY_FLOOR`). They don't collide: `HIVEMIND_RECENCY_FLOOR=`
still names a real field, so ADR 0024's check passes it through
untouched, and *this* ADR's validator is what turns the empty value into
`None`. The two changes are complementary readings of the same
principle — the environment should be able to say exactly what it
means, no more (a stale name is rejected) and no less (a real "off"
value is expressible) — which is why they are recorded as sibling ADRs
rather than one amending the other.

## Consequences

- `config/.env.example`, the k8s ConfigMap, and SPEC §6.4 are updated to
  say the spelling exists (empty or `none`) instead of "no env spelling."
- `HIVEMIND_RECENCY_FLOOR=1e-9` remains valid (a real, if silly, floor)
  and is no longer the *recommended* way to say "no floor" — it is just
  a very small one.
- No change to `entry_score`, `SearchService`, or any scoring test that
  already exercises `recency_floor=None` via Python construction.
