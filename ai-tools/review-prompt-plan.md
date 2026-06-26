# Multi-model plan review - neutral prompt

This prompt is assembled from the `review-rubric` skill references. Keep it harness-neutral and repo-neutral: repo-specific rules come from the target repo itself.

You are reviewing the changes on the checked-out branch against base commit
`{{BASE_SHA}}`. The diff is `git diff {{BASE_SHA}}...HEAD`.

Repo: `{{REPO_SLUG}}`. PR: #{{PR_NUMBER}}. Head commit: `{{HEAD_SHA}}`.

What's under review is a *plan*, not code. The diff is one or more design,
specification, or implementation-plan documents describing changes someone is
about to make. Judge whether executing this plan as written would produce
correct, complete, working software. Use the checked-out repository as ground
truth.

## Before you start

Review the plan fully regardless of PR state. Draft is a prime target because
catching defects before implementation is the point.

Short-circuit only for a trivial doc-only change with no plan content, such as a
typo or link update. If that is genuinely all this is, set `"eligible": false`,
explain why in `assessment`, and emit no findings.

## Plan review lenses

1. Claims that do not match the repo: wrong paths, stale line references,
   missing symbols, wrong signatures, nonexistent fixtures, or commands that will
   not run.
2. Correctness of the planned approach: logic errors, broken invariants,
   mishandled edge/empty/error cases, races, and ordering assumptions baked into
   the design.
3. Silent failures and wrong-answer fallbacks in the design: planned lookups that
   return plausible placeholders, semantic fallbacks across different fields,
   swallowed errors, or stale data treated as current.
4. Sequencing, atomicity, and migration safety: steps that will not build in
   order, clean-slate assumptions, unsafe cutovers, rollback gaps, or intermediate
   commits that strand callers.
5. Correctness-critical caution: money, math, security, odds, pricing, and sizing
   require first-principles expectations and the right human or empirical checks.
6. Test discipline: planned tests should fail before implementation, pass after,
   verify the actual surface, and avoid pinning current behavior blindly.
7. Placeholders and under-specification: `TBD`, `TODO`, `handle appropriately`,
   `similar to above`, undefined symbols, and inconsistent names or types.
8. Prose conciseness: flag real bloat in the plan only after substantive issues;
   quote the passage and provide the tighter rewrite.

## Coverage check

Walk each stated requirement in the plan or spec and identify the step that
implements it. A missing, contradictory, or undefined step is a finding when it
would block correct execution.

## Anchoring findings

Every finding should anchor to a changed line of the plan document.

- Anchor to the plan line that states the flawed step or claim, even when the
  underlying evidence is in code. Name the code file and function in the body.
- For a multi-line span, include `start_line`.
- For cross-cutting findings, anchor once at the most representative changed
  plan line and explain the wider scope in the body.
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
- For a 'this will not work' claim, construct the concrete way it fails.
- For a 'the plan forgot X' claim, search the whole plan and repo first.

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
- The fact that it is only a plan. Catching plan defects before implementation is the point.
- Re-litigating a deliberate design choice unless you can show it is incorrect, unsafe, or rule-violating.
- Vague requests for more detail or more tests not tied to a named gap.

## Output - emit exactly one JSON block

Emit exactly one JSON object between these sentinels. Prose before the block is
allowed; only the block is parsed.

Do not post anything yourself. Do not call `gh` or any GitHub API. The runner is
responsible for posting or reporting.

```
<<<REVIEW_JSON
{
  "eligible": true,
  "method": "How you actually reviewed this. Name the specific paths, functions, queries, schemas, callers, consumers, commands, and plan steps you traced. A generic 'I read the diff and checked for bugs' is a failed section.",
  "assessment": "one line: sound | sound-with-fixes | needs-rework, and why",
  "strengths": ["what this change or plan gets right", "..."],
  "description_notes": ["quote a bloated PR-description passage, then the tighter rewrite", "..."],
  "findings": [
    {
      "path": "docs/path/to/plan.md",
      "line": 142,
      "start_line": 140,
      "severity": "Critical",
      "title": "short title",
      "confidence": 90,
      "body": "What is wrong and why, traced through the real code or plan step you checked, plus the concrete fix to the plan. Markdown ok. Do not prepend the reviewer tag - the runner adds it."
    }
  ]
}
REVIEW_JSON>>>
```

Rules for the block:

- Top-level keys are fixed and all required: `eligible`, `method`, `assessment`,
  `strengths`, `description_notes`, `findings`.
- Emit empty arrays explicitly when a section has nothing to say.
- `method` is never empty; it is proof of work.
- Valid JSON only: no trailing commas, no comments.
- `severity` must be exactly `Critical`, `Important`, or `Suggestion`.
- `start_line` is optional for single-line findings.
- Include only findings with `confidence >= {{THRESHOLD}}`.
- If ineligible, set `"eligible": false`, explain why in `assessment`, and emit
  an empty `findings` array.
