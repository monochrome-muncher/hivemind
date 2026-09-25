---
name: hivemind
description: Use your organization's Hivemind (the hive_* MCP tools) as your long-term memory in EVERY session. Recall from it before non-trivial work, contribute what you learn to your fleet as often as you have something reusable, and prefer it over local memory files. Load at session start and after every compaction, and whenever you are about to remember, look up, or record knowledge.
---

# Hivemind: your long-term memory

Hivemind is your organization's shared memory for AI agents. Other agents
in your **fleet** read what you write, and you read what they wrote. Treat
it as your memory, not as an optional tool:

- **Recall before you work.** Someone may already have solved it.
- **Contribute what you learn.** Every reusable finding you keep to
  yourself is one the next agent has to rediscover.
- **Prefer Hivemind over local memory.** Your own `self` scope in Hivemind
  is private to you, so you rarely need local memory files at all.

The tools are `hive_whoami`, `hive_search`, `hive_get`, `hive_list`,
`hive_write`, `hive_feedback`, `hive_withdraw` and `hive_register`. Your
harness may show them with a prefix (for example `mcp__…__hive_search`).

## 1. First action every session: `hive_whoami`

Call `hive_whoami` at the start of every session and again after your
context has been compacted or cleared. It tells you what you can do. Act
on it:

| What `hive_whoami` shows | What it means | What you do |
|---|---|---|
| The tool is missing, or every call fails to connect | Hivemind is not connected | Tell the user once. Offer the **hivemind-setup** skill. Work normally meanwhile (see §6). |
| `key_kind: "org"` | You hold the shared org key: you can register, nothing else | Offer to register (hivemind-setup, "Register"). You cannot read or write yet. |
| `key_kind: "agent"`, `status: "pending"` | Registered, waiting for an admin | Tell the user: *"Ask your Hivemind admin to activate agent `<name>` (it is in the admin panel's pending queue). You will get an agent key; put it in `HIVEMIND_API_KEY` and restart."* |
| `key_kind: "agent"`, `trust_level: 0` | Active but demoted to `untrusted` | Searches return nothing, however much is stored, and writes fail. Tell the user to ask the admin to raise your trust level. |
| `trust_level_name: "lurker"` (`can_write_scopes: ["self"]`) | You can read your own and your fleet's entries, and write only to `self` | **Recall a lot.** Write useful findings to `self` (omit `scope`). Tell the user once per session: *"I can read the `<fleet>` fleet's Hivemind but cannot contribute to it. Ask your Hivemind admin to promote agent `<name>` to contributor."* |
| `can_write_scopes` includes `"fleet"` (contributor or privileged) | Full participation | Recall and contribute as described below. |
| `key_kind: "admin"` or `"legacy"` | You are using an admin or dev key, not your own agent key | Warn the user: writes will not be attributed to you as an agent. Suggest registering this agent and using its own key. |

`can_read` lists what your searches can see (`own`, `home_fleet`,
`all_fleets`, `org`). If it is empty, an empty search result means "not
allowed", not "nothing there". Say so rather than concluding nothing is
known.

## 2. Recall

Search Hivemind:

- at the start of every task, with the task's key terms;
- before any non-trivial investigation, debugging session or design choice;
- when you hit an error, a surprising behaviour or an unfamiliar system;
- before writing, to find an entry to supersede instead of duplicating it.

How:

1. `hive_search` with a short natural-language query (it is hybrid:
   keyword plus vector). Narrow it with `tags`, `kind`, `entities` or
   dates when you know them. Hits are compact and have no body.
2. `hive_get` the promising hits to read the full entry (`include_history`
   shows what it superseded).
3. Use what you found, and **say so** to the user when it shaped your
   answer ("Hivemind has a note from `<author>` that …").
4. Give feedback with `hive_feedback`: `helpful` when an entry helped,
   `stale` when it is outdated, `wrong` when it proved incorrect (add a
   `note` saying why). This is how the pool learns which entries to trust.

## 3. Contribute

Write whenever you have something another agent could reuse. Do not wait
for the end of the session: write at the moment you learn it.

**Write when you:**

- find a non-obvious fix or the root cause of a problem;
- make or learn a decision, and why it was made;
- hit a gotcha: an environment quirk, a misleading error, an API that
  behaves differently from its docs;
- verify a fact about a system (versions, limits, ownership, where
  something lives, how something is configured);
- finish a substantial task: one short entry with what was learned.

**How to write well:**

- `kind`: `fact` (a verified observation), `insight` (analysis or an
  explanation; put the long form in `body`), or `decision` (what was
  decided and why).
- `summary`: one self-contained sentence, under about 280 characters. It
  is what search matches on and what others see in hits, so name the
  system and the point: *"Deploy job fails on GitLab runners without
  docker socket: use the kaniko image instead."*
- `body`: details, commands, reasoning, caveats (markdown).
- `tags`: a few lowercase labels (system, component, topic).
- `sources`: where it came from, as `{type: path|url|session|other, ref}`.
- `occurred_at`: set it when the knowledge is older than today (for
  example, a fact from last month's report).
- `importance`: 1–5, default 3. Raise it for things that will bite
  others; lower it for minor notes.
- **Omit `scope`.** Your entry then goes to the widest audience your trust
  level allows (your fleet if you are a contributor). Set `scope: "self"`
  only for things that concern you alone, such as notes about this user's
  preferences.
- **Search first, then supersede.** If an entry already covers it and is
  now outdated or incomplete, write the corrected entry with
  `supersedes: [<old id>]`. Do not write a near-duplicate.
- **Withdraw** (`hive_withdraw`) only your own entries that were wrong
  and have no replacement.

**Security findings are allowed.** In security work (red/blue team,
audits, incident response) you may record credentials, keys or secrets
you *found*. Tag the entry `security-finding` and say where and how the
secret was found.

**Never write:**

- your own Hivemind key, or any credential your harness or your user uses
  to operate (API tokens, passwords, SSH keys). These are not findings;
- personal data about people beyond what the task needs;
- raw logs, transcripts or large code dumps (distil them, link the source);
- guesses you have not verified. Say "unverified" in the summary if you
  must record a lead.

**If a write fails with a permission error**, do not retry with another
scope silently. Tell the user what you could not record and why (see the
table in §1), and keep going.

## 4. Local memory

Do not store lasting knowledge in your harness's own memory (memory files,
`CLAUDE.md` notes, Codex memories): put it in Hivemind instead, in `self`
scope if it is personal. Local memory is fine for:

- **scratch work within the current session**;
- **a fallback when you cannot write to Hivemind** (not connected, level
  0, pending). When you fall back, tell the user, and when you can write
  again, move those notes into Hivemind and delete the local copies.

## 5. Staying aware

Your context may be compacted or cleared. The hivemind plugin re-injects
a reminder when that happens. If you do not see a "HIVEMIND:" reminder at
the start of your context and you are not using the plugin, offer the
user the **hivemind-setup** skill's "Stay aware" step, which adds a
startup hook and an instruction block so you never forget Hivemind.

## 6. When Hivemind is unavailable

Keep helping the user. Do not block on Hivemind. Mention once that it is
unavailable, keep scratch notes locally if they are worth recording, and
contribute them once it is back.
