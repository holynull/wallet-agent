"""The UI transcript merges like add_messages: append, same id replaces in place."""

from wallet_agent.graph.state import _merge_conversation_history


def test_entries_are_appended_and_same_id_replaces_in_place():
    existing = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在", "id": "assistant:r1:aaa"},
    ]

    merged = _merge_conversation_history(
        existing,
        [
            {"role": "assistant", "content": "在（更新）", "id": "assistant:r1:aaa"},
            {"role": "user", "content": "查余额"},
        ],
    )

    assert [item["content"] for item in merged] == ["你好", "在（更新）", "查余额"]
    assert len(merged) == 3


def test_entries_without_ids_keep_appending():
    merged = _merge_conversation_history(
        [{"role": "user", "content": "a"}],
        [{"role": "user", "content": "a"}],
    )

    assert len(merged) == 2


def test_merge_does_not_mutate_the_existing_list():
    existing = [{"role": "user", "content": "a", "id": "u1"}]
    _merge_conversation_history(existing, [{"role": "user", "content": "b", "id": "u2"}])

    assert [item["content"] for item in existing] == ["a"]
