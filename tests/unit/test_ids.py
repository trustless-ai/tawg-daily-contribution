from tawg_bot.ids import github_id, magicians_id, telegram_id


def test_stable_source_ids_encode_their_source_coordinates() -> None:
    assert telegram_id("tawg", 42) == "tg:tawg:42"
    assert github_id("agent-ercs", "pr", "9", "comment", "17") == (
        "gh:agent-ercs:pr:9:comment:17"
    )
    assert magicians_id(123, 456) == "magicians:123:post:456"
