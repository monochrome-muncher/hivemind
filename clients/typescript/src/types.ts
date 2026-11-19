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
