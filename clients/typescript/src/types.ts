/*
 * Option keys are snake_case on purpose: they are the wire shape, exactly the argument names the
 * hive_* tools take, so what you pass is what the server receives.
 */

export type EntryKind = "fact" | "insight" | "decision";
export type Verdict = "helpful" | "stale" | "wrong";
export type Recency = "default" | "prefer_recent" | "ignore_age";

export interface EntryFilters {
  kind?: EntryKind;
  tags?: string[];
  entities?: string[];
  scope?: string;
  author?: string;
  agent?: string;
  occurred_from?: string;
  occurred_to?: string;
  created_from?: string;
  created_to?: string;
  include_inactive?: boolean;
  flagged?: boolean;
  limit?: number;
  offset?: number;
}

export interface SearchOptions extends EntryFilters {
  explore?: boolean;
  recency?: Recency;
}

export interface ListOptions extends EntryFilters {
  relied_on_changed?: boolean;
  include_body?: boolean;
}

export interface Source {
  type: "path" | "url" | "session" | "other";
  ref: string;
}

export interface WriteOptions {
  body?: string;
  payload?: Record<string, unknown>;
  sources?: Source[];
  tags?: string[];
  occurred_at?: string;
  importance?: number;
  scope?: string;
  supersedes?: string[];
  agent?: string;
  see_also?: string[];
  check?: boolean;
}

export interface GetOptions {
  include_history?: boolean;
}

export interface FeedbackOptions {
  note?: string;
  agent?: string;
}

export interface PinOptions {
  unpin?: boolean;
}

export interface PinnedOptions {
  fleet_id?: string;
}

export interface RegisterOptions {
  owner_alias?: string;
}

export type Reply = Record<string, unknown>;

/*
 * The shapes of the tools' replies (SPEC §5.2), as the server sends them. Optional keys are the
 * ones the server leaves out when they have nothing to say. A newer server may add keys.
 */

export type EntryState = "active" | "superseded" | "withdrawn";

export interface FeedbackCounts {
  helpful: number;
  stale: number;
  wrong: number;
}

export interface Warning {
  code: string;
  message: string;
}

export interface Entry {
  id: string;
  kind: EntryKind;
  summary: string;
  author: string;
  agent: string | null;
  body: string | null;
  payload: Record<string, unknown> | null;
  sources: Source[];
  tags: string[];
  importance: number;
  importance_source: string;
  scope: string;
  fleet_id: string | null;
  state: EntryState;
  superseded_by: string | null;
  withdrawn_reason: string | null;
  withdrawn_at: string | null;
  entities: { name: string; kind: string }[];
  entities_model: string | null;
  occurred_at: string;
  created_at: string;
}

export interface Feedback extends FeedbackCounts {
  recent: { verdict: Verdict; note: string | null; reporter: string; updated_at: string | null }[];
  last_confirmed_at?: string;
}

export interface Link {
  id: string;
  kind: EntryKind;
  summary: string;
  author: string;
  state: EntryState;
  fleet_id: string | null;
}

/** `get` of one id; `successors` and `superseded` come with `include_history`. */
export interface EntryDetail extends Entry {
  feedback: Feedback;
  see_also: Link[];
  linked_from: Link[];
  successors?: Entry[];
  superseded?: Entry[];
}

/** `get` of a list of ids: the readable ones, then the ids that named nothing readable. */
export interface Entries {
  entries: EntryDetail[];
  not_found: string[];
}

/** A compact search hit, without the body: `get` it to read it. */
export interface Hit {
  id: string;
  kind: EntryKind;
  summary: string;
  tags: string[];
  author: string;
  agent: string | null;
  occurred_at: string;
  score: number;
  match: string;
  scope: string;
  fleet_id: string | null;
  similarity?: number;
  feedback?: FeedbackCounts;
  last_confirmed_at?: string;
  contested?: boolean;
  correction?: { verdict: Verdict; note: string | null; reporter: string; updated_at: string };
  supersedes?: string[];
  state?: EntryState;
  similar?: { id: string; occurred_at: string }[];
  upstream_changed?: { id: string; change: string }[];
}

/** `note` explains a `degraded` (keyword-only) search or a `closest: "loose"` one. */
export interface SearchReply {
  count: number;
  hits: Hit[];
  recency?: Recency;
  degraded?: string;
  closest?: string;
  note?: string;
}

/** With `include_body` an item is a whole `Entry`; `feedback` comes with `relied_on_changed`. */
export interface ListItem {
  id: string;
  kind: EntryKind;
  summary: string;
  tags: string[];
  author: string;
  agent: string | null;
  occurred_at: string;
  created_at: string;
  scope: string;
  fleet_id: string | null;
  state: EntryState;
  superseded_by: string | null;
  body?: string | null;
  feedback?: FeedbackCounts;
}

export interface ListReply {
  count: number;
  entries: ListItem[];
}

export interface RelatedEntry {
  id: string;
  kind: EntryKind;
  summary: string;
  author: string;
  scope: string;
  fleet_id: string | null;
  occurred_at: string;
  similarity: number;
  tags: string[];
  feedback: FeedbackCounts;
  same_author: boolean;
}

/** The stored entry, its nearest `related` entries and advisory `warnings`. */
export interface WriteReply extends Entry {
  related: RelatedEntry[];
  warnings: Warning[];
}

/** `write` with `check: true`: what a write would report, with nothing stored. */
export interface CheckReply {
  checked: true;
  related: RelatedEntry[];
  warnings: Warning[];
}

/** `fix` says how to supersede the entry when the rater may. */
export interface FeedbackReply {
  entry_id: string;
  verdict: Verdict;
  note: string | null;
  quality: number;
  feedback: {
    entry_id: string;
    user: string;
    agent: string | null;
    verdict: Verdict;
    note: string | null;
    updated_at: string | null;
  };
  fix?: string;
}

export interface BatchFeedbackReply {
  verdict: Verdict;
  note: string | null;
  results: { entry_id: string; quality: number }[];
  fix?: { may_supersede: string[]; how: string };
}

export interface FleetPhase {
  phase: string;
  until: string;
  hint: string;
}

export interface Pin {
  id: string;
  pinned_id: string;
  kind: EntryKind;
  summary: string;
  author: string;
  state: EntryState;
  pinned_by: string;
  pinned_at: string;
  changed_since_pin: boolean;
  change_reason: string | null;
}

export interface PinnedReply {
  fleet_id: string | null;
  pins: Pin[];
  fleet_phase?: FleetPhase;
}

/** Pinning adds `fleet_id`, `pinned_by` and `pinned_at`; unpinning adds `removed`. */
export interface PinReply {
  entry_id: string;
  pinned: boolean;
  fleet_id?: string;
  pinned_by?: string;
  pinned_at?: string;
  removed?: boolean;
}

export interface RegisterReply {
  name: string;
  status: string;
  trust_level: number;
  home_fleet_id: string | null;
  already_registered: boolean;
  message: string;
}

export interface WhoAmI {
  key_kind: string;
  name: string | null;
  status: string | null;
  trust_level: number;
  trust_level_name: string;
  home_fleet_id: string | null;
  home_fleet_name: string | null;
  can_read: string[];
  can_write_scopes: string[];
  inherits_from: string[];
  key_last_used_at: string | null;
  fleet_phase?: FleetPhase;
  role?: string;
  role_hint?: string;
}
