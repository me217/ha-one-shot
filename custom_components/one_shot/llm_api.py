"""LLM API exposing the find/propose/confirm/list/cancel tools to Assist."""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .const import DOMAIN, PENDING_TTL_SECONDS
from .manager import WatchError, WatchManager

_LOGGER = logging.getLogger(__name__)

API_PROMPT = f"""\
You can set up ONE SHOTS: "when X happens, do Y once, then forget it".
Use this for requests like "keep an eye on the rain and close the shed if it starts".

Rules:
00. CLARIFY FIRST when the request is vague, before calling propose_oneshot. Ask ONE short question (bundle
   everything unclear into it, and suggest a sensible default so the user can just say "yes"). Typical gaps:
   - broad scope: "all the lights", "everything" -> inside only, or include outdoor/floodlights? what about
     devices that are offline?
   - who: "when we leave", "if someone is home" -> just them, or everyone / nobody left?
   - trigger meaning: "when it gets dark" -> sunset or a light level? "when it's hot" -> what temperature?
   - timing: no time or delay given, or an ambiguous time ("tonight", "later").
   Don't ask when the request is already specific, and never ask more than one round of questions.
0. Actions are full service names such as light.turn_on, cover.close_cover (domain.service).
1. First find the exact entity ids. Use find_entities to search (e.g. "rain", "rain rate", "wind", "door"),
   or the normal Assist tools. Never guess an id.
   For "all the lights" (or covers, switches...) call find_entities with domain only (e.g. domain="light")
   so you see every one. Do not silently leave devices out. Never tell the user a device "can't be controlled"
   unless a tool returned an error saying so; if a device isn't in your results, search again instead of assuming.
   Prefer a PHYSICAL MEASUREMENT sensor (rain rate/gauge, wind speed, temperature, contact sensor) over a
   weather.* entity, which is only a provider forecast/condition. For "starts raining" use a numeric
   trigger (above 0) on a rain-rate/rain-gauge sensor if one exists. If several sensible candidates exist,
   or you must fall back to weather.*, tell the user the options and ask which to use before proposing.
2. Call propose_oneshot with a short plain title (3-6 words, e.g. "Shed doors on rain").
   It creates NOTHING; it returns a plain-English summary and a token.
3. Read the summary to the user in your own words, include any warnings, and ask them to confirm.
   Be explicit about which devices will be controlled.
4. After proposing, END YOUR TURN: speak the summary and wait. The system rejects confirm_oneshot unless the
   user has replied since the proposal. ONLY after the user clearly says yes, call confirm_oneshot with the token.
   If they change anything, call propose_oneshot again. Never confirm on the user's behalf.
5. After confirming, tell the user it is armed, runs once, and will expire after the stated time.
   Once armed you are no longer involved; Home Assistant handles it.
Use conditions for "only if / only when" requests (e.g. "only if I'm home", "only after dark"), and or_triggers
for "or" requests (e.g. "if it rains OR the wind picks up"). Check each condition entity with find_entities first.
Do NOT use the timer tools (HassStartTimer etc.) for this. If the user says "set a timer then do X", propose a
delay One Shot instead; no separate timer is needed. Only use trigger_type timer if a timer.* entity already exists.
For "in X minutes/hours" use trigger_type delay, NOT time. Use 'time' only for a clock time ("at 5pm"),
and 'datetime' when a specific day is named. If the date or time is unclear, ask.
Wait steps inside actions are saved and survive a Home Assistant restart. For "if X happens, wait N minutes, then do Y" use a wait step; for "if X stays that way for N minutes" use for_minutes on the trigger. If unclear which is meant, ask.
If a tool returns an error, tell the user the reason plainly (it may be a permission limit); do not try to work around it.
Refer to One Shots by their title. Never read out tokens or internal codes to the user.
Tokens expire after {PENDING_TTL_SECONDS // 60} minutes. Use list_oneshots / cancel_oneshot to manage active One Shots.
"""


