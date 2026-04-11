# bot-simple-heuristics

Simple heuristic Kriegspiel bot.

## What it does

- registers with the Kriegspiel API
- authenticates with a bot bearer token
- polls assigned games in a single process
- keeps one open human-joinable lobby game advertised when it can
- can join another bot's waiting lobby game once per minute with `10%` probability
- caps itself at `5` active games in parallel
- if the opponent just captured, it immediately tries to recapture on that square
- if a pawn can promote, it prefers promotion to queen
- otherwise it uses a geometric piece-selection fallback:
  - `50%` chance to ask any pawn captures when available
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

## Configuration

- `KRIEGSPIEL_AUTO_CREATE_LOBBY_GAME=true|false`
- `KRIEGSPIEL_AUTO_CREATE_RULE_VARIANT=berkeley|berkeley_any`
- `KRIEGSPIEL_AUTO_CREATE_PLAY_AS=white|black|random`
- `KRIEGSPIEL_SUPPORTED_RULE_VARIANTS=berkeley|berkeley_any`
- `KRIEGSPIEL_MAX_ACTIVE_GAMES=5`
- `BOT_GAME_PICK_PROBABILITY=0.1`
- `ASK_ANY_PROBABILITY=0.5`

Bot-vs-bot join sampling follows the same once-per-minute rule as the other main bots.

## systemd

A production host can run the bot as a service with `deploy/kriegspiel-simple-heuristics-bot.service`.
