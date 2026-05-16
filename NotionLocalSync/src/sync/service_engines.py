from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class EngineSnapshot:
    readiness: str
    summary: str
    capabilities: list[str] = field(default_factory=list)


class BaseServiceSyncEngine:
    service_key = "service"
    display_name = "Service"

    def snapshot(self) -> EngineSnapshot:
        return EngineSnapshot(
            readiness="Ready",
            summary="The module shell is live with local storage and status tracking, but no dedicated remote connector is attached yet.",
            capabilities=["Shared plugin hook ready"],
        )

    def run_pull(self) -> tuple[bool, str]:
        return False, f"{self.display_name} import can be added next, but the module shell is already ready."

    def run_push(self) -> tuple[bool, str]:
        return False, f"{self.display_name} export can be added next, but the module shell is already ready."

    def run_sync(self) -> tuple[bool, str]:
        return False, f"{self.display_name} has a working module shell, but no dedicated sync adapter is attached yet."


class PlannedServiceSyncEngine(BaseServiceSyncEngine):
    def __init__(self, service_key: str, display_name: str, future_focus: list[str] | None = None):
        self.service_key = service_key
        self.display_name = display_name
        self.future_focus = list(future_focus or [])

    def snapshot(self) -> EngineSnapshot:
        focus = self.future_focus or [
            "Credentials and connection settings",
            "Pull / push job wiring",
            "Recovery snapshots in PostgreSQL",
        ]
        return EngineSnapshot(
            readiness="Shell ready",
            summary=(
                f"{self.display_name} already has its dashboard slot, storage schema, and quick-action wiring in place, "
                "so a dedicated sync engine can be added later without rebuilding the app shell."
            ),
            capabilities=focus,
        )
