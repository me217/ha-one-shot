# One Shot for Home Assistant

**Ask Assist for a temporary, run-once automation. It plans it, reads it back, waits for your yes, then Home Assistant handles it on its own and the One Shot deletes itself afterwards.**

> *"Keep an eye on the rain and shut the shed doors if it starts."*
> *"Turn the lights off when I leave, but only if the kids are still home."*
> *"In 25 minutes, turn the media room lights on."*
> *"Tonight at 9, close the garage, then turn the porch light off 10 minutes later."*

> Status: early version (0.6), tested on a single setup (Home Assistant with a local LLM). Written against HA 2024.10+ APIs. Bug reports welcome in Issues.

## Why it exists

Home Assistant has two kinds of automation: permanent ones, and nothing. If you only need something to happen *once* ("it flooded, so the doors are open for now, shut them if it rains again"), you either build a full automation in the editor and remember to delete it, or you forget about it.

One Shot fills that gap. You describe the one-off in plain words to Assist. A One Shot is:

- **Temporary.** It runs once, then removes itself. If it never triggers, it expires (24 hours by default, 14 days at most).
- **Confirmed.** Assist never arms anything without reading the plan back and getting your clear yes.
- **Deterministic.** After you confirm, the LLM is out of the loop. Home Assistant's own trigger and script engines run it, so it works even if the model, the network or the LLM server is down by then.
- **Visible.** A "One Shot" sidebar page shows what is armed and what recently ran, and everything is written to the logbook.

It is **not** a replacement for normal automations. It is for the "just this once" jobs.

## What you can ask for

**Triggers** (what starts it)

| Type | Example |
|---|---|
| State change | "when the gate opens", "if the dryer has been off for 10 minutes" |
| Numeric above / below | "if the rain rate goes above 0", "if the tank drops below 20%" |
| Time of day | "at 5:30pm" |
| Sunrise / sunset, with offset | "30 minutes before sunset" |
| Delay | "in 25 minutes", "after an hour" (the clock starts when you confirm) |
| Date and time | "on Friday at 6am" (within 14 days) |
| A timer finishing | an existing `timer.*` entity |
| **Several triggers** | "if it rains **or** the wind picks up": any one fires it |

**Conditions** (extra checks made the moment it triggers, like automation conditions)
"only if I'm home", "only after 17:00", "only on weekdays", "only if the sun is down", "only if a numeric sensor is above/below something". If they aren't met, an event trigger keeps waiting; a time/delay trigger has no second chance, so the One Shot ends as *Skipped*.

**Actions** (what it does)
Service calls on devices you have exposed to Assist, up to 5 steps, with optional **wait steps** (1-120 minutes) between them: "pump on, wait 10 minutes, pump off". Once triggered, a One Shot that is waiting stays on the sidebar page as *Running* (with a Cancel button) and picks up again after a Home Assistant restart.

Note the difference: "if the door opens, wait 10 minutes, then do X" fires immediately and then waits; "if the door is open **for** 10 minutes" needs it to stay open throughout. Assist asks if you're unclear which you mean.

## How it works

| Step | Who | What |
|---|---|---|
| 1 | LLM | asks one clarifying question if the request is vague, finds the right entities, calls `propose_oneshot` (creates nothing) |
| 2 | Code | validates against Home Assistant's own schemas; returns a plain-English summary, warnings and a token |
| 3 | You | confirm out loud or in chat |
| 4 | LLM | calls `confirm_oneshot(token)`. Code refuses unless you have actually replied since the proposal |
| 5 | Code | arms the trigger, survives restarts, fires once, notifies you, removes itself |

The model also gets helper tools to search entities (`find_entities`, including "list every light"), list your One Shots and cancel one by its title. The model gives each One Shot a short title like *"Shed doors on rain"*, which is how you refer to it.

