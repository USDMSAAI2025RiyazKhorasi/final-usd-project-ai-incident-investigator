"""Real fault behaviours for the live target app. One entry per fault in scenarios/catalogue.yaml,
plus noise behaviours used by the injector.

Each fault changes how the service actually behaves (memory really grows, the disk really
fills, the CPU really burns), so Prometheus and Loki record genuine symptoms.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

FAULTS = {
    "memory_leak", "slow_query", "bad_config_deploy", "buggy_code_deploy", "dependency_down",
    "pool_exhausted", "cpu_saturation", "cascading_failure", "expired_credential", "disk_full",
    # noise templates
    "background_errors", "cpu_burst",
}
RELEASE_FAULTS = {"bad_config_deploy", "buggy_code_deploy", "expired_credential"}   # undone by rollback


class FaultState:
    def __init__(self, service: str):
        self.service = service
        self._on: dict[str, dict] = {}
        self._leak: list[bytearray] = []
        self._burners: list[threading.Event] = []
        self.data_dir = Path(os.environ.get("DATA_DIR", f"/var/lib/{service}"))

    # ---------------------------------------------------------- switches
    def set(self, name: str, enabled: bool, params: dict | None = None) -> None:
        if enabled:
            self._on[name] = params or {}
            if name in ("cpu_saturation", "cpu_burst", "cascading_failure"):
                self._start_burner(params or {})
            if name == "disk_full":
                threading.Thread(target=self._fill_disk, daemon=True).start()
        else:
            self._on.pop(name, None)
            if name in ("cpu_saturation", "cpu_burst", "cascading_failure"):
                self._stop_burners()
            if name == "memory_leak":
                self._leak.clear()
            if name == "disk_full":
                for f in self.data_dir.glob("filler-*"):
                    f.unlink(missing_ok=True)

    def is_on(self, name: str) -> bool:
        return name in self._on

    def params(self, name: str) -> dict:
        return self._on.get(name, {})

    def active(self) -> list[str]:
        return sorted(self._on)

    def reset(self) -> None:
        for name in list(self._on):
            self.set(name, False)

    def rollback(self) -> list[str]:
        undone = [f for f in self._on if f in RELEASE_FAULTS]
        for f in undone:
            self.set(f, False)
        return undone

    # ---------------------------------------------------------- effects
    def intensity(self, name: str) -> float:
        return {"low": 0.6, "medium": 1.0, "high": 1.6}.get(self.params(name).get("intensity", "medium"), 1.0)

    def on_request(self) -> None:
        if self.is_on("memory_leak"):
            self._leak.append(bytearray(int(1_000_000 * self.intensity("memory_leak"))))
        if self.is_on("cascading_failure"):
            time.sleep(4.0 * self.intensity("cascading_failure"))

    def _start_burner(self, params: dict) -> None:
        stop = threading.Event()
        self._burners.append(stop)

        def burn():
            while not stop.is_set():
                x = 0
                for i in range(200_000):
                    x += i * i
                time.sleep(0.001)
        for _ in range(2):
            threading.Thread(target=burn, daemon=True).start()

    def _stop_burners(self) -> None:
        if not any(self.is_on(n) for n in ("cpu_saturation", "cpu_burst", "cascading_failure")):
            for e in self._burners:
                e.set()
            self._burners.clear()

    def _fill_disk(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        i = 0
        chunk = b"\0" * (4 * 1024 * 1024)
        while self.is_on("disk_full"):
            try:
                with open(self.data_dir / f"filler-{i}", "wb") as f:
                    f.write(chunk)
                i += 1
                time.sleep(0.5 / self.intensity("disk_full"))
            except OSError:
                time.sleep(2)
