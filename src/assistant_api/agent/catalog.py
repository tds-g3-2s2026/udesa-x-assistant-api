"""The actions the assistant can take on the screen: the catalog agreed in #94.

Each one is a tool of the Converse API. The model never calls UdeSA-X: it picks
one of these and the app performs it as if the user had tapped. Deleting,
blocking, reporting, changing the password and closing the account are left out
on purpose: the assistant can take the user to that screen, never do it.
"""

from dataclasses import dataclass

# The same limit a post has in the app.
MAX_TEXT_LENGTH = 280

PREFERENCE_VALUES = {"theme": ("light", "dark"), "feed_language": ("es", "en", "all")}


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    parameters: dict[str, dict]
    # Visible to other people: the app shows it and the user confirms it first.
    needs_confirmation: bool = False


REF = {
    "type": "string",
    "description": "Referencia de un elemento visible en la pantalla actual, como p2 o a1",
}
TEXT = {"type": "string", "description": f"Hasta {MAX_TEXT_LENGTH} caracteres"}

ACTIONS = (
    Action(
        "scroll_feed",
        "Baja o sube una pantalla en el feed.",
        {"direction": {"type": "string", "enum": ["down", "up"]}},
    ),
    Action("refresh_feed", "Vuelve al principio del feed y trae lo más nuevo.", {}),
    Action("open_post", "Abre el detalle de un post visible.", {"ref": REF}),
    Action("like_post", "Da like a un post visible.", {"ref": REF}),
    Action("unlike_post", "Saca el like de un post visible.", {"ref": REF}),
    Action(
        "retweet_post",
        "Retuitea un post visible. La app le pide confirmación al usuario.",
        {"ref": REF},
        needs_confirmation=True,
    ),
    Action(
        "reply_to_post",
        "Responde un post visible con un texto que redactás a partir de lo que pidió el "
        "usuario. La app se lo muestra para que lo confirme.",
        {"ref": REF, "text": TEXT},
        needs_confirmation=True,
    ),
    Action(
        "compose_post",
        "Publica un post nuevo con un texto que redactás a partir de lo que pidió el usuario. "
        "La app se lo muestra para que lo confirme.",
        {"text": TEXT},
        needs_confirmation=True,
    ),
    Action(
        "search",
        "Abre Buscar con un texto. Muestra las cuentas (a1, a2...) y los posts (p1, p2...) "
        "que coinciden.",
        {"query": {"type": "string"}},
    ),
    Action(
        "open_profile",
        "Abre el perfil del autor de un post visible (p1) o de una cuenta visible (a1).",
        {"ref": REF},
    ),
    Action(
        "follow",
        "Sigue a la cuenta del perfil abierto (a1). La app le pide confirmación al usuario.",
        {"ref": REF},
        needs_confirmation=True,
    ),
    Action(
        "set_preference",
        "Cambia el tema (theme: light o dark) o el idioma del feed (feed_language: es, en o all).",
        {
            "name": {"type": "string", "enum": list(PREFERENCE_VALUES)},
            "value": {"type": "string"},
        },
    ),
    Action(
        "finish",
        "Termina el pedido con una respuesta breve que la app lee en voz alta. También para "
        "decir que no se puede o para preguntar qué quiso decir.",
        {"say": {"type": "string"}},
    ),
)

CATALOG = {action.name: action for action in ACTIONS}


def tool_specs() -> list[dict]:
    """The catalog as the `tools` of a Converse request. Every parameter is required."""
    return [
        {
            "toolSpec": {
                "name": action.name,
                "description": action.description,
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": action.parameters,
                        "required": list(action.parameters),
                    }
                },
            }
        }
        for action in ACTIONS
    ]


def invalid_arguments(name: str, arguments: dict) -> str | None:
    """Why the call cannot reach the app, or None when it can.

    The schema already travels with the tool, but a model can ignore it: what
    it answers is checked here before the app sees it.
    """
    action = CATALOG.get(name)
    if action is None:
        return f"{name} no está en el catálogo"
    for parameter, schema in action.parameters.items():
        value = arguments.get(parameter)
        if not isinstance(value, str) or not value.strip():
            return f"Falta {parameter}"
        if "enum" in schema and value not in schema["enum"]:
            return f"{parameter} tiene que ser uno de: {', '.join(schema['enum'])}"
    if name == "set_preference" and arguments["value"] not in PREFERENCE_VALUES[arguments["name"]]:
        allowed = ", ".join(PREFERENCE_VALUES[arguments["name"]])
        return f"{arguments['name']} tiene que ser uno de: {allowed}"
    if len(arguments.get("text", "")) > MAX_TEXT_LENGTH:
        return f"El texto supera los {MAX_TEXT_LENGTH} caracteres"
    return None