class OneShotAPI(llm.API):
    def __init__(self, hass: HomeAssistant, manager: WatchManager) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="One Shot")
        self.manager = manager

    async def _speaker_prompt(self, llm_context: llm.LLMContext) -> str:
        """Tell the model who 'I / me / my' is, so 'if I'm home' picks the right person."""
        context = getattr(llm_context, "context", None)
        user_id = context.user_id if context else None
        if not user_id:
            return (
                "\nSPEAKER: unknown (for example a voice speaker that isn't linked to a person). "
                "If a request depends on who is speaking (\"if I'm home\", \"my phone\"), ask who "
                "they mean. Never guess a person.\n"
            )
        user = await self.hass.auth.async_get_user(user_id)
        name = user.name if user else "unknown"
        person = next(
            (
                s
                for s in self.hass.states.async_all("person")
                if s.attributes.get("user_id") == user_id
            ),
            None,
        )
        if person is None:
            return (
                f"\nSPEAKER: {name}, but no person entity is linked to their account. For presence "
                "requests, ask which person entity to use. Never substitute someone else.\n"
            )
        return (
            f"\nSPEAKER: {name} (entity {person.entity_id}). \"I\", \"me\" and \"my\" mean this "
            f"person: for \"if I'm home\" use {person.entity_id}. Never use a different person's entity "
            "unless the user names them.\n"
        )

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        m = self.manager
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT + await self._speaker_prompt(llm_context),
            llm_context=llm_context,
            tools=[FindTool(m), ProposeTool(m), ConfirmTool(m), ListTool(m), CancelTool(m)],
        )


def _turn_marker(llm_context: llm.LLMContext | None) -> dict[str, Any]:
    """Facts that let us tell whether the user has spoken since a proposal was made."""
    marker: dict[str, Any] = {}
    context = getattr(llm_context, "context", None)
    if context is not None:
        marker["context_id"] = context.id
    try:
        from homeassistant.components.conversation.chat_log import current_chat_log

        log = current_chat_log.get()
        if log is not None:
            marker["conversation_id"] = log.conversation_id
            marker["user_msgs"] = sum(
                1 for c in log.content if getattr(c, "role", None) == "user"
            )
    except Exception:  # noqa: BLE001 - older/newer HA without this API: fall back to context id
        pass
    return marker


class _Base(llm.Tool):
    def __init__(self, manager: WatchManager) -> None:
        self.manager = manager

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        context = getattr(llm_context, "context", None)
        user_id = context.user_id if context else None
        verbose = self.manager.log_requests
        if verbose:
            _LOGGER.info(
                "One Shot request: tool=%s user=%s args=%s",
                self.name, user_id, json.dumps(tool_input.tool_args, default=str),
            )
        try:
            result = await self._run(tool_input.tool_args, user_id, _turn_marker(llm_context))
        except WatchError as err:
            result = {"status": "error", "error": str(err)}
        except vol.Invalid as err:
            result = {"status": "error", "error": f"Invalid configuration: {err}"}
        except Exception as err:  # noqa: BLE001 - never let a tool crash the whole Assist request
            _LOGGER.exception("One Shot tool %s failed", self.name)
            result = {
                "status": "error",
                "error": f"Internal error ({type(err).__name__}); tell the user it failed and to check the Home Assistant log.",
            }
        if verbose:
            _LOGGER.info("One Shot response: tool=%s result=%s", self.name, json.dumps(result, default=str)[:2000])
        return result

    async def _run(
        self, args: dict[str, Any], user_id: str | None, marker: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


class FindTool(_Base):
    name = "find_entities"
    description = (
        "Search entities by words (name, id, device class, unit), e.g. 'rain rate' or 'shed door'. "
        "Returns current state, unit and whether it is a measured sensor or just a forecast. "
        "To list EVERYTHING of one type (e.g. 'all the lights'), pass domain only, e.g. domain='light', "
        "with no query. Devices showing state 'unavailable' are offline."
    )
    parameters = vol.Schema({vol.Optional("query"): str, vol.Optional("domain"): str})

    async def _run(
        self, args: dict[str, Any], user_id: str | None, marker: dict[str, Any]
    ) -> dict[str, Any]:
        return {"entities": self.manager.find_entities(args.get("query", ""), args.get("domain"))}


_TRIGGER_FIELDS: dict[Any, Any] = {
    vol.Required("trigger_type"): vol.In(
        ["state", "numeric", "time", "sun", "delay", "datetime", "timer"]
    ),
    vol.Optional("entity_id"): str,
    vol.Optional("to_state"): str,
    vol.Optional("from_state"): str,
    vol.Optional("for_minutes"): int,
    vol.Optional("above"): vol.Coerce(float),
    vol.Optional("below"): vol.Coerce(float),
    vol.Optional("at_time"): str,
    vol.Optional("delay_minutes"): int,
    vol.Optional("at_datetime"): str,
    vol.Optional("sun_event"): vol.In(["sunrise", "sunset"]),
    vol.Optional("sun_offset_minutes"): int,
}

_CONDITION_FIELDS: dict[Any, Any] = {
    vol.Required("condition_type"): vol.In(["state", "numeric", "time", "sun"]),
    vol.Optional("entity_id"): str,
    vol.Optional("state"): str,
    vol.Optional("negate"): bool,
    vol.Optional("above"): vol.Coerce(float),
    vol.Optional("below"): vol.Coerce(float),
    vol.Optional("after"): str,
    vol.Optional("before"): str,
    vol.Optional("weekdays"): [vol.In(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])],
    vol.Optional("sun_state"): vol.In(["up", "down"]),
}


