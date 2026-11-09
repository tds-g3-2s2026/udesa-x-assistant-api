"""The catalog of actions and the checks on what the model answers."""

import pytest

from assistant_api.agent.catalog import CATALOG, invalid_arguments, tool_specs


def test_every_action_is_a_tool_with_all_its_parameters_required():
    specs = {spec["toolSpec"]["name"]: spec["toolSpec"] for spec in tool_specs()}

    assert set(specs) == set(CATALOG)
    reply = specs["reply_to_post"]["inputSchema"]["json"]
    assert reply["required"] == ["ref", "text"]


def test_destructive_actions_are_not_in_the_catalog():
    for name in ("delete_post", "block", "report", "change_password", "close_account"):
        assert name not in CATALOG


def test_what_others_see_goes_through_confirmation():
    confirmed = {name for name, action in CATALOG.items() if action.needs_confirmation}

    assert confirmed == {"retweet_post", "reply_to_post", "compose_post", "follow"}


@pytest.mark.parametrize(
    ("name", "arguments", "problem"),
    [
        ("like_post", {}, "Falta ref"),
        ("like_post", {"ref": 3}, "Falta ref"),
        ("scroll_feed", {"direction": "left"}, "direction tiene que ser uno de: down, up"),
        ("set_preference", {"name": "font", "value": "big"}, "name tiene que ser uno de"),
        ("set_preference", {"name": "theme", "value": "blue"}, "theme tiene que ser uno de"),
        ("compose_post", {"text": "x" * 281}, "El texto supera los 280 caracteres"),
    ],
)
def test_arguments_outside_the_schema_are_refused(name, arguments, problem):
    assert invalid_arguments(name, arguments).startswith(problem)


def test_valid_arguments_pass():
    assert invalid_arguments("set_preference", {"name": "theme", "value": "dark"}) is None
    assert invalid_arguments("compose_post", {"text": "x" * 280}) is None
    assert invalid_arguments("refresh_feed", {}) is None
