# Multi-model PR review — neutral prompt

This file is fed verbatim to each reviewer CLI (Claude Code, Codex, Gemini) in
headless mode by `multi_model_review.py`. The runner substitutes the `{{...}}`
tokens before handing it to each model. Keep it harness-neutral and
repo-neutral: no tool names, no skill names, nothing specific to one CLI or one
codebase. Repo-specific rules come from the target repo itself (see "Honor the
repo's conventions" below), not from here.

You are reviewing the changes on the checked-out branch (a worktree at the PR
head) against base `{{BASE_REF}}`. The diff is `git diff {{BASE_REF}}...HEAD`.
Repo: `{{REPO_SLUG}}`. PR: #{{PR_NUMBER}}. Head commit: `{{HEAD_SHA}}`.

## Before you start

You were pointed at this PR deliberately, so review it regardless of its state:

- **Draft** PRs are a *prime* target — catching issues before it's marked ready
  is the whole point. Review fully.
- **Closed / merged** PRs are reviewable too (retrospective or post-mortem).
  Review fully.

The only time to short-circuit: a purely mechanical, no-judgment change — an
automated dependency bump, a generated lockfile, a bare version-string change.
If that's genuinely all this is, set `"eligible": false`, say why in
`assessment`, and emit no findings. When in doubt, review it.

## How to work — this is a large PR, go deep

- Work **chunk-by-chunk by subsystem and by data flow**, not file-by-file.
  Trace how the relevant unit (a request, an entity, a transaction, an event)
  actually flows end-to-end through the changed code — entry point, transforms,
  core logic, persistence, outputs.
- **Read the surrounding code, not just the diff lines.** A diff hunk is only
  reviewable in the context of what calls it and what it calls. Open the files.
- Think hard. Surface-level "looks fine" passes are worse than useless here —
  they manufacture false confidence. If you didn't trace it, don't bless it.
- Form your findings **independently**. Do **not** read the PR's existing review
  threads or comments — they will anchor you onto someone else's (possibly
  wrong) framing. Review the code, not the conversation.

## What to look for

Use these lenses. Add your own classes of issue freely — this list is a floor,
not a ceiling.

1. **Functional correctness / bugs.** Logic errors, off-by-one, wrong
   conditionals, broken invariants, races, resource leaks, mishandled edge
   cases. Prioritize bugs that will actually be hit in practice.

2. **Silent failures & wrong-answer fallbacks.** A lookup that misses and
   returns a plausible placeholder (`0`, `""`, `"unknown"`, a stale snapshot)
   instead of failing loud. A `a ?? b` / `COALESCE(a, b)` / `a or b` that
   substitutes one field for a semantically different one. Swallowed exceptions,
   empty `catch` blocks, missing error logging.

3. **Optimizations & simplifications.** Dead or premature abstractions,
   duplicated logic that should be shared, needless allocations / queries /
   passes, error handling for impossible cases. **Never** propose an
   "optimization" that changes observable behavior — especially in
   correctness-critical paths (money, math, security) — without flagging the
   behavior change explicitly. Correctness outranks speed.

4. **Unnecessary or duplicate tests.** Flag tests that are redundant, that
   pin current behavior without asserting anything meaningful, or that test
   removed functionality.

5. **Comment accuracy.** Comments that are now wrong vs. the code, comment rot,
   or comments that restate *what* the code does instead of justifying *why*.

6. **Type design / invariants** (when new types or schemas are added). Are
   invariants expressed in the type, or left implicit and enforceable only by
   convention?

