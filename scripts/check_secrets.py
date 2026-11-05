"""Block commits/pushes that contain any secret value from .env.

Run by the git hooks in .githooks/ (enabled with: git config core.hooksPath .githooks).

    python3 scripts/check_secrets.py staged          # pre-commit: the staged diff
    python3 scripts/check_secrets.py push <range>    # pre-push: commits about to be pushed

Exit code 1 (and a list of variable NAMES, never values) if a secret is found.
Uses only the standard library, so it works with any python3.
"""
import re
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
SECRET_NAME = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN|DB_URL)$")
MIN_LENGTH = 8  # ignore short/placeholder values


def secret_values() -> dict[str, str]:
    """{NAME: value} for every secret-looking variable in .env."""
    if not ENV_FILE.exists():
        return {}
    secrets = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, value = line.split("=", 1)
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip('"').strip("'")
        if SECRET_NAME.search(name.strip()) and len(value) >= MIN_LENGTH and value not in ("paste-here", "filled-by-claude"):
            secrets[name.strip()] = value
    return secrets


def git_text(args: list[str]) -> str:
    # --text so binary files (e.g. PDFs) are included; undecodable bytes are replaced, not fatal
    out = subprocess.run(["git", *args], capture_output=True, check=True).stdout
    return out.decode("utf-8", errors="replace")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "staged"
    if mode == "staged":
        content, label = git_text(["diff", "--cached", "--text"]), "staged changes"
        if ".env" in git_text(["diff", "--cached", "--name-only"]).split():
            print("BLOCKED: .env is staged. Unstage it: git restore --staged .env")
            return 1
    elif mode == "push":
        commit_range = sys.argv[2]
        content, label = git_text(["log", "-p", "--text", commit_range]), f"commits {commit_range}"
    else:
        print(f"unknown mode {mode!r}")
        return 2

    secrets = secret_values()
    leaked = sorted(name for name, value in secrets.items() if value in content)
    if leaked:
        print(f"BLOCKED: {label} contain the value of: {', '.join(leaked)}")
        print("Remove it, then try again. (Values are not printed.)")
        return 1
    print(f"secret scan: {len(secrets)} secrets checked against {label}: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
