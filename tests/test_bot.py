from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class BotTests(unittest.TestCase):
    def test_under_active_game_limit_caps_parallel_games_at_five(self) -> None:
        self.assertTrue(bot.under_active_game_limit([{"state": "active"}] * 4))
        self.assertFalse(bot.under_active_game_limit([{"state": "active"}] * 5))

    def test_sort_moves_longest_first_prefers_longer_attempts(self) -> None:
        self.assertEqual(
            bot.sort_moves_longest_first(["a2a4", "a1a8", "b1c3"]),
            ["a1a8", "a2a4", "b1c3"],
        )

    def test_piece_move_groups_rank_pieces_by_longest_available_move(self) -> None:
        self.assertEqual(
            bot.piece_move_groups(["a2a4", "a1a8", "a1a3", "b1h7", "b1c3"]),
            [
                ("a1", ["a1a8", "a1a3"]),
                ("b1", ["b1h7", "b1c3"]),
                ("a2", ["a2a4"]),
            ],
        )

    def test_choose_piece_or_ask_any_uses_geometric_option_weights(self) -> None:
        class SequenceRng:
            def __init__(self, values: list[float]):
                self._values = list(values)

            def random(self) -> float:
                return self._values.pop(0)

        state = {
            "possible_actions": ["move", "ask_any"],
            "allowed_moves": ["a2a4", "a1a8", "b1c3"],
        }
        self.assertEqual(bot.choose_piece_or_ask_any(state, rng=SequenceRng([0.0])), ("ask_any", None))
        self.assertEqual(bot.choose_piece_or_ask_any(state, rng=SequenceRng([0.6])), ("piece", "a1"))
        self.assertEqual(bot.choose_piece_or_ask_any(state, rng=SequenceRng([0.9])), ("piece", "b1"))

    def test_supported_rule_variants_default_to_berkeley_family_and_wild16(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bot.supported_rule_variants(), ["berkeley", "berkeley_any", "wild16"])

    def test_supported_rule_variants_dedupe_and_ignore_unknown_rulesets(self) -> None:
        with patch.dict(
            "os.environ",
            {"KRIEGSPIEL_SUPPORTED_RULE_VARIANTS": "wild16,standard,berkeley_any,wild16"},
        ):
            self.assertEqual(bot.supported_rule_variants(), ["wild16", "berkeley_any"])

    def test_supported_rule_variants_expand_legacy_default_to_wild16(self) -> None:
        with patch.dict("os.environ", {"KRIEGSPIEL_SUPPORTED_RULE_VARIANTS": "berkeley,berkeley_any"}):
            self.assertEqual(bot.supported_rule_variants(), ["berkeley", "berkeley_any", "wild16"])

    def test_supported_rule_variants_preserve_explicit_narrower_config(self) -> None:
        with patch.dict("os.environ", {"KRIEGSPIEL_SUPPORTED_RULE_VARIANTS": "berkeley"}):
            self.assertEqual(bot.supported_rule_variants(), ["berkeley"])

    def test_create_payload_accepts_wild16_when_configured(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KRIEGSPIEL_SUPPORTED_RULE_VARIANTS": "berkeley,berkeley_any,wild16",
                "KRIEGSPIEL_AUTO_CREATE_RULE_VARIANT": "wild16",
            },
        ):
            self.assertEqual(bot.create_payload()["rule_variant"], "wild16")

    def test_create_payload_falls_back_to_supported_rule_variant(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KRIEGSPIEL_SUPPORTED_RULE_VARIANTS": "wild16",
                "KRIEGSPIEL_AUTO_CREATE_RULE_VARIANT": "standard",
            },
        ):
            self.assertEqual(bot.create_payload()["rule_variant"], "wild16")

    def test_register_bot_advertises_wild16_by_default(self) -> None:
        posts: list[dict] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"api_token": "token-123"}

        def fake_post(*args, **kwargs):
            posts.append(kwargs)
            return FakeResponse()

        env = {
            "KRIEGSPIEL_API_BASE": "https://api.example.test",
            "KRIEGSPIEL_BOT_REGISTRATION_KEY": "registration-key",
            "KRIEGSPIEL_BOT_USERNAME": "simpleheuristics",
            "KRIEGSPIEL_BOT_DISPLAY_NAME": "Simple Heuristics Bot",
            "KRIEGSPIEL_BOT_OWNER_EMAIL": "bots@example.test",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(bot, "STATE_PATH", Path(temp_dir) / ".bot-state.json"):
                with patch.dict("os.environ", env, clear=True):
                    with patch.object(bot.requests, "post", side_effect=fake_post):
                        bot.register_bot()

        self.assertEqual(posts[0]["json"]["supported_rule_variants"], ["berkeley", "berkeley_any", "wild16"])

    def test_sync_bot_profile_reports_supported_rule_variants(self) -> None:
        with patch.object(bot, "post_json", return_value={"ok": True}) as post_json:
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(bot.sync_bot_profile(), {"ok": True})

        post_json.assert_called_once_with(
            "/bots/profile",
            {"supported_rule_variants": ["berkeley", "berkeley_any", "wild16"]},
        )

    def test_poll_once_syncs_profile_before_polling_games(self) -> None:
        calls: list[str] = []

        def fake_sync() -> bool:
            calls.append("sync")
            return True

        def fake_get_json(path: str) -> dict:
            calls.append(path)
            if path == "/game/mine/active":
                return {"games": []}
            raise AssertionError(path)

        with patch.object(bot, "maybe_sync_bot_profile", side_effect=fake_sync):
            with patch.object(bot, "get_json", side_effect=fake_get_json):
                with patch.object(bot, "maybe_create_lobby_game", side_effect=lambda games: calls.append("create")):
                    with patch.object(bot, "maybe_join_bot_lobby_game", side_effect=lambda: calls.append("join")):
                        bot.poll_once()

        self.assertEqual(calls, ["sync", "/game/mine/active", "create", "join"])

    def test_recapture_moves_target_latest_opponent_capture_square(self) -> None:
        state = {
            "your_color": "white",
            "allowed_moves": ["e4d5", "a2a4", "c2d3"],
            "referee_log": [
                {"ply": 1, "capture_square": None},
                {"ply": 2, "capture_square": "d5"},
            ],
        }

        self.assertEqual(bot.recapture_moves(state), ["e4d5"])

    def test_maybe_play_game_recaptures_before_anything_else(self) -> None:
        state = {
            "state": "active",
            "turn": "white",
            "your_color": "white",
            "possible_actions": ["move", "ask_any"],
            "allowed_moves": ["a2a4", "e4d5"],
            "referee_log": [{"ply": 2, "capture_square": "d5"}],
        }

        with patch.object(bot, "get_json", return_value=state):
            with patch.object(bot, "post_json", return_value={"announcement": "Move complete", "move_done": True}) as post_json:
                self.assertTrue(bot.maybe_play_game("game-1"))

        post_json.assert_called_once_with("/game/game-1/move", {"uci": "e4d5"})

    def test_maybe_play_game_prefers_queen_promotion_before_ask_any(self) -> None:
        state = {
            "state": "active",
            "turn": "white",
            "your_color": "white",
            "possible_actions": ["move", "ask_any"],
            "allowed_moves": ["e7e8n", "e7e8q", "a2a4"],
            "referee_log": [],
        }

        with patch.object(bot, "get_json", return_value=state):
            with patch.object(bot, "post_json", return_value={"announcement": "Move complete", "move_done": True}) as post_json:
                self.assertTrue(bot.maybe_play_game("game-1"))

        post_json.assert_called_once_with("/game/game-1/move", {"uci": "e7e8q"})

    def test_maybe_play_game_asks_any_with_probability_before_generic_moves(self) -> None:
        states = [
            {
                "state": "active",
                "turn": "white",
                "your_color": "white",
                "possible_actions": ["move", "ask_any"],
                "allowed_moves": ["a2a4", "a1a8"],
                "referee_log": [],
            },
            {
                "state": "active",
                "turn": "white",
                "your_color": "white",
                "possible_actions": ["move"],
                "allowed_moves": ["a2a4", "a1a8"],
                "referee_log": [],
            },
        ]
        posts: list[tuple[str, dict | None]] = []

        def fake_get_json(path: str) -> dict:
            self.assertEqual(path, "/game/game-1/state")
            return states.pop(0)

        def fake_post_json(path: str, payload: dict | None = None) -> dict:
            posts.append((path, payload))
            if path.endswith("/ask-any"):
                return {"announcement": "No pawn captures."}
            return {"announcement": "Move complete", "move_done": True}

        class PredictableRng:
            @staticmethod
            def random() -> float:
                return 0.0

        with patch.object(bot, "get_json", side_effect=fake_get_json):
            with patch.object(bot, "post_json", side_effect=fake_post_json):
                self.assertTrue(bot.maybe_play_game("game-1", rng=PredictableRng()))

        self.assertEqual(
            posts,
            [
                ("/game/game-1/ask-any", None),
                ("/game/game-1/move", {"uci": "a1a8"}),
            ],
        )

    def test_maybe_play_game_retries_selected_piece_then_falls_back_to_next_piece(self) -> None:
        state = {
            "state": "active",
            "turn": "white",
            "your_color": "white",
            "possible_actions": ["move"],
            "allowed_moves": ["a1a8", "a1a7", "b1c3"],
            "referee_log": [],
        }
        posts: list[tuple[str, dict | None]] = []
        results = [
            {"announcement": "Illegal move", "move_done": False},
            {"announcement": "Illegal move", "move_done": False},
            {"announcement": "Move complete", "move_done": True},
        ]

        def fake_post_json(path: str, payload: dict | None = None) -> dict:
            posts.append((path, payload))
            return results.pop(0)

        class SequenceRng:
            def __init__(self, values: list[float]):
                self._values = list(values)

            def random(self) -> float:
                return self._values.pop(0)

        with patch.object(bot, "get_json", return_value=state):
            with patch.object(bot, "post_json", side_effect=fake_post_json):
                with patch.object(bot.time, "sleep") as sleep_mock:
                    self.assertTrue(bot.maybe_play_game("game-1", rng=SequenceRng([0.0, 0.0])))

        self.assertEqual(
            posts,
            [
                ("/game/game-1/move", {"uci": "a1a8"}),
                ("/game/game-1/move", {"uci": "a1a7"}),
                ("/game/game-1/move", {"uci": "b1c3"}),
            ],
        )
        sleep_mock.assert_called_once_with(bot.FAILED_MOVE_RETRY_DELAY_SECONDS)

    def test_open_bot_lobby_candidates_only_include_other_bot_waiting_games(self) -> None:
        with patch.dict("os.environ", {"KRIEGSPIEL_BOT_USERNAME": "simpleheuristics"}):
            candidates = bot.open_bot_lobby_candidates(
                [
                    {"game_code": "BOT123", "created_by": "gptnano", "rule_variant": "berkeley_any"},
                    {"game_code": "WLD123", "created_by": "gptnano", "rule_variant": "wild16"},
                    {"game_code": "SELF12", "created_by": "simpleheuristics", "rule_variant": "berkeley_any"},
                    {"game_code": "HUM123", "created_by": "fil", "rule_variant": "berkeley_any"},
                ],
                profile_lookup=lambda username: {"role": "bot" if username == "gptnano" else "user"},
            )

        self.assertEqual([game["game_code"] for game in candidates], ["BOT123", "WLD123"])

    def test_maybe_join_bot_lobby_game_records_attempt_even_when_probability_misses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / ".bot-state.json"
            mine = {"games": []}
            open_games = {"games": [{"game_code": "BOT123", "created_by": "gptnano", "rule_variant": "berkeley_any"}]}

            def fake_get_json(path: str) -> dict:
                if path == "/game/mine/active":
                    return mine
                if path == "/game/open":
                    return open_games
                raise AssertionError(path)

            with patch.object(bot, "STATE_PATH", state_path):
                with patch.dict("os.environ", {"KRIEGSPIEL_BOT_USERNAME": "simpleheuristics"}):
                    with patch.object(bot, "get_json", side_effect=fake_get_json):
                        with patch.object(bot, "get_public_user", return_value={"role": "bot"}):
                            with patch.object(bot.random, "choice", side_effect=lambda items: items[0]):
                                with patch.object(bot.random, "random", return_value=0.9):
                                    with patch.object(bot.time, "time", return_value=100.0):
                                        with patch.object(bot, "post_json") as post_mock:
                                            self.assertFalse(bot.maybe_join_bot_lobby_game(rng=bot.random))

                self.assertFalse(bot.can_attempt_bot_join(now=130.0))
                self.assertTrue(bot.can_attempt_bot_join(now=161.0))
                post_mock.assert_not_called()

    def test_maybe_join_bot_lobby_game_records_sample_even_without_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / ".bot-state.json"

            def fake_get_json(path: str) -> dict:
                if path == "/game/mine/active":
                    return {"games": []}
                if path == "/game/open":
                    return {"games": []}
                raise AssertionError(path)

            with patch.object(bot, "STATE_PATH", state_path):
                with patch.object(bot, "get_json", side_effect=fake_get_json):
                    with patch.object(bot.time, "time", return_value=100.0):
                        with patch.object(bot, "post_json") as post_mock:
                            self.assertFalse(bot.maybe_join_bot_lobby_game())

                self.assertFalse(bot.can_attempt_bot_join(now=130.0))
                self.assertTrue(bot.can_attempt_bot_join(now=161.0))
                post_mock.assert_not_called()

    def test_maybe_join_bot_lobby_game_skips_open_sample_during_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / ".bot-state.json"
            calls: list[str] = []

            def fake_get_json(path: str) -> dict:
                calls.append(path)
                if path == "/game/mine/active":
                    return {"games": []}
                raise AssertionError(path)

            with patch.object(bot, "STATE_PATH", state_path):
                bot.record_bot_join_attempt(now=100.0)
                with patch.object(bot, "get_json", side_effect=fake_get_json):
                    with patch.object(bot.time, "time", return_value=130.0):
                        self.assertFalse(bot.maybe_join_bot_lobby_game())

            self.assertEqual(calls, ["/game/mine/active"])

    def test_maybe_create_lobby_game_respects_active_limit_and_own_waiting_game(self) -> None:
        with patch.object(bot, "get_json", return_value={"games": []}):
            with patch.object(bot, "has_own_waiting_game", return_value=False):
                with patch.object(bot, "post_json", return_value={"game_id": "g1", "game_code": "ABC123"}) as post_json:
                    self.assertTrue(bot.maybe_create_lobby_game([]))

        post_json.assert_called_once_with("/game/create", bot.create_payload())


if __name__ == "__main__":
    unittest.main()
