"""The markdown vault.

The cases that matter here are the ones where a *person* has been in the
directory: hand-typed notes with no frontmatter, a reformatted field, a link to
a page that doesn't exist yet. None of those are corruption, and none of them
may raise.
"""

import pytest

from ada import vault


@pytest.fixture(autouse=True)
def temp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_DIR", tmp_path / "Vault")
    yield


def test_slug_survives_punctuation_and_case():
    assert vault.slug("Retire Unused Agents!") == "retire-unused-agents"
    assert vault.slug("  spaced  out  ") == "spaced-out"
    assert vault.slug("!!!") == "untitled"


def test_write_page_round_trips_through_frontmatter():
    vault.write_page("decisions", "Retire unused agents", "We removed three.", tags=["instaplan"])
    text = vault.read_page("decisions", "Retire unused agents")
    fields, body = vault.parse_frontmatter(text)

    assert fields["title"] == "Retire unused agents"
    assert fields["tags"] == ["instaplan"]
    assert "We removed three." in body


def test_links_become_wikilinks_and_are_discoverable():
    vault.write_page("decisions", "Retire unused agents", "Body", links=["instaplan"])
    text = vault.read_page("decisions", "Retire unused agents")

    assert "[[instaplan]]" in text
    assert vault.links_in(text) == ["instaplan"]


def test_backlinks_finds_pages_pointing_here():
    vault.write_page("projects", "instaplan", "The product.")
    vault.write_page("decisions", "Retire unused agents", "Body", links=["instaplan"])

    assert vault.backlinks("instaplan") == ["decisions/retire-unused-agents"]


def test_backlinks_ignores_self_reference():
    vault.write_page("projects", "instaplan", "See [[instaplan]] itself.")
    assert vault.backlinks("instaplan") == []


def test_page_written_by_hand_without_frontmatter_still_reads():
    vault.ensure()
    (vault.VAULT_DIR / "notes" / "typed-in-obsidian.md").write_text("# Just a note\n\nNo frontmatter.\n")

    fields, body = vault.parse_frontmatter(vault.read_page("notes", "typed-in-obsidian"))
    assert fields == {}
    assert "Just a note" in body


def test_unparseable_frontmatter_line_is_skipped_not_fatal():
    vault.ensure()
    (vault.VAULT_DIR / "notes" / "messy.md").write_text(
        "---\ntitle: Messy\nthis line has no colon\ntags: [a, b]\n---\n\nBody.\n"
    )

    fields, body = vault.parse_frontmatter(vault.read_page("notes", "messy"))
    assert fields == {"title": "Messy", "tags": ["a", "b"]}
    assert body.strip() == "Body."


def test_read_page_returns_none_when_absent():
    assert vault.read_page("notes", "never-written") is None


def test_link_to_a_page_that_does_not_exist_is_allowed():
    vault.write_page("notes", "idea", "Body", links=["not-written-yet"])
    assert "[[not-written-yet]]" in vault.read_page("notes", "idea")
    assert vault.list_pages("notes") == ["idea"]


def test_path_for_cannot_escape_the_vault():
    """Traversal is neutralised by slugging, not by rejection.

    `slug()` runs before the containment check, so `../../.ssh/id_rsa` becomes
    the harmless filename `ssh-id-rsa.md`. The guard in `path_for` is
    defence-in-depth for a future where that stops being true; the property
    worth asserting is that the result stays inside the vault either way.
    """
    path = vault.path_for("notes", "../../.ssh/id_rsa")

    assert path.name == "ssh-id-rsa.md"
    assert (vault.VAULT_DIR / "notes").resolve() in path.parents


def test_path_for_rejects_unknown_section():
    with pytest.raises(ValueError):
        vault.path_for("secrets", "x")


def test_search_matches_case_insensitively():
    vault.write_page("notes", "deploys", "Railway deploy is manual.")
    assert vault.search("RAILWAY") == [("notes/deploys", "Railway deploy is manual.")]


def test_empty_frontmatter_values_are_omitted():
    rendered = vault.render_frontmatter({"title": "X", "tags": [], "status": None, "note": ""})
    assert rendered == "---\ntitle: X\n---"
