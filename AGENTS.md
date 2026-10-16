# Project working rules

- Never execute real-account trades or enable live-order execution without explicit user confirmation for that action.
- Never commit API keys, secrets, access tokens, account details, API/, .env files, credentials, settings.json, databases or private exports. Do not print secret values.
- At the end of each user-requested code change, review the diff, run relevant offline checks, scan the exact staged content for secrets and commit only files belonging to that task. Do not use git add . blindly.
- After GitHub origin and local authentication are configured, push successful task commits to the current task branch. Report the commit and push result. Do not claim a push succeeded without checking it.
- Stop committing or pushing if a secret is suspected, checks fail, authentication is unavailable, or unrelated user edits cannot be separated. Ask for the missing information without exposing secrets.
- Never force-push, delete remote branches, rewrite history or change repository visibility without explicit user authorization.
- Version control work must not launch main.py, the GUI, live market clients, or order endpoints. Use offline tests with temporary data only.
- These rules govern Codex task completion; they are not an unattended background scheduler.
