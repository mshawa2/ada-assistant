from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable[..., str]


_registry: dict[str, Tool] = {}


def tool(name: str, description: str, input_schema: dict):
    """Decorator that registers a function as a tool the model can call.

    `description` is read by the model to decide when to use the tool, so
    write it for a reader, not a compiler.
    """

    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        _registry[name] = Tool(name=name, description=description, input_schema=input_schema, func=func)
        return func

    return decorator


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
