"""The app as the assistant sees it, simulated: 30 synthetic posts, five per screen.

It plays the part of the phone: it describes what is on screen, with the refs
the model may use (p1, p2... for posts, a1, a2... for accounts), and performs
the action the model picks. The simulated user confirms everything the app
would ask them to confirm, and the app keeps a record of what was confirmed.
"""

import json
import unicodedata
from pathlib import Path

from assistant_api.agent.catalog import CATALOG

FEED = Path(__file__).with_name("feed.json")
PAGE_SIZE = 5
# The post that tries to give the assistant orders.
INJECTED_POST = "post-02"


class ScreenError(Exception):
    """An action that makes no sense on the current screen. The model is told why."""


def normalized(text: str) -> str:
    """Lowercase and without accents, so "Mendez" finds "Méndez"."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def matches(query: str, text: str) -> bool:
    """Every word of the query appears somewhere in the text."""
    haystack = normalized(text)
    return all(word in haystack for word in normalized(query).split())


def elapsed(minutes: int) -> str:
    return f"hace {minutes} min" if minutes < 60 else f"hace {minutes // 60} h"


class SimulatedApp:
    def __init__(self) -> None:
        data = json.loads(FEED.read_text(encoding="utf-8"))
        self.me = data["me"]["handle"]
        self.accounts = {account["handle"]: account["name"] for account in data["accounts"]}
        self.accounts[self.me] = data["me"]["name"]
        self.posts = {post["id"]: dict(post) for post in data["posts"]}
        self.order = [post["id"] for post in data["posts"]]

        self.initial_liked = frozenset(
            post["id"] for post in data["posts"] if post.get("liked_by_me")
        )
        self.initial_following = frozenset(data["following"])
        self.initial_preferences = {"theme": "light", "feed_language": "all"}
        self.liked = set(self.initial_liked)
        self.following = set(self.initial_following)
        self.preferences = dict(self.initial_preferences)
        self.retweeted: set[str] = set()
        self.replies: dict[str, list[str]] = {}
        self.composed: list[str] = []
        # The actions the user confirmed in the app, in order.
        self.confirmed: list[str] = []

        # The feed at its first page. Other screens: ("post", id), ("search", text),
        # ("profile", handle) and ("settings", None).
        self.view: tuple[str, object] = ("feed", 0)

    # What is on screen

    def visible(self) -> tuple[list[str], list[str]]:
        """The posts and accounts on screen, in the order of their refs."""
        kind, detail = self.view
        if kind == "feed":
            return self.order[detail : detail + PAGE_SIZE], []
        if kind == "post":
            return [detail], []
        if kind == "search":
            accounts = [
                handle
                for handle, name in self.accounts.items()
                if matches(detail, f"{name} {handle}")
            ]
            posts = [
                post_id for post_id in self.order if matches(detail, self._searchable(post_id))
            ]
            return posts[:PAGE_SIZE], accounts[:PAGE_SIZE]
        if kind == "profile":
            return [post_id for post_id in self.order if self.posts[post_id]["author"] == detail][
                :3
            ], [detail]
        return [], []

    def describe(self) -> dict:
        kind, detail = self.view
        posts, accounts = self.visible()
        screen: dict = {"screen": kind}
        if kind == "feed":
            screen["page"] = detail // PAGE_SIZE + 1
            screen["more_below"] = detail + PAGE_SIZE < len(self.order)
        if kind == "search":
            screen["query"] = detail
        if kind == "settings":
            screen["preferences"] = dict(self.preferences)
        if accounts:
            screen["accounts"] = [
                {
                    "ref": f"a{index}",
                    "handle": handle,
                    "name": self.accounts[handle],
                    "following": handle in self.following,
                }
                for index, handle in enumerate(accounts, start=1)
            ]
        if kind in ("feed", "post", "search", "profile"):
            screen["posts"] = [self._view(index, post_id) for index, post_id in enumerate(posts, 1)]
        if kind == "post":
            screen["replies"] = list(self.replies.get(detail, []))
        return screen

    # What the assistant does

    def perform(self, name: str, arguments: dict) -> dict:
        try:
            done = getattr(self, f"_{name}")(**arguments)
        except ScreenError as error:
            return {"ok": False, "error": str(error)}
        if CATALOG[name].needs_confirmation:
            self.confirmed.append(name)
        return {"ok": True, "done": done}

    def changes(self) -> dict:
        """What the requests left different from the start: what the scoring compares."""
        return {
            "like": sorted(self.liked - self.initial_liked),
            "unlike": sorted(self.initial_liked - self.liked),
            "retweet": sorted(self.retweeted),
            "follow": sorted(self.following - self.initial_following),
            "preferences": {
                name: value
                for name, value in self.preferences.items()
                if value != self.initial_preferences[name]
            },
            "reply": {post_id: list(texts) for post_id, texts in self.replies.items()},
            "compose": list(self.composed),
        }

    def _scroll_feed(self, direction: str) -> str:
        kind, offset = self.view
        if kind != "feed":
            raise ScreenError("scroll_feed solo funciona en el feed; refresh_feed vuelve al feed")
        if direction == "down":
            if offset + PAGE_SIZE >= len(self.order):
                raise ScreenError("Ya estás al final del feed")
            self.view = ("feed", offset + PAGE_SIZE)
        else:
            if offset == 0:
                raise ScreenError("Ya estás arriba de todo")
            self.view = ("feed", offset - PAGE_SIZE)
        return "El feed se movió una pantalla"

    def _refresh_feed(self) -> str:
        self.view = ("feed", 0)
        return "El feed está arriba de todo, con lo más nuevo"

    def _open_post(self, ref: str) -> str:
        self.view = ("post", self._post(ref))
        return "Se abrió el post"

    def _like_post(self, ref: str) -> str:
        post_id = self._post(ref)
        if post_id in self.liked:
            return "Ya tenía tu like"
        self.liked.add(post_id)
        return "Like dado"

    def _unlike_post(self, ref: str) -> str:
        post_id = self._post(ref)
        if post_id not in self.liked:
            return "No tenía tu like"
        self.liked.discard(post_id)
        return "Like sacado"

    def _retweet_post(self, ref: str) -> str:
        self.retweeted.add(self._post(ref))
        return "El usuario confirmó y se retuiteó"

    def _reply_to_post(self, ref: str, text: str) -> str:
        self.replies.setdefault(self._post(ref), []).append(text)
        return "El usuario confirmó y se publicó la respuesta"

    def _compose_post(self, text: str) -> str:
        post_id = f"mine-{len(self.composed) + 1}"
        self.posts[post_id] = {
            "id": post_id,
            "author": self.me,
            "text": text,
            "likes": 0,
            "minutes_ago": 0,
        }
        self.order.insert(0, post_id)
        self.composed.append(text)
        self.view = ("feed", 0)
        return "El usuario confirmó y se publicó el post"

    def _search(self, query: str) -> str:
        self.view = ("search", query)
        return "Resultados de la búsqueda"

    def _open_profile(self, ref: str) -> str:
        handle = (
            self.posts[self._post(ref)]["author"] if ref.startswith("p") else self._account(ref)
        )
        self.view = ("profile", handle)
        return f"Perfil de {self.accounts[handle]}"

    def _follow(self, ref: str) -> str:
        if self.view[0] != "profile":
            raise ScreenError("Para seguir a alguien, primero abrí su perfil (open_profile)")
        handle = self._account(ref)
        if handle in self.following:
            return "Ya lo seguías"
        self.following.add(handle)
        return "El usuario confirmó y ahora lo sigue"

    def _set_preference(self, name: str, value: str) -> str:
        self.preferences[name] = value
        self.view = ("settings", None)
        return "Preferencia guardada"

    def _post(self, ref: str) -> str:
        return self._resolve(ref, "p", self.visible()[0])

    def _account(self, ref: str) -> str:
        return self._resolve(ref, "a", self.visible()[1])

    @staticmethod
    def _resolve(ref: str, prefix: str, items: list[str]) -> str:
        position = ref[1:]
        if ref.startswith(prefix) and position.isdigit() and 1 <= int(position) <= len(items):
            return items[int(position) - 1]
        raise ScreenError(f"{ref} no está en la pantalla")

    def _searchable(self, post_id: str) -> str:
        post = self.posts[post_id]
        return f"{post['text']} {self.accounts[post['author']]} {post['author']}"

    def _view(self, index: int, post_id: str) -> dict:
        post = self.posts[post_id]
        return {
            "ref": f"p{index}",
            "author": self.accounts[post["author"]],
            "handle": post["author"],
            "text": post["text"],
            "likes": post["likes"] + (1 if post_id in self.liked else 0),
            "liked_by_me": post_id in self.liked,
            "retweeted_by_me": post_id in self.retweeted,
            "time": elapsed(post["minutes_ago"]),
        }
