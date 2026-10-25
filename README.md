# One Shot for Home Assistant

Tell Assist: *"Keep an eye on the rain and shut the shed doors if it starts."*

Assist works out the entities, **reads back exactly what it is about to arm, and waits for your yes**.
After that it is plain Home Assistant code (the normal trigger + script engines). No LLM is in the
loop. The One Shot runs **once**, notifies you, and deletes itself. Unfired One Shots expire (default 24 h).

> Status: early version (0.6), tested on a single setup (HA with a local LLM). Written against HA 2024.10+ APIs. Bug reports welcome in Issues.

## How it works

| Step | Who | What |
|---|---|---|
| 1 | LLM | finds entities, calls `propose_oneshot` (creates nothing) |
| 2 | Code | validates against HA's own schemas; returns summary + warnings + token |
| 3 | You | confirm out loud / in chat |
| 4 | LLM | calls `confirm_oneshot(token)` |
| 5 | Code | arms trigger, survives restarts, fires once, notifies, self-deletes |

## Safety

- Only entities **exposed to Assist** can be used or controlled.
- Actions are limited to an allowlist of device types (default: covers, lights, switches, fans,
  climate, media players, vacuums, valves, humidifiers, scenes, scripts, input_booleans, notify).
  **Locks and alarm panels are off by default**; enable in the integration options.
- Max 5 actions per One Shot, max 14 days expiry, proposal tokens last 5 minutes.
- Fire-once is enforced in code, not by the model.

## Access control

Set in the integration options:

- **Who may use it:** admins always can. Pick specific non-admin users, or leave empty for all users.
- **Unidentified requests** (e.g. voice satellites, where HA doesn't know who spoke): allow or block.
- **Admin-only device types:** e.g. let everyone control lights but only admins control climate.
- **Ownership:** each One Shot remembers who made it. Non-admins can only list and cancel their own;
  admins see everything (and who made it) in the sidebar page. A proposal can only be confirmed by
  the same user who made it.
- The sidebar page itself is admin-only.

## Install

**With HACS (recommended)**

1. HACS > three-dot menu > **Custom repositories**.
2. Add `https://github.com/me217/ha-one-shot`, category **Integration**.
3. Download **One Shot**, then restart Home Assistant.

**Manually:** copy `custom_components/one_shot` into your HA `config/custom_components/` and restart.

**Then:**

1. Settings > Devices & services > Add integration > **One Shot**.
2. In your conversation agent's settings, under **Control Home Assistant**, tick **both** *Assist* and *One Shot*.
   The model needs to support tool calling.
3. Set the notification action (e.g. `notify.mobile_app_yourphone`) in the integration options.
4. Link each HA user to a person (Settings > People) so "if I'm home" resolves to the right person.

## Triggers supported

State change (e.g. `weather.home` becomes `rainy`, optional "for N minutes"), numeric above/below,
a time of day, sunrise/sunset with offset.

## Conditions and multiple triggers

- **Several triggers:** "if it rains **or** the wind picks up". Any one of them fires the One Shot.
- **Conditions:** "...but **only if** someone is home / after 17:00 / on weekdays / the sun is down".
  Checked at the moment a trigger fires, like automation conditions. If they aren't met, an event
  trigger keeps waiting; a time/delay trigger has no second chance so the One Shot ends as *Skipped*.
- **Logbook:** arming, firing, skipping, expiry and cancelling are written to the HA logbook, and each
  history entry records which trigger fired it.

## Events

`one_shot_completed` and `one_shot_expired` fire on the HA bus, so you can chain your own automations.

## Known limits / ideas

- If the condition is already true when you ask, it waits for the next change (the model is told to say so).
- Conditions ("only if I'm home") are not in v0.1.
- Idea: optional confirmation via an actionable phone notification instead of voice.
- v0.2 adds a "One Shot" sidebar page (active + recent history, cancel button, admin only) and `sensor.one_shot_active`.
