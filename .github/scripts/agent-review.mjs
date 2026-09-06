#!/usr/bin/env node
/**
 * Layer 2 of docs/DELIVERY-ARCHITECTURE.md: read a pull request's diff as an
 * agent that did not write it, and report only what can break for a user or in
 * production.
 *
 * Deliberately dependency-free — it runs on the shared self-hosted runner with
 * no `npm ci` in front of it, so it uses fetch and the Anthropic HTTP API
 * directly.
 *
 * Output: JSON on --out with `findings[]` and a `markdown` summary, plus the
 * same markdown appended to the job summary. A finding that is still
 * `blocking: true` after the second look below fails the check: this script
 * exits 1. The prompt is written to make that rare on purpose: a gate that
 * fires on style is a gate people learn to bypass, and this repository's own
 * rules forbid bypassing gates.
 */

const args = new Map()
for (let i = 2; i < process.argv.length; i += 2) args.set(process.argv[i], process.argv[i + 1])

const diffPath = args.get("--diff")
const outPath = args.get("--out")
if (!diffPath || !outPath) {
  console.error("usage: agent-review.mjs --diff <file> --out <file>")
  process.exit(2)
}

const { readFileSync, writeFileSync } = await import("node:fs")
const diff = readFileSync(diffPath, "utf8")

const MODEL = process.env.AGENT_REVIEW_MODEL ?? "claude-sonnet-5"

const SYSTEM = `You review one pull request in a pharmacy price monitoring service in production.
You did not write this change. Your job is to find what will break for a user or in production.

Report ONLY defects of these kinds:
- wrong behaviour: the code does not do what the change claims, or breaks a case it used to handle
- data loss or corruption, including a refused write whose data cannot be re-entered
- a security hole: leaked credentials, a missing authorisation check, one tenant's data reachable by another
- a migration that cannot run, or that rewrites rows a running deployment still reads
- a check, gate or test that was weakened, disabled or deleted to make something pass
- a claim in the pull request description that the diff does not support

Do NOT report: naming, formatting, comment density, "could be simpler", missing tests for
code that has them elsewhere, or anything you would preface with "consider".

Mark a finding blocking ONLY if you can name the concrete case that goes wrong: the input, the
state, and the result. If you are unsure, report it non-blocking and say what you could not check.
If, while writing a finding, you conclude it is not real, leave it out rather than reporting it.
An empty findings list is a perfectly good answer for a small, correct change.`

const USER = `Pull request title: ${process.env.PR_TITLE ?? "(none)"}

Pull request description:
${(process.env.PR_BODY ?? "(none)").slice(0, 4000)}

Unified diff under review:

${diff}`

const TOOL = {
  name: "report_review",
  description: "Report the review outcome.",
  input_schema: {
    type: "object",
    properties: {
      summary: { type: "string", description: "One or two sentences: what this change does and whether it is safe to merge." },
      findings: {
        type: "array",
        items: {
          type: "object",
          properties: {
            title: { type: "string" },
            blocking: { type: "boolean" },
            file: { type: "string" },
            detail: { type: "string", description: "The concrete failing case: input, state, result." },
          },
          required: ["title", "blocking", "detail"],
        },
      },
    },
    required: ["summary", "findings"],
  },
}

// The sixth review of the pull request that introduced this file marked a
// finding `blocking: true` whose own last sentence was "this finding is
// retracted on closer check". The structured flag is what fails the job, so
// the flag — not the prose — gets a second look: each blocking finding goes to
// a fresh context that has not invested in it, with one question. What that
// pass withdraws stays in the report, labelled as withdrawn, so nothing is
// hidden; it just stops turning the check red.
const VERIFY_SYSTEM = `You audit code-review findings before they are allowed to block a merge.
You are given the diff they were raised against and each finding's own text.

A finding HOLDS only if its detail names a concrete failing case — the input, the state and the
wrong result — and the diff really contains what the finding says it contains.

A finding does NOT hold if it withdraws or retracts itself, says the problem could not be
verified, describes something the diff does not contain, or states a preference rather than a
failure. Judge each finding on its own text. Do not add findings of your own.`

const VERIFY_TOOL = {
  name: "confirm_findings",
  description: "Say which blocking findings hold.",
  input_schema: {
    type: "object",
    properties: {
      verdicts: {
        type: "array",
        items: {
          type: "object",
          properties: {
            index: { type: "integer" },
            holds: { type: "boolean" },
            reason: { type: "string", description: "One sentence." },
          },
          required: ["index", "holds", "reason"],
        },
      },
    },
    required: ["verdicts"],
  },
}

