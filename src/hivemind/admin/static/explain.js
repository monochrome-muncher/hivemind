// Hivemind admin panel and portal: "Explain ranking" (ADRs 0137, 0139). Pure: no DOM.
// The parts of one hit's score, as plain lines. The server's `explain` object is the source;
// this only words it.

const num = (x) => String(Number(Number(x).toPrecision(4)));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

const keywordLine = (e) => `Keyword: ${e.keyword_rank == null ? "not in the keyword results" : `rank ${e.keyword_rank}`}`;

function vectorLine(e) {
  if (e.mode === "keyword_only") return "Vector: unavailable, this answer is keyword-only";
  return `Vector: ${e.vector_rank == null ? "not in the vector results" : `rank ${e.vector_rank}`}`;
}

function memoryAge(days) {
  const whole = Math.round(days);
  return whole === 0 ? "memory date today" : `memory date ${plural(whole, "day")} ago`;
}

function recencyLine({ recency: r }) {
  if (r.hint === "ignore_age") return `Recency x${num(r.factor)} (this search ignores age)`;
  const notes = [memoryAge(r.age_days)];
  if (r.hint !== "default") notes.push(`hint ${r.hint}`);
  if (r.floor_applied) notes.push(`the ${num(r.floor)} floor applied`);
  return `Recency x${num(r.factor)} (${notes.join("; ")})`;
}

function feedbackLine({ feedback: f }) {
  const counts = [[f.helpful, "helpful"], [f.stale, "stale"], [f.wrong, "wrong"]]
    .filter(([n]) => n > 0).map(([n, word]) => `${n} ${word}`);
  if (counts.length === 0) return `Feedback x${num(f.quality)} (no feedback)`;
  if (f.flags_halved) counts.push("flags at half weight");
  return `Feedback x${num(f.quality)} (${counts.join(", ")})`;
}

const socialLine = ({ social: s }) =>
  s.cap_applied ? `Social cap x${num(s.cap)} (the feedback factor was held at the cap)` : null;

const importanceLine = ({ importance: i }) => `Importance x${num(i.factor)} (importance ${i.value})`;

/** The lines that show how `explain` (an ExplainedHitOut `explain`) added up to the score. */
export function explainLines(explain) {
  return [
    keywordLine(explain), vectorLine(explain), `Fused (RRF): ${num(explain.fused)}`,
    recencyLine(explain), feedbackLine(explain), socialLine(explain), importanceLine(explain),
    `Score ${num(explain.score)}`,
  ].filter(Boolean);
}
