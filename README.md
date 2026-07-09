# bot-simple-heuristics

Simple heuristic Kriegspiel bot.

## What it does

- registers with the Kriegspiel API
- authenticates with a bot bearer token
- syncs its supported rulesets to the backend before polling games
- runs one bot process with one lightweight runner thread per active game
- polls assigned games from the main process and lets each runner poll/play its own game
- keeps one open human-joinable lobby game advertised when it can
- can join another bot's waiting lobby game once every five minutes with `5%` probability
- caps itself at `10` active games in parallel
- supports `berkeley`, `berkeley_any`, and `wild16`
- if the opponent just captured, it immediately tries to recapture on that square
- if a pawn can promote, it prefers promotion to queen
- otherwise it uses a geometric piece-selection fallback:
  - `50%` chance to ask any pawn captures when that action is available
  - otherwise it ranks pieces by the longest move each piece can make
  - piece choice then uses halving weights: `50%`, `25%`, `12.5%`, ...
  - once a piece is chosen, it tries that piece's moves from longest to shortest
  - if all moves for that piece fail, it chooses again from the remaining pieces

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py --register
python bot.py
```

Multiple instances should use separate env and state files:

```bash
python bot.py \
  --env-file instances/simpleheuristics.env \
  --state-file instances/simpleheuristics-state.json
```

## Configuration

- `KRIEGSPIEL_AUTO_CREATE_LOBBY_GAME=true|false`
- `KRIEGSPIEL_AUTO_CREATE_RULE_VARIANT=berkeley|berkeley_any|wild16`
- `KRIEGSPIEL_AUTO_CREATE_PLAY_AS=white|black|random`
- `KRIEGSPIEL_SUPPORTED_RULE_VARIANTS=berkeley,berkeley_any,wild16`
- `KRIEGSPIEL_MAX_ACTIVE_GAMES=10`
- `KRIEGSPIEL_ACTIVE_GAME_DISCOVERY_LIMIT=100`
- `BOT_GAME_PICK_PROBABILITY=0.05`
- `ASK_ANY_PROBABILITY=0.5`

Bot-vs-bot join sampling follows the same five-minute scan cooldown as the cheap random bots.

The main loop uses `KRIEGSPIEL_ACTIVE_GAME_DISCOVERY_LIMIT` when discovering
assigned active games, then starts one runner thread per active game. A slow
game no longer blocks other assigned games in the same process. Existing runner
threads are not stopped only because a later capped discovery response omits
them; each runner exits when its own game-state poll reports completion or
unavailability. The local JSON state file is protected by an in-process lock,
but separate bot identities or model variants should still use separate state
files.

## systemd

A production host can run the bot as a service with `deploy/kriegspiel-simple-heuristics-bot.service`.
