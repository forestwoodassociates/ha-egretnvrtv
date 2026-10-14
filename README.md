# <img src="custom_components/egretnvrtv/brand/dark_icon.png" width="48"> Egret NVR TV (Home Assistant integration)

A Home Assistant custom integration that finds an [Egret NVR TV](https://play.google.com/store/apps/details?id=com.programmersbox.forestwoodass.egretnvrtv) Android TV app on your local network and pairs it with this Home Assistant instance — no Long-Lived Access Token to generate and copy, no server address to type on the TV's remote.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=forestwoodassociates&repository=ha-egretnvrtv&category=integration)

Requires [HACS](https://hacs.xyz/) already installed on your Home Assistant instance — the button opens HACS's "Add custom repository" dialog pre-filled with this repo; you still confirm and hit Download there.

## What it does

1. Discovers the TV automatically via mDNS/zeroconf (or you can enter its address manually if discovery doesn't find it).
2. Asks a few setup questions here instead of on the TV's own remote: your Frigate server's MQTT topic prefix (only needed if you changed it from Frigate's default of `frigate`), and whether to register the TV as a Home Assistant companion app (and what to call it) — the same optional step the TV's own setup wizard offers.
3. Tells the TV to show a short pairing code on screen.
4. You type that code back in here. Home Assistant then mints a fresh access token for the TV and sends it — along with this instance's address and your answers above — directly to the TV over your local network.

The TV saves that host/token exactly as if you'd entered them by hand in its own setup wizard, and connects immediately.

Once paired, you can also put a camera's live view on a TV — from an automation, a dashboard button, or by voice ("show the driveway" on the TV in the room you're in). See [Showing a camera on a TV](#showing-a-camera-on-a-tv-egretnvrtvshow_camera) and [Voice control](#voice-control-show-the-driveway).

## Requirements

- Home Assistant and the TV on the same local network — this only ever works over LAN (no Nabu Casa/remote pairing; see the app's own README for why).
- The Egret NVR TV app already installed and running on the TV.
- Home Assistant must have a local URL configured (Settings → System → Network) so it knows what address to hand the TV.

## Installation

### HACS (custom repository)

Click the badge above (opens HACS's "Add custom repository" dialog pre-filled), or add it by hand:

1. HACS → Integrations → ⋮ → Custom repositories → add this repo's URL, category "Integration".
2. Install "Egret NVR TV", restart Home Assistant.

### Manual

Copy `custom_components/egretnvrtv` into your Home Assistant `custom_components` directory and restart.

## Setup

Settings → Devices & Services — a discovered TV appears automatically ("Set up" prompt); otherwise use **Add Integration** → "Egret NVR TV" and enter its address manually. Follow the on-screen steps described above.

## Re-pairing

Running setup again for the same TV (e.g. after a factory reset or reinstall) mints a new token and revokes the old one — the previous token stops working the moment the new pairing completes.

## Showing a camera on a TV: `egretnvrtv.show_camera`

Shows a camera's live view on the TVs you target — in a popup over whatever is on screen, or full screen. Requires an Egret NVR TV app version with live-view support (released after 0.9.27); an older app answers with an "update the app" error.

| Field | | |
|---|---|---|
| `camera` | required | The Frigate camera name (e.g. `driveway`), or the camera's name in Home Assistant |
| `mode` | `popup` (default) or `fullscreen` | |
| `duration` | 5–600 seconds, default 60 | How long the popup stays up; ignored for full screen |

Target a TV's device, or an **area** to reach whichever TV is in that room — assign each TV's device to its area under Settings → Devices & Services. On the TV, OK/Enter turns the popup into the full-screen view and Back closes it.

```yaml
action: egretnvrtv.show_camera
target:
  area_id: living_room
data:
  camera: driveway
  duration: 30
```

The call fails with the TV's own reason (e.g. no such camera) when no targeted TV could show it, so a script or voice command can report that back.

## Closing it: `egretnvrtv.hide_camera`

Closes whatever live view the targeted TVs are showing — the popup, or a full-screen live view — with the same device/area targeting. A TV with nothing open isn't an error; it only fails when no targeted TV could be reached.

```yaml
action: egretnvrtv.hide_camera
target:
  area_id: living_room
```

## Voice control: "show the driveway"

With a voice assistant in the same room as a TV, you can say **"show the driveway"** and that camera comes up on *that room's* TV. It's an ordinary automation that calls the two services above — copy it from [`examples/assist_show_camera.yaml`](examples/assist_show_camera.yaml).

| Say | What happens |
|---|---|
| "show (me) (the) *camera* (camera)" | The camera in a popup on this room's TV, for 60 seconds |
| "show (me) (the) *camera* full screen" | The full-screen live view instead |
| "put (the) *camera* on (the) TV" | Same as "show" |
| "hide the camera" / "close the camera" / "stop showing the camera" | Closes it |

Camera names are matched loosely against your camera entities — case, spaces, "the", a trailing "camera" and punctuation don't matter, so "pool room", "right back yard camera" or "the patio camera" all work. The reply says what it did ("Showing the Driveway camera on the Den TV"), or why it couldn't ("There's no Egret TV in Kitchen", "I don't know a camera called mailbox", or the TV's own error).

### Setting it up

1. **Put each TV and each voice device in an area.** Settings → Devices & Services → Devices → open the device → ✏️ → Area. The TV and the voice assistant in the same room need the same area — that's how a command finds "this room's" TV.
2. **Add the automation.** Settings → Automations & Scenes → Create Automation → Create new automation → ⋮ → Edit in YAML, paste the contents of [`examples/assist_show_camera.yaml`](examples/assist_show_camera.yaml), and save.
3. **If your voice assistant uses an AI conversation agent** (OpenAI, Google Gemini, Anthropic, Ollama, …), turn on **Prefer handling commands locally**: Settings → Voice assistants → open the assistant your voice devices use → toggle it on. Without it every sentence goes straight to the AI agent, which never runs the automation's sentences — and will often just make up an answer ("the driveway feed is unavailable"). With it, Home Assistant tries its own commands (and this automation's) first, and anything it doesn't recognize still goes to the AI agent. Not needed with the plain "Home Assistant" agent.
4. **Update each TV's app** to a version with live-view support (see [above](#showing-a-camera-on-a-tv-egretnvrtvshow_camera)) — a TV on an older version answers "couldn't show that camera".

Then say "show the driveway" to a voice assistant in a room with a TV.

### Troubleshooting

- **The reply isn't one of the automation's** (e.g. the AI agent answering on its own): step 3 is off for that voice device's assistant, or the sentence didn't match. Settings → Voice assistants → the assistant → ⋮ → Debug shows exactly what text was heard, which agent handled it, and whether it was "processed locally".
- **Speech-to-text that adds who spoke** — e.g. the heard text is `[dave] Show me the driveway camera.` — is handled: every sentence in the example also accepts a leading speaker label.
- **"There's no Egret TV in …"**: that voice device's area has no Egret TV in it (step 1). Typed commands from the Home Assistant app have no room at all, so they get this too.
- **"I don't know a camera called …"**: the spoken name didn't match a camera entity's id or name. Rename the camera entity in Home Assistant to what you'd say, or add the sentence wording you use to the automation's `command:` list.
- **Trying it without speaking**: Developer Tools → Assist lets you type a sentence — but it has no device, so you'll get the "no Egret TV" reply; that still shows the sentence matched.

## Scope

Pairing, the TVs' diagnostic sensors and lock switch, and `egretnvrtv.show_camera`/`hide_camera` (plus the example voice automation above). Otherwise, once paired, the TV talks to Home Assistant's existing REST/WebSocket APIs on its own.
