#!/usr/bin/env python3
"""Reject candidate-controlled pull-request workflow triggers."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


class WorkflowTriggerPolicy:
    """Validate the security-sensitive top-level ``on`` declaration."""

    _ON_KEY = re.compile(r"^(?:on|'on'|\"on\")\s*:\s*(.*)$")
    _FORBIDDEN_EVENT = re.compile(
        r"(?<![A-Za-z0-9_-])pull_request(?![A-Za-z0-9_-])"
    )
    _YAML_REFERENCE = re.compile(r"(?:^|[\s\[{,:])(?:[&*!]|<<\s*:)")

    def validate(self, workflow: Path) -> list[str]:
        """Return policy errors for one workflow, failing closed on ambiguity."""
        source = workflow.read_text(encoding="utf-8")
        trigger_blocks = self._trigger_blocks(source)
        if len(trigger_blocks) != 1:
            return [self._unsafe(workflow)]

        trigger = trigger_blocks[0]
        searchable = "\n".join(self._strip_comment(line) for line in trigger)
        declaration = self._ON_KEY.fullmatch(searchable.splitlines()[0])
        if declaration is None:
            return [self._unsafe(workflow)]

        inline_value = declaration.group(1).strip()
        unquoted = "\n".join(self._unquoted_text(line) for line in trigger)
        if (
            "\\" in searchable
            or inline_value.startswith(("|", ">"))
            or self._YAML_REFERENCE.search(unquoted)
        ):
            return [self._unsafe(workflow)]

        if self._FORBIDDEN_EVENT.search(searchable):
            return [
                f"{workflow}: candidate-controlled pull_request trigger is forbidden"
            ]
        return []

    def validate_directory(self, directory: Path) -> list[str]:
        """Return all policy errors for workflow files in ``directory``."""
        if not directory.is_dir():
            return [f"{directory}: workflow directory does not exist"]
        workflows = sorted((*directory.glob("*.yml"), *directory.glob("*.yaml")))
        if not workflows:
            return [f"{directory}: no workflow files found"]
        return [error for workflow in workflows for error in self.validate(workflow)]

    def _trigger_blocks(self, source: str) -> list[list[str]]:
        lines = source.splitlines()
        starts = [
            index
            for index, line in enumerate(lines)
            if self._ON_KEY.fullmatch(self._strip_comment(line))
        ]
        blocks = []
        for start in starts:
            block = [lines[start]]
            for line in lines[start + 1 :]:
                stripped = self._strip_comment(line)
                if stripped and not line[0].isspace():
                    break
                block.append(line)
            blocks.append(block)
        return blocks

    @staticmethod
    def _strip_comment(line: str) -> str:
        in_single = False
        in_double = False
        index = 0
        while index < len(line):
            character = line[index]
            if character == "'" and not in_double:
                if in_single and index + 1 < len(line) and line[index + 1] == "'":
                    index += 2
                    continue
                in_single = not in_single
            elif character == '"' and not in_single:
                if not in_double or index == 0 or line[index - 1] != "\\":
                    in_double = not in_double
            elif character == "#" and not in_single and not in_double:
                return line[:index].rstrip()
            index += 1
        return line.rstrip()

    @staticmethod
    def _unquoted_text(line: str) -> str:
        output = []
        in_single = False
        in_double = False
        index = 0
        while index < len(line):
            character = line[index]
            if character == "'" and not in_double:
                if in_single and index + 1 < len(line) and line[index + 1] == "'":
                    index += 2
                    continue
                in_single = not in_single
            elif character == '"' and not in_single:
                if not in_double or index == 0 or line[index - 1] != "\\":
                    in_double = not in_double
            elif not in_single and not in_double:
                output.append(character)
            index += 1
        return "".join(output)

    @staticmethod
    def _unsafe(workflow: Path) -> str:
        return f"{workflow}: cannot safely validate workflow trigger"


def main() -> int:
    """Validate a candidate workflow directory from trusted policy code."""
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow_directory", type=Path)
    arguments = parser.parse_args()
    errors = WorkflowTriggerPolicy().validate_directory(arguments.workflow_directory)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
