// Hivemind front-end kit: CSV export of a list.
//
// Generic like lists.js: it knows no route, key or app state. A caller gives
// columns ({header, value(row)}) and a page fetcher; the rows are fetched in
// the largest pages the API allows, written as CSV and saved by the browser.
// Nothing leaves the browser but the same API reads the list makes.

export const EXPORT_PAGE = 100; // the API's largest page (SPEC §5.3)
export const EXPORT_MAX = 10000; // the API's largest offset: the most one export reaches

// A cell that a spreadsheet would read as a formula (=, +, -, @, or a tab or
// carriage return before one) gets a leading apostrophe. Agents write the
// summaries and notes, so an export opened in a spreadsheet must never run them.
const FORMULA_START = /^[=+\-@\t\r]/;

/** One CSV cell: text quoted when it holds a comma, quote or line break. */
export function csvCell(value) {
  if (value === null || value === undefined) return "";
  let text = Array.isArray(value) ? value.join("; ") : String(value);
  if (typeof value !== "number" && typeof value !== "boolean" && FORMULA_START.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

/** CSV text (CRLF lines, RFC 4180) for `rows` under `columns`. */
export function toCsv(columns, rows) {
  const lines = [columns.map((c) => csvCell(c.header)).join(",")];
  for (const row of rows) lines.push(columns.map((c) => csvCell(c.value(row))).join(","));
  return lines.join("\r\n") + "\r\n";
}

/** Save `text` as a file named `filename` (a byte-order mark so Excel reads UTF-8). */
export function downloadCsv(filename, text) {
  const url = URL.createObjectURL(new Blob(["﻿", text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Fetch every row `fetchPage({limit, offset})` gives, up to `max`, a page of
 * `EXPORT_PAGE` at a time. Resolves to {rows, truncated}, or null when a page
 * resolves to undefined (a sign-out) or `cancelled()` turns true between pages.
 */
export async function fetchAll(fetchPage, { max = EXPORT_MAX, cancelled = () => false } = {}) {
  const rows = [];
  for (;;) {
    const page = await fetchPage({ limit: EXPORT_PAGE, offset: rows.length });
    if (!page || cancelled()) return null;
    rows.push(...page);
    if (page.length < EXPORT_PAGE) return { rows, truncated: false };
    if (rows.length >= max) return { rows: rows.slice(0, max), truncated: true };
  }
}

/** A file name like `hivemind-memories-2026-10-18.csv`; anything but letters, digits, - and _ becomes _. */
export const csvName = (what, now = new Date()) => `hivemind-${String(what).replace(/[^A-Za-z0-9_-]/g, "_")}-${now.toISOString().slice(0, 10)}.csv`;

/** A column: `header`, read from the row's field of that name unless `value` says otherwise. */
export const column = (header, value = (row) => row[header]) => ({ header, value });
