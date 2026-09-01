# Contributing

1. Branch from `main` or `closure/p0-safety`.
2. Do not weaken risk gates to make a test pass.
3. Do not set live `EXEC_MODE` in compose.
4. New Python under `shared/`, `selene/`, `scripts/` must pass `ruff check` and unit tests.
5. Qualification artifacts are produced by jobs, not by editing env vars.
6. Owner-blocked items (license, live capital, PR #8/#9 merge) stay blocked.
