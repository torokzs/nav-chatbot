from app.services.query_rewrite import rewrite_query


def test_rewrite_query_expands_tax_synonyms() -> None:
    rewritten = rewrite_query("Mi az szja és az áfa szabálya?")

    assert len(rewritten) == 1
    assert "személyi jövedelemadó" in rewritten[0]
    assert "általános forgalmi adó" in rewritten[0]



def test_rewrite_query_splits_multi_question_input() -> None:
    rewritten = rewrite_query("Mi az szja? Mi az áfa?")

    assert len(rewritten) == 2
    assert rewritten[0].startswith("Mi az szja")
    assert rewritten[1].startswith("Mi az áfa")



def test_rewrite_query_passthrough_for_simple_question() -> None:
    rewritten = rewrite_query("Mikor kell bevallást benyújtani")

    assert rewritten == ["Mikor kell bevallást benyújtani"]
