import { readFileSync } from "node:fs";

const textDir = new URL("../text/", import.meta.url);

function readText(name: string): string {
  return readFileSync(new URL(name, textDir), "utf8");
}

export const REMINDER: string = readText("reminder.txt");

export function skillBody(): string {
  return readText("skill.md").replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "").trim();
}

export const INSTRUCTIONS: string = `${readText("instructions.txt").trim()}\n\n${skillBody()}`;

export const VERSION: string = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
).version;
