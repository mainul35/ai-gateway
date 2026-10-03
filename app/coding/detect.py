"""Works out how a project is checked, from its files, so adding one asks for nothing but where it is.

What a project is built with decides three things: the container image its commands run in, the
command that says whether a change works, and whether that command needs the network to fetch
dependencies. They are guessed here from the usual files - package.json, pom.xml, pyproject.toml -
and shown afterwards, so a wrong guess is one edit to fix rather than a form to fill in up front.

It reads through a small interface (exists, read), because the project may be a clone on this
server or a folder on somebody's computer that only sends the few files this looks at.
"""
import json
import os

# The files detection reads, which a folder on someone's computer is asked to send
KEY_FILES = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "pom.xml", "mvnw",
             "build.gradle", "build.gradle.kts", "gradlew", "go.mod", "Cargo.toml", "pyproject.toml",
             "setup.py", "requirements.txt", "requirements-dev.txt", "pytest.ini", "conftest.py",
             "tests/conftest.py", "tests", "test", "app")
READ_FILES = ("package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt")


class LocalFolder:
    def __init__(self, root):
        self.root = root

    def exists(self, name):
        return os.path.exists(os.path.join(self.root, name))

    def read(self, name):
        try:
            with open(os.path.join(self.root, name), encoding="utf-8", errors="replace") as f:
                return f.read(200_000)
        except OSError:
            return ""

    def has_python(self):
        for _, _, files in os.walk(self.root):
            if any(f.endswith(".py") for f in files):
                return True
        return False


class Snapshot:
    """What a remote folder sent: which of KEY_FILES exist, a few of their contents, and whether it
    has any Python at all."""

    def __init__(self, present, contents, has_python=False):
        self.present, self.contents, self._python = set(present), dict(contents), has_python

    def exists(self, name):
        return name in self.present

    def read(self, name):
        return self.contents.get(name, "")

    def has_python(self):
        return self._python


def _has(folder, *names):
    return any(folder.exists(name) for name in names)


def _python(folder):
    requirements = next((n for n in ("requirements-dev.txt", "requirements.txt") if folder.exists(n)), None)
    pyproject = folder.read("pyproject.toml")
    uses_pytest = (_has(folder, "pytest.ini", "conftest.py", "tests/conftest.py")
                   or "pytest" in pyproject or (requirements and "pytest" in folder.read(requirements)))
    install = ""
    if requirements:
        install = f"pip install -q -r {requirements} && "
    elif _has(folder, "pyproject.toml", "setup.py"):
        install = "pip install -q -e . && "
    if uses_pytest:
        return {"kind": "Python", "image": "python:3.12-slim", "network": True,
                "verify": f"{install}pip install -q pytest && python -m pytest -q", "local_verify": "python -m pytest -q"}
    if _has(folder, "tests", "test"):
        tests = "tests" if folder.exists("tests") else "test"
        return {"kind": "Python", "image": "python:3.12-slim", "network": bool(install),
                "verify": f"{install}python -m unittest discover -s {tests}",
                "local_verify": f"python -m unittest discover -s {tests}"}
    # No tests: at least every file has to compile
    return {"kind": "Python", "image": "python:3.12-slim", "network": False, "verify": "python -m compileall -q ."}


def _node(folder):
    try:
        package = json.loads(folder.read("package.json") or "{}")
    except ValueError:
        package = {}
    scripts = package.get("scripts") or {}
    install = "npm ci" if folder.exists("package-lock.json") else "npm install"
    if folder.exists("pnpm-lock.yaml"):
        install = "corepack enable && pnpm install --frozen-lockfile"
    elif folder.exists("yarn.lock"):
        install = "corepack enable && yarn install --frozen-lockfile"
    test = scripts.get("test", "")
    if test and "no test specified" not in test:
        run = "npm test --silent"
    elif "build" in scripts:
        run = "npm run build --silent"
    elif "lint" in scripts:
        run = "npm run lint --silent"
    else:
        run = ""
    return {"kind": "Node.js", "image": "node:22-slim", "network": True,
            "verify": f"{install} && {run}" if run else "", "local_verify": run}


def detect(folder):
    """{kind, image, network, verify, local_verify} for a project, read through LocalFolder or Snapshot.

    verify is for the sandbox, where installing dependencies first is harmless. local_verify is for a
    folder on somebody's own computer, where it must not install anything into their environment:
    the same check without the install step."""
    found = _detect(folder)
    found.setdefault("local_verify", found["verify"])
    return found


def _detect(folder):
    if folder.exists("package.json"):
        return _node(folder)
    if folder.exists("pom.xml"):
        wrapper = "./mvnw" if folder.exists("mvnw") else "mvn"
        return {"kind": "Java (Maven)", "image": "maven:3-eclipse-temurin-21", "network": True,
                "verify": f"{wrapper} -q -B test"}
    if _has(folder, "build.gradle", "build.gradle.kts"):
        wrapper = "./gradlew" if folder.exists("gradlew") else "gradle"
        return {"kind": "Java (Gradle)", "image": "eclipse-temurin:21" if wrapper == "./gradlew" else "gradle:8-jdk21",
                "network": True, "verify": f"{wrapper} test --no-daemon -q"}
    if folder.exists("go.mod"):
        return {"kind": "Go", "image": "golang:1.23", "network": True, "verify": "go test ./..."}
    if folder.exists("Cargo.toml"):
        return {"kind": "Rust", "image": "rust:1", "network": True, "verify": "cargo test -q"}
    if _has(folder, "pyproject.toml", "setup.py", "requirements.txt") or folder.has_python():
        return _python(folder)
    return {"kind": "Other", "image": "python:3.12-slim", "network": False, "verify": ""}
