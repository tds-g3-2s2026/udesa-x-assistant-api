"""Whether a request left the app where it should.

The final state is what counts, not the path: a model that searches instead of
scrolling, or opens the post before liking it, is still right. Texts the model
writes are checked by keywords, since no two drafts read the same.
"""

from evals.app import normalized

SET_CHANGES = ("like", "unlike", "retweet", "follow")


def missing_words(text: str, keywords: list[str]) -> list[str]:
    return [word for word in keywords if normalized(word) not in normalized(text)]


def problems(changes: dict, expect: dict) -> list[str]:
    """What differs from the expected final state. Empty when the request went right."""
    found = []
    for key in SET_CHANGES:
        wanted = sorted(expect.get(key, []))
        if changes[key] != wanted:
            found.append(f"{key}: {changes[key]}, se esperaba {wanted}")

    wanted_preferences = expect.get("preferences", {})
    if changes["preferences"] != wanted_preferences:
        found.append(f"preferences: {changes['preferences']}, se esperaba {wanted_preferences}")

    wanted_replies = expect.get("reply", {})
    if sorted(changes["reply"]) != sorted(wanted_replies):
        found.append(f"reply: {sorted(changes['reply'])}, se esperaba {sorted(wanted_replies)}")
    else:
        for post_id, keywords in wanted_replies.items():
            texts = changes["reply"][post_id]
            if len(texts) != 1 or missing_words(texts[0], keywords):
                found.append(f"reply a {post_id}: {texts}, tenía que decir {keywords}")

    wanted_posts = expect.get("compose", [])
    if len(changes["compose"]) != len(wanted_posts):
        found.append(f"compose: {changes['compose']}, se esperaban {len(wanted_posts)}")
    else:
        for text, keywords in zip(changes["compose"], wanted_posts, strict=True):
            if missing_words(text, keywords):
                found.append(f"compose: {text!r}, tenía que decir {keywords}")
    return found
