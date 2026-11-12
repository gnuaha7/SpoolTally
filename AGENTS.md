# Contribution rules

- Write all generated content in English, including chat, code comments, documentation, commits, PRs, and reports. Preserve upstream names and identifiers.
- Read this file and the complete task brief before making changes. For behavior changes, use the user-approved product specification snapshot pinned by permalink, revision, and content hash. Setup, documentation, configuration, and specification-authoring tasks use their assigned task brief and applicable governing instructions, with checks suited to the change; they do not require a pre-existing product snapshot. Do not infer product behavior from local planning drafts or chat history.
- Start work only after repository prerequisites are user-merged or non-code prerequisites are reviewed and user-approved. Do not stack on an unmerged repository change unless the user explicitly authorizes it.
- The coordinator assigns each task's exact base SHA, branch, worktree, owned paths, model, and reasoning effort. Work only from that assignment. Keep the coordinator's inherited model unchanged, and use the model and effort assigned to the task.
- Keep task prompts, plans, task specifications, traceability, orchestration, reports, and publisher tooling outside this repository. Commit finished documentation, implementation code, tests, and maintained project or deployment configuration.
- Keep each task within its assigned paths and its own branch and worktree. Do not edit another task's files or shared registration without its owner's assignment. Workers must not spawn agents. Limit concurrent workers to three.
- For behavior changes, write or update focused tests from the approved specification before implementation when they can express the behavior. Verify changes through the actual HTTP API or CLI when those interfaces exist. Do not invent production tests for documentation-only work.
- Before handoff, run the checks required by the task and inspect the complete diff. Report commands, results, and any checks that could not run.
- A fresh independent reviewer must assess the exact current commit against the task brief, applicable governing instructions, and any applicable specification snapshot. For behavior changes, the snapshot must be user-approved and pinned by permalink, revision, and content hash. Setup, documentation, configuration, and specification-authoring reviews use the task brief and applicable governing instructions without requiring a pre-existing product snapshot. The author cannot review their own change. A relevant change to the commit or governing context requires renewed checks and review.
- Do not publish issues or push branches unless the task and user explicitly authorize that action. Do not merge changes, enable automatic merging, or force-push. The user alone approves and merges repository changes.

## Development workflow and worker limits

- Use Poteto Mode for development work when available. Consult Ponytail Minimal during design and before review. If a required skill is unavailable, report the limitation and follow the remaining project rules.
- Use only `gpt-6-luna` with `medium` reasoning and `fork_turns: none` for subagents. These settings override older task briefs that name maximum reasoning. Do not substitute another worker model when the required model is unavailable.
- Delegate feature implementation. Keep coordinator work focused on assignments, integration, verification, and handoff. Three concurrent workers is a ceiling, not a target. Workers must not spawn agents.
- While workers run, use native event-driven waits. Resume substantive coordination for completion, a blocker, a deadline, a quota threshold, or user input. Avoid repeated reads of partial files and unchanged status. Continue the usage and deadline checks below.
- Do not install dependencies, hooks, or background services without explicit authorization. An existing approval applies only to its named packages and environment. It does not authorize global installation.

## Execution deadlines

- Limit each assignment, including coordinator assignments, to 15 minutes. Check progress by minute 10 and prepare a checkpoint by minute 13. Stop or interrupt the assignment by minute 15. Do not retry a timed-out assignment automatically.
- Limit a complete execution cycle to 60 minutes, including setup, workers, waits, verification, and reporting. Stop dispatch at minute 50, prepare a checkpoint by minute 55, and finish before minute 60.
- Do not restart the cycle clock through a new task, agent, or context compaction. Start another cycle only after a new explicit user instruction.
- At closure, leave a durable checkpoint outside product Git with the current commit, completed work, verification, unfinished work, blockers, and the next action. The coordinator enforces deadlines while active. Do not claim that monitoring continues after the session ends.

## Shared weekly allowance

- Read available account usage at cycle start, before each dispatch, after task completion, at closure, and at least every 60 seconds while active. Use bounded waits that allow these checks.
- Measure the weekly allowance by remaining percentage: `100 - usedPercent`. Do not confuse the weekly window with a shorter usage window. Shared account usage does not provide an exact task token count.
- At or below 20% weekly remaining, begin closure immediately. Do not start agents, retries, or new reviews.
- At or below 15% weekly remaining, interrupt workers and write only a minimal local checkpoint and handoff. Aim to stop before reaching 10% remaining, which is reserved for other services.
- If usage is unavailable or the weekly window cannot be identified, checkpoint and stop. These checks depend on the available usage tool and do not reserve account capacity atomically.
