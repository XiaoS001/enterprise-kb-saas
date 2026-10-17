from skills.get_question import create_get_question_skill


def test_get_question_skill_fetches_question_from_source() -> None:
    source = {
        "12": {
            "id": "12",
            "title": "What is a binary tree?",
        }
    }
    skill = create_get_question_skill(source)

    assert skill.name == "get_question"
    assert skill.run("12") == {
        "id": "12",
        "title": "What is a binary tree?",
    }


def test_get_question_skill_missing_question_raises_key_error() -> None:
    skill = create_get_question_skill({})

    try:
        skill.run("missing")
    except KeyError as exc:
        assert exc.args == ("missing",)
    else:
        raise AssertionError("Expected KeyError")
