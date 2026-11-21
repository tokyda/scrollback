import pytest
from typer.testing import CliRunner

from scrollback.cli import NOT_IMPLEMENTED_EXIT_CODE, app

runner = CliRunner()

TOP_LEVEL = [
    "ingest", "synth", "build", "inspect", "ask", "enrich",
    "eval", "kb", "alerts", "digest", "serve",
]  # fmt: skip
GROUPS = {
    "eval": ["run", "report", "retrieval", "import-feedback"],
    "kb": ["index", "search"],
    "alerts": ["check"],
    "digest": ["send"],
    "serve": ["api", "bot"],
    "inspect": ["gaps"],
}


def test_help_lists_every_top_level_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in TOP_LEVEL:
        assert name in result.output, name


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_group_help_lists_subcommands(group: str) -> None:
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0
    for name in GROUPS[group]:
        assert name in result.output, f"{group} {name}"


@pytest.mark.parametrize(
    ("args", "label"),
    [
        (["alerts", "check"], "stretch"),
        (["digest", "send"], "stretch"),
        (["serve", "api"], "stretch"),
        (["enrich"], "stretch"),
    ],
)
def test_stubs_exit_nonzero_with_label(args: list[str], label: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == NOT_IMPLEMENTED_EXIT_CODE
    assert label in result.output
