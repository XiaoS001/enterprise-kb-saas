import pytest

from skills import Skill, SkillRegistry


def make_skill(name: str = "test_skill", description: str = "Test skill") -> Skill:
    return Skill(
        name=name,
        description=description,
        run=lambda: None,
        input_schema={},
    )


def test_registered_skill_can_be_retrieved_with_original_name() -> None:
    registry = SkillRegistry()
    skill = make_skill()

    registry.register(skill)

    assert registry.get(skill.name) is skill
    assert registry.get("test_skill").name == "test_skill"


def test_duplicate_registration_raises_value_error() -> None:
    registry = SkillRegistry()
    skill = make_skill()

    registry.register(skill)

    with pytest.raises(ValueError):
        registry.register(skill)


def test_get_missing_skill_raises_key_error() -> None:
    registry = SkillRegistry()

    with pytest.raises(KeyError):
        registry.get("missing")


def test_list_all_is_sorted_by_name_regardless_of_registration_order() -> None:
    registry = SkillRegistry()
    registry.register(make_skill("zeta"))
    registry.register(make_skill("alpha"))
    registry.register(make_skill("middle"))

    assert [skill.name for skill in registry.list_all()] == [
        "alpha",
        "middle",
        "zeta",
    ]


def test_describe_contains_each_skill_name_and_description() -> None:
    registry = SkillRegistry()
    registry.register(make_skill("alpha", "Alpha skill"))
    registry.register(make_skill("beta", "Beta skill"))

    output = registry.describe()

    assert "alpha - Alpha skill" in output
    assert "beta - Beta skill" in output


def test_registries_do_not_share_state() -> None:
    first = SkillRegistry()
    second = SkillRegistry()

    first.register(make_skill("first_only"))

    assert [skill.name for skill in first.list_all()] == ["first_only"]
    assert second.list_all() == []
