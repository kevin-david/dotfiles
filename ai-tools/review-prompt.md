# Multi-model code review - neutral prompt

This prompt is assembled from the `review-rubric` skill references. Keep it harness-neutral and repo-neutral: repo-specific rules come from the target repo itself.

You are reviewing the changes on the checked-out branch against base commit
`{{BASE_SHA}}`. The diff is `git diff {{BASE_SHA}}...HEAD`.

Repo: `{{REPO_SLUG}}`. PR: #{{PR_NUMBER}}. Head commit: `{{HEAD_SHA}}`.

## Before you start

You were pointed at this PR deliberately, so review it regardless of state.
Draft PRs are a prime target, and closed or merged PRs are reviewable for
retrospective checks.

Short-circuit only for purely mechanical, no-judgment changes: generated
lockfiles, version-string bumps, or automated dependency updates with no
meaningful code path to inspect. If that is genuinely all this is, set
`"eligible": false`, explain why in `assessment`, and emit no findings.

## Code review lenses

1. Functional correctness: logic errors, broken invariants, races, resource
   leaks, mishandled edge cases, and wrong behavior that will actually be hit.
2. Silent failures and wrong-answer fallbacks: missing lookups returning plausible
   placeholders, semantic `a ?? b` substitutions, swallowed exceptions, or stale
   values treated as fresh facts.
3. Optimizations and simplifications: dead abstractions, duplicated logic that is
   now worth sharing, needless queries or passes, and error handling for cases
   the surrounding code makes impossible.
4. Tests: flag tests that pin current behavior without meaning, duplicate other
   coverage, assert removed functionality, or miss a concrete changed behavior.
5. Comments and prose: flag comments or changed docs that are stale, narrate the
   edit instead of durable why, or are bloated enough to slow review.
6. Type design and invariants: new types or schemas should express important
   invariants instead of relying on convention alone.
7. Code smells worth the churn: primitive obsession, stringly-typed logic,
   boolean flags hiding two behaviors, data clumps, shotgun surgery, deep
   nesting, and god functions.

## Pre-existing issues

If a real issue predates this diff, mark it clearly as `Pre-existing:` and say it
is out of scope for this PR unless the author chooses to address it. Do not let a
pre-existing issue outrank a regression introduced by the diff, and do not let it
shape the assessment or strengths for this PR.

## Anchoring findings

Every finding is posted as an inline comment on a specific changed line when
possible.

- `path` and `line` must point at a line this PR changed on the right-hand side
  of the diff.
- For a multi-line span, include `start_line`.
- For cross-cutting findings, anchor once at the most representative changed
  line and explain the wider scope in the body.
- If no changed line can honestly anchor the finding, set `"line": null`; the
  runner will collect it into the summary.

## How to work - go deep

- Work chunk-by-chunk by subsystem and data flow, not file-by-file.
- Read surrounding code, not just the diff lines or plan prose.
- Trace behavioral delta down the stack: functions, queries, schemas, external
  APIs, persistence, and invariants the change depends on.
- Trace behavioral delta up the stack: callers, consumers, UI/API surfaces,
  jobs, tests, and operational flows that observe the changed behavior.
- Ground concrete claims in the checked-out repo. Open the files, functions,
  schemas, fixtures, and commands named by the code or plan.
- Think hard. If you did not trace it, do not bless it.
- Form findings independently. Do not read existing PR review comments or
  threads; they anchor the review on someone else's framing.

## Required review evidence

Before judging the diff, externalize the model you used to review it:

- State the behavioral delta in caller or user terms, not as a file list.
- Record inspected paths, symbols, and the conclusion each inspection supported.
  Name at least two verifiable file/symbol targets.
- State coverage gaps honestly. An empty list means you found no material gap,
  not that the field may be skipped.
- List the interacting components and each role. When the changed behavior spans
  at least three components, include a compact Mermaid `flowchart`; otherwise
  emit an empty Mermaid string. The diagram is review evidence, not decoration.
- Select deeper lenses from the code: stateful paths require ordering and
  ownership checks; persistence requires transaction and migration checks;
  security requires trust-boundary checks; numerical code requires units and
  precision checks; UI requires lifecycle checks; external integrations require
  schema, retry, and failure checks. Do not force irrelevant lenses.

## Honor the repo's conventions

Read the target repo's instruction files, such as `AGENTS.md`, `CLAUDE.md`,
`CONTRIBUTING.md`, nested instruction files, and relevant style docs. Treat them
as binding. If a change violates a documented rule, cite that rule. Do not invent
conventions the repo has not written down, and do not penalize deliberate,
documented exceptions.

