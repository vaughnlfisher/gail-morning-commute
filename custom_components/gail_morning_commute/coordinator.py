"""Coordinator for Gail Morning Commute (Twyford → Ealing Broadway → Hammersmith)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, DARWIN_TOKEN,
    LEG1_FROM, LEG1_TO, LEG2_FROM, LEG2_TO,
    EALING_INTERCHANGE_MINS, NUM_TRAINS, MAX_LEG2,
    SCAN_INTERVAL_PEAK, SCAN_INTERVAL_OFFPEAK, SCAN_INTERVAL_NIGHT,
    HUXLEY_ROWS, EASTBOUND_TERMINI, HAMMERSMITH_TERMINI,
    LEG1_HISTORY_PROXY_ENTITY, LEG2_HISTORY_PROXY_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

HUXLEY_DEP = (
    "https://huxley2.azurewebsites.net/departures/{frm}/to/{to}/{rows}"
    "?expand=true&accessToken={token}"
)


def _get_scan_interval() -> timedelta:
    h = datetime.now().hour
    if 6 <= h < 10 or 16 <= h < 20:
        return timedelta(seconds=SCAN_INTERVAL_PEAK)
    if 23 <= h or h < 5:
        return timedelta(seconds=SCAN_INTERVAL_NIGHT)
    return timedelta(seconds=SCAN_INTERVAL_OFFPEAK)


def _parse_hhmm_after(val, ref):
    try:
        h, m = map(int, val.split(":"))
        dt = ref.replace(hour=h, minute=m, second=0, microsecond=0)
        if (dt - ref).total_seconds() < -3600:
            dt += timedelta(days=1)
        return dt
    except (ValueError, TypeError, AttributeError):
        return None


def _svc_dest(svc):
    dest = svc.get("destination") or []
    if isinstance(dest, list) and dest:
        return dest[0].get("locationName", "")
    return str(dest)


def _svc_time(svc):
    now = datetime.now().astimezone()
    for key in ("etd", "std"):
        val = (svc.get(key) or "").strip()
        if val in ("", "Delayed", "Cancelled", "On time"):
            continue
        try:
            h, m = map(int, val.split(":"))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if (dt - now).total_seconds() < -3600:
                dt += timedelta(days=1)
            return dt
        except (ValueError, TypeError):
            continue
    std = (svc.get("std") or "").strip()
    if std:
        try:
            h, m = map(int, std.split(":"))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if (dt - now).total_seconds() < -3600:
                dt += timedelta(days=1)
            return dt
        except (ValueError, TypeError):
            pass
    return None


def _svc_status(svc):
    etd = (svc.get("etd") or "").strip()
    if etd == "Cancelled":
        return "Cancelled", None
    if etd in ("On time", ""):
        return "On time", 0
    if etd == "Delayed":
        return "Delayed", None
    std = (svc.get("std") or "").strip()
    try:
        eh, em = map(int, etd.split(":"))
        sh, sm = map(int, std.split(":"))
        delay = (eh * 60 + em) - (sh * 60 + sm)
        if delay < 0:
            delay += 1440
        return ("On time" if delay == 0 else "Delayed"), delay
    except (ValueError, TypeError):
        return "On time", 0


def _arrival_at(svc, dest_names, dep_dt):
    scp = svc.get("subsequentCallingPoints")
    if not scp or not isinstance(scp, list):
        return None, None
    pts = scp[0].get("callingPoint", []) if isinstance(scp[0], dict) else []
    for p in pts:
        name = (p.get("locationName") or "").lower()
        if any(d in name for d in dest_names):
            t = (p.get("et") or "").strip()
            if t in ("", "On time", "Delayed", "Cancelled"):
                t = (p.get("st") or "").strip()
            arr_dt = _parse_hhmm_after(t, dep_dt)
            if arr_dt:
                transit = max(0, round((arr_dt - dep_dt).total_seconds() / 60))
                return arr_dt, transit
            break
    return None, None


def _is_to(svc, termini):
    dest = _svc_dest(svc).lower()
    return any(kw in dest for kw in termini)


def _upcoming(services, after_dt, termini=None):
    out = []
    for svc in services:
        if termini and not _is_to(svc, termini):
            continue
        dt = _svc_time(svc)
        if not dt or dt < after_dt:
            continue
        status, delay = _svc_status(svc)
        out.append({
            "dt": dt,
            "time": dt.strftime("%H:%M"),
            "destination": _svc_dest(svc),
            "status": status,
            "delay_minutes": delay,
            "platform": svc.get("platform"),
            "operator": svc.get("operator"),
            "operator_code": svc.get("operatorCode"),
            "_svc": svc,
        })
    out.sort(key=lambda x: x["dt"])
    return out


class GailMorningCoordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=_get_scan_interval())
        self.entry = entry
        self._history: dict = {}

    def schedule_hsp_fetch(self) -> None:
        self.hass.async_create_background_task(
            self._async_hsp_fetch(),
            name="gail_morning_commute_hsp_fetch",
        )

    async def _async_hsp_fetch(self) -> None:
        import asyncio as _aio
        await _aio.sleep(30)
        try:
            result = {}
            # Both legs are TfL — proxy from Vaughn's existing sensors
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
            _LOGGER.warning("Gail morning HSP proxy error: %s", err)

    async def _fetch_leg(self, frm: str, to: str) -> list[dict]:
        url = HUXLEY_DEP.format(frm=frm, to=to, rows=HUXLEY_ROWS, token=DARWIN_TOKEN)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Huxley %s->%s HTTP %s", frm, to, resp.status)
                        return []
                    data = await resp.json(content_type=None)
                    return data.get("trainServices") or []
        except Exception as err:
            _LOGGER.warning("Huxley %s->%s error: %s", frm, to, err)
            return []

    async def _async_update_data(self) -> dict:
        self.update_interval = _get_scan_interval()
        try:
            now = datetime.now().astimezone()

            leg1_services = await self._fetch_leg(LEG1_FROM, LEG1_TO)
            leg2_services = await self._fetch_leg(LEG2_FROM, LEG2_TO)

            # Leg 1: Twyford → Ealing Broadway (Elizabeth line eastbound)
            leg1 = _upcoming(leg1_services, now, EASTBOUND_TERMINI)
            if not leg1:
                leg1 = _upcoming(leg1_services, now)

            trains = []
            for l1 in leg1[:NUM_TRAINS]:
                l1_arr, l1_transit = _arrival_at(l1["_svc"], ["ealing broadway"], l1["dt"])
                if l1_arr is None:
                    l1_arr = l1["dt"] + timedelta(minutes=25)
                    l1_transit = 25

                board2 = l1_arr + timedelta(minutes=EALING_INTERCHANGE_MINS)
                leg2_opts = []
                for l2 in _upcoming(leg2_services, board2, HAMMERSMITH_TERMINI):
                    _, l2_transit = _arrival_at(l2["_svc"], ["hammersmith"], l2["dt"])
                    if l2_transit is None:
                        l2_transit = 10
                    wait2 = max(0, round((l2["dt"] - l1_arr).total_seconds() / 60))
                    total = (l1_transit or 0) + (l2_transit or 0)
                    leg2_opts.append({
                        "time": l2["time"],
                        "destination": l2["destination"],
                        "status": l2["status"],
                        "delay_minutes": l2["delay_minutes"],
                        "platform": l2["platform"],
                        "operator": l2["operator"],
                        "operator_code": l2["operator_code"],
                        "wait_mins": wait2,
                        "transit_mins": l2_transit,
                        "total_transit_mins": total,
                    })
                    if len(leg2_opts) >= MAX_LEG2:
                        break

                total_transit = None
                if leg2_opts:
                    total_transit = leg2_opts[0].get("total_transit_mins")
                elif l1_transit is not None:
                    total_transit = l1_transit

                trains.append({
                    "time": l1["time"],
                    "destination": l1["destination"],
                    "status": l1["status"],
                    "delay_minutes": l1["delay_minutes"],
                    "platform": l1["platform"],
                    "operator": l1["operator"],
                    "operator_code": l1["operator_code"],
                    "transit_mins": l1_transit,
                    "total_transit_mins": total_transit,
                    "leg2": leg2_opts,
                })

            data = {
                "summary": {
                    "state": trains[0]["time"] if trains else "No service",
                    "leg1_from": LEG1_FROM,
                    "leg1_to": LEG1_TO,
                    "leg2_to": LEG2_TO,
                    "ealing_interchange_mins": EALING_INTERCHANGE_MINS,
                    "trains": trains,
                    "last_updated": now.isoformat(),
                    "history": self._history,
                },
                "history": self._history,
            }
            for i, t in enumerate(trains, 1):
                data[f"train_{i}"] = {"state": t["time"], **t}
            return data

        except Exception as err:
            raise UpdateFailed(f"Error updating Gail morning commute: {err}") from err
