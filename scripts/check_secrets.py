#!/usr/bin/env python3
"""Finds credentials in commits before they reach a remote.

Used by scripts/git-hooks/pre-push, which blocks the push when anything is found. Can also be run by hand:

    python scripts/check_secrets.py origin/develop..HEAD     # a range
    python scripts/check_secrets.py --all                    # every commit in the repository

Only lines being added are checked, and found values are never printed in full.
"""
import re
import subprocess
import sys

ZERO = "0" * 40

# Values that are obviously placeholders, not credentials. "${VAR:?message with spaces}" is cut at the
# first space by the value patterns, so an opening "${" alone is enough to recognise a variable reference.
PLACEHOLDER = re.compile(r"^(|change-?me|changeme|xxx+|\*+|<[^>]*>|your[-_].*|example.*|\$\{.*|\$\w+)$", re.I)

ANY_FILE = [
    ("URL with an inline password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:(?P<value>[^/\s@'\"]+)@", re.I)),
    ("OpenAI-style API key", re.compile(r"\b(?P<value>sk-(?:proj-|master-)?[A-Za-z0-9_-]{20,})")),
    ("JSON web token or tunnel token", re.compile(r"\b(?P<value>eyJ[A-Za-z0-9_-]{20,}\.?[A-Za-z0-9_.-]*)")),
    ("GitHub token", re.compile(r"\b(?P<value>(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("HuggingFace token", re.compile(r"\b(?P<value>hf_[A-Za-z0-9]{30,})")),
    ("AWS access key", re.compile(r"\b(?P<value>AKIA[0-9A-Z]{16})\b")),
    ("private key", re.compile(r"(?P<value>-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
]
BY_SUFFIX = {
    (".yml", ".yaml"): [("secret-looking YAML value", re.compile(
        r"^\s*[\w.-]*(?:password|passwd|secret|token|api[_-]?key)[\w.-]*\s*:\s*['\"]?(?P<value>[^'\"\s#]+)", re.I))],
    (".properties", ".env", ".ini", ".cfg", ".toml"): [("secret-looking setting", re.compile(
        r"^\s*[\w.-]*(?:password|passwd|secret|token|api[_-]?key|master[._]key)[\w.-]*\s*=\s*['\"]?(?P<value>[^'\"\s#]+)",
        re.I))],
}


def rules_for(path):
    rules = list(ANY_FILE)
    name = path.lower().rsplit("/", 1)[-1]
    for suffixes, extra in BY_SUFFIX.items():
        if name.endswith(suffixes) or (".env" in suffixes and name.startswith(".env")):
            rules += extra
    return rules


def mask(value):
    return value[:3] + "..." if len(value) > 6 else "..."


def scan(rev_args):
    log = subprocess.run(["git", "log", "-p", "--no-color", "--format=commit %H %s", *rev_args],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if log.returncode != 0:
        sys.exit(f"git log failed: {log.stderr.strip()}")
    findings, commit, path = [], None, None
    for line in log.stdout.splitlines():
        if line.startswith("commit "):
            commit = line[7:48]
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and not line.startswith("+++") and path:
            if path.endswith(".env.example") or path == "scripts/check_secrets.py":
                continue
            for label, rule in rules_for(path):
                for match in rule.finditer(line[1:]):
                    value = match.group("value")
                    # Settings like sso.token.url hold endpoints; URLs get the inline-password check instead
                    if label != "URL with an inline password" and re.match(r"https?://", value, re.I):
                        continue
                    if not PLACEHOLDER.match(value):
                        findings.append((commit, path, label, mask(value)))
    return findings


def pushed_ranges(stdin):
    """Turns pre-push input lines into git log arguments for the commits actually being sent."""
    for line in stdin:
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:  # deleting a remote branch sends nothing
            continue
        local_sha, remote_sha = parts[1], parts[3]
        yield [local_sha, "--not", "--remotes"] if remote_sha == ZERO else [f"{remote_sha}..{local_sha}"]


def main(argv):
    if "--pre-push" in argv:
        ranges = list(pushed_ranges(sys.stdin))
    elif "--all" in argv:
        ranges = [["--all"]]
    else:
        ranges = [[a for a in argv if not a.startswith("--")] or ["HEAD"]]

    findings = [f for rev_args in ranges for f in scan(rev_args)]
    if not findings:
        print("check_secrets: no credentials found")
        return 0
    print("check_secrets: refusing, these commits add what looks like a credential:\n")
    for commit, path, label, value in dict.fromkeys(findings):
        print(f"  {commit[:10]}  {path}: {label} ({value})")
    print("\nMove the value to a gitignored file (.env, config/config.properties on the server) and rewrite "
          "the commit.\nIf this is a false positive, push with --no-verify.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
