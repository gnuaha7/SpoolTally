# Contribution rules

- Write repository documentation and code in English.
- Read this file and the complete task brief before making changes. Use only the user-approved specification snapshot pinned by permalink, revision, and content hash. Do not infer requirements from local planning drafts or chat history.
- Start work only after repository prerequisites are user-merged or non-code prerequisites are reviewed and user-approved. Do not stack on an unmerged repository change unless the user explicitly authorizes it.
- The coordinator assigns each task's exact base SHA, branch, worktree, owned paths, model, and reasoning effort. Work only from that assignment. Keep the coordinator's inherited model unchanged, and use the model and effort assigned to the task.
- Keep task prompts, plans, task specifications, traceability, orchestration, reports, and publisher tooling outside this repository. Commit finished documentation, implementation code, tests, and maintained project or deployment configuration.
- Keep each task within its assigned paths and its own branch and worktree. Do not edit another task's files or shared registration without its owner's assignment. Workers must not spawn agents. Limit concurrent workers to three.
- For behavior changes, write or update focused tests from the approved specification before implementation when they can express the behavior. Verify changes through the actual HTTP API or CLI when those interfaces exist. Do not invent production tests for documentation-only work.
- Before handoff, run the checks required by the task and inspect the complete diff. Report commands, results, and any checks that could not run.
- A fresh independent reviewer must assess the exact current commit and specification snapshot. The author cannot review their own change. A relevant change to the commit or specification requires renewed checks and review.
- Do not publish issues or push branches unless the task and user explicitly authorize that action. Do not merge changes, enable automatic merging, or force-push. The user alone approves and merges repository changes.
