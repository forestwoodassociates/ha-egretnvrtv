import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "egretnvrtv"


def make_entry(hass, device_id, host, title):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"host": host, "port": 7676, "device_id": device_id, "device_name": title,
              "webhook_id": f"secret-{device_id}"},
        unique_id=device_id,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_two_tvs(hass):
    living = make_entry(hass, "tv-living", "10.0.0.1", "Living Room TV")
    bed = make_entry(hass, "tv-bed", "10.0.0.2", "Bedroom TV")
    assert await hass.config_entries.async_setup(living.entry_id)
    await hass.async_block_till_done()
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    living_area = areas.async_create("Living Room")
    bed_area = areas.async_create("Bedroom")
    living_dev = devices.async_get_device(identifiers={(DOMAIN, "tv-living")})
    bed_dev = devices.async_get_device(identifiers={(DOMAIN, "tv-bed")})
    devices.async_update_device(living_dev.id, area_id=living_area.id)
    devices.async_update_device(bed_dev.id, area_id=bed_area.id)
    return living_area, bed_area, living_dev


async def test_area_target_reaches_only_that_rooms_tv(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view", json={"status": "ok", "camera": "Driveway"})
    aioclient_mock.post("http://10.0.0.2:7676/live_view", json={"status": "ok", "camera": "Driveway"})

    result = await hass.services.async_call(
        DOMAIN, "show_camera", {"camera": "driveway", "area_id": living_area.id},
        blocking=True, return_response=True)

    assert result == {"tvs": {"Living Room TV": {"success": True, "camera": "Driveway"}}}
    assert aioclient_mock.call_count == 1
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert str(url) == "http://10.0.0.1:7676/live_view"
    assert body == {"camera": "driveway", "mode": "popup", "duration": 60}
    assert headers["X-Egret-Webhook-Id"] == "secret-tv-living"


async def test_device_target_and_fullscreen(hass, aioclient_mock):
    _, _, living_dev = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view", json={"status": "ok", "camera": "Doorbell"})
    await hass.services.async_call(
        DOMAIN, "show_camera",
        {"camera": "doorbell", "mode": "fullscreen", "duration": 30, "device_id": living_dev.id},
        blocking=True)
    assert aioclient_mock.mock_calls[0][2] == {"camera": "doorbell", "mode": "fullscreen", "duration": 30}


async def test_tv_error_is_raised_for_the_caller(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view", status=404,
                        json={"status": "error", "message": "No camera named nope"})
    with pytest.raises(HomeAssistantError, match="No camera named nope"):
        await hass.services.async_call(
            DOMAIN, "show_camera", {"camera": "nope", "area_id": living_area.id}, blocking=True)


async def test_area_without_a_tv_is_a_validation_error(hass, aioclient_mock):
    await setup_two_tvs(hass)
    kitchen = ar.async_get(hass).async_create("Kitchen")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "show_camera", {"camera": "driveway", "area_id": kitchen.id}, blocking=True)
    assert aioclient_mock.call_count == 0


async def test_unreachable_tv(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view", exc=TimeoutError())
    with pytest.raises(HomeAssistantError, match="Couldn't reach the TV"):
        await hass.services.async_call(
            DOMAIN, "show_camera", {"camera": "driveway", "area_id": living_area.id}, blocking=True)


async def test_duration_out_of_range_is_rejected(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN, "show_camera", {"camera": "driveway", "duration": 2, "area_id": living_area.id},
            blocking=True)


async def test_hide_camera_reports_what_closed(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view/hide", json={"status": "ok", "closed": True})
    result = await hass.services.async_call(
        DOMAIN, "hide_camera", {"area_id": living_area.id}, blocking=True, return_response=True)
    assert result == {"tvs": {"Living Room TV": {"success": True, "closed": True}}}
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert headers["X-Egret-Webhook-Id"] == "secret-tv-living"


async def test_hide_camera_with_nothing_open_is_not_an_error(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view/hide", json={"status": "ok", "closed": False})
    await hass.services.async_call(DOMAIN, "hide_camera", {"area_id": living_area.id}, blocking=True)


async def test_hide_camera_unreachable_tv(hass, aioclient_mock):
    living_area, _, _ = await setup_two_tvs(hass)
    aioclient_mock.post("http://10.0.0.1:7676/live_view/hide", exc=TimeoutError())
    with pytest.raises(HomeAssistantError, match="Couldn't close the live view"):
        await hass.services.async_call(DOMAIN, "hide_camera", {"area_id": living_area.id}, blocking=True)