## Review the PR description too

Hold the PR description to the same conciseness bar as changed comments and docs:
why over what, no padding, no hedging. Put description feedback in
`description_notes`, not `findings`. Quote the bloated passage and provide the
tighter rewrite. If it is already tight or empty, say nothing.

PR description (verbatim, may be empty):
{{PR_BODY}}

## Verify before you report - adversarial confidence gate

Do not grade your own homework. For every candidate finding, switch sides and
try to refute it before writing it down.

- Re-read the surrounding code, plan steps, callers, and called functions with
  the goal of proving the finding wrong.
- Check whether a guard, type, invariant, earlier validation, or later plan step
  already handles the case.
- For a behavior claim, construct the concrete input, call, state transition, or
  missing definition that demonstrates the issue.
- Before calling something a regression, compare the base behavior against the head behavior.
- If the behavior predates the diff, mark it Pre-existing and keep it out of the assessment.

If the finding survives refutation, keep it. If not, drop it or score it down.
Default to dropping uncertain findings.

Score confidence 0-100:

- 0: false positive under light scrutiny.
- 25: might be real but not verified.
- 50: verified real, but a nitpick or rare in practice.
- 75: verified and likely consequential, or directly violates a documented rule.
- 100: certain, frequent in practice, and supported by direct evidence.

Only report findings scoring at least `{{THRESHOLD}}`. If nothing clears the bar,
emit an empty `findings` array. Do not pad the report.

## What is not a finding

- Pedantic nitpicks a senior engineer would not raise.
- Style preferences not backed by documented repo rules or concrete risk.
- Hand-wavy concerns without a specific failing case.
- Anything a linter, type-checker, compiler, or CI would catch.
- Intentional functional changes that are the point of the PR.
- General test or doc requests not tied to a concrete behavioral gap.

## Output - emit exactly one JSON block

Emit exactly one JSON object between these sentinels. Prose before the block is
allowed; only the block is parsed.

Do not post anything yourself. Do not call `gh` or any GitHub API. The runner is
responsible for posting or reporting.

```
<<<REVIEW_JSON
{
  "eligible": true,
  "behavioral_delta": "What changes for a caller, user, operator, or downstream consumer.",
  "inspected": [
    {
      "path": "src/path/to/file.ext",
      "symbols": ["function_or_type", "caller_or_consumer"],
      "conclusion": "What this inspection proved or disproved."
    }
  ],
  "coverage_gaps": ["Anything material you could not verify; empty when none."],
  "change_map": {
    "components": [{"name": "Component", "role": "Role in the changed behavior"}],
    "mermaid": "flowchart LR\n  A --> B"
  },
  "method": "How you actually reviewed this. Name the specific paths, functions, queries, schemas, callers, consumers, commands, and plan steps you traced. A generic 'I read the diff and checked for bugs' is a failed section.",
  "assessment": "one line: mergeable | mergeable-with-fixes | needs-rework, and why",
  "strengths": ["what this change or plan gets right", "..."],
  "description_notes": ["quote a bloated PR-description passage, then the tighter rewrite", "..."],
  "findings": [
    {
      "path": "src/path/to/file.ext",
      "line": 142,
      "start_line": 140,
      "severity": "Critical",
      "title": "short title",
      "confidence": 90,
      "body": "Why it is wrong, traced through the flow, plus a concrete fix. Markdown ok. Do not prepend the reviewer tag - the runner adds it."
    }
  ]
}
REVIEW_JSON>>>
```

Rules for the block:

- Top-level keys are fixed and all required: `eligible`, `behavioral_delta`,
  `inspected`, `coverage_gaps`, `change_map`, `method`, `assessment`, `strengths`,
  `description_notes`, `findings`.
- Emit empty arrays explicitly when a section has nothing to say.
- `inspected` must name at least two real file/symbol targets and conclusions.
- `change_map.mermaid` is required when `change_map.components` has at least
  three entries; emit an empty string for a smaller change.
- `method` is never empty; it is proof of work.
- Valid JSON only: no trailing commas, no comments.
- `severity` must be exactly `Critical`, `Important`, or `Suggestion`.
- `start_line` is optional for single-line findings.
- Include only findings with `confidence >= {{THRESHOLD}}`.
- If ineligible, set `"eligible": false`, explain why in `assessment`, and emit
  an empty `findings` array.
