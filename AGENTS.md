# Project working rules

- Never execute real-account trades or enable live-order execution without explicit user confirmation for that action.
- Never commit API keys, secrets, access tokens, account details, API/, .env files, credentials, settings.json, databases or private exports. Do not print secret values.
- At the end of each user-requested code change, review the diff, run relevant offline checks, scan the exact staged content for secrets and commit only files belonging to that task. Do not use git add . blindly.
- After GitHub origin and local authentication are configured, push successful task commits to the current task branch. Report the commit and push result. Do not claim a push succeeded without checking it.
- Stop committing or pushing if a secret is suspected, checks fail, authentication is unavailable, or unrelated user edits cannot be separated. Ask for the missing information without exposing secrets.
- Never force-push, delete remote branches, rewrite history or change repository visibility without explicit user authorization.
- Version control work must not launch main.py, the GUI, live market clients, or order endpoints. Use offline tests with temporary data only.
- These rules govern Codex task completion; they are not an unattended background scheduler.
- Follow DEVELOPMENT_ROADMAP.md in order; record the current stage and remaining validation, and never equate offline tests with real executions.
- Keep the default UI understandable to a middle-school student: plain Korean, short guided steps, and optional advanced research screens.
- Clearly separate synthetic practice, Kiwoom mock investment, real quote viewing, and actual real-money execution. Keep mode-specific data and credentials separate. Real quotes do not authorize real orders.

## Independent development and verification (2026-10-21 user approval)

- Apply this workflow to every goal in roadmap stages 0–12, priorities A–F, Theme1, and later user-approved goals. Keep the roadmap's whole-product-flow-first priority; this does not require finishing every edge case before moving to another feature.
- Assign implementation to a development agent and verification/goal assessment to a separate verification agent. The development agent may run its own checks but must not make the final completion decision for its own changes.
- Before implementation, the coordinator records the requirement IDs, scope, observable acceptance criteria, and checks required for the current milestone. Use docs/DEVELOPMENT_REVIEW_WORKFLOW.md.
- Freeze the submitted change set for independent review. The verification agent reviews that exact HEAD and diff, runs relevant offline checks independently, and checks the acceptance criteria and remaining roadmap requirements. It reports findings without directly editing product code; the development agent fixes them, then requests re-verification of the changed scope.
- The coordinator accepts or returns the milestone based on the independent report, reviews the final diff, scans the exact staged content for secrets, commits only task files, pushes the current task branch, and verifies the remote result. Developer self-tests do not replace independent verification; a later change invalidates verification of the affected content.
- Record implementation, offline verification, and real-environment validation separately. For each acceptance criterion use fulfilled, partial, unmet, or real-environment-unverified with evidence and remaining work. Passing offline tests is never proof of actual execution, broker fills, user-visible delivery, or full goal completion.
- If a separate verification agent is unavailable, record independent verification as pending and do not present the milestone as independently accepted. Keep the current safety restrictions and explicit user authorizations unchanged.
- At every milestone, save the review outcome, remaining validation, commit/push result, and next step in the continuation/handoff records. This workflow does not authorize GUI execution, real-account orders, secret uploads, or unattended scheduling.