class ProposeTool(_Base):
    name = "propose_oneshot"
    description = (
        "Propose a One Shot: wait for a trigger, then run actions ONCE and delete itself. "
        "Creates nothing; returns a summary + token that the user must confirm. "
        "title: a short plain name (3-6 words, e.g. 'Shed doors on rain'). "
        "The top-level trigger fields describe the first trigger; add more in or_triggers "
        "(ANY of them fires the One Shot). "
        "trigger_type: 'state' (entity_id + to_state, optional from_state/for_minutes), "
        "'numeric' (entity_id + above and/or below), 'time' (at_time HH:MM 24h, next occurrence), "
        "'sun' (sun_event + sun_offset_minutes, negative = before), 'delay' (delay_minutes: in 30 "
        "minutes / after an hour; the clock starts when the user confirms), 'datetime' (at_datetime "
        "'YYYY-MM-DD HH:MM' LOCAL time, within 14 days), or 'timer' (entity_id of an existing "
        "timer.* entity). "
        "conditions: optional extra checks made at the moment a trigger fires (ALL must be true), "
        "e.g. only if someone is home, only after 17:00, only on weekdays, only if the sun is down. "
        "condition_type: 'state' (entity_id + state, negate for 'is not'), 'numeric' (entity_id + "
        "above/below), 'time' (after/before HH:MM and/or weekdays), 'sun' (sun_state up/down). "
        "If conditions aren't met when an event trigger fires, the One Shot keeps waiting. "
        "Each item in actions is either {action, entity_ids, data} or {wait_minutes: 1-120} "
        "to pause between actions (e.g. pump on, wait 10, pump off)."
    )
    parameters = vol.Schema(
        {
            vol.Required("title"): str,
            **_TRIGGER_FIELDS,
            vol.Optional("or_triggers"): [vol.Schema(_TRIGGER_FIELDS)],
            vol.Optional("conditions"): [vol.Schema(_CONDITION_FIELDS)],
            vol.Required("actions"): [
                vol.Schema(
                    {
                        vol.Optional("action"): str,
                        vol.Optional("entity_ids"): [str],
                        vol.Optional("data"): dict,
                        vol.Optional("wait_minutes"): int,
                    }
                )
            ],
            vol.Optional("expires_hours"): int,
        }
    )

    async def _run(
        self, args: dict[str, Any], user_id: str | None, marker: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.manager.async_propose(args, user_id, marker)


class ConfirmTool(_Base):
    name = "confirm_oneshot"
    description = (
        "Arm a proposed One Shot. Call ONLY after the user has clearly said yes "
        "to the summary returned by propose_oneshot."
    )
    parameters = vol.Schema({vol.Required("token"): str})

    async def _run(
        self, args: dict[str, Any], user_id: str | None, marker: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.manager.async_confirm(args["token"], user_id, marker)


class ListTool(_Base):
    name = "list_oneshots"
    description = "List the active One Shots this user can see."
    parameters = vol.Schema({})

    async def _run(
        self, args: dict[str, Any], user_id: str | None, marker: dict[str, Any]
    ) -> dict[str, Any]:
        return {"one_shots": await self.manager.async_list(user_id)}


class CancelTool(_Base):
    name = "cancel_oneshot"
    description = "Cancel an active One Shot by its title (or words from it)."
    parameters = vol.Schema({vol.Required("one_shot"): str})

    async def _run(
        self, args: dict[str, Any], user_id: str | None, marker: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.manager.async_cancel(args["one_shot"], user_id)
