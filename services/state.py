import logging
from datetime import datetime, timezone

from config import ONBOARD_STATES, STATE_TTL_SECONDS
from db import save_onboard_state, load_onboard_state

log = logging.getLogger(__name__)

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)

user_states: dict[int, dict] = {}
_last_scan:  dict[int, datetime] = {}
SCAN_COOLDOWN_SEC = 4


def _get_state(uid: int) -> dict:
    s = user_states.get(uid)
    if not s:
        return {}
    ts = s.get("_ts")
    if ts and (_utcnow() - ts).total_seconds() > STATE_TTL_SECONDS:
        user_states.pop(uid, None)
        return {}
    return s


def _set_state(uid: int, state: str, data: dict | None = None):
    user_states[uid] = {"state": state, "data": data or {}, "_ts": _utcnow()}


def _set_onboard_state(uid: int, state: str, data: dict) -> None:
    user_states[uid] = {"state": state, "data": data, "_ts": _utcnow()}
    try:
        save_onboard_state(uid, state, data)
    except Exception as exc:
        log.warning(f"save_onboard_state uid={uid}: {exc}")


def _try_restore_onboard(uid: int) -> None:
    if uid in user_states:
        return
    try:
        persisted = load_onboard_state(uid)
    except Exception:
        return
    if persisted and persisted.get("state") in ONBOARD_STATES:
        user_states[uid] = {
            "state": persisted["state"],
            "data":  persisted["data"],
            "_ts":   _utcnow(),
        }
        log.info(f"Restored onboard state uid={uid} state={persisted['state']}")
