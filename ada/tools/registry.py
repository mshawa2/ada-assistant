from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable[..., str]
    confirm_description: Optional[Callable[[dict], str]] = None
    confirm_when: Optional[Callable[[dict], bool]] = None


_registry: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    input_schema: dict,
    confirm_description: Optional[Callable[[dict], str]] = None,
    confirm_when: Optional[Callable[[dict], bool]] = None,
):
    """Decorator that registers a function as a tool the model can call.

    `description` is read by the model to decide when to use the tool, so
    write it for a reader, not a compiler.

    `confirm_description` renders the confirmation prompt for a gated tool. The
    generic prompt dumps `key=value` pairs, which is fine for deleting note #3
    and badly wrong for sending a message — there, the exact recipient and the
    full text are the entire decision, and they need to be readable at a
    glance. A tool that can do something irreversible to someone else should
    say so in its own words.

    `confirm_when` decides per *call* rather than per tool. Autonomy levels
    can't express "confirm the front door but not the lamp" — that depends on
    the arguments, not the tool. A tool that spans risk levels supplies a
    predicate over its own input.

    It can only ever *add* confirmation. If autonomy already says confirm,
    this can't take it away — the mechanism tightens, never loosens.
    """

    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        _registry[name] = Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            func=func,
            confirm_description=confirm_description,
            confirm_when=confirm_when,
        )
        return func

    return decorator


def get(name: str) -> Optional[Tool]:
    """The registered tool, or None. Used by the agent to find a tool's own
    confirmation wording before deciding what to show the user."""
    return _registry.get(name)


def specs() -> list[dict]:
    """The tool definitions to hand to the model each turn."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in _registry.values()
    ]


def call(name: str, tool_input: dict) -> str:
    if name not in _registry:
        raise ValueError(f"No tool named '{name}'.")
    return _registry[name].func(**tool_input)
