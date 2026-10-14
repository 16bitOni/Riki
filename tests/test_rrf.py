from app.memory.retrieval import reciprocal_rank_fusion


def test_item_found_by_several_channels_wins():
    keyword = ["a", "b", "c"]
    semantic = ["c", "d", "a"]
    merged = reciprocal_rank_fusion([keyword, semantic])
    assert merged[0] == "a"  # high in both lists
    assert set(merged) == {"a", "b", "c", "d"}


def test_empty_channels_are_fine():
    assert reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([["x"]]) == ["x"]
