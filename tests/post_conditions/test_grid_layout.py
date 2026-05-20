"""Tests for grid_layout.split_by_meta_group.

The expected meta-group labels in these fixtures ("Faction & League",
"Role, Affinity, Rarity", "Effects & Other") are the source-of-truth
values defined in ``mom_bot.post_conditions.grouping.META_GROUPS``.
If those labels are renamed there, update every fixture below (and the
sub-pagination tests) accordingly.
"""

from __future__ import annotations

from mom_bot.post_conditions.grid_layout import split_by_meta_group

# ---------------------------------------------------------------------------
# Test 1: empty input
# ---------------------------------------------------------------------------


def test_split_by_meta_group_empty_returns_empty_list() -> None:
    """Empty input yields an empty page list."""
    assert split_by_meta_group([]) == []


# ---------------------------------------------------------------------------
# Test 2: single group, single page (<=20 conditions)
# ---------------------------------------------------------------------------


def test_split_by_meta_group_single_group_single_page() -> None:
    """A group with <=20 conditions produces one GridPage; label == meta_label."""
    conditions = [
        {"id": i, "condition_type": "faction", "description": f"F{i}"} for i in range(1, 6)
    ]
    pages = split_by_meta_group(conditions)

    assert len(pages) == 1
    page = pages[0]
    assert page.label == "Faction & League"
    assert page.meta_label == "Faction & League"
    assert page.label == page.meta_label, "Single-page group: label must equal meta_label"
    assert len(page.conditions) == 5


# ---------------------------------------------------------------------------
# Test 3: single group, multi-page (>20 conditions)
# ---------------------------------------------------------------------------


def test_split_by_meta_group_single_group_multi_page() -> None:
    """A group with >20 conditions sub-paginates with (i/N) suffix labels."""
    conditions = [{"id": i, "condition_type": "faction", "description": f"F{i}"} for i in range(25)]
    pages = split_by_meta_group(conditions)

    assert [p.label for p in pages] == [
        "Faction & League (1/2)",
        "Faction & League (2/2)",
    ]
    assert len(pages[0].conditions) == 20
    assert len(pages[1].conditions) == 5
    # meta_label has no (i/N) suffix on any sub-page
    assert pages[0].meta_label == "Faction & League"
    assert pages[1].meta_label == "Faction & League"


# ---------------------------------------------------------------------------
# Test 4: multiple groups, all single-page → one GridPage per group in order
# ---------------------------------------------------------------------------


def test_split_by_meta_group_multiple_groups_single_page_each() -> None:
    """One condition per meta-group → three GridPages in META_GROUPS order.

    Critically: pages from different meta-groups are NEVER merged, even
    when the prior page has unused capacity. This asserts three pages
    rather than one (which a capacity-packing scheme might produce).

    Note: the expected labels below are the source-of-truth values
    defined in ``mom_bot.post_conditions.grouping.META_GROUPS``.
    """
    conditions = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
        {"id": 2, "condition_type": "role", "description": "R1"},
        {"id": 3, "condition_type": "effect", "description": "E1"},
    ]
    pages = split_by_meta_group(conditions)

    assert [p.label for p in pages] == [
        "Faction & League",
        "Role, Affinity, Rarity",
        "Effects & Other",
    ]
    assert [p.meta_label for p in pages] == [
        "Faction & League",
        "Role, Affinity, Rarity",
        "Effects & Other",
    ]
    assert pages[0].conditions[0]["id"] == 1
    assert pages[1].conditions[0]["id"] == 2
    assert pages[2].conditions[0]["id"] == 3


# ---------------------------------------------------------------------------
# Test 5: mixed (one group multi-page, others single)
# ---------------------------------------------------------------------------


def test_split_by_meta_group_mixed_preserves_order_and_adjacency() -> None:
    """Multi-page group sub-pages stay adjacent; META_GROUPS order is preserved."""
    # 21 faction → (1/2) + (2/2), 2 role → single, 1 effect → single
    faction_conds = [
        {"id": i, "condition_type": "faction", "description": f"F{i}"} for i in range(21)
    ]
    other_conds = [
        {"id": 100, "condition_type": "role", "description": "R1"},
        {"id": 200, "condition_type": "effect", "description": "E1"},
    ]
    pages = split_by_meta_group(faction_conds + other_conds)

    assert [p.label for p in pages] == [
        "Faction & League (1/2)",
        "Faction & League (2/2)",
        "Role, Affinity, Rarity",
        "Effects & Other",
    ]
    # All faction sub-pages share the same meta_label
    assert pages[0].meta_label == "Faction & League"
    assert pages[1].meta_label == "Faction & League"
    # Other groups are single-page; label == meta_label
    assert pages[2].label == pages[2].meta_label
    assert pages[3].label == pages[3].meta_label