async function ask(system, user, tool) {
  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": process.env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: 4096,
      system,
      tools: [tool],
      tool_choice: { type: "tool", name: tool.name },
      messages: [{ role: "user", content: user }],
    }),
  })
  if (!response.ok) {
    return { ok: false, status: response.status, body: (await response.text()).slice(0, 500) }
  }
  const payload = await response.json()
  const block = (payload.content ?? []).find((c) => c.type === "tool_use")
  return { ok: true, input: block?.input }
}

function finish(result) {
  writeFileSync(outPath, JSON.stringify(result, null, 2))
  if (process.env.GITHUB_STEP_SUMMARY) {
    writeFileSync(process.env.GITHUB_STEP_SUMMARY, `${result.markdown}\n`, { flag: "a" })
  }
}

const first = await ask(SYSTEM, USER, TOOL)

if (!first.ok) {
  console.error(`Anthropic API returned ${first.status}: ${first.body}`)
  // A provider outage must not become a merge block — otherwise one bad hour at
  // the provider stops all delivery, which is the failure mode this whole
  // architecture exists to end. But it must never be quiet either: the review
  // that did not happen is written on the pull request in as many words, so
  // merging anyway is a decision somebody took, not something that slipped by.
  console.log("::warning::The change was NOT reviewed: the Anthropic API failed. Merging now means merging unreviewed.")
  finish({
    summary: `Ревью не выполнено: API ответил ${first.status}.`,
    findings: [],
    markdown: `### Ревью агентом\n\n**Не выполнено.** Anthropic API ответил \`${first.status}\`.\n\nПроверка пропущена намеренно, чтобы сбой провайдера не останавливал всю доставку. Но это значит, что изменение **никто не разобрал**: мерж сейчас — это мерж непроверенного. Либо дождись восстановления API и перезапусти проверку, либо скажи об этом владельцу прямо.`,
  })
  process.exit(0)
}

const result = first.input ?? { summary: "Ревью не вернуло структурированный ответ.", findings: [] }
const findings = Array.isArray(result.findings) ? result.findings : []

let verifyNote = ""
const candidates = findings.map((f, index) => ({ f, index })).filter(({ f }) => f.blocking)
if (candidates.length) {
  const list = candidates
    .map(({ f, index }) => `[${index}] ${f.title}\n${f.file ? `file: ${f.file}\n` : ""}${f.detail}`)
    .join("\n\n")
  const second = await ask(
    VERIFY_SYSTEM,
    `Blocking findings:\n\n${list}\n\nUnified diff they were raised against:\n\n${diff}`,
    VERIFY_TOOL,
  )
  if (!second.ok) {
    // Fail towards the finding: a second look that did not happen is not a
    // reason to drop what the first look found.
    verifyNote = `Перепроверка блокирующих замечаний не удалась (API ответил ${second.status}); они оставлены как есть.`
    console.log(`::warning::${verifyNote}`)
  } else {
    const verdicts = Array.isArray(second.input?.verdicts) ? second.input.verdicts : []
    for (const v of verdicts) {
      const item = candidates.find((c) => c.index === v.index)
      if (item && v.holds === false) {
        item.f.blocking = false
        item.f.withdrawn = typeof v.reason === "string" ? v.reason : ""
      }
    }
  }
}

const blocking = findings.filter((f) => f.blocking)

const lines = ["### Ревью агентом", "", result.summary ?? "", ""]
if (!findings.length) {
  lines.push("Блокирующих замечаний нет.")
} else {
  for (const f of findings) {
    const label = f.withdrawn !== undefined ? "Снято при перепроверке" : f.blocking ? "Блокирует" : "К сведению"
    lines.push(`**${label}: ${f.title}**`)
    if (f.file) lines.push(`\`${f.file}\``)
    lines.push("", f.detail, "")
    if (f.withdrawn) lines.push(`_Перепроверка: ${f.withdrawn}_`, "")
  }
}
lines.push("", `_Слой 2 из \`docs/DELIVERY-ARCHITECTURE.md\`. Блокирующих: ${blocking.length}._`)
if (verifyNote) lines.push("", `_${verifyNote}_`)

finish({ ...result, findings, markdown: lines.join("\n") })
console.log(`Findings: ${findings.length}, blocking: ${blocking.length}`)

if (blocking.length) {
  console.log(`::error::Agent review found ${blocking.length} blocking issue(s); see the job summary.`)
  process.exit(1)
}
