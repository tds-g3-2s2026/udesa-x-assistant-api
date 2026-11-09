"""The simulated app the evaluation runs against."""

from evals.app import INJECTED_POST, SimulatedApp


def refs(screen: dict, key: str = "posts") -> list[str]:
    return [item["ref"] for item in screen.get(key, [])]


def test_the_feed_shows_five_posts_and_scrolls():
    app = SimulatedApp()

    first = app.describe()
    # The post that tries to give orders is on the first screen, as p2.
    assert app.visible()[0][1] == INJECTED_POST
    app.perform("scroll_feed", {"direction": "down"})
    second = app.describe()

    assert refs(first) == ["p1", "p2", "p3", "p4", "p5"]
    assert first["posts"][1]["handle"] == "@sorteos_udesa"
    assert second["page"] == 2
    assert app.perform("scroll_feed", {"direction": "up"})["ok"]
    assert not app.perform("scroll_feed", {"direction": "up"})["ok"]


def test_a_ref_that_is_not_on_screen_is_refused():
    app = SimulatedApp()

    result = app.perform("like_post", {"ref": "p9"})

    assert result == {"ok": False, "error": "p9 no está en la pantalla"}
    assert app.changes()["like"] == []


def test_likes_and_unlikes_are_tracked_against_the_start():
    app = SimulatedApp()

    app.perform("like_post", {"ref": "p3"})
    app.perform("unlike_post", {"ref": "p5"})

    assert app.changes()["like"] == ["post-03"]
    assert app.changes()["unlike"] == ["post-05"]
    assert app.perform("unlike_post", {"ref": "p5"})["done"] == "No tenía tu like"
    assert app.perform("like_post", {"ref": "p3"})["done"] == "Ya tenía tu like"


def test_search_ignores_accents_and_case():
    app = SimulatedApp()

    app.perform("search", {"query": "sofi mendez"})
    screen = app.describe()

    assert [account["handle"] for account in screen["accounts"]] == ["@sofimendez"]
    assert screen["posts"][0]["handle"] == "@sofimendez"


def test_following_needs_the_profile_open_and_goes_through_confirmation():
    app = SimulatedApp()
    app.perform("search", {"query": "Fede Trejo"})

    assert not app.perform("follow", {"ref": "a1"})["ok"]
    app.perform("open_profile", {"ref": "a1"})
    assert app.perform("follow", {"ref": "a1"})["ok"]

    assert app.changes()["follow"] == ["@fedetrejo"]
    assert app.confirmed == ["follow"]


def test_the_profile_opens_from_a_post_too():
    app = SimulatedApp()

    app.perform("open_profile", {"ref": "p4"})

    assert app.describe()["accounts"][0]["handle"] == "@tomicass"
    assert app.perform("follow", {"ref": "a1"})["done"] == "Ya lo seguías"


def test_a_new_post_lands_on_top_of_the_feed():
    app = SimulatedApp()

    app.perform("compose_post", {"text": "Llego tarde a la clase de las 10"})

    assert app.describe()["posts"][0]["handle"] == "@alumno"
    assert app.changes()["compose"] == ["Llego tarde a la clase de las 10"]


def test_replies_retweets_and_preferences_are_recorded():
    app = SimulatedApp()

    app.perform("open_post", {"ref": "p4"})
    app.perform("reply_to_post", {"ref": "p1", "text": "Nos vemos mañana"})
    app.perform("retweet_post", {"ref": "p1"})
    assert app.describe()["replies"] == ["Nos vemos mañana"]
    app.perform("set_preference", {"name": "theme", "value": "dark"})

    changes = app.changes()
    assert changes["reply"] == {"post-04": ["Nos vemos mañana"]}
    assert changes["retweet"] == ["post-04"]
    assert changes["preferences"] == {"theme": "dark"}
    assert app.describe()["preferences"]["theme"] == "dark"
    assert app.confirmed == ["reply_to_post", "retweet_post"]


def test_scrolling_only_works_on_the_feed_and_refresh_goes_back():
    app = SimulatedApp()
    app.perform("search", {"query": "torneo"})

    assert not app.perform("scroll_feed", {"direction": "down"})["ok"]
    app.perform("refresh_feed", {})
    assert app.describe()["page"] == 1


def test_the_end_of_the_feed_stops_scrolling():
    app = SimulatedApp()
    for _ in range(5):
        app.perform("scroll_feed", {"direction": "down"})

    assert app.describe()["more_below"] is False
    assert (
        app.perform("scroll_feed", {"direction": "down"})["error"] == "Ya estás al final del feed"
    )