It understands who is asking: "if **I'm** home" resolves to the person linked to your HA user (or it asks, if the speaker can't be identified, such as on some voice devices).

## The sidebar page and sensor

- **One Shot** in the sidebar (admins): active One Shots with a countdown, owner, and a Cancel button; a Recent list of what fired, failed, was skipped, expired or was cancelled, including which trigger fired it.
- **`sensor.one_shot_active`**: count of active One Shots, with the list as attributes. Use it on your own dashboards.
- **Logbook**: arming, firing, skipping, expiry and cancelling all appear under "One Shot".
- **Events**: `one_shot_completed` and `one_shot_expired` fire on the HA bus so you can chain your own automations.

## Safety

- Only entities **exposed to Assist** can be used or controlled.
- Actions are limited to an allowlist of device types (default: covers, lights, switches, fans, climate, media players, vacuums, valves, humidifiers, scenes, scripts, input_booleans, notify). **Locks and alarm panels are off by default**; enable them in the options if you want them.
- Offline devices are flagged when the plan is proposed and again when it runs.
- Max 5 actions per One Shot, max 14 days expiry, proposal tokens last 5 minutes.
- Fire-once is enforced in code, not by the model.

## Access control

Set in the integration options:

- **Who may use it:** admins always can. Pick specific non-admin users, or leave empty to allow all users.
- **Unidentified requests** (e.g. voice satellites, where HA doesn't know who spoke): allow or block.
- **Admin-only device types:** e.g. everyone can control lights but only admins can control climate.
- **Ownership:** each One Shot remembers who made it. Non-admins can only list and cancel their own; admins see everything (and who made it) on the sidebar page. A proposal can only be confirmed by the user who made it.

## Requirements

- Home Assistant 2024.10 or newer.
- A conversation agent that supports **tool calling** (an LLM integration such as OpenAI, Anthropic, Google, Ollama, or an OpenAI-compatible local server). The built-in non-LLM agent can't use it.
- The devices you want to control must be **exposed to Assist**.

## Install

**With HACS (recommended)**

1. HACS > three-dot menu > **Custom repositories**.
2. Add `https://github.com/me217/ha-one-shot`, category **Integration**.
3. Download **One Shot**, then restart Home Assistant.

**Manually:** copy `custom_components/one_shot` into your HA `config/custom_components/` and restart.

**Then:**

1. Settings > Devices & services > Add integration > **One Shot**.
2. In your conversation agent's settings, under **Control Home Assistant**, tick **both** *Assist* and *One Shot*.
3. In the One Shot options, set the notification action (e.g. `notify.mobile_app_yourphone`; the default is a persistent notification) and review the access settings.
4. Link each HA user to a person (Settings > People) so "if I'm home" resolves to the right person.

## Tips

- Say "one shot" or just describe the one-off: *"tonight at 9, if the garage is still open, close it"*.
- Don't say "set a timer": Assist has its own voice timers. One Shot uses a delay trigger instead and doesn't need a separate timer. (It is told to avoid the built-in timer tools.)
- Refer to a One Shot by its title when cancelling: *"cancel the shed one"*.
- For rain, use a real rain sensor if you have one. A `weather.*` entity is a forecast provider's guess, not a measurement. The model is told to prefer sensors and to ask when there is a choice.

## Known limits

- If the trigger's state is already true when you ask, it waits for the *next* change (the model is told to say so).
- A wait step that ends while Home Assistant is down resumes if it is less than 10 minutes late; otherwise that One Shot is skipped and you're told. The result notification arrives when the whole sequence finishes.
- A timed One Shot (time/delay/date) that came due while HA was down fires if it's less than 10 minutes late; otherwise it's skipped and you're told.
- The sidebar page is view-and-cancel only; creating a One Shot is done through Assist.
- "Confirmation required" is enforced by checking that a new user message arrived after the proposal. This depends on Home Assistant exposing the conversation to tools; if it doesn't, a weaker fallback applies (the same request can't both propose and confirm).
- How well the vague-request questions, entity choices and wording work depends on the quality of your LLM.

## Contributing

Issues and pull requests are welcome. If something goes wrong, include the Assist debug trace (Settings > Voice assistants > your assistant > Debug) and any `one_shot` lines from Settings > System > Logs.

## Licence

MIT. See [LICENSE](LICENSE).
