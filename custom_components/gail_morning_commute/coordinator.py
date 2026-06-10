"""Coordinator for Gail Morning Commute (Twyford → Ealing Broadway → Hammersmith).

Reads the London TfL integration sensors directly (no Huxley/Darwin), and builds
the same trains[].leg2[] schema that the multileg card expects.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, NUM_TRAINS, MAX_LEG2,
    SCAN_INTERVAL_PEAK, SCAN_INTERVAL_OFFPEAK, SCAN_INTERVAL_NIGHT,
    EALING_INTERCHANGE_MINS,
    LEG1_HISTORY_PROXY_ENTITY, LEG2_HISTORY_PROXY_ENTITY,
    TFL_APP_KEY, TFL_JOURNEY_URL, NAPTAN_EALING_BROADWAY, NAPTAN_HAMMERSMITH,
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


    async def _fetch_journey(self, frm, to, depart_dt):
        """Call the TfL Journey Planner for real timetabled connections.

        Returns a list of dicts: time, destination(line summary), arrival, duration, wait.
        depart_dt is the earliest departure (Gail's interchange-ready time).
        """
        url = TFL_JOURNEY_URL.format(frm=frm, to=to)
        params = {
            "mode": "tube",
            "timeIs": "Departing",
            "date": depart_dt.strftime("%Y%m%d"),
            "time": depart_dt.strftime("%H%M"),
            "app_key": TFL_APP_KEY,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("TfL Journey %s->%s HTTP %s", frm, to, resp.status)
                        return []
                    data = await resp.json(content_type=None)
        except Exception as err:
            _LOGGER.warning("TfL Journey %s->%s error: %s", frm, to, err)
            return []

        out = []
        for jn in (data.get("journeys") or []):
            start = _parse_dt(jn.get("startDateTime"))
            arr = _parse_dt(jn.get("arrivalDateTime"))
            if not start:
                continue
            legs = jn.get("legs") or []
            lines = []
            for lg in legs:
                ro = (lg.get("routeOptions") or [{}])
                nm = ro[0].get("name") if ro else None
                if nm and nm not in lines:
                    lines.append(nm)
            out.append({
                "dt": start,
                "arr": arr,
                "duration": jn.get("duration"),
                "lines": lines,
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
            # Leg 2: EAL → HMM via the TfL Journey Planner (real timetabled tube connections).
            trains = []
            for l1 in twy[:NUM_TRAINS]:
                l1_dt = l1["dt"]
                eal_arr = l1_dt + timedelta(minutes=EAL_TRANSIT_MINS)
                board_after = eal_arr + timedelta(minutes=EALING_INTERCHANGE_MINS)

                journeys = await self._fetch_journey(
                    NAPTAN_EALING_BROADWAY, NAPTAN_HAMMERSMITH, board_after
                )
                leg2 = []
                for jn in journeys[:MAX_LEG2]:
                    wait = max(0, round((jn["dt"] - eal_arr).total_seconds() / 60))
                    line_summary = " + ".join(jn["lines"]) if jn["lines"] else "District / Piccadilly"
                    leg2.append({
                        "time": _hhmm(jn["dt"]),
                        "destination": f"Hammersmith ({line_summary})",
                        "status": "On time",
                        "delay_minutes": 0,
                        "platform": None,
                        "operator": line_summary,
                        "operator_code": "LU",
                        "wait_mins": wait,
                        "transit_mins": jn["duration"] if jn["duration"] else HMM_TRANSIT_MINS,
                    })

                total = None
                if leg2 and journeys:
                    j0 = journeys[0]
                    if j0.get("arr"):
                        total = round((j0["arr"] - l1_dt).total_seconds() / 60)

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
