"""Validate target audit requirements against the project's uv lock."""

import argparse
import re
import tomllib
from pathlib import Path


class LockedRequirementValidator:
    """Reject target requirements containing versions absent from uv.lock."""

    REQUIREMENT = re.compile(
        r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)"
    )

    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        with self.lock_path.open("rb") as lock_file:
            lock = tomllib.load(lock_file)
        self.locked_packages = {
            (self.normalize(package["name"]), package["version"])
            for package in lock["package"]
        }

    @staticmethod
    def normalize(name):
        """Normalize a distribution name using Python packaging rules."""
        return re.sub(r"[-_.]+", "-", name).lower()

    def validate(self, requirements_path):
        """Return exact pins after confirming each exists in the root lock."""
        requirements_path = Path(requirements_path)
        requirements = set()
        for line_number, line in enumerate(
            requirements_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "--")):
                continue
            match = self.REQUIREMENT.match(stripped)
            if match is None:
                raise ValueError(
                    f"{requirements_path}:{line_number}: expected an exact pin"
                )
            requirement = (
                self.normalize(match.group("name")),
                match.group("version"),
            )
            if requirement not in self.locked_packages:
                name, version = requirement
                raise ValueError(
                    f"{requirements_path}:{line_number}: {name}=={version} "
                    f"is absent from {self.lock_path}"
                )
            requirements.add(requirement)

        if not requirements:
            raise ValueError(f"{requirements_path}: no exact requirements found")
        return requirements


class DependencyAuditCli:
    """Command-line client for locked target requirement validation."""

    @staticmethod
    def parser():
        parser = argparse.ArgumentParser()
        parser.add_argument("lock", type=Path)
        parser.add_argument("requirements", nargs="+", type=Path)
        return parser

    def run(self, arguments=None):
        args = self.parser().parse_args(arguments)
        validator = LockedRequirementValidator(args.lock)
        for requirements_path in args.requirements:
            packages = validator.validate(requirements_path)
            print(
                f"Validated {len(packages)} locked packages in "
                f"{requirements_path}"
            )


if __name__ == "__main__":
    DependencyAuditCli().run()
