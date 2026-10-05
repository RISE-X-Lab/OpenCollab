"""Create editable team files and inspect their resolved configuration."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer
import yaml
from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from opencollab.bootstrap.team_config import TeamConfig


class _TeamDumper(yaml.SafeDumper):
    """Keep multiline prompts editable without changing their contents."""


def _represent_string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_TeamDumper.add_representer(str, _represent_string)


def _team_document(team: TeamConfig) -> dict[str, object]:
    roles = {}
    for name, role in team.roles.items():
        fields = role.model_dump(exclude_none=True, exclude={"budget_tokens"})
        if role.budget_tokens is not None:
            fields["budget"] = {"tokens": role.budget_tokens}
        roles[name] = fields
    context = {"policy": team.context.name}
    if team.context.tool_result_budget is not None:
        context["tool_result_budget"] = team.context.tool_result_budget
    return {
        "entry": team.entry,
        "roles": roles,
        "topology": {name: sorted(destinations) for name, destinations in team.topology.edges.items()},
        "context": context,
        "tool_limits": team.tool_limits,
    }


def build_team_app(
    default_factory: Callable[[], TeamConfig],
    config_loader: Callable[..., TeamConfig],
) -> typer.Typer:
    """Wire the team CLI to the composition root's existing configuration functions."""
    app = typer.Typer(name="team", help="Create and inspect team configuration files.")
    console = Console()

    @app.command(name="init")
    def init_cmd(
        path: str = typer.Argument("team.yaml", help="Destination for the built-in Self-Collaboration team"),
    ) -> None:
        """Write an editable copy of the built-in Self-Collaboration team."""
        destination = Path(path)
        document = yaml.dump(
            _team_document(default_factory()),
            Dumper=_TeamDumper,
            sort_keys=False,
            allow_unicode=True,
        )
        try:
            with destination.open("x", encoding="utf-8") as stream:
                stream.write(document)
        except FileExistsError:
            console.print(Text(f"Team file already exists — {path}", style="red"), soft_wrap=True)
            raise typer.Exit(code=2) from None
        except FileNotFoundError:
            console.print(
                Text(f"Destination directory does not exist — {destination.parent}", style="red"),
                soft_wrap=True,
            )
            raise typer.Exit(code=2) from None
        except OSError as exc:
            console.print(Text(f"Cannot create team file — {exc}", style="red"), soft_wrap=True)
            raise typer.Exit(code=2) from None
        console.print(Text(f"Created editable Self-Collaboration team — {path}", style="green"), soft_wrap=True)
        console.print(Text("Edit the roles, prompts, tools and topology in this file."))
        console.print(Text(f"opencollab --team-config {shlex.quote(path)} --workspace ."), soft_wrap=True)

    @app.command(name="show")
    def show_cmd(
        team_config: Optional[str] = typer.Option(
            None,
            "--team-config",
            help="Explicit team YAML file (otherwise OPENCOLLAB_TEAM_FILE or the built-in team)",
        ),
    ) -> None:
        """Show configured roles and capabilities before starting a team run."""
        source = team_config if team_config is not None else os.environ.get("OPENCOLLAB_TEAM_FILE")
        source_label = source or "built-in Self-Collaboration"
        try:
            team = config_loader(path=team_config)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            console.print(Text(f"Cannot load team configuration {source_label} — {exc}", style="red"), soft_wrap=True)
            raise typer.Exit(code=2) from None
        console.print(Text(f"Configuration — {source_label}"), soft_wrap=True)
        console.print(Text(f"Entry role — {team.entry}"))
        console.print(Text(f"Configured roles — {', '.join(team.roles) or '(none declared)'}"), soft_wrap=True)
        console.print(Text("A team run starts an entry session and creates active sessions as work is delegated."))
        for name, role in team.roles.items():
            console.print(
                Text(f"Declared tools for {name} — {', '.join(role.tools) or '(none declared)'}"),
                soft_wrap=True,
            )
        console.print(Text(f"Topology — {'all roles allowed' if team.topology.allow_all else 'declared edges'}"))
        sources = dict.fromkeys((*team.roles, *team.topology.edges))
        for name in sources:
            destinations = ", ".join(sorted(team.topology.edges.get(name, ()))) or "(no outgoing roles)"
            console.print(Text(f"{name} → {destinations}"), soft_wrap=True)

    return app


__all__ = ["build_team_app"]
