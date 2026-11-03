# Contribution rules

- Write repository documentation and code in English.
- Read this file and the complete task brief before changing behavior. Use only the user-approved specification snapshot pinned by permalink, revision, and content hash. Do not infer requirements from local planning drafts or chat history.
- Keep task prompts, plans, task specifications, traceability, orchestration, reports, and publisher tooling outside this repository. Commit finished documentation, implementation code, tests, and maintained project or deployment configuration.
- Keep each task within its assigned paths and its own branch and worktree. Do not edit another task's files or shared registration without its owner. Workers must not spawn agents.
- For behavior changes, write or update focused tests from the approved specification before implementation when they can express the behavior. Verify changes through the actual HTTP API or CLI when those interfaces exist. Do not invent production tests for documentation-only work.
- Before handoff, run the checks required by the task and inspect the complete diff. Report commands, results, and any checks that could not run.
- A fresh reviewer must assess the exact commit and specification snapshot. The author cannot review their own change. A changed commit or specification requires renewed checks and review.
- Do not publish issues, push branches, merge, or enable automatic merging unless the task and user explicitly authorize that action. The user approves and merges repository changes.
