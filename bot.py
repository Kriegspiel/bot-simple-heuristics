"""Simple heuristic Kriegspiel bot.

This bot keeps the same small polling-loop shape as the random bots, but uses
a few cheap heuristics:

1. if the opponent just captured and a move can land on that square, try the
   recapture immediately
2. if a pawn can promote, try queen promotion first
3. otherwise choose one action source with geometric weights:
   - 50% chance to ask any pawn captures when that action is available
     (Wild 16 has no ask-any action, so its pawn tries are ordinary moves)
   - otherwise choose a piece, ranked by the longest move that piece can make
4. once a piece is chosen, try that piece's moves from longest to shortest
5. if all moves for that piece fail, choose again from the remaining pieces
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
import random
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / ".bot-state.json"
DEFAULT_ENV_PATH = BASE_DIR / ".env"
STATE_PATH = DEFAULT_STATE_PATH
ENV_PATH = DEFAULT_ENV_PATH
DEFAULT_TIMEOUT_SECONDS = 20
BOT_JOIN_COOLDOWN_SECONDS = int(os.environ.get("BOT_JOIN_COOLDOWN_SECONDS", "300"))
BOT_GAME_PICK_PROBABILITY = float(os.environ.get("BOT_GAME_PICK_PROBABILITY", "0.05"))
ASK_ANY_PROBABILITY = float(os.environ.get("ASK_ANY_PROBABILITY", "0.5"))
MAX_ACTIVE_GAMES = int(os.environ.get("KRIEGSPIEL_MAX_ACTIVE_GAMES", "10"))
DEFAULT_ACTIVE_GAME_DISCOVERY_LIMIT = 100
FAILED_MOVE_RETRY_DELAY_SECONDS = 1
SUPPORTED_RULE_VARIANTS = ("berkeley", "berkeley_any", "wild16")
DEFAULT_SUPPORTED_RULE_VARIANTS = list(SUPPORTED_RULE_VARIANTS)
LEGACY_DEFAULT_SUPPORTED_RULE_VARIANTS = ["berkeley", "berkeley_any"]
DEFAULT_AUTO_CREATE_RULE_VARIANT = "berkeley_any"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_BOT_PROFILE_SYNCED = False

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)
_STATE_LOCK = threading.RLock()


def configure_runtime_paths(*, env_path: str | Path | None = None, state_path: str | Path | None = None) -> None:
    global ENV_PATH, STATE_PATH
    ENV_PATH = Path(env_path).expanduser().resolve() if env_path else DEFAULT_ENV_PATH
    STATE_PATH = Path(state_path).expanduser().resolve() if state_path else DEFAULT_STATE_PATH


def load_env_file(path: str | Path | None = None) -> None:
    env_path = Path(path) if path is not None else ENV_PATH
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def base_url() -> str:
    return os.environ.get("KRIEGSPIEL_API_BASE", "http://localhost:8000").rstrip("/")


def auth_headers() -> dict[str, str]:
    token = os.environ.get("KRIEGSPIEL_BOT_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def bot_username() -> str:
    return os.environ.get("KRIEGSPIEL_BOT_USERNAME", "").strip().lower()


def _load_state_unlocked() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def _save_state_unlocked(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_state() -> dict:
    with _STATE_LOCK:
        return _load_state_unlocked()


def save_state(state: dict) -> None:
    with _STATE_LOCK:
        _save_state_unlocked(state)


def save_token(token: str) -> None:
    with _STATE_LOCK:
        state = _load_state_unlocked()
        state["token"] = token
        _save_state_unlocked(state)


def maybe_restore_token() -> None:
    if os.environ.get("KRIEGSPIEL_BOT_TOKEN"):
        return
    if STATE_PATH.exists():
        token = load_state().get("token")
        if token:
            os.environ["KRIEGSPIEL_BOT_TOKEN"] = token


def register_bot() -> None:
    response = requests.post(
        f"{base_url()}/auth/bots/register",
        json={
            "username": os.environ["KRIEGSPIEL_BOT_USERNAME"],
            "display_name": os.environ["KRIEGSPIEL_BOT_DISPLAY_NAME"],
            "owner_email": os.environ["KRIEGSPIEL_BOT_OWNER_EMAIL"],
            "description": os.environ.get("KRIEGSPIEL_BOT_DESCRIPTION", ""),
            "supported_rule_variants": supported_rule_variants(),
        },
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    save_token(payload["api_token"])
    logger.debug("%s", json.dumps(payload, indent=2))


def get_json(path: str) -> dict:
    response = requests.get(f"{base_url()}{path}", headers=auth_headers(), timeout=DEFAULT_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def get_public_user(username: str) -> dict:
    response = requests.get(f"{base_url()}/user/{username}", headers=auth_headers(), timeout=DEFAULT_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def post_json(path: str, payload: dict | None = None) -> dict:
    response = requests.post(
        f"{base_url()}{path}",
        headers=auth_headers(),
        json=payload or {},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def sync_bot_profile() -> dict:
    return post_json("/bots/profile", {"supported_rule_variants": supported_rule_variants()})


def maybe_sync_bot_profile() -> bool:
    global _BOT_PROFILE_SYNCED
    if _BOT_PROFILE_SYNCED:
        return False
    payload = sync_bot_profile()
    _BOT_PROFILE_SYNCED = True
    logger.info("synced bot profile: %s", payload.get("supported_rule_variants"))
    return True


def auto_create_enabled() -> bool:
    raw = os.environ.get("KRIEGSPIEL_AUTO_CREATE_LOBBY_GAME", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def create_payload() -> dict[str, str]:
    return {
        "rule_variant": auto_create_rule_variant(),
        "play_as": os.environ.get("KRIEGSPIEL_AUTO_CREATE_PLAY_AS", "random").strip() or "random",
        "time_control": "rapid",
        "opponent_type": "human",
    }


def supported_rule_variants() -> list[str]:
    raw = os.environ.get("KRIEGSPIEL_SUPPORTED_RULE_VARIANTS", ",".join(DEFAULT_SUPPORTED_RULE_VARIANTS))
    variants: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if value in SUPPORTED_RULE_VARIANTS and value not in variants:
            variants.append(value)
    if variants == LEGACY_DEFAULT_SUPPORTED_RULE_VARIANTS:
        return DEFAULT_SUPPORTED_RULE_VARIANTS.copy()
    return variants or DEFAULT_SUPPORTED_RULE_VARIANTS.copy()


def auto_create_rule_variant() -> str:
    configured = os.environ.get("KRIEGSPIEL_AUTO_CREATE_RULE_VARIANT", DEFAULT_AUTO_CREATE_RULE_VARIANT).strip()
    supported = supported_rule_variants()
    if configured in supported:
        return configured
    if DEFAULT_AUTO_CREATE_RULE_VARIANT in supported:
        return DEFAULT_AUTO_CREATE_RULE_VARIANT
    return supported[0]


def active_games(games: list[dict]) -> list[dict]:
    return [game for game in games if game.get("state") == "active"]


def waiting_games(games: list[dict]) -> list[dict]:
    return [game for game in games if game.get("state") == "waiting"]


def under_active_game_limit(games: list[dict]) -> bool:
    return len(active_games(games)) < MAX_ACTIVE_GAMES


def active_game_discovery_limit() -> int:
    raw = os.environ.get("KRIEGSPIEL_ACTIVE_GAME_DISCOVERY_LIMIT", str(DEFAULT_ACTIVE_GAME_DISCOVERY_LIMIT)).strip()
    try:
        return max(1, min(100, int(raw)))
    except ValueError:
        return DEFAULT_ACTIVE_GAME_DISCOVERY_LIMIT


def open_bot_lobby_candidates(open_games: list[dict], *, profile_lookup=None) -> list[dict]:
    profile_lookup = profile_lookup or get_public_user
    own_username = bot_username()
    candidates = []
    for game in open_games:
        creator_username = str(game.get("created_by") or "").strip()
        if not creator_username:
            continue
        if str(game.get("rule_variant") or "").strip() not in supported_rule_variants():
            continue
        creator_username_lower = creator_username.lower()
        if creator_username_lower == own_username:
            continue

        try:
            profile = profile_lookup(creator_username)
        except requests.RequestException:
            continue

        is_bot = bool(profile.get("is_bot")) or str(profile.get("role") or "").strip().lower() == "bot"
        if not is_bot:
            continue
        candidates.append(game)
    return candidates


def has_own_waiting_game(open_games: list[dict]) -> bool:
    own_username = bot_username()
    for game in open_games:
        created_by = str(game.get("created_by") or "").strip().lower()
        if created_by and created_by == own_username:
            return True
    return False


def can_attempt_bot_join(now: float | None = None) -> bool:
    current = time.time() if now is None else now
    last_attempt = load_state().get("last_bot_game_join_attempt_at", 0)
    try:
        last_attempt = float(last_attempt)
    except (TypeError, ValueError):
        last_attempt = 0
    return current - last_attempt >= BOT_JOIN_COOLDOWN_SECONDS


def record_bot_join_attempt(now: float | None = None) -> None:
    state = load_state()
    state["last_bot_game_join_attempt_at"] = time.time() if now is None else now
    save_state(state)


def choose_bot_game_to_join(open_games: list[dict], *, rng: random.Random = random) -> dict | None:
    candidates = open_bot_lobby_candidates(open_games)
    if not candidates:
        return None
    return rng.choice(candidates)


def maybe_join_bot_lobby_game(*, rng: random.Random = random) -> bool:
    if not can_attempt_bot_join():
        return False

    mine = get_json("/game/mine/active")
    if not under_active_game_limit(mine.get("games", [])):
        return False

    record_bot_join_attempt()
    open_games = get_json("/game/open").get("games", [])
    candidate = choose_bot_game_to_join(open_games, rng=rng)
    if not candidate:
        return False

    if rng.random() >= BOT_GAME_PICK_PROBABILITY:
        return False

    game_code = candidate.get("game_code")
    if not isinstance(game_code, str) or not game_code.strip():
        return False

    joined = post_json(f"/game/join/{game_code.strip()}")
    logger.debug("joined bot lobby game %s (%s)", joined["game_id"], joined["game_code"])
    return True


def should_create_lobby_game(games: list[dict]) -> bool:
    if not auto_create_enabled():
        return False
    if not under_active_game_limit(games):
        return False
    return not waiting_games(games)


def maybe_create_lobby_game(games: list[dict]) -> bool:
    if not should_create_lobby_game(games):
        return False

    open_games = get_json("/game/open").get("games", [])
    if has_own_waiting_game(open_games):
        return False

    created = post_json("/game/create", create_payload())
    logger.debug("created lobby game %s (%s)", created["game_id"], created["game_code"])
    return True


def square_coords(square: str) -> tuple[int, int] | None:
    if len(square) != 2 or square[0] < "a" or square[0] > "h" or square[1] < "1" or square[1] > "8":
        return None
    return ord(square[0]) - ord("a"), int(square[1]) - 1


def move_distance(uci: str) -> int:
    if len(uci) < 4:
        return -1
    start = square_coords(uci[:2].lower())
    end = square_coords(uci[2:4].lower())
    if start is None or end is None:
        return -1
    dx = abs(end[0] - start[0])
    dy = abs(end[1] - start[1])
    return max(dx, dy)


def sort_moves_longest_first(allowed_moves: list[str]) -> list[str]:
    valid_moves = [move for move in allowed_moves if isinstance(move, str) and len(move) >= 4]
    return sorted(valid_moves, key=lambda move: (-move_distance(move), move))


def choose_geometric_item(items: list[str], *, rng: random.Random = random) -> str | None:
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    roll = rng.random()
    cumulative = 0.0
    weight = 0.5
    for index, item in enumerate(items):
        if index == len(items) - 1:
            return item
        cumulative += weight
        if roll < cumulative:
            return item
        weight /= 2
    return items[-1]


def piece_move_groups(allowed_moves: list[str]) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {}
    for move in sort_moves_longest_first(allowed_moves):
        if not isinstance(move, str) or len(move) < 4:
            continue
        grouped.setdefault(move[:2].lower(), []).append(move)

    def sort_key(item: tuple[str, list[str]]) -> tuple[int, str]:
        square, moves = item
        longest = move_distance(moves[0]) if moves else -1
        return (-longest, square)

    return sorted(grouped.items(), key=sort_key)


def queen_promotion_moves(allowed_moves: list[str]) -> list[str]:
    promotions = [move for move in allowed_moves if len(move) >= 5 and move[4].lower() == "q"]
    return sort_moves_longest_first(promotions)


def ply_color(ply: int | None) -> str | None:
    if not isinstance(ply, int) or ply < 1:
        return None
    return "white" if ply % 2 == 1 else "black"


def last_opponent_capture_square(state: dict) -> str | None:
    our_color = str(state.get("your_color") or "").strip().lower()
    if our_color not in {"white", "black"}:
        return None
    opponent_color = "black" if our_color == "white" else "white"

    for entry in reversed(state.get("referee_log", [])):
        if not isinstance(entry, dict):
            continue
        capture_square = entry.get("capture_square")
        if not isinstance(capture_square, str) or not capture_square.strip():
            continue
        if ply_color(entry.get("ply")) != opponent_color:
            continue
        return capture_square.strip().lower()
    return None


def recapture_moves(state: dict) -> list[str]:
    capture_square = last_opponent_capture_square(state)
    if not capture_square:
        return []
    candidates = [
        move
        for move in state.get("allowed_moves", [])
        if isinstance(move, str) and len(move) >= 4 and move[2:4].lower() == capture_square
    ]
    return sort_moves_longest_first(candidates)


def priority_moves(state: dict) -> list[str]:
    recaptures = recapture_moves(state)
    if recaptures:
        return recaptures
    promotions = queen_promotion_moves(state.get("allowed_moves", []))
    if promotions:
        return promotions
    return []


def choose_piece_or_ask_any(
    state: dict,
    *,
    excluded_pieces: set[str] | None = None,
    allow_ask_any: bool = True,
    rng: random.Random = random,
) -> tuple[str, str | None]:
    excluded = excluded_pieces or set()
    ranked_pieces = [square for square, _moves in piece_move_groups(state.get("allowed_moves", [])) if square not in excluded]
    options: list[tuple[str, str | None]] = []
    if allow_ask_any and "ask_any" in state.get("possible_actions", []):
        options.append(("ask_any", None))
    options.extend(("piece", square) for square in ranked_pieces)
    selected = choose_geometric_item(options, rng=rng)
    return selected if selected is not None else ("none", None)


def moves_for_piece(state: dict, square: str) -> list[str]:
    target_square = square.strip().lower()
    return [move for origin, moves in piece_move_groups(state.get("allowed_moves", [])) if origin == target_square for move in moves]


def try_moves(game_id: str, moves: list[str]) -> bool:
    if not moves:
        return False
    for index, uci in enumerate(moves):
        result = post_json(f"/game/{game_id}/move", {"uci": uci})
        logger.debug("%s: tried %s -> %s", game_id, uci, result["announcement"])
        if result.get("move_done"):
            return True
        if index < len(moves) - 1:
            time.sleep(FAILED_MOVE_RETRY_DELAY_SECONDS)
    return False


def maybe_play_game(game_id: str, *, rng: random.Random = random) -> bool:
    state = get_json(f"/game/{game_id}/state")
    if state.get("state") != "active" or state.get("turn") != state.get("your_color"):
        return False

    special_moves = priority_moves(state)
    if special_moves:
        return try_moves(game_id, special_moves)

    if "move" not in state.get("possible_actions", []):
        return False

    allow_ask_any = True
    excluded_pieces: set[str] = set()
    while True:
        choice_kind, choice_value = choose_piece_or_ask_any(
            state,
            excluded_pieces=excluded_pieces,
            allow_ask_any=allow_ask_any,
            rng=rng,
        )
        if choice_kind == "none":
            return False

        if choice_kind == "ask_any":
            result = post_json(f"/game/{game_id}/ask-any")
            logger.debug("%s: ask-any -> %s", game_id, result["announcement"])
            allow_ask_any = False
            excluded_pieces.clear()
            state = get_json(f"/game/{game_id}/state")
            if state.get("state") != "active" or state.get("turn") != state.get("your_color"):
                return False
            special_moves = priority_moves(state)
            if special_moves:
                return try_moves(game_id, special_moves)
            if "move" not in state.get("possible_actions", []):
                return False
            continue

        assert choice_kind == "piece"
        assert choice_value is not None
        if try_moves(game_id, moves_for_piece(state, choice_value)):
            return True
        excluded_pieces.add(choice_value)


def http_status_code(exc: requests.RequestException) -> int | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    status_code = getattr(response, "status_code", None)
    return int(status_code) if isinstance(status_code, int) else None


class GameRunner:
    def __init__(self, game_id: str, *, poll_seconds: float) -> None:
        self.game_id = game_id
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"simple-heuristics-bot-game-{game_id}", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        logger.info("%s: starting game runner", self.game_id)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self._started:
            self.thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._started and self.thread.is_alive()

    def _wait(self) -> None:
        self.stop_event.wait(self.poll_seconds)

    def _run(self) -> None:
        stop_reason = "stopped"
        try:
            while not self.stop_event.is_set():
                try:
                    state = get_json(f"/game/{self.game_id}/state")
                except requests.RequestException as exc:
                    status_code = http_status_code(exc)
                    if status_code in {400, 403, 404, 409}:
                        stop_reason = f"state unavailable http_{status_code}"
                        break
                    logger.warning("%s: runner state poll failed: %s", self.game_id, exc)
                    self._wait()
                    continue

                state_value = state.get("state")
                if state_value != "active":
                    stop_reason = f"state={state_value}"
                    break

                if state.get("turn") == state.get("your_color"):
                    try:
                        maybe_play_game(self.game_id)
                    except requests.RequestException as exc:
                        status_code = http_status_code(exc)
                        if status_code in {400, 403, 404, 409}:
                            stop_reason = f"play stopped http_{status_code}"
                            break
                        logger.warning("%s: runner play failed: %s", self.game_id, exc)

                self._wait()
        finally:
            logger.info("%s: stopped game runner (%s)", self.game_id, stop_reason)


class GameRunnerScheduler:
    def __init__(self, *, poll_seconds: float, runner_factory: Any | None = None) -> None:
        self.poll_seconds = poll_seconds
        self.runner_factory = runner_factory or (lambda game_id: GameRunner(game_id, poll_seconds=poll_seconds))
        self.runners: dict[str, Any] = {}

    @staticmethod
    def game_id_for(game: dict[str, Any]) -> str:
        return str(game.get("game_id") or "").strip()

    def reconcile(self, games: list[dict[str, Any]]) -> None:
        active_ids: set[str] = set()
        for game in active_games(games):
            game_id = self.game_id_for(game)
            if not game_id:
                continue
            active_ids.add(game_id)
            runner = self.runners.get(game_id)
            if runner is not None and runner.is_alive():
                continue
            if runner is not None:
                runner.join(timeout=0)
            runner = self.runner_factory(game_id)
            self.runners[game_id] = runner
            runner.start()

        for game_id, runner in list(self.runners.items()):
            if game_id in active_ids or runner.is_alive():
                continue
            runner.join(timeout=0)
            self.runners.pop(game_id, None)

        self.prune_finished()

    def prune_finished(self) -> None:
        for game_id, runner in list(self.runners.items()):
            if runner.is_alive():
                continue
            runner.join(timeout=0)
            self.runners.pop(game_id, None)

    def stop_all(self) -> None:
        for runner in list(self.runners.values()):
            runner.stop()
        for runner in list(self.runners.values()):
            runner.join(timeout=2.0)
        self.runners.clear()


def poll_once(scheduler: GameRunnerScheduler | None = None) -> None:
    maybe_sync_bot_profile()
    path = f"/game/mine/active?limit={active_game_discovery_limit()}" if scheduler is not None else "/game/mine/active"
    mine = get_json(path)
    games = mine.get("games", [])
    maybe_create_lobby_game(games)
    maybe_join_bot_lobby_game()
    if scheduler is not None:
        scheduler.reconcile(games)
        return
    for game in active_games(games):
        maybe_play_game(game["game_id"])


def run_loop(poll_seconds: float) -> None:
    discovery_limit = active_game_discovery_limit()
    logger.info("active-game discovery limit configured: max=%s", discovery_limit)
    scheduler = GameRunnerScheduler(poll_seconds=poll_seconds)
    try:
        while True:
            try:
                poll_once(scheduler)
            except requests.RequestException as exc:
                logger.warning("poll failed: %s", exc)
            time.sleep(poll_seconds)
    finally:
        scheduler.stop_all()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kriegspiel simple heuristics bot")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("KRIEGSPIEL_BOT_ENV_FILE", str(DEFAULT_ENV_PATH)),
        help="path to the bot instance env file",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("KRIEGSPIEL_BOT_STATE_FILE", str(DEFAULT_STATE_PATH)),
        help="path to the bot instance state file",
    )
    parser.add_argument("--register", action="store_true", help="register the bot and store its bearer token")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="poll interval between API rounds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_runtime_paths(env_path=args.env_file, state_path=args.state_file)
    load_env_file()
    maybe_restore_token()

    if args.register:
        register_bot()
        return 0

    if not os.environ.get("KRIEGSPIEL_BOT_TOKEN"):
        logger.error("missing KRIEGSPIEL_BOT_TOKEN; run with --register first or set it in the environment")
        return 1

    run_loop(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