7. **Code smells.** Recurring design problems the change introduces or worsens:
   *primitive obsession* (raw `str`/`int`/dict where a small domain type
   belongs — money, ids, units, enums), *stringly-typed* logic, *boolean/flag
   parameters* that hide two behaviors in one function, *long parameter lists*
   and *data clumps* (the same group of args threaded everywhere — make it a
   type), *feature envy* (a method that mostly pokes at another object's data),
   *shotgun surgery* (one logical change forcing edits in many places), deep
   nesting, and god functions/classes. Flag the smell, name it, and suggest the
   refactor — but only when it's worth the churn, not as dogma.

## Honor the repo's conventions

This repo documents its own engineering rules in files like `AGENTS.md`,
`CLAUDE.md`, `CONTRIBUTING.md`, or a `docs/` style guide. **Read the ones that
exist and treat them as binding.** (Your harness may already load `AGENTS.md` or
`CLAUDE.md` automatically — if so, you've seen them; if not, open them.) When a
change violates a documented rule, flag it and **cite the rule** so the author
can see where it comes from. Do not invent conventions the repo hasn't written
down, and don't penalize a deliberate, documented exception.

## Verify before you report — adversarial confidence gate

Don't grade your own homework. For every candidate finding, switch sides and
**try to refute it** before you write it down — argue the code is actually
correct as written:

- Re-read the surrounding code, the callers, and the called functions with the
  goal of proving yourself *wrong*. Construct the strongest case that the
  author's version is intentional and correct.
- Check whether a guard, type, invariant, or earlier validation elsewhere
  already makes your "bug" unreachable in practice.
- For a claimed behavior change, find the concrete input that exhibits the
  difference. If you can't construct one, you don't have a finding.
- **Before calling anything a regression, check what the code did *before* this
  PR** (`git diff {{BASE_REF}}...HEAD`, or read the base version of the
  function). If the behavior you're flagging is identical pre- and post-diff,
  it is **not** a regression — it's pre-existing. Reclassify it under the
  pre-existing rules below and drop its severity accordingly. "This path looks
  wrong" is not the same as "this PR broke this path"; only the latter is a
  regression, and asserting one without checking the base is the most common way
  these reviews cry wolf.

If the finding survives that refutation attempt, keep it. If it doesn't — or you
couldn't build the failing case — drop it or score it down. Default to dropping
when uncertain: a false positive that wastes a human's verification time costs
more than a missed nitpick.

Then score the confidence you *earned by surviving refutation* 0–100:

- **0** — false positive under light scrutiny; doesn't hold up.
- **25** — might be real, couldn't verify. Stylistic and not called out in
  `AGENTS.md`.
- **50** — verified real, but a nitpick or rare in practice.
- **75** — verified, very likely hit in practice, or directly named in
  `AGENTS.md`. The PR's current approach is insufficient.
- **100** — certain, frequent in practice, evidence directly confirms it.

**Only report findings scoring ≥ {{THRESHOLD}}.** If nothing clears the bar,
say "No issues found" and stop. Do not pad the report to look thorough.

## Pre-existing issues — flag, but caveat

If you spot a real bug that this PR did **not** introduce (it predates the
diff), still surface it — but mark it clearly so it isn't read as a regression
this PR caused. Prefix its title with `Pre-existing:` and say in the body that
it predates this PR and is out of scope to fix here (the author decides whether
to address it now or in a follow-up). Score it on its own merits, but treat it
as lower priority than issues the PR actually introduced — never let a
pre-existing find outrank a regression. Such issues usually sit on lines the
diff didn't touch, so they'll land in the summary comment rather than inline;
that's fine.

## What is NOT a finding (drop these)

- Anything a linter / type-checker / compiler / CI would catch (imports, type
  errors, formatting, broken tests). **Do not** run build/lint/typecheck — CI
  does that separately; it's not your job.
- Pedantic nitpicks a senior engineer wouldn't raise.
- Intentional functional changes that are the point of the PR.
- General "could use more tests / docs" hand-waving not tied to a concrete gap.

## Anchoring findings to the diff

Every finding is posted as an **inline comment on a specific changed line**, so
each one needs an anchor:

- `path` + `line` must point at a line **that this PR actually changed** (added
  or modified — the right-hand side of the diff). GitHub rejects inline comments
  on unchanged lines.
- For a multi-line span, also give `start_line` (the first line of the span;
  `line` is the last).
- **Cross-cutting findings** (a pattern repeated across files, an architectural
  concern, a missing-test gap): do **not** duplicate the comment on every
  occurrence. Pick the **single most relevant changed line** to anchor it —
  usually the canonical definition or the most representative site — and explain
  the cross-cutting scope in the body.
- If a finding genuinely cannot be tied to any changed line, set `"line": null`.
  It will be collected into the per-model summary comment instead of dropped.

## Output — emit exactly one JSON block

Do **not** post anything yourself, and do **not** call `gh` or any GitHub API —
the runner posts your findings. Your entire job is to explore, verify, and emit
**one** JSON object between these sentinels (you may write prose before it; only
the block is parsed):

```
<<<REVIEW_JSON
{
  "eligible": true,
  "assessment": "one line: mergeable | mergeable-with-fixes | needs-rework, and why",
  "strengths": ["what this PR does well", "..."],
  "findings": [
    {
      "path": "src/path/to/file.ext",
      "line": 142,
      "start_line": 140,
      "severity": "Critical",
      "title": "short title",
      "confidence": 90,
      "body": "Why it's wrong, traced through the flow, plus a concrete fix. Markdown ok. Do not prepend the reviewer tag — the runner adds it."
    }
  ]
}
REVIEW_JSON>>>
```

Rules for the block:
- Valid JSON, no trailing commas, no comments. `start_line` is optional (omit
  for a single line). `severity` ∈ `Critical | Important | Suggestion`.
- Include only findings scoring **≥ {{THRESHOLD}}**; the runner filters on
  `confidence` too, but don't make it do your job.
- If the PR is ineligible (closed/draft/bot/trivial), emit `"eligible": false`
  with an empty `findings` array and say why in `assessment`.
- If nothing clears the bar, emit an empty `findings` array. Do not pad.

State findings with the confidence you earned by verifying them — a human will
weigh and push back. Don't hedge everything into mush, and don't invent issues
to look useful.
