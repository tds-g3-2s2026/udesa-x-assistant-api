"""How a request is judged, and the scripted requests themselves."""

import json

from evals.app import SimulatedApp
from evals.run import REQUESTS, percentile, summarize
from evals.scoring import problems


def test_ai03_compares_the_final_state_with_the_expected_one():
    app = SimulatedApp()
    app.perform("like_post", {"ref": "p3"})

    assert problems(app.changes(), {"like": ["post-03"]}) == []
    assert problems(app.changes(), {"like": ["post-04"]}) == [
        "like: ['post-03'], se esperaba ['post-04']"
    ]
    # A request that should change nothing fails if anything changed.
    assert problems(app.changes(), {}) != []


def test_texts_are_judged_by_their_keywords_ignoring_accents():
    app = SimulatedApp()
    app.perform("reply_to_post", {"ref": "p4", "text": "Dale, nos vemos MANANA"})
    app.perform("compose_post", {"text": "Llego tarde a la de las 10"})

    expected = {"reply": {"post-04": ["mañana"]}, "compose": [["tarde", "10"]]}
    assert problems(app.changes(), expected) == []
    assert problems(app.changes(), {**expected, "compose": [["temprano"]]}) == [
        "compose: 'Llego tarde a la de las 10', tenía que decir ['temprano']"
    ]


def test_a_reply_to_the_wrong_post_fails():
    app = SimulatedApp()
    app.perform("reply_to_post", {"ref": "p3", "text": "Nos vemos mañana"})

    assert problems(app.changes(), {"reply": {"post-04": ["mañana"]}}) == [
        "reply: ['post-03'], se esperaba ['post-04']"
    ]


def test_a_reply_without_its_keywords_fails():
    app = SimulatedApp()
    app.perform("reply_to_post", {"ref": "p4", "text": "Dale"})

    assert problems(app.changes(), {"reply": {"post-04": ["mañana"]}}) == [
        "reply a post-04: ['Dale'], tenía que decir ['mañana']"
    ]


def test_a_preference_or_post_count_off_fails():
    app = SimulatedApp()
    app.perform("set_preference", {"name": "theme", "value": "dark"})

    assert problems(app.changes(), {"preferences": {"theme": "dark"}, "compose": [["x"]]}) == [
        "compose: [], se esperaban 1"
    ]
    assert problems(app.changes(), {}) == ["preferences: {'theme': 'dark'}, se esperaba {}"]


def test_the_scripted_requests_point_at_things_that_exist():
    requests = json.loads(REQUESTS.read_text(encoding="utf-8"))
    app = SimulatedApp()

    assert len(requests) == 25
    assert len({item["id"] for item in requests}) == 25
    for item in requests:
        expect = item["expect"]
        for post_id in (
            expect.get("like", []) + expect.get("unlike", []) + expect.get("retweet", [])
        ):
            assert post_id in app.posts, item["id"]
        for post_id in expect.get("reply", {}):
            assert post_id in app.posts, item["id"]
        for handle in expect.get("follow", []):
            assert handle in app.accounts and handle not in app.following, item["id"]
    # The four requests of the demo agreed in #94 are there.
    assert sum(item["kind"] == "demo" for item in requests) == 4


def test_the_summary_adds_up_the_rows():
    rows = [
        {
            "ok": True,
            "model_calls": 2,
            "first_action_ms": 800,
            "call_ms": [800, 600],
            "input_tokens": 3000,
            "output_tokens": 60,
            "cost_usd": 0.0004,
            "followed_injection": False,
        },
        {
            "ok": False,
            "model_calls": 4,
            "first_action_ms": 1200,
            "call_ms": [1200, 700, 650, 640],
            "input_tokens": 7000,
            "output_tokens": 120,
            "cost_usd": 0.0008,
            "followed_injection": True,
        },
    ]

    summary = summarize(rows)

    assert summary["success_rate"] == 0.5
    assert summary["model_calls_per_request"] == 3
    assert summary["first_action_ms_median"] == 1000
    assert summary["followed_injection"] == 1
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5
