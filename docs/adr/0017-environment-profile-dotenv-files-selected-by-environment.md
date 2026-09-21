# Environment-profile dotenv files, selected by ENVIRONMENT

A single shared `.env` file was the local-dev quickstart (`cp
.env.example .env`), but it cannot express the difference between a
**staging** box and a **production** box, and it sits in the way of
the deployment story: Kubernetes/CI never read a dotenv file at all
(values come from env vars / Secrets), while a file-based deployment
staging box wants its own values without editing a shared file.

## Decision

1. **One profile file per environment, selected by `ENVIRONMENT`
   (ADR 0017 → the code's `load_settings`):** `production` →
   `.env.production`, `staging` → `.env.staging`, `test` →
   `.env.test`, anything else (including unset) → `.env.local`
   (the fallback profile). The name is normalized with
   `.strip().lower()` (so `Production` works).
2. **The master template moves to `config/.env.example`** — the repo
   root stops being a dotenv dump. The quickstart is
   `cp config/.env.example .env.local` (or the profile you need).
3. **Precedence is fixed: real env vars always win over file values.**
   A profile file can never override a deployed value (a k8s Secret
   beats a stray file), and **a missing file is silently ignored** —
   the Kubernetes/CI posture (no profile file exists; values come
   from env / Secrets) is therefore unaffected.
4. **The bare `.env` file is retired** (the class default profile is
   `.env.local`; `hivemind` never reads a bare `.env` again).

## Consequences

- `config.py` grows `load_settings()` (the factory the six console
  entry points use); bare `Settings()` still works and reads the
  `.local` profile (tests/hermetic use).
- Per-environment copies (`.env.local` / `.env.staging` /
  `.env.production` / `.env.test`) are git-ignored operator data;
  only `config/.env.example` is tracked.
- Supersedes the `.env` quickstart shipped with the deployment story
  (DEPLOY.md §2 now documents the profile files).

## Alternatives considered

- *Layered files* (`.env` + `.env.production`, both read): rejected —
  the precedence rules become surprising (two files, one winner per
  key, hard to reason about).
- *No files at all (env vars / Secrets only)*: rejected — a file-based
  staging box has no painless local workflow; the quickstart is a
  first-class part of the dev story.