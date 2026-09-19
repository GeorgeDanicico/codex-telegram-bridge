import tempfile
import unittest
from pathlib import Path
from typing import Any

from bot import Bridge, CodexProfile, CodexRunner, Settings, SkodaGatewayClient


class FakeTelegramBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(("message", (chat_id, text, reply_to_message_id, reply_markup)))

    async def send_typing(self, chat_id: int) -> None:
        self.calls.append(("typing", chat_id))

    async def get_me(self) -> dict[str, Any]:
        return {"username": "test_bridge_bot"}

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        self.calls.append(("callback", (callback_query_id, text, show_alert)))

    async def remove_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("remove_keyboard", (chat_id, message_id)))


class FakeCodexRunner:
    def __init__(self, response: str = "Codex response") -> None:
        self.response = response
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response

    async def version(self) -> str:
        return "test"


class FakeCarApi:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.actions: list[str] = []
        self.payload: dict[str, Any] = {
            "vehicleName": "Enyaq",
            "licensePlate": "B 123 CAR",
            "rangeKm": 287,
            "batteryPercent": 72,
            "doorsLocked": "Locked",
            "locked": "Locked",
            "doors": "Closed",
            "windows": "Closed",
            "reliableLockStatus": "Reliable",
            "trunk": "Closed",
            "bonnet": "Closed",
            "lights": "Off",
            "location": {
                "country": "Belgium",
                "county": "Brussels-Capital",
                "latitude": 50.8503,
                "longitude": 4.3517,
                "address": "Example Street",
            },
            "capturedAt": "2026-08-12T10:00:00Z",
            "partial": False,
            "unavailableSections": [],
        }

    async def snapshot(self) -> dict[str, Any]:
        self.snapshot_calls += 1
        return self.payload

    async def action(self, action: str) -> dict[str, Any]:
        self.actions.append(action)
        return {"accepted": True}


