from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import MOCK_ENV
from app.dongle.base import Station
from app.dongle.mock import make_mock_stations

log = logging.getLogger(__name__)


class DongleManager:
    def __init__(self) -> None:
        self.stations: dict[str, Station] = {}
        self.mock = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def all(self) -> list[Station]:
        return list(self.stations.values())

    def get(self, station_id: str) -> Station:
        station = self.stations.get(station_id)
        if not station:
            raise KeyError(station_id)
        return station

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        force_mock = MOCK_ENV is not None and MOCK_ENV.strip().lower() in {"1", "true", "yes", "on"}
        force_real = MOCK_ENV is not None and MOCK_ENV.strip().lower() in {"0", "false", "no", "off"}
        if force_mock:
            await self._start_mock()
            return
        found = await self._start_real()
        if not found and not force_real:
            log.warning("No BleuIO dongles found; starting 4 mock stations. Set BLEUIO_MOCK=0 to disable fallback.")
            await self._start_mock()
        elif not found and force_real:
            log.error("BLEUIO_MOCK=0 but no dongles were detected.")

    async def _start_mock(self) -> None:
        self.mock = True
        for station in make_mock_stations(4):
            station.bind_loop(self._loop)  # type: ignore[arg-type]
            await station.start()
            self.stations[station.id] = station
        log.info("Mock mode: %s stations", len(self.stations))

    async def _start_real(self) -> int:
        try:
            from app.dongle.worker import BleuIoStation, list_candidate_ports
        except Exception as exc:
            log.warning("Cannot import BleuIO worker: %s", exc)
            return 0
        ports = list_candidate_ports()
        log.info("Candidate serial ports: %s", ports)
        started = 0
        for index, port in enumerate(ports, start=1):
            station_id = f"dongle-{index}"
            station = BleuIoStation(station_id, port)
            station.bind_loop(self._loop)  # type: ignore[arg-type]
            try:
                await asyncio.wait_for(station.start(), timeout=12)
            except Exception as exc:
                log.warning("Failed to open %s: %s", port, exc)
                try:
                    await station.close()
                except Exception:
                    pass
                continue
            # If ATI didn't look like BleuIO, still keep it if mac/firmware populated or product set.
            self.stations[station.id] = station
            started += 1
            log.info("Opened %s as %s (mac=%s fw=%s)", port, station.id, station.mac, station.firmware)
        self.mock = False
        return started

    async def refresh(self) -> dict[str, Any]:
        """Re-scan for dongles. Leaves claimed stations alone; adds newly found ports."""
        if self.mock:
            return {"mock": True, "stations": [s.snapshot() for s in self.all()]}
        from app.dongle.worker import BleuIoStation, list_candidate_ports

        existing_ports = {s.port for s in self.all()}
        added = []
        for port in list_candidate_ports():
            if port in existing_ports:
                continue
            station_id = f"dongle-{len(self.stations) + 1}"
            # Avoid id collision
            n = len(self.stations) + 1
            while f"dongle-{n}" in self.stations:
                n += 1
            station_id = f"dongle-{n}"
            station = BleuIoStation(station_id, port)
            station.bind_loop(self._loop)  # type: ignore[arg-type]
            try:
                await asyncio.wait_for(station.start(), timeout=12)
                self.stations[station.id] = station
                added.append(station.snapshot())
            except Exception as exc:
                log.warning("refresh: failed %s: %s", port, exc)
        return {"added": added, "stations": [s.snapshot() for s in self.all()]}

    async def close(self) -> None:
        for station in self.all():
            try:
                await station.close()
            except Exception:
                pass
        self.stations.clear()
