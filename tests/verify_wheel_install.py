"""Smoke-test the built distribution in an isolated Windows/Linux environment.

The ordinary test matrix installs an editable checkout. This separate check
ensures a wheel actually contains the CLI package and its console entry point.
It invokes only ``--help``; no OCD configuration or disk mutations occur.
"""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=cwd, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if result.returncode:
        print(result.stdout, file=sys.stderr, end="")
        result.check_returncode()
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ocd-wheel-install-") as temporary:
        scratch = Path(temporary).resolve()
        wheels = scratch / "wheels"
        wheels.mkdir()
        build = run(
            sys.executable, "-m", "pip", "wheel", "--no-deps",
            "--wheel-dir", str(wheels), str(ROOT), cwd=scratch,
        )
        print(build.stdout, end="")

        candidates = list(wheels.glob("ocd_organizer-*.whl"))
        if len(candidates) != 1:
            raise AssertionError(f"expected one OCD wheel, found {candidates!r}")

        environment = scratch / "isolated"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        console = scripts / ("ocd.exe" if os.name == "nt" else "ocd")

        install = run(
            str(python), "-m", "pip", "install", "--no-deps",
            str(candidates[0]), cwd=scratch,
        )
        print(install.stdout, end="")

        package = run(
            str(python), "-c", "import ocd; print(ocd.__file__)", cwd=scratch,
        )
        module_path = Path(package.stdout.strip()).resolve()
        if not module_path.is_relative_to(environment):
            raise AssertionError(f"OCD imported outside isolated venv: {module_path}")

        for command in ((str(python), "-m", "ocd", "--help"),
                        (str(console), "--help")):
            help_result = run(*command, cwd=scratch)
            if "usage: ocd" not in help_result.stdout:
                raise AssertionError(f"CLI usage missing from {command!r}")
        print("PASS: built OCD wheel imports and both CLI entry points run in an isolated venv")


if __name__ == "__main__":
    main()