def settings(
    allowed_user_ids: frozenset[int] = frozenset({7}),
    projects_dir: Path | None = None,
    wiki_workdir: Path | None = None,
) -> Settings:
    profile = CodexProfile("test-model", "low", 30)
    return Settings(
        telegram_token="test",
        allowed_user_ids=allowed_user_ids,
        codex_bin="codex",
        codex_workdir=Path(tempfile.gettempdir()),
        projects_dir=projects_dir or Path(tempfile.gettempdir()),
        wiki_workdir=wiki_workdir or Path(tempfile.gettempdir()),
        codex_model=None,
        codex_unsafe_mode=False,
        codex_timeout_seconds=30,
        fast_profile=profile,
        default_profile=profile,
        deep_profile=profile,
        skoda_gateway_url="http://127.0.0.1:8091",
        skoda_gateway_token="test-token",
        skoda_request_timeout_seconds=20,
        skoda_snapshot_cache_seconds=5,
        skoda_action_cooldown_seconds=0,
    )


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = FakeTelegramBot()
        self.bridge = Bridge(settings(), self.bot)  # type: ignore[arg-type]
        self.codex = FakeCodexRunner()
        self.bridge._codex = self.codex  # type: ignore[assignment]
        self.bridge._fast_codex = self.codex  # type: ignore[assignment]
        self.bridge._deep_codex = self.codex  # type: ignore[assignment]
        self.bridge._wiki_codex = self.codex  # type: ignore[assignment]
        self.car_api = FakeCarApi()
        self.bridge._car_api = self.car_api  # type: ignore[assignment]

    async def test_prompt_sends_typing_and_response(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "Hello bridge"}
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.codex.prompts, ["Hello bridge"])
        self.assertIn(("typing", 99), self.bot.calls)
        self.assertEqual(
            [call for call in self.bot.calls if call[0] == "message"][-1][1],
            (99, "Codex response", 12, None),
        )

    async def test_commands_are_case_insensitive_and_support_mentions(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/quick@test_bridge_bot do it"}
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.codex.prompts, ["do it"])

    async def test_access_is_denied_for_users_outside_allowlist(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 8}, "chat": {"id": 99}, "message_id": 12, "text": "Hello bridge"}
        )

        self.assertEqual(self.codex.prompts, [])
        self.assertEqual(
            [call for call in self.bot.calls if call[0] == "message"][-1][1],
            (99, "Access denied.", 12, None),
        )

    async def test_car_sends_text_location_and_controls_without_map(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/car"}
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.car_api.snapshot_calls, 1)
        self.assertFalse(any(call[0] == "location" for call in self.bot.calls))
        summary = [call for call in self.bot.calls if call[0] == "message"][-1][1]
        self.assertIn("Country: Belgium", summary[1])
        self.assertIn("County: Brussels-Capital", summary[1])
        self.assertIn("Coordinates: 50.850300, 4.351700", summary[1])
        callback_data = [
            button["callback_data"]
            for row in summary[3]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("car:confirm:honk_flash", callback_data)
        self.assertIn("car:confirm:unlock", callback_data)

    async def test_car_snapshot_uses_cache(self) -> None:
        for message_id in (12, 13):
            await self.bridge.handle_message(
                {"from": {"id": 7}, "chat": {"id": 99}, "message_id": message_id, "text": "/car"}
            )
            await self.bridge._jobs[99]

        self.assertEqual(self.car_api.snapshot_calls, 1)

    async def test_car_action_requires_confirmation_and_calls_api(self) -> None:
        base_query = {
            "id": "callback-1",
            "from": {"id": 7},
            "message": {"message_id": 50, "chat": {"id": 99}},
        }
        await self.bridge.handle_callback_query({**base_query, "data": "car:confirm:flash"})
        confirmation = [call for call in self.bot.calls if call[0] == "message"][-1][1]
        self.assertEqual(
            confirmation[3]["inline_keyboard"][0][0]["callback_data"],
            "car:run:flash",
        )

        await self.bridge.handle_callback_query(
            {
                **base_query,
                "id": "callback-2",
                "message": {"message_id": 51, "chat": {"id": 99}},
                "data": "car:run:flash",
            }
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.car_api.actions, ["flash"])
        self.assertIn(("remove_keyboard", (99, 51)), self.bot.calls)

    async def test_car_is_disabled_without_allowlist(self) -> None:
        bot = FakeTelegramBot()
        bridge = Bridge(settings(frozenset()), bot)  # type: ignore[arg-type]

        await bridge.handle_message(
            {"from": {"id": 123}, "chat": {"id": 99}, "message_id": 12, "text": "/car"}
        )

        self.assertFalse(bridge._jobs)
        self.assertIn("disabled", bot.calls[-1][1][1])

    async def test_help_lists_only_supported_commands(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/help"}
        )

        response = [call for call in self.bot.calls if call[0] == "message"][-1][1][1]
        self.assertIn("/status", response)
        self.assertIn("/cancel", response)
        self.assertIn("/car", response)

    async def test_projects_lists_project_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projects_dir = Path(directory)
            (projects_dir / "alpha").mkdir()
            (projects_dir / "zebra").mkdir()
            (projects_dir / "notes.txt").write_text("not a project", encoding="utf-8")
            bridge = Bridge(settings(projects_dir=projects_dir), self.bot)  # type: ignore[arg-type]

            await bridge.handle_message(
                {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/projects"}
            )

        response = [call for call in self.bot.calls if call[0] == "message"][-1][1][1]
        self.assertEqual(response, "Projects:\n• alpha\n• zebra")

    async def test_wiki_uses_dedicated_runner(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/wiki What is DNS?"}
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.codex.prompts, ["What is DNS?"])

    async def test_status_reports_codex_and_telegram(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/status"}
        )

        response = [call for call in self.bot.calls if call[0] == "message"][-1][1][1]
        self.assertIn("Codex: online (test)", response)
        self.assertIn("Telegram: online (@test_bridge_bot)", response)
        self.assertNotIn("transport", response.lower())

    async def test_removed_command_is_unknown(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/unknown"}
        )

        self.assertEqual(
            [call for call in self.bot.calls if call[0] == "message"][-1][1],
            (99, "Unknown command. Use /help.", 12, None),
        )


class CodexProfileTests(unittest.TestCase):
    def test_command_overrides_model_and_reasoning_effort(self) -> None:
        profile = CodexProfile("gpt-test", "medium", 30)
        command = CodexRunner(settings(), profile).command("/tmp/response")
        self.assertIn("gpt-test", command)
        self.assertIn('model_reasoning_effort="medium"', command)

    def test_command_can_use_a_dedicated_working_directory(self) -> None:
        workdir = Path(tempfile.gettempdir()) / "dedicated-workdir"
        profile = CodexProfile("gpt-test", "medium", 30)
        command = CodexRunner(settings(), profile, workdir).command("/tmp/response")

        self.assertEqual(command[command.index("--cd") + 1], str(workdir))


class SkodaGatewayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_and_actions_use_gateway_contract(self) -> None:
        client = SkodaGatewayClient(settings())
        calls: list[tuple[str, str]] = []

        async def request(method: str, path: str) -> dict[str, Any]:
            calls.append((method, path))
            return {"location": {"latitude": 50.8503, "longitude": 4.3517, "address": "Example Street"}}

        client._request = request  # type: ignore[method-assign]
        payload = await client.snapshot()
        await client.action("flash")
        await client.action("honk-and-flash")
        await client.action("lock")

        self.assertIn("location", payload)
        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/car"),
                ("POST", "/api/v1/car/actions/flash"),
                ("POST", "/api/v1/car/actions/honk-and-flash"),
                ("POST", "/api/v1/car/actions/lock"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
