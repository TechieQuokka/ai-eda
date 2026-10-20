"""Unified result and error format shared by every command."""

from dataclasses import dataclass, field
from enum import IntEnum

from pydantic import BaseModel, ConfigDict, JsonValue


class ExitCode(IntEnum):
    OK = 0
    DESIGN_ERROR = 1
    TOOL_ERROR = 2


@dataclass(frozen=True)
class Location:
    """Where an issue points: file relative to the project root, 1-based line/column."""

    file: str | None = None
    line: int | None = None
    column: int | None = None
    path: str | None = None


class Issue(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    path: str | None = None
    details: dict[str, JsonValue] = {}


class Result(BaseModel):
    ok: bool
    command: str
    project: str
    errors: list[Issue]
    warnings: list[Issue]
    tool_error: bool = False
    data: dict[str, JsonValue] = {}

    @property
    def exit_code(self) -> ExitCode:
        if self.tool_error:
            return ExitCode.TOOL_ERROR
        return ExitCode.OK if self.ok else ExitCode.DESIGN_ERROR


@dataclass
class Diagnostics:
    """Collects issues while a command runs."""

    errors: list[Issue] = field(default_factory=lambda: list[Issue]())
    warnings: list[Issue] = field(default_factory=lambda: list[Issue]())
    tool_error: bool = False

    def error(self, rule: str, message: str, at: Location | None = None, **details: object) -> None:
        self.errors.append(_issue(rule, message, at, details))

    def warning(
        self, rule: str, message: str, at: Location | None = None, **details: object
    ) -> None:
        self.warnings.append(_issue(rule, message, at, details))

    def fail_tool(self, rule: str, message: str, **details: object) -> None:
        """Record a failure of an external tool or the environment (exit code 2)."""
        self.tool_error = True
        self.errors.append(_issue(rule, message, None, details))

    def extend(self, other: "Diagnostics") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.tool_error = self.tool_error or other.tool_error

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_result(self, command: str, project: str, data: dict[str, JsonValue]) -> Result:
        return Result(
            ok=self.ok,
            command=command,
            project=project,
            errors=self.errors,
            warnings=self.warnings,
            tool_error=self.tool_error,
            data=data,
        )


def _issue(rule: str, message: str, at: Location | None, details: dict[str, object]) -> Issue:
    loc = at or Location()
    return Issue.model_validate(
        {
            "rule": rule,
            "message": message,
            "file": loc.file,
            "line": loc.line,
            "column": loc.column,
            "path": loc.path,
            "details": details,
        }
    )
