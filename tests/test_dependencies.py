import pytest

from lastday.dependencies import file_dependencies, mentions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/src/ @leaver", True),
        ("if: github.actor == 'leaver'", True),
        ("if: github.actor == 'LEAVER'", True),
        ("/src/ @leaver-bot", False),
        ("/src/ @old-leaver", False),
        ("/src/ @leavers", False),
    ],
)
def test_mentions_matches_whole_logins_only(text, expected):
    assert mentions(text, "leaver") is expected


def test_codeowners_line_is_a_blocking_codeowner_dependency():
    text = "# owners\n* @operator\n/src/ @leaver\n/docs/ @leaver @acme/writers\n"
    found = file_dependencies("app", ".github/CODEOWNERS", text, "leaver")
    assert [(d.kind, d.where, d.detail, d.blocking) for d in found] == [
        ("codeowner", ".github/CODEOWNERS:3", "sole owner of /src/", True),
        ("codeowner", ".github/CODEOWNERS:4", "co-owner of /docs/", True),
    ]


def test_workflow_mention_is_a_blocking_file_reference():
    text = "jobs:\n  deploy:\n    if: github.actor == 'leaver'\n"
    [found] = file_dependencies("app", ".github/workflows/deploy.yml", text, "leaver")
    assert (found.kind, found.where, found.blocking) == ("file_reference", ".github/workflows/deploy.yml:3", True)


def test_comment_lines_are_not_dependencies():
    assert file_dependencies("app", ".github/CODEOWNERS", "# was owned by @leaver\n", "leaver") == []
