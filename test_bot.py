import tempfile
import unittest
from pathlib import Path
from typing import Any

from bot import Bridge, CarSnapshot, CodexProfile, Settings


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

    async def send_location(
        self, chat_id: int, latitude: float, longitude: float, reply_to_message_id: int | None = None
    ) -> None:
        self.calls.append(("location", (chat_id, latitude, longitude, reply_to_message_id)))

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
    def __init__(self) -> None:
        self.json_calls: list[tuple[str, dict[str, Any]]] = []
        self.text_calls: list[str] = []

    async def run_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.json_calls.append((prompt, schema))
        return {
            "vehicle_name": "Skoda Enyaq",
            "license_plate": "B 123 CAR",
            "range_km": 287,
            "battery_percent": 72,
            "doors_locked": "Locked",
            "doors": "Closed",
            "windows": "Closed",
            "trunk": "Closed",
            "bonnet": "Closed",
            "lights": "Off",
            "location_address": "Example Street",
            "latitude": 44.4268,
            "longitude": 26.1025,
            "updated_at": "2026-08-12 10:00 EEST",
        }

    async def run(self, prompt: str) -> str:
        self.text_calls.append(prompt)
        return "The vehicle lights were flashed successfully."

    async def version(self) -> str:
        return "test"


class EmptyFirstSnapshotRunner(FakeCodexRunner):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_attempts = 0

    async def run_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.snapshot_attempts += 1
        if self.snapshot_attempts == 1:
            raise RuntimeError("Codex completed without a final response.")
        return await super().run_json(prompt, schema)


class FakeGateway:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.actions: list[str] = []
        self.payload: dict[str, Any] = {
            "vehicleName": "Skoda Enyaq",
            "licensePlate": "B 123 CAR",
            "rangeKm": 287,
            "batteryPercent": 72,
            "doorsLocked": "Locked",
            "doors": "Closed",
            "windows": "Closed",
            "trunk": "Closed",
            "bonnet": "Closed",
            "lights": "Off",
            "location": {"latitude": 44.4268, "longitude": 26.1025, "address": "Example Street"},
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
    transport: str = "http",
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
        skoda_transport=transport,  # type: ignore[arg-type]
        skoda_gateway_url="http://127.0.0.1:8090",
        skoda_gateway_token="test-token",
        skoda_request_timeout_seconds=20,
        skoda_snapshot_cache_seconds=5,
        skoda_action_cooldown_seconds=0,
    )


class CarSnapshotTests(unittest.TestCase):
    def test_format_includes_basic_status(self) -> None:
        snapshot = CarSnapshot.from_payload(
            {
                "vehicle_name": "Enyaq",
                "license_plate": None,
                "range_km": 250.5,
                "battery_percent": 80,
                "doors_locked": "Locked",
                "doors": "Closed",
                "windows": None,
                "trunk": None,
                "bonnet": None,
                "lights": "Off",
                "location_address": None,
                "latitude": None,
                "longitude": None,
                "updated_at": None,
            }
        )

        self.assertIn("Range: 250.5 km", snapshot.format())
        self.assertIn("Doors: Closed", snapshot.format())
        self.assertIn("Location unavailable", snapshot.format())

    def test_rejects_invalid_coordinates(self) -> None:
        with self.assertRaises(RuntimeError):
            CarSnapshot.from_payload({"latitude": 91, "longitude": 0})


class BridgeCarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = FakeTelegramBot()
        self.bridge = Bridge(settings(), self.bot)  # type: ignore[arg-type]
        self.codex = FakeCodexRunner()
        self.bridge._car_codex = self.codex  # type: ignore[assignment]
        self.gateway = FakeGateway()
        self.bridge._gateway = self.gateway  # type: ignore[assignment]

    async def test_car_command_sends_map_summary_and_buttons(self) -> None:
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/car"}
        )
        job = self.bridge._jobs[99]
        await job

        self.assertFalse(self.codex.json_calls, "/car must not invoke Codex when using HTTP")
        self.assertEqual(self.gateway.snapshot_calls, 1)
        location = next(call for call in self.bot.calls if call[0] == "location")
        self.assertEqual(location[1], (99, 44.4268, 26.1025, 12))
        summary = [call for call in self.bot.calls if call[0] == "message"][-1][1]
        self.assertIn("Range: 287 km", summary[1])
        callback_data = [
            button["callback_data"]
            for row in summary[3]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("car:confirm:honk_flash", callback_data)
        self.assertIn("car:confirm:unlock", callback_data)

    async def test_car_snapshot_uses_short_cache_and_displays_partial_data(self) -> None:
        self.gateway.payload["partial"] = True
        self.gateway.payload["unavailableSections"] = ["location"]
        self.gateway.payload["location"] = {"latitude": None, "longitude": None, "address": None}

        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/car"}
        )
        await self.bridge._jobs[99]
        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 13, "text": "/car"}
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.gateway.snapshot_calls, 1)
        self.assertFalse(any(call[0] == "location" for call in self.bot.calls))
        self.assertIn("Unavailable: location", [call for call in self.bot.calls if call[0] == "message"][-1][1][1])

    async def test_car_action_requires_confirmation_then_calls_exact_tool(self) -> None:
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
        job = self.bridge._jobs[99]
        await job

        self.assertEqual(self.gateway.actions, ["flash"])
        self.assertFalse(self.codex.text_calls)
        self.assertIn(("remove_keyboard", (99, 51)), self.bot.calls)

    async def test_car_is_disabled_without_allowlist(self) -> None:
        bot = FakeTelegramBot()
        bridge = Bridge(settings(frozenset()), bot)  # type: ignore[arg-type]

        await bridge.handle_message(
            {"from": {"id": 123}, "chat": {"id": 99}, "message_id": 12, "text": "/car"}
        )

        self.assertFalse(bridge._jobs)
        self.assertIn("disabled", bot.calls[-1][1][1])

    async def test_mcp_fallback_remains_available_for_rollback(self) -> None:
        bridge = Bridge(settings(transport="mcp"), self.bot)  # type: ignore[arg-type]
        bridge._car_codex = self.codex  # type: ignore[assignment]
        await bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/car"}
        )
        await bridge._jobs[99]
        self.assertEqual(len(self.codex.json_calls), 1)

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

    async def test_wiki_command_uses_dedicated_wiki_runner(self) -> None:
        self.bridge._wiki_codex = self.codex  # type: ignore[assignment]

        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/wiki What is DNS?"}
        )
        await self.bridge._jobs[99]

        self.assertEqual(self.codex.text_calls, ["What is DNS?"])

    async def test_status_reports_codex_and_telegram(self) -> None:
        self.bridge._codex = self.codex  # type: ignore[assignment]

        await self.bridge.handle_message(
            {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": "/status"}
        )

        response = [call for call in self.bot.calls if call[0] == "message"][-1][1][1]
        self.assertIn("Codex: online (test)", response)
        self.assertIn("Telegram: online (@test_bridge_bot)", response)

    async def test_removed_commands_are_unknown(self) -> None:
        for command in ("/ask hello", "/echo hello", "/ping"):
            await self.bridge.handle_message(
                {"from": {"id": 7}, "chat": {"id": 99}, "message_id": 12, "text": command}
            )

        responses = [call[1][1] for call in self.bot.calls if call[0] == "message"]
        self.assertEqual(responses, ["Unknown command. Use /help."] * 3)


class CodexProfileTests(unittest.TestCase):
    def test_command_overrides_model_and_reasoning_effort(self) -> None:
        from bot import CodexRunner

        profile = CodexProfile("gpt-test", "medium", 30)
        command = CodexRunner(settings(), profile).command("/tmp/response")
        self.assertIn("gpt-test", command)
        self.assertIn('model_reasoning_effort="medium"', command)

    def test_command_can_use_a_dedicated_working_directory(self) -> None:
        from bot import CodexRunner

        wiki_workdir = Path(tempfile.gettempdir()) / "llm-wiki"
        profile = CodexProfile("gpt-test", "medium", 30)
        command = CodexRunner(settings(wiki_workdir=wiki_workdir), profile, wiki_workdir).command(
            "/tmp/response"
        )

        self.assertEqual(command[command.index("--cd") + 1], str(wiki_workdir))


if __name__ == "__main__":
    unittest.main()
