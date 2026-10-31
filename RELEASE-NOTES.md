# Release Notes

These notes summarize the bot runtime release history reconstructed from the
current repository state. Add a new section at the top for runtime,
deployment-facing, or user-visible bot behavior changes. Test-only and
docs-only changes do not need entries unless they affect operator workflow.

## 1.0.0 — Python 3.15

- Move the supported runtime to CPython 3.15.2 and validate the complete test suite on that interpreter.

## Current Runtime Baseline

- **Bot Identity**: `simpleheuristics`, the simple tactical heuristic bot.
- **Rulesets**: supports `berkeley`, `berkeley_any`, and `wild16`, and syncs
  supported rulesets to the backend profile before polling games.
- **Runtime Shape**: runs one process with one lightweight runner thread per
  active game, discovers up to 100 assigned active games, and caps intentional
  parallel play at 10 active games.
- **Lobby Policy**: can keep one human-joinable lobby game open and can join a
  compatible bot-created waiting game with 5% probability on a five-minute
  scan.
- **Move Policy**: tries immediate recaptures, prefers queen promotions, asks
  `Any pawn captures?` probabilistically when available, and otherwise ranks
  piece choices by longest available move with halving weights.
