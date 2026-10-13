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

## Scope

Pairing, the TVs' diagnostic sensors and lock switch, and `egretnvrtv.show_camera`/`hide_camera`. Otherwise, once paired, the TV talks to Home Assistant's existing REST/WebSocket APIs on its own.
