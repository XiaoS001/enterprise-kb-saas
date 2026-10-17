from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    run: Callable[..., Any]
    input_schema: dict[str, Any]


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        return self._skills[name]

    def list_all(self) -> list[Skill]:
        return [self._skills[name] for name in sorted(self._skills)]

    def describe(self) -> str:
        return "\n".join(
            f"{skill.name} - {skill.description}" for skill in self.list_all()
        )


registry = SkillRegistry()
