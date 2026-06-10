"""Coordinator for Gail Morning Commute (Twyford → Ealing Broadway → Hammersmith).

Reads the London TfL integration sensors directly (no Huxley/Darwin), and builds
the same trains[].leg2[] schema that the multileg card expects.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, NUM_TRAINS, MAX_LEG2,
    SCAN_INTERVAL_PEAK, SCAN_INTERVAL_OFFPEAK, SCAN_INTERVAL_NIGHT,
    EALING_INTERCHANGE_MINS,
    LEG1_HISTORY_PROXY_ENTITY, LEG2_HISTORY_PROXY_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

# TfL sensor entities
TWY_ELIZABETH = "sensor.london_tfl_elizabeth_910gtwyford"   # leg1 TWY → EAL (eastbound)
EAL_DISTRICT  = "sensor.london_tfl_district_940gzzlueby"    # leg2 EAL → HMM (eastbound District)

EAL_TRANSIT_MINS = 25   # TWY → EAL on Elizabeth line
HMM_TRANSIT_MINS = 6    # EAL → HMM on District line


def _get_scan_interval() -> timedelta:
    h = datetime.now().hour
    if 6 <= h < 10 or 16 <= h < 20:
        return timedelta(seconds=SCAN_INTERVAL_PEAK)
    if 23 <= h or h < 5:
        return timedelta(seconds=SCAN_INTERVAL_NIGHT)
    return timedelta(seconds=SCAN_INTERVAL_OFFPEAK)


def _parse_dt(val):
    """Parse a TfL 'expected' ISO timestamp into an aware datetime."""
    if not val:
        return None
    try:
        s = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _hhmm(dt):
    if not dt:
        return None
    return dt.astimezone().strftime("%H:%M")


class GailMorningCoordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=_get_scan_interval())
        self.entry = entry
        self._history: dict = {}

    def schedule_hsp_fetch(self) -> None:
        self.hass.async_create_background_task(
            self._async_history_fetch(),
            name="gail_morning_commute_history_fetch",
        )

    async def _async_history_fetch(self) -> None:
        import asyncio as _aio
        await _aio.sleep(30)
        try:
            result = {}
            for key, entity, label in [
                ("leg1", LEG1_HISTORY_PROXY_ENTITY, "Twyford → Ealing Broadway (Elizabeth line)"),
                ("leg2", LEG2_HISTORY_PROXY_ENTITY, "Ealing Broadway → Hammersmith (District line)"),
            ]:
                s = self.hass.states.get(entity)
                if s and s.state not in (None, "unknown", "unavailable", ""):
                    attrs = s.attributes
                    result[key] = {
                        "label": label,
                        "on_time_pct_today": attrs.get("on_time_pct_today"),
                        "on_time_pct_7day": attrs.get("on_time_pct_7day"),
                        "on_time_pct_30day": attrs.get("on_time_pct_30day"),
                        "daily_breakdown": attrs.get("daily_breakdown", []),
                        "best_day": attrs.get("best_day"),
                        "worst_day": attrs.get("worst_day"),
                        "proxy": True,
                    }
            if result:
                self._history = result
                if self.data:
                    self.data["history"] = result
                    if isinstance(self.data.get("summary"), dict):
                        self.data["summary"]["history"] = result
                    self.async_set_updated_data(self.data)
        except Exception as err:
            _LOGGER.warning("Gail morning history proxy error: %s", err)

    def _tfl_departures(self, entity_id, filter_fn=None):
        """Return upcoming TfL departures (sorted), each as a normalised dict."""
        s = self.hass.states.get(entity_id)
        if not s or "departures" not in s.attributes:
            return []
        now = datetime.now(timezone.utc)
        out = []
        for d in s.attributes["departures"]:
            dt = _parse_dt(d.get("expected"))
            if not dt or dt <= now:
                continue
            if filter_fn and not filter_fn(d):
                continue
            out.append({
                "dt": dt,
                "destination": d.get("destination", ""),
                "designation": (d.get("line") or {}).get("designation", ""),
            })
        out.sort(key=lambda x: x["dt"])
        return out

    async def _async_update_data(self) -> dict:
        self.update_interval = _get_scan_interval()
        try:
            # Leg 1: TWY → EAL (Elizabeth line eastbound). Exclude designation "3" = Reading (westbound).
            twy = self._tfl_departures(
                TWY_ELIZABETH,
                filter_fn=lambda d: (d.get("line") or {}).get("designation") != "3",
            )
            # Leg 2: EAL → HMM. Ealing Broadway is the District line western terminus;
            # the TfL feed reports destination as the terminus ("Ealing Broadway") and only
            # ~15 min ahead, so live time-matching for a connection 25+ min out is unreliable.
            # District/Piccadilly run every ~3 min, so synthesise connections from the
            # interchange time onward (still rendered in the standard leg2 row structure).
            EAL_FREQ_MINS = 3
            eal = self._tfl_departures(EAL_DISTRICT)  # kept for future use / availability check

            trains = []
            for l1 in twy[:NUM_TRAINS]:
                l1_dt = l1["dt"]
                eal_arr = l1_dt + timedelta(minutes=EAL_TRANSIT_MINS)
                board_after = eal_arr + timedelta(minutes=EALING_INTERCHANGE_MINS)

                leg2 = []
                for n in range(MAX_LEG2):
                    dep = board_after + timedelta(minutes=n * EAL_FREQ_MINS)
                    wait = max(0, round((dep - eal_arr).total_seconds() / 60))
                    leg2.append({
                        "time": _hhmm(dep),
                        "destination": "Hammersmith",
                        "status": "On time",
                        "delay_minutes": 0,
                        "platform": None,
                        "operator": "District / Piccadilly line",
                        "operator_code": "LU",
                        "wait_mins": wait,
                        "transit_mins": HMM_TRANSIT_MINS,
                    })

                total = None
                if leg2:
                    first_l2_dt = eal_arr + timedelta(minutes=leg2[0]["wait_mins"])
                    total = round((first_l2_dt - l1_dt).total_seconds() / 60) + HMM_TRANSIT_MINS

                trains.append({
                    "time": _hhmm(l1_dt),
                    "destination": l1["destination"] or "London",
                    "status": "On time",
                    "delay_minutes": 0,
                    "platform": None,
                    "operator": "Elizabeth Line",
                    "operator_code": "XR",
                    "transit_mins": EAL_TRANSIT_MINS,
                    "total_transit_mins": total,
                    "leg2": leg2,
                })

            data = {
                "summary": {
                    "state": trains[0]["time"] if trains else "No service",
                    "leg1_from": "TWY",
                    "leg1_to": "EAL",
                    "leg2_to": "HMM",
                    "ealing_interchange_mins": EALING_INTERCHANGE_MINS,
                    "farringdon_interchange_mins": EALING_INTERCHANGE_MINS,
                    "trains": trains,
                    "last_updated": datetime.now().astimezone().isoformat(),
                    "history": self._history,
                },
                "history": self._history,
            }
            for i, t in enumerate(trains, 1):
                data[f"train_{i}"] = {"state": t["time"], **t}
            return data

        except Exception as err:
            raise UpdateFailed(f"Error updating Gail morning commute: {err}") from err
