# Vouch (engine)

Architectural + functional conformance for pull requests. Stdlib Python; run from the repo root:

    PYTHONPATH=tools/vouch python3 -m vouch infer .  --out .vouch/cache/architecture.json
    PYTHONPATH=tools/vouch python3 -m vouch review . --base origin/master --head HEAD --ticket .vouch/tickets/<KEY>.json --markdown

`.github/workflows/vouch.yml` runs this on every pull request and posts one sticky comment.
Architect rules live in `.vouch/rules.json`; tickets the reviewer can read in `.vouch/tickets/`.
