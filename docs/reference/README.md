# TypeSafe reference documents

These are documentation snapshots, not an installed agent skill or plugin.
Downloaded on 2026-09-20 for this project's Jev integration.

- `typesafe-guide.md`: https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md (upstream declares MIT)
- `typesafe-api.md`: https://docs.typesafe.ai/api.md
- `typesafe-score.md`: https://docs.typesafe.ai/primitives/score.md
- `typesafe-choice.md`: https://docs.typesafe.ai/primitives/choice.md

The API uses `POST https://api.typesafe.ai/v1/systemone` with a bearer token.
Score is a probability-weighted position on ordered levels, not the probability
of reaching the WikiRace goal. This project divides by `len(levels) - 1` for a
0–1 ranking score. Score and Choice must be separate requests when Choice needs
the newly computed scores. Returned `usage` and resolved model versions are
recorded in episode traces; authentication headers are never included.

Refresh these references from their sources when changing the API integration.