# ---------------------------------------------------------------------------
# Test 6: sort within a group by (condition_type, id) ascending
# ---------------------------------------------------------------------------


def test_split_by_meta_group_sorts_within_group_by_type_then_id() -> None:
    """Shuffled input is sorted by (condition_type, id) ascending per group."""
    # Mix league and faction types (both belong to "Faction & League")
    # with non-sequential ids to verify sort, not insertion order.
    conditions = [
        {"id": 30, "condition_type": "faction", "description": "F30"},
        {"id": 5, "condition_type": "league", "description": "L5"},
        {"id": 10, "condition_type": "faction", "description": "F10"},
        {"id": 2, "condition_type": "league", "description": "L2"},
    ]
    pages = split_by_meta_group(conditions)

    assert len(pages) == 1
    sorted_ids = [c["id"] for c in pages[0].conditions]
    # Expected order: faction(10), faction(30), league(2), league(5)
    assert sorted_ids == [10, 30, 2, 5]


# ---------------------------------------------------------------------------
# Test 7: empty groups suppressed (no GridPage for a zero-condition META_GROUP)
# ---------------------------------------------------------------------------


def test_split_by_meta_group_suppresses_empty_meta_groups() -> None:
    """Meta-groups with no matching conditions produce no GridPage output."""
    # Only faction conditions — role and effect groups have none.
    conditions = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
    ]
    pages = split_by_meta_group(conditions)

    assert len(pages) == 1
    assert pages[0].meta_label == "Faction & League"


# ---------------------------------------------------------------------------
# Test 8: specific 19+11+6 catalog case → exactly 3 GridPages
# ---------------------------------------------------------------------------


def _make_conditions(
    count: int,
    condition_type: str,
    id_offset: int = 0,
) -> list[dict]:
    """Build ``count`` minimal conditions of the given type."""
    return [
        {
            "id": id_offset + i,
            "condition_type": condition_type,
            "description": f"{condition_type}-{id_offset + i}",
        }
        for i in range(count)
    ]


def test_split_by_meta_group_19_faction_11_rar_6_effect_yields_3_pages() -> None:
    """Current catalog shape: 19 Faction + 11 RAR + 6 EO → exactly 3 GridPages.

    This is the concrete production scenario described in plan § 3.1:
    ceil(19/20) + ceil(11/20) + ceil(6/20) = 1 + 1 + 1 = 3 pages.
    No sub-pagination triggers. Labels carry no (i/N) suffix.

    Note: the expected labels below are the source-of-truth values
    defined in ``mom_bot.post_conditions.grouping.META_GROUPS``.
    """
    conditions = (
        _make_conditions(19, "faction", id_offset=1)
        + _make_conditions(11, "role", id_offset=100)
        + _make_conditions(6, "effect", id_offset=200)
    )
    pages = split_by_meta_group(conditions)

    assert len(pages) == 3
    assert pages[0].label == "Faction & League"
    assert pages[1].label == "Role, Affinity, Rarity"
    assert pages[2].label == "Effects & Other"
    assert len(pages[0].conditions) == 19
    assert len(pages[1].conditions) == 11
    assert len(pages[2].conditions) == 6
    # All single-page groups: label == meta_label for each
    for page in pages:
        assert page.label == page.meta_label


# ---------------------------------------------------------------------------
# Test: custom page_size kwarg is respected
# ---------------------------------------------------------------------------


def test_split_by_meta_group_respects_custom_page_size() -> None:
    """`page_size` kwarg overrides the default of 20."""
    conditions = [{"id": i, "condition_type": "faction", "description": f"F{i}"} for i in range(5)]
    pages = split_by_meta_group(conditions, page_size=2)

    assert [p.label for p in pages] == [
        "Faction & League (1/3)",
        "Faction & League (2/3)",
        "Faction & League (3/3)",
    ]
    assert all(p.meta_label == "Faction & League" for p in pages)
