from skills.check_citation import check_citation, create_check_citation_skill


def test_check_citation_skill_accepts_existing_ids() -> None:
    skill = create_check_citation_skill()

    result = skill.run(
        "Use a queue [Q7] and check BFS (Q9).",
        [{"id": "7"}, {"question_id": "9"}],
    )

    assert skill.name == "check_citation"
    assert result == {
        "ok": True,
        "citations": ["7", "9"],
        "retrieved_ids": ["7", "9"],
        "invalid_citations": [],
    }


def test_check_citation_skill_flags_missing_ids() -> None:
    result = check_citation(
        "This claim cites Q404 and [12].",
        [{"id": "12", "metadata": {"source": "question_bank"}}],
    )

    assert result["ok"] is False
    assert result["citations"] == ["404", "12"]
    assert result["invalid_citations"] == ["404"]
