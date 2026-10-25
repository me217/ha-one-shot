"""Watch lifecycle: validate -> pending -> armed -> fired/expired -> removed.

Everything after confirmation is deterministic HA machinery (trigger + script
engines). No LLM is involved once a watch is armed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import secrets
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import condition
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.script import Script
from homeassistant.helpers.storage import Store
from homeassistant.helpers.trigger import (
    async_initialize_triggers,
    async_validate_trigger_config,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADMIN_ONLY_DOMAINS,
    CONF_ALLOW_ANONYMOUS,
    CONF_ALLOWED_DOMAINS,
    CONF_ALLOWED_USERS,
    CONF_DEFAULT_EXPIRY_HOURS,
    CONF_NOTIFY_SERVICE,
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_EXPIRY_HOURS,
    DEFAULT_NOTIFY_SERVICE,
    DOMAIN,
    EVENT_COMPLETED,
    EVENT_EXPIRED,
    HISTORY_LIMIT,
    MAX_EXPIRY_HOURS,
    PENDING_TTL_SECONDS,
    SIGNAL_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


MISSED_GRACE = timedelta(minutes=10)
MIN_CONFIRM_SECONDS = 3
_WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
NOT_ANSWERED = (
    "The user hasn't answered yet. Read the summary to them, stop, and wait for their "
    "reply. Only confirm in a later turn, after they say yes."
)


def _label(watch: dict[str, Any]) -> str:
    """Human title for a watch (older watches without one fall back to the summary)."""
    return watch.get("title") or watch["summary"][:60]


def _fmt_minutes(n: int) -> str:
    hours, mins = divmod(n, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


class WatchError(Exception):
    """Raised with a message that is safe to show/speak to the user."""


class WatchManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.watches: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self._pending: dict[str, tuple[float, dict[str, Any]]] = {}
        self._unsub_trigger: dict[str, list[Callable[[], None]]] = {}
        self._unsub_expiry: dict[str, Callable[[], None]] = {}

    # ---- options -------------------------------------------------------

    @property
    def allowed_domains(self) -> set[str]:
        return set(self.entry.options.get(CONF_ALLOWED_DOMAINS, DEFAULT_ALLOWED_DOMAINS))

    @property
    def admin_only_domains(self) -> set[str]:
        return set(self.entry.options.get(CONF_ADMIN_ONLY_DOMAINS, []))

    @property
    def allowed_users(self) -> set[str]:
        return set(self.entry.options.get(CONF_ALLOWED_USERS, []))

    @property
    def allow_anonymous(self) -> bool:
        return bool(self.entry.options.get(CONF_ALLOW_ANONYMOUS, True))

    async def _check_access(self, user_id: str | None) -> bool:
        """Raise if this user may not use One Shot. Returns True if they are an admin."""
        if user_id is None:
            if not self.allow_anonymous:
                raise WatchError(
                    "I can't tell who is asking, so I can't set up or manage One Shots from here."
                )
            return False
        user = await self.hass.auth.async_get_user(user_id)
        if user is None or not user.is_active:
            raise WatchError("That user isn't allowed to use One Shot.")
        if user.is_admin:
            return True
        if self.allowed_users and user_id not in self.allowed_users:
            raise WatchError("You don't have permission to use One Shot.")
        return False

    async def user_name(self, user_id: str | None) -> str:
        if user_id is None:
            return "Voice / unidentified"
        user = await self.hass.auth.async_get_user(user_id)
        return user.name if user and user.name else "Unknown user"

    @property
    def default_expiry_hours(self) -> int:
        return int(self.entry.options.get(CONF_DEFAULT_EXPIRY_HOURS, DEFAULT_EXPIRY_HOURS))

    @property
    def notify_service(self) -> str:
        return self.entry.options.get(CONF_NOTIFY_SERVICE, DEFAULT_NOTIFY_SERVICE)

    # ---- load / shutdown ----------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.history = data.get("history", [])
        now = dt_util.utcnow()
        for wid, watch in data.get("watches", {}).items():
            if "trigger" in watch:  # v0.5 and earlier stored a single trigger
                watch["triggers"] = [watch.pop("trigger")]
            watch.setdefault("conditions", [])
            if dt_util.parse_datetime(watch["expires"]) <= now:
                self._announce(f"One Shot expired while HA was down and was dropped: {_label(watch)}")
                continue
            kept, due_now = [], False
            for trig in watch["triggers"]:
                if trig.get("platform") == "at":
                    when = dt_util.parse_datetime(trig["when"])
                    if when <= now:
                        due_now = due_now or (now - when <= MISSED_GRACE)
                        continue
                kept.append(trig)
            watch["triggers"] = kept
            if not kept and not due_now:
                self._record(watch, wid, "expired", "missed while Home Assistant was down")
                self._announce(f"One Shot was due while HA was down and was skipped: {_label(watch)}")
                continue
            self.watches[wid] = watch
            try:
                await self._arm(wid)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Could not re-arm One Shot %s; dropping it", wid)
                self.watches.pop(wid, None)
                continue
            if due_now:
                self.hass.async_create_task(
                    self._fire(wid, "the scheduled time (just after a restart)", timed=True)
                )
        await self._save()

    @callback
    def async_shutdown(self) -> None:
        for unsub in (*(u for us in self._unsub_trigger.values() for u in us), *self._unsub_expiry.values()):
            unsub()
        self._unsub_trigger.clear()
        self._unsub_expiry.clear()

    async def _save(self) -> None:
        await self._store.async_save({"watches": self.watches, "history": self.history})
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    def _record(self, watch: dict[str, Any], wid: str, status: str, detail: str = "") -> None:
        self.history.insert(
            0,
            {
                "id": wid,
                "title": _label(watch),
                "summary": watch["summary"],
                "owner": watch.get("owner"),
                "status": status,
                "detail": detail,
                "time": dt_util.utcnow().isoformat(),
            },
        )
        del self.history[HISTORY_LIMIT:]

    async def panel_data(self) -> dict[str, Any]:
        names: dict[str | None, str] = {}
        for src in (self.watches.values(), self.history):
            for item in src:
                owner = item.get("owner")
                if owner not in names:
                    names[owner] = await self.user_name(owner)
        return {
            "watches": [
                {
                    "id": wid,
                    "fires": min((t["when"] for t in w["triggers"] if t.get("when")), default=None),
                    "owner": names[w.get("owner")],
                    "title": _label(w),
                    **{k: w[k] for k in ("summary", "created", "expires")},
                }
                for wid, w in self.watches.items()
            ],
            "history": [{**h, "owner": names.get(h.get("owner"), "")} for h in self.history],
        }

    # ---- propose (creates nothing) --------------------------------------

    async def async_propose(
        self, args: dict[str, Any], user_id: str | None = None, marker: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        is_admin = await self._check_access(user_id)
        specs = [args, *(args.get("or_triggers") or [])]
        if len(specs) > 5:
            raise WatchError("At most 5 triggers per One Shot.")
        built = [self._build_trigger(s) for s in specs]
        triggers = [t for t, _ in built]
        trigger_text = " or ".join(txt for _, txt in built)
        conditions, cond_text = self._build_conditions(args.get("conditions") or [])
        actions, action_text = self._build_actions(args.get("actions") or [], is_admin)

        hours = int(args.get("expires_hours") or self.default_expiry_hours)
        if not 1 <= hours <= MAX_EXPIRY_HOURS:
            raise WatchError(f"Expiry must be between 1 and {MAX_EXPIRY_HOURS} hours.")

        # Prove the HA engines accept it before the user is asked to confirm.
        ha_triggers = [t for t in triggers if t["platform"] != "at"]
        if ha_triggers:
            await async_validate_trigger_config(self.hass, ha_triggers)
        for cond in conditions:
            await condition.async_validate_condition_config(self.hass, cond)
        cv.SCRIPT_SCHEMA(actions)

        title = (args.get("title") or args.get("description") or "").strip()[:60]
        if not title:
            raise WatchError("Give the One Shot a short title, e.g. 'Shed doors on rain'.")
        warnings = self._warnings(specs)
        offline = self._unavailable(actions)
        if offline:
            warnings.append(
                "These devices are unavailable right now and would not respond: "
                + ", ".join(offline)
                + ". Tell the user."
            )
        summary = (
            f"When {trigger_text}{cond_text}, then {action_text}. "
            f"It runs once, then deletes itself"
            f" (or is cancelled automatically after {hours} hours)."
        )

        self._purge_pending()
        token = secrets.token_hex(3)
        self._pending[token] = (
            time.monotonic(),
            {
                "title": title,
                "description": title,
                "summary": summary,
                "triggers": triggers,
                "conditions": conditions,
                "actions": actions,
                "expires_hours": hours,
                "owner": user_id,
                "marker": marker or {},
            },
        )
        return {
            "status": "needs_user_confirmation",
            "title": title,
            "token": token,
            "summary": summary,
            "warnings": warnings,
        }

    # ---- confirm (arms it) ------------------------------------------------

    async def async_confirm(
        self, token: str, user_id: str | None = None, marker: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._check_access(user_id)
        self._purge_pending()
        key = token.strip().lower()
        item = self._pending.get(key)
        if item is None:
            raise WatchError("That proposal has expired or doesn't exist. Please set it up again.")
        spec = item[1]
        if spec.get("owner") != user_id:
            raise WatchError("That proposal was made by someone else.")
        self._require_new_user_turn(item[0], spec.get("marker") or {}, marker or {})
        del self._pending[key]

        now = dt_util.utcnow()
        expires = now + timedelta(hours=spec["expires_hours"])
        triggers = []
        for trig in spec["triggers"]:
            trig = dict(trig)
            if trig["platform"] == "at":
                if "delay_minutes" in trig:  # the clock starts at confirmation
                    when = now + timedelta(minutes=trig["delay_minutes"])
                    trig = {"platform": "at", "when": when.isoformat()}
                when = dt_util.parse_datetime(trig["when"])
                if when <= now:
                    raise WatchError("That time has already passed. Please set it up again.")
                expires = max(expires, when + timedelta(hours=1))
            triggers.append(trig)

        wid = secrets.token_hex(4)
        self.watches[wid] = {
            "title": spec["title"],
            "description": spec["description"],
            "summary": spec["summary"],
            "triggers": triggers,
            "conditions": spec["conditions"],
            "actions": spec["actions"],
            "owner": spec.get("owner"),
            "created": now.isoformat(),
            "expires": expires.isoformat(),
        }
        try:
            await self._arm(wid)
        except Exception as err:  # noqa: BLE001
            self._cleanup(wid)
            raise WatchError(f"Couldn't start the One Shot: {err}") from err
        await self._save()
        self._logbook(f"{spec['title']}: armed. {spec['summary']}")
        return {"status": "armed", "title": spec["title"], "summary": spec["summary"]}

    # ---- entity search ---------------------------------------------------

    def find_entities(self, query: str = "", domain: str | None = None) -> list[dict[str, Any]]:
        """Search Assist-exposed entities; best matches (most query words) first.

        With only a domain (e.g. "light") it lists every exposed entity in that domain.
        """
        words = [w for w in (query or "").lower().replace("_", " ").split() if w]
        domain = (domain or "").strip().lower() or None
        if not words and not domain:
            raise WatchError("Give a few words to search for (e.g. 'rain rate') or a domain (e.g. 'light').")
        scored = []
        for st in self.hass.states.async_all(domain) if domain else self.hass.states.async_all():
            if not async_should_expose(self.hass, "conversation", st.entity_id):
                continue
            attrs = st.attributes
            haystack = " ".join(
                str(x).lower().replace("_", " ")
                for x in (
                    st.entity_id,
                    st.name,
                    attrs.get("device_class", ""),
                    attrs.get("unit_of_measurement", ""),
                )
            )
            score = sum(1 for w in words if w in haystack) if words else 1
            if not score:
                continue
            if st.domain == "weather":
                kind = "forecast/provider conditions (not a measurement)"
            elif st.domain in ("sensor", "binary_sensor"):
                kind = "measured sensor"
            elif st.domain in ("person", "device_tracker"):
                kind = "presence (home / not_home)"
            else:
                kind = f"{st.domain} (controllable device)"
            scored.append(
                (
                    score,
                    {
                        "entity_id": st.entity_id,
                        "name": st.name,
                        "state": st.state,
                        "unit": attrs.get("unit_of_measurement"),
                        "device_class": attrs.get("device_class"),
                        "kind": kind,
                    },
                )
            )
        scored.sort(key=lambda x: -x[0])
        best = scored[0][0] if scored else 0
        # keep only the strongest matches so a broad word ("light") doesn't flood the model
        found = [e for s, e in scored if s == best]
        # a domain-only listing is a deliberate "show me all of them"
        return found[:50] if (domain and not words) else found[:15]

    @staticmethod
    def _require_new_user_turn(created: float, old: dict[str, Any], new: dict[str, Any]) -> None:
        """Refuse to arm unless the user has spoken since the proposal (the model can't self-confirm)."""
        age = time.monotonic() - created
        _LOGGER.debug("confirm check: proposed=%s now=%s age=%.1fs", old, new, age)
        if age < MIN_CONFIRM_SECONDS:
            _LOGGER.warning("One Shot refused confirm: too soon after proposal (%s -> %s)", old, new)
            raise WatchError(NOT_ANSWERED)
        same_chat = old.get("conversation_id") and old.get("conversation_id") == new.get("conversation_id")
        if same_chat and old.get("user_msgs") is not None and new.get("user_msgs") is not None:
            if new["user_msgs"] <= old["user_msgs"]:
                _LOGGER.warning("One Shot refused confirm: no new user message (%s -> %s)", old, new)
                raise WatchError(NOT_ANSWERED)
        elif old.get("context_id") and old.get("context_id") == new.get("context_id"):
            _LOGGER.warning("One Shot refused confirm: same request context (%s -> %s)", old, new)
            raise WatchError(NOT_ANSWERED)

    # ---- list / cancel -----------------------------------------------------

    def _visible(self, user_id: str | None, is_admin: bool) -> dict[str, dict[str, Any]]:
        """Admins see everything; everyone else only their own watches."""
        return {
            wid: w
            for wid, w in self.watches.items()
            if is_admin or w.get("owner") == user_id
        }

    async def async_list(self, user_id: str | None = None) -> list[dict[str, Any]]:
        is_admin = await self._check_access(user_id)
        return [
            {"title": _label(w), "summary": w["summary"], "expires": w["expires"]}
            for w in self._visible(user_id, is_admin).values()
        ]

    async def async_cancel(self, ref: str, user_id: str | None = None) -> dict[str, Any]:
        """Cancel by id, or by a unique substring of the summary."""
        is_admin = await self._check_access(user_id)
        ref_l = ref.strip().lower()
        matches = [
            wid
            for wid, w in self._visible(user_id, is_admin).items()
            if wid == ref_l or ref_l in _label(w).lower() or ref_l in w["summary"].lower()
        ]
        if not matches:
            raise WatchError("No active One Shot matches that.")
        if len(matches) > 1:
            titles = ", ".join(_label(self.watches[x]) for x in matches)
            raise WatchError(f"More than one One Shot matches ({titles}); say which one.")
        wid = matches[0]
        watch = self.watches[wid]
        self._record(watch, wid, "cancelled")
        await self._remove(wid)
        self._logbook(f"{_label(watch)}: cancelled")
        return {"status": "cancelled", "title": _label(watch), "summary": watch["summary"]}

    # ---- arming / firing -----------------------------------------------------

    async def _arm(self, wid: str) -> None:
        watch = self.watches[wid]
        unsubs: list[Callable[[], None]] = []
        self._unsub_trigger[wid] = unsubs  # registered early so _cleanup can undo a partial arm

        async def _fired(run_variables: dict, context: Context | None = None) -> None:
            desc = (run_variables.get("trigger") or {}).get("description", "")
            await self._fire(wid, desc or "a trigger")

        ha_triggers = [t for t in watch["triggers"] if t["platform"] != "at"]
        if ha_triggers:
            trigger_conf = await async_validate_trigger_config(self.hass, ha_triggers)
            unsub = await async_initialize_triggers(
                self.hass,
                trigger_conf,
                _fired,
                DOMAIN,
                watch["description"],
                _LOGGER.log,
            )
            if unsub is None:
                raise WatchError("the trigger could not be started")
            unsubs.append(unsub)

        for trig in watch["triggers"]:
            if trig["platform"] == "at":

                @callback
                def _due(_now: datetime) -> None:
                    self.hass.async_create_task(self._fire(wid, "the scheduled time", timed=True))

                unsubs.append(
                    async_track_point_in_utc_time(
                        self.hass, _due, dt_util.parse_datetime(trig["when"])
                    )
                )

        @callback
        def _expired(_now: datetime) -> None:
            self.hass.async_create_task(self._expire(wid))

        self._unsub_expiry[wid] = async_track_point_in_utc_time(
            self.hass, _expired, dt_util.parse_datetime(watch["expires"])
        )

    async def _conditions_pass(self, watch: dict[str, Any]) -> bool:
        for cfg in watch.get("conditions", []):
            validated = await condition.async_validate_condition_config(self.hass, cfg)
            test = await condition.async_from_config(self.hass, validated)
            if not test(self.hass, {}):
                return False
        return True

    async def _fire(self, wid: str, source: str = "", timed: bool = False) -> None:
        watch = self.watches.get(wid)
        if watch is None:  # already fired/cancelled; guarantees "once"
            return
        via = f" (triggered by {source})" if source else ""

        try:
            passed = await self._conditions_pass(watch)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("One Shot %s: condition check failed", wid)
            await self._remove(wid)
            self._record(watch, wid, "failed", f"condition error: {err}{via}")
            await self._save()
            self._announce(f"One Shot FAILED (condition error): {_label(watch)}")
            return

        if not passed:
            if timed and not any(t["platform"] != "at" for t in watch["triggers"]):
                await self._remove(wid)
                self._record(watch, wid, "skipped", f"conditions not met{via}")
                await self._save()
                self._logbook(f"{_label(watch)}: skipped, conditions not met{via}")
                self._announce(f"One Shot skipped (conditions not met): {_label(watch)}")
            else:
                self._logbook(f"{_label(watch)}: conditions not met{via}; still waiting")
            return

        await self._remove(wid)  # first, so it can never fire twice
        offline = self._unavailable(watch["actions"])
        detail = ""
        try:
            script = Script(
                self.hass,
                cv.SCRIPT_SCHEMA(watch["actions"]),
                f"One Shot: {watch['summary']}",
                DOMAIN,
            )
            await script.async_run(context=Context())
            outcome = "done"
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("One Shot %s failed", wid)
            outcome = f"FAILED ({err})"
            detail = str(err)
        note = f"not reachable at the time: {', '.join(offline)}" if offline else ""
        info = "; ".join(x for x in ((detail or via.strip(" ()")), note) if x)
        self._record(watch, wid, "failed" if detail else "done", info)
        await self._save()
        self.hass.bus.async_fire(
            EVENT_COMPLETED,
            {
                "id": wid,
                "title": _label(watch),
                "summary": watch["summary"],
                "outcome": outcome,
                "triggered_by": source,
            },
        )
        self._logbook(f"{_label(watch)}: {outcome}{via}")
        self._announce(f"One Shot {outcome}: {_label(watch)}" + (f" ({note})" if note else ""))

    async def _expire(self, wid: str) -> None:
        watch = self.watches.get(wid)
        if watch is None:
            return
        self._record(watch, wid, "expired")
        await self._remove(wid)
        self.hass.bus.async_fire(
            EVENT_EXPIRED, {"id": wid, "title": _label(watch), "summary": watch["summary"]}
        )
        self._logbook(f"{_label(watch)}: expired without triggering")
        self._announce(f"One Shot expired without triggering: {_label(watch)}")

    def _cleanup(self, wid: str) -> None:
        for unsub in self._unsub_trigger.pop(wid, []):
            unsub()
        unsub = self._unsub_expiry.pop(wid, None)
        if unsub:
            unsub()
        self.watches.pop(wid, None)

    async def _remove(self, wid: str) -> None:
        self._cleanup(wid)
        await self._save()

    def _logbook(self, message: str) -> None:
        if self.hass.services.has_service("logbook", "log"):
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "logbook",
                    "log",
                    {"name": "One Shot", "message": message, "domain": DOMAIN},
                    blocking=False,
                )
            )

    def _announce(self, message: str) -> None:
        domain, _, service = self.notify_service.partition(".")
        data: dict[str, Any] = {"message": message}
        if domain == "persistent_notification":
            data["title"] = "One Shot"
        if not self.hass.services.has_service(domain, service):
            _LOGGER.warning("Notify service %s not found; message: %s", self.notify_service, message)
            return
        self.hass.async_create_task(
            self.hass.services.async_call(domain, service, data, blocking=False)
        )

    # ---- spec -> HA config ---------------------------------------------------

    def _entity(self, entity_id: str | None) -> str:
        if not entity_id:
            raise WatchError("An entity is required.")
        entity_id = entity_id.strip().lower()
        if self.hass.states.get(entity_id) is None:
            raise WatchError(f"I can't find {entity_id}.")
        if not async_should_expose(self.hass, "conversation", entity_id):
            raise WatchError(f"{entity_id} isn't exposed to Assist.")
        return entity_id

    def _name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.name if state else entity_id

    def _build_trigger(self, a: dict[str, Any]) -> tuple[dict[str, Any], str]:
        kind = a.get("trigger_type")
        if kind == "state":
            ent = self._entity(a.get("entity_id"))
            if not a.get("to_state"):
                raise WatchError("Say which state to wait for (to_state).")
            trig: dict[str, Any] = {"platform": "state", "entity_id": ent, "to": a["to_state"]}
            text = f"{self._name(ent)} becomes '{a['to_state']}'"
            if a.get("from_state"):
                trig["from"] = a["from_state"]
                text += f" (from '{a['from_state']}')"
            if a.get("for_minutes"):
                trig["for"] = {"minutes": int(a["for_minutes"])}
                text += f" for {int(a['for_minutes'])} minutes"
            return trig, text
        if kind == "numeric":
            ent = self._entity(a.get("entity_id"))
            if a.get("above") is None and a.get("below") is None:
                raise WatchError("Give an above and/or below value.")
            trig = {"platform": "numeric_state", "entity_id": ent}
            parts = []
            if a.get("above") is not None:
                trig["above"] = a["above"]
                parts.append(f"goes above {a['above']}")
            if a.get("below") is not None:
                trig["below"] = a["below"]
                parts.append(f"goes below {a['below']}")
            if a.get("for_minutes"):
                trig["for"] = {"minutes": int(a["for_minutes"])}
            return trig, f"{self._name(ent)} " + " and ".join(parts)
        if kind == "time":
            at = a.get("at_time")
            try:
                parsed = datetime.strptime(at or "", "%H:%M")
            except ValueError:
                raise WatchError("Time must be HH:MM (24-hour).") from None
            return (
                {"platform": "time", "at": parsed.strftime("%H:%M:00")},
                f"it next reaches {parsed.strftime('%H:%M')}",
            )
        if kind == "sun":
            event = a.get("sun_event")
            if event not in ("sunrise", "sunset"):
                raise WatchError("sun_event must be sunrise or sunset.")
            off = int(a.get("sun_offset_minutes") or 0)
            sign = "-" if off < 0 else ""
            trig = {
                "platform": "sun",
                "event": "sunrise" if event == "sunrise" else "sunset",
                "offset": f"{sign}{abs(off) // 60:02d}:{abs(off) % 60:02d}:00",
            }
            text = event if not off else f"{abs(off)} minutes {'before' if off < 0 else 'after'} {event}"
            return trig, text
        if kind == "delay":
            n = a.get("delay_minutes")
            if not n or not 1 <= int(n) <= MAX_EXPIRY_HOURS * 60:
                raise WatchError(f"delay_minutes must be between 1 and {MAX_EXPIRY_HOURS * 60}.")
            return (
                {"platform": "at", "delay_minutes": int(n)},
                f"{_fmt_minutes(int(n))} have passed",
            )
        if kind == "datetime":
            try:
                naive = datetime.strptime(a.get("at_datetime") or "", "%Y-%m-%d %H:%M")
            except ValueError:
                raise WatchError("at_datetime must be local time as 'YYYY-MM-DD HH:MM'.") from None
            local = naive.replace(tzinfo=dt_util.get_default_time_zone())
            when = dt_util.as_utc(local)
            now = dt_util.utcnow()
            if when <= now:
                raise WatchError("That date/time is in the past.")
            if when > now + timedelta(hours=MAX_EXPIRY_HOURS):
                raise WatchError(f"That is more than {MAX_EXPIRY_HOURS // 24} days away.")
            return (
                {"platform": "at", "when": when.isoformat()},
                f"it is {local.strftime('%a %d %b %H:%M')}",
            )
        if kind == "timer":
            ent = self._entity(a.get("entity_id"))
            if not ent.startswith("timer."):
                raise WatchError("trigger_type 'timer' needs a timer.* entity.")
            return (
                {
                    "platform": "event",
                    "event_type": "timer.finished",
                    "event_data": {"entity_id": ent},
                },
                f"the timer {self._name(ent)} finishes",
            )
        raise WatchError("trigger_type must be one of: state, numeric, time, sun, delay, datetime, timer.")

    def _build_actions(
        self, raw: list[dict[str, Any]], is_admin: bool = False
    ) -> tuple[list[dict[str, Any]], str]:
        if not raw:
            raise WatchError("At least one action is required.")
        if len(raw) > 5:
            raise WatchError("At most 5 actions per One Shot.")
        actions, texts = [], []
        for item in raw:
            if item.get("wait_minutes"):
                n = int(item["wait_minutes"])
                if not 1 <= n <= 120:
                    raise WatchError("A wait step must be between 1 and 120 minutes.")
                actions.append({"delay": {"minutes": n}})
                texts.append(f"wait {_fmt_minutes(n)}")
                continue
            service = (item.get("action") or "").strip().lower()
            if service and "." not in service and item.get("entity_ids"):
                service = f"{item['entity_ids'][0].split('.', 1)[0].lower()}.{service}"
            domain, _, name = service.partition(".")
            if not name:
                raise WatchError(f"'{service}' isn't a valid action (expected domain.service).")
            if domain not in self.allowed_domains:
                raise WatchError(
                    f"One Shots aren't allowed to use {domain} actions. "
                    "An admin can allow that in the integration options."
                )
            if not self.hass.services.has_service(domain, name):
                raise WatchError(f"The action {service} doesn't exist.")
            entities = [self._entity(e) for e in item.get("entity_ids") or []]
            if not is_admin:
                restricted = self.admin_only_domains
                if domain in restricted or any(e.split(".", 1)[0] in restricted for e in entities):
                    raise WatchError("Only an admin can set up One Shots that control that kind of device.")
            for e in entities:
                if e.split(".", 1)[0] not in self.allowed_domains:
                    raise WatchError(f"{e} is a device type One Shots aren't allowed to control.")
            if domain not in ("notify",) and not entities:
                raise WatchError(f"{service} needs at least one entity.")
            step: dict[str, Any] = {"action": service}
            if entities:
                step["target"] = {"entity_id": entities}
            if item.get("data"):
                step["data"] = item["data"]
            actions.append(step)
            who = ", ".join(self._name(e) for e in entities)
            texts.append(f"run {service}" + (f" on {who}" if who else ""))
        if not any("action" in step for step in actions):
            raise WatchError("At least one real action is required (not just a wait).")
        return actions, "do: " + ", then ".join(texts)

    def _unavailable(self, actions: list[dict[str, Any]]) -> list[str]:
        """Names of action targets that are offline right now (a service call silently skips them)."""
        names = []
        for step in actions:
            for ent in (step.get("target") or {}).get("entity_id", []):
                st = self.hass.states.get(ent)
                if st is not None and st.state in ("unavailable", "unknown") and st.name not in names:
                    names.append(st.name)
        return names

    def _build_conditions(self, raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Extra checks made at the moment a trigger fires (like automation conditions)."""
        if len(raw) > 5:
            raise WatchError("At most 5 conditions per One Shot.")
        confs, texts = [], []
        for c in raw:
            kind = c.get("condition_type")
            if kind == "state":
                ent = self._entity(c.get("entity_id"))
                if not c.get("state"):
                    raise WatchError("A state condition needs a state.")
                conf: dict[str, Any] = {"condition": "state", "entity_id": ent, "state": c["state"]}
                text = f"{self._name(ent)} is '{c['state']}'"
                if c.get("negate"):
                    conf = {"condition": "not", "conditions": [conf]}
                    text = f"{self._name(ent)} is not '{c['state']}'"
            elif kind == "numeric":
                ent = self._entity(c.get("entity_id"))
                if c.get("above") is None and c.get("below") is None:
                    raise WatchError("A numeric condition needs above and/or below.")
                conf = {"condition": "numeric_state", "entity_id": ent}
                parts = []
                if c.get("above") is not None:
                    conf["above"] = c["above"]
                    parts.append(f"above {c['above']}")
                if c.get("below") is not None:
                    conf["below"] = c["below"]
                    parts.append(f"below {c['below']}")
                text = f"{self._name(ent)} is " + " and ".join(parts)
            elif kind == "time":
                conf = {"condition": "time"}
                parts = []
                for key in ("after", "before"):
                    if c.get(key):
                        try:
                            parsed = datetime.strptime(c[key], "%H:%M")
                        except ValueError:
                            raise WatchError("Condition times must be HH:MM (24-hour).") from None
                        conf[key] = parsed.strftime("%H:%M:00")
                        parts.append(f"{key} {parsed.strftime('%H:%M')}")
                days = [d.lower() for d in c.get("weekdays") or []]
                if days:
                    if any(d not in _WEEKDAYS for d in days):
                        raise WatchError("weekdays must be from: " + ", ".join(_WEEKDAYS))
                    conf["weekday"] = days
                    parts.append("on " + "/".join(days))
                if len(conf) == 1:
                    raise WatchError("A time condition needs after, before and/or weekdays.")
                text = "the time is " + " ".join(parts)
            elif kind == "sun":
                if c.get("sun_state") not in ("up", "down"):
                    raise WatchError("sun_state must be up or down.")
                up = c["sun_state"] == "up"
                conf = {
                    "condition": "state",
                    "entity_id": "sun.sun",
                    "state": "above_horizon" if up else "below_horizon",
                }
                text = f"the sun is {c['sun_state']}"
            else:
                raise WatchError("condition_type must be one of: state, numeric, time, sun.")
            confs.append(conf)
            texts.append(text)
        return confs, (", but only if " + " and ".join(texts)) if texts else ""

    def _warnings(self, specs: list[dict[str, Any]]) -> list[str]:
        warnings = []
        for a in specs:
            if a.get("trigger_type") == "state":
                state = self.hass.states.get((a.get("entity_id") or "").lower())
                if state and state.state == a.get("to_state"):
                    warnings.append(
                        f"{state.name} is ALREADY '{a['to_state']}'. That trigger will only fire the "
                        "next time it changes to that state, not right now. Tell the user."
                    )
        return warnings

    def _purge_pending(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL_SECONDS
        for tok in [t for t, (ts, _) in self._pending.items() if ts < cutoff]:
            del self._pending[tok]
