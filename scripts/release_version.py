"""Versions the plugin automatically, the way Relay-SDK versions its npm packages.

Relay-SDK (scripts/staging-bump.mjs, scripts/release-derive.mjs) runs two
lanes: every push to ``staging`` bumps ``X.Y.Z-staging.N`` and publishes it
under the ``staging`` dist-tag, and ``main`` is staging's exact tree, so the
release job on ``main`` strips the prerelease and publishes ``X.Y.Z`` as
``latest``. This is the same program for PyPI, in PEP 440:

    npm                     PyPI                  meaning
    X.Y.Z-staging.N         X.Y.Z.devN            staging build of a final
    (no npm shape)          X.Y.ZrcK.devN         staging build of a candidate
    X.Y.Z                   X.Y.Z or X.Y.ZrcK     the target the promotion publishes

The tree names the target with an optional ``.devN`` suffix. Nothing else
parses: ``1.0.0-rc.2``, ``1.0``, ``1.0.0a1`` all fail closed.

The staging decision table (``next_staging_version``), one package:

    tree names   | version on PyPI | target on PyPI | content changed | result
    -------------+-----------------+----------------+-----------------+-------------------------
    a bare target| (not asked)     | no             | (not asked)     | <target>.dev0
    a bare target| (not asked)     | yes            | (not asked)     | <next target>.dev0
    X.devN       | yes             | any            | no              | none: PyPI holds this content
    X.devN       | yes             | no             | yes             | X.dev(N+1)
    X.devN       | yes             | yes            | yes             | <next target>.dev0
    X.devN       | no              | no             | (not asked)     | keep: the tree names an
                 |                 |                |                 | unpublished dev of an
                 |                 |                |                 | unpublished target
    X.devN       | no              | yes            | (not asked)     | <next target>.dev0: main
                 |                 |                |                 | would derive the target
                 |                 |                |                 | and skip it forever

``next target`` is ``X.Y.Zrc(K+1)`` after a candidate and ``X.Y.(Z+1)`` after
a final. A candidate PyPI already has (published and then abandoned) moves on
to the next N. To aim at a different target, edit ``pyproject.toml`` by hand
to the target you want, with or without ``.dev0``: ``1.0.0`` or ``1.0.0.dev0``
turns the candidate line into the GA line, ``1.1.0`` starts a minor, and the
table above carries it from there. This is the same rule Relay-SDK's bump
script applies (a hand-edited unpublished prerelease is kept as written).

The manifest form ``plugin.yaml`` carries mirrors the PEP 440 version one to
one: ``1.0.0rc3`` is ``1.0.0-rc.3``, ``1.0.0rc3.dev0`` is ``1.0.0-rc.3.dev.0``,
``1.0.0.dev0`` is ``1.0.0-dev.0``, ``1.0.0`` is ``1.0.0``.

    python scripts/release_version.py show
        print EXPECTED_PACKAGE_VERSION=, EXPECTED_MANIFEST_VERSION= and
        RELEASE_TARGET_VERSION= for the tree, in GITHUB_ENV form
    python scripts/release_version.py staging --dry-run
        print the staging plan against live PyPI, touch nothing tracked
    python scripts/release_version.py staging --write
        rewrite pyproject.toml and plugin.yaml to the decided version
    python scripts/release_version.py release --dry-run
        derive the target, rewrite a scratch copy of the tree, build it,
        run twine check on it, prove no shipped file carries the staging
        version, print the plan
    python scripts/release_version.py release --write
        rewrite pyproject.toml and plugin.yaml to the target, in place
    python scripts/release_version.py verify-dist --dist DIR --version V [--forbid V0]
        the two distributions for V are there, twine check passes, and no
        shipped file carries V0
    python scripts/release_version.py verify-published --dist DIR --version V
        PyPI serves V and its file digests match DIR

``--assume-published a,b`` (dry runs only) treats those versions as on PyPI,
so CI can rehearse a skip without one. ``GITHUB_OUTPUT``, when set, receives
``version=``, ``target=``, ``publish=`` and ``bumped=``.

Only the standard library is imported; ``build`` and ``twine`` run as
subprocesses when a mode needs them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

PACKAGE = "relay-hermes"
DIST_PREFIX = "relay_hermes"
PYPI_JSON = "https://pypi.org/pypi/relay-hermes/json"
PYPI_RELEASE_JSON = "https://pypi.org/pypi/relay-hermes/{version}/json"
ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".release-tmp"

VERSION_SHAPE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?(?:\.dev(\d+))?$")
MANIFEST_SHAPE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?(?:(?:-|\.)dev\.(\d+))?$"
)
PYPROJECT_VERSION = re.compile(r'^version = "([^"\n]+)"$', re.MULTILINE)
MANIFEST_VERSION = re.compile(r"^version: (\S+)$", re.MULTILINE)


# ------------------------------------------------------------------ the pure part


@dataclass(frozen=True)
class Parsed:
    major: int
    minor: int
    patch: int
    rc: int | None
    dev: int | None

    @property
    def is_dev(self) -> bool:
        return self.dev is not None

    @property
    def target(self) -> "Parsed":
        """The version with ``.devN`` stripped: what a promotion publishes."""
        return replace(self, dev=None)

    @property
    def next_target(self) -> "Parsed":
        """The target after this one: rc K+1 after a candidate, patch+1 after a final."""
        if self.rc is not None:
            return Parsed(self.major, self.minor, self.patch, self.rc + 1, None)
        return Parsed(self.major, self.minor, self.patch + 1, None, None)

    def with_dev(self, dev: int) -> "Parsed":
        return replace(self, dev=dev)

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.rc is not None:
            text += f"rc{self.rc}"
        if self.dev is not None:
            text += f".dev{self.dev}"
        return text


def parse_version(text: str) -> Parsed:
    match = VERSION_SHAPE.fullmatch(str(text))
    if not match:
        raise ValueError(
            f"{text!r} is not X.Y.Z, X.Y.ZrcK, X.Y.Z.devN or X.Y.ZrcK.devN"
        )
    major, minor, patch, rc, dev = match.groups()
    return Parsed(
        int(major),
        int(minor),
        int(patch),
        None if rc is None else int(rc),
        None if dev is None else int(dev),
    )


def derive_release(version: str) -> str:
    """``X.Y.Z[rcK].devN`` to ``X.Y.Z[rcK]``; a bare target derives to itself."""
    return str(parse_version(version).target)


def manifest_form(version: str) -> str:
    """The ``plugin.yaml`` spelling of a PEP 440 version."""
    parsed = parse_version(version)
    text = f"{parsed.major}.{parsed.minor}.{parsed.patch}"
    if parsed.rc is not None:
        text += f"-rc.{parsed.rc}"
    if parsed.dev is not None:
        text += ("." if parsed.rc is not None else "-") + f"dev.{parsed.dev}"
    return text


def pep440_form(manifest_version: str) -> str:
    """The inverse of ``manifest_form``; anything else fails closed."""
    match = MANIFEST_SHAPE.fullmatch(str(manifest_version))
    if not match:
        raise ValueError(f"{manifest_version!r} is not a manifest version")
    major, minor, patch, rc, dev = match.groups()
    text = f"{major}.{minor}.{patch}"
    if rc is not None:
        text += f"rc{int(rc)}"
    if dev is not None:
        text += f".dev{int(dev)}"
    if manifest_form(text) != manifest_version:
        raise ValueError(f"{manifest_version!r} is not the canonical manifest form")
    return text


@dataclass(frozen=True)
class Decision:
    action: str  # none | keep | bump
    version: str
    reason: str


def next_staging_version(
    version: str,
    version_published: bool,
    target_published: bool,
    content_changed: bool | None,
    is_published: Callable[[str], bool],
) -> Decision:
    """The decision table in the module docstring."""
    parsed = parse_version(version)
    if not parsed.is_dev:
        if target_published:
            base = parsed.next_target
            reason = f"{parsed} is already on PyPI, so the next target starts"
        else:
            base = parsed.target
            reason = "the tree names a bare target, so its first dev starts"
        return Decision("bump", str(_first_free(base, 0, is_published)), reason)
    if version_published and not content_changed:
        return Decision("none", version, "PyPI holds this content")
    if not version_published and not target_published:
        return Decision(
            "keep",
            version,
            "the tree names an unpublished dev of an unpublished target",
        )
    if target_published:
        base = parsed.next_target
        reason = (
            f"content changed and {parsed.target} is already on PyPI"
            if version_published
            else f"{parsed.target} is already on PyPI, so main could never publish it"
        )
        return Decision("bump", str(_first_free(base, 0, is_published)), reason)
    return Decision(
        "bump",
        str(_first_free(parsed.target, parsed.dev + 1, is_published)),
        "content changed under a published dev",
    )


def _first_free(base: Parsed, dev: int, is_published: Callable[[str], bool]) -> Parsed:
    candidate = base.with_dev(dev)
    while is_published(str(candidate)):
        candidate = candidate.with_dev(candidate.dev + 1)
    return candidate


def sdist_file_hashes(sdist: Path) -> dict[str, str]:
    """sha256 per file inside an sdist, keyed by path under the top directory.

    ``PKG-INFO`` and ``*.egg-info/`` are setuptools output that restates the
    other files and the version; every fact in them is either another file's
    content or the version the comparison already holds equal.
    """
    hashes: dict[str, str] = {}
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if relative == "PKG-INFO" or ".egg-info/" in relative:
                continue
            handle = archive.extractfile(member)
            assert handle is not None
            hashes[relative] = hashlib.sha256(handle.read()).hexdigest()
    return hashes


def differing_files(left: dict[str, str], right: dict[str, str]) -> list[str]:
    return sorted(path for path in set(left) | set(right) if left.get(path) != right.get(path))


VERBATIM_SOURCE_DIRS = ("scripts", "tests")


def files_carrying_version(
    directory: Path, version: str, skip_dirs: tuple[str, ...] = ()
) -> list[str]:
    """Every file under ``directory`` whose bytes carry ``version``.

    Relay-SDK's first release on main published nothing for one package
    because a generated file still embedded the staging version; this is the
    same closed door, run over the unpacked distributions. ``skip_dirs`` names
    directory components to leave out: the sdist ships ``scripts/`` and
    ``tests/`` verbatim (MANIFEST.in), and their fixtures and docstrings name
    versions as examples, which is content and not an embedding.
    """
    needle = version.encode()
    found = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part in skip_dirs for part in relative.parts[:-1]):
            continue
        if path.is_file() and needle in path.read_bytes():
            found.append(str(relative))
    return found


# ---------------------------------------------------------------- tree access


def read_tree(root: Path) -> tuple[str, str]:
    """(pyproject version, plugin.yaml version); both must exist exactly once."""
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
    versions = PYPROJECT_VERSION.findall(pyproject)
    manifests = MANIFEST_VERSION.findall(manifest)
    if len(versions) != 1:
        raise ValueError(f"pyproject.toml names {len(versions)} versions, expected 1")
    if len(manifests) != 1:
        raise ValueError(f"plugin.yaml names {len(manifests)} versions, expected 1")
    return versions[0], manifests[0]


def check_tree(root: Path) -> str:
    """The tree's version, after proving plugin.yaml carries its manifest form."""
    version, manifest = read_tree(root)
    parse_version(version)
    expected = manifest_form(version)
    if manifest != expected:
        raise ValueError(
            f"plugin.yaml says {manifest}, pyproject.toml {version} needs {expected}"
        )
    return version


def write_tree(root: Path, version: str) -> list[str]:
    """Rewrite both version carriers; returns the relative paths written."""
    parse_version(version)
    pyproject_path = root / "pyproject.toml"
    manifest_path = root / "plugin.yaml"
    pyproject = PYPROJECT_VERSION.sub(
        f'version = "{version}"', pyproject_path.read_text(encoding="utf-8"), count=1
    )
    manifest = MANIFEST_VERSION.sub(
        f"version: {manifest_form(version)}",
        manifest_path.read_text(encoding="utf-8"),
        count=1,
    )
    pyproject_path.write_text(pyproject, encoding="utf-8")
    manifest_path.write_text(manifest, encoding="utf-8")
    assert check_tree(root) == version
    return ["pyproject.toml", "plugin.yaml"]


# ------------------------------------------------------------------ the driver


def say(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def run(args: list[str], cwd: Path) -> None:
    say(f"$ {' '.join(args)}")
    subprocess.run(args, cwd=cwd, check=True)


def github_output(**values: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def fetch_json(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": "relay-hermes-release"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


class PyPI:
    """What PyPI holds for the package, read once, plus assumed versions."""

    def __init__(self, assumed: set[str]):
        payload = fetch_json(PYPI_JSON)
        self.releases = set(payload["releases"]) if payload else set()
        self.assumed = assumed

    def is_published(self, version: str) -> bool:
        return version in self.assumed or version in self.releases

    def sdist_url(self, version: str) -> tuple[str, str]:
        payload = fetch_json(PYPI_RELEASE_JSON.format(version=version))
        if not payload:
            raise RuntimeError(f"PyPI has no release JSON for {version}")
        for entry in payload["urls"]:
            if entry["packagetype"] == "sdist":
                return entry["url"], entry["digests"]["sha256"]
        raise RuntimeError(f"PyPI holds no sdist for {version}")


def download(url: str, sha256: str, destination: Path) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": "relay-hermes-release"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != sha256:
        raise RuntimeError(f"{url} does not match the digest PyPI lists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def build_sdist(root: Path, outdir: Path) -> Path:
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True)
    run([sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir)], cwd=root)
    [sdist] = list(outdir.glob("*.tar.gz"))
    return sdist


def copy_tracked_tree(root: Path, destination: Path) -> None:
    """A scratch copy of the tracked files, so a rehearsal writes nowhere tracked."""
    shutil.rmtree(destination, ignore_errors=True)
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    for relative in listing.decode().split("\0"):
        if not relative:
            continue
        source = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def parse_assumed(text: str | None) -> set[str]:
    return {item.strip() for item in (text or "").split(",") if item.strip()}


def mode_show(args: argparse.Namespace) -> None:
    version = check_tree(ROOT)
    say(f"EXPECTED_PACKAGE_VERSION={version}")
    say(f"EXPECTED_MANIFEST_VERSION={manifest_form(version)}")
    say(f"RELEASE_TARGET_VERSION={derive_release(version)}")


def mode_staging(args: argparse.Namespace) -> None:
    write = args.write
    version = check_tree(ROOT)
    parsed = parse_version(version)
    pypi = PyPI(parse_assumed(args.assume_published if not write else None))
    scratch = SCRATCH / "staging"
    shutil.rmtree(scratch, ignore_errors=True)
    version_published = parsed.is_dev and pypi.is_published(version)
    target_published = pypi.is_published(str(parsed.target))
    content_changed = None
    changed_files: list[str] = []
    if version_published:
        packed = build_sdist(ROOT, scratch / "tree")
        url, sha256 = pypi.sdist_url(version)
        published = download(url, sha256, scratch / "registry" / "registry.tar.gz")
        changed_files = differing_files(sdist_file_hashes(published), sdist_file_hashes(packed))
        content_changed = bool(changed_files)
    decision = next_staging_version(
        version, version_published, target_published, content_changed, pypi.is_published
    )
    say(
        f"{PACKAGE}@{version}: published={version_published} "
        f"target={parsed.target} target_published={target_published} "
        f"changed={content_changed} -> {decision.action} {decision.version} ({decision.reason})"
    )
    if changed_files:
        say(f"  differs in {', '.join(changed_files)}")
    if decision.action == "bump" and write:
        say(f"rewrote {', '.join(write_tree(ROOT, decision.version))}")
    say(f"\nstaging plan ({'written' if write else 'dry run'}):")
    say(f"  {PACKAGE}@{version} -> {decision.version}  {decision.action}")
    say(f"  manifest {manifest_form(decision.version)}")
    say(f"  pip install --pre {PACKAGE}=={decision.version}")
    plan = {
        "package": PACKAGE,
        "from": version,
        "to": decision.version,
        "manifest": manifest_form(decision.version),
        "action": decision.action,
        "reason": decision.reason,
        "changed_files": changed_files,
    }
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    github_output(
        version=decision.version,
        target=derive_release(decision.version),
        publish=decision.action != "none",
        bumped=decision.action == "bump",
    )


def mode_release(args: argparse.Namespace) -> None:
    write = args.write
    staging_version = check_tree(ROOT)
    target = derive_release(staging_version)
    pypi = PyPI(parse_assumed(args.assume_published if not write else None))
    published = pypi.is_published(target)
    action = "skip" if published else "publish"
    assumed = target in pypi.assumed
    say(f"release plan ({'publish' if write else 'dry run'}):")
    say(
        f"  {PACKAGE}@{staging_version} -> {target}  {action}"
        f"{' (assumed published)' if assumed else ''}  tag v{target}"
    )
    say(f"  manifest {manifest_form(target)}")
    say(f"  pip install {PACKAGE}=={target}")
    github_output(
        version=target,
        staging_version=staging_version,
        tag=f"v{target}",
        publish=action == "publish",
    )
    if write:
        if action == "publish":
            say(f"rewrote {', '.join(write_tree(ROOT, target))}")
        return
    if args.plan_only:
        say(f"release plan only: {action}")
        return
    scratch = SCRATCH / "release"
    tree = scratch / "tree"
    copy_tracked_tree(ROOT, tree)
    if staging_version != target:
        say(f"rewrote {', '.join(write_tree(tree, target))} (scratch copy)")
    dist = scratch / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    dist.mkdir(parents=True)
    run([sys.executable, "-m", "build", "--outdir", str(dist)], cwd=tree)
    verify_dist(dist, target, staging_version if staging_version != target else None)
    say(f"\nrelease dry run finished: {action}")


def verify_dist(dist: Path, version: str, forbid: str | None) -> None:
    wheel = dist / f"{DIST_PREFIX}-{version}-py3-none-any.whl"
    sdist = dist / f"{DIST_PREFIX}-{version}.tar.gz"
    files = sorted(path for path in dist.iterdir() if path.is_file())
    if files != sorted([wheel, sdist]):
        raise SystemExit(
            f"{dist} holds {[path.name for path in files]}, expected exactly "
            f"{wheel.name} and {sdist.name}"
        )
    run([sys.executable, "-m", "twine", "check", "--strict", str(wheel), str(sdist)], cwd=dist)
    if forbid is None:
        say("no staging version to scan for")
        return
    with tempfile.TemporaryDirectory(prefix="relay-hermes-release-") as scratch:
        unpacked = Path(scratch)
        with tarfile.open(sdist, "r:gz") as archive:
            archive.extractall(unpacked / "sdist", filter="data")
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(unpacked / "wheel")
        stale = files_carrying_version(unpacked, forbid, VERBATIM_SOURCE_DIRS)
    if stale:
        raise SystemExit(
            f"{version} distributions still carry {forbid} in: {', '.join(stale)}"
        )
    say(f"no shipped file of {PACKAGE}@{version} carries {forbid}")


def mode_verify_dist(args: argparse.Namespace) -> None:
    verify_dist(Path(args.dist), args.version, args.forbid)


def mode_verify_published(args: argparse.Namespace) -> None:
    """PyPI serves the version and every file digest matches what we uploaded."""
    dist = Path(args.dist)
    local = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in dist.iterdir()
        if path.is_file()
    }
    deadline = time.monotonic() + args.timeout
    payload = None
    while time.monotonic() < deadline:
        payload = fetch_json(PYPI_RELEASE_JSON.format(version=args.version))
        if payload:
            break
        say(f"PyPI does not serve {args.version} yet; waiting")
        time.sleep(15)
    if not payload:
        raise SystemExit(f"PyPI did not serve {args.version} within {args.timeout}s")
    remote = {entry["filename"]: entry["digests"]["sha256"] for entry in payload["urls"]}
    if remote != local:
        raise SystemExit(f"PyPI digests {remote} differ from uploaded {local}")
    say(f"PyPI serves {PACKAGE}=={args.version} with the uploaded digests")
    for name in sorted(local):
        say(f"  {name} {local[name]}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("show").set_defaults(func=mode_show)
    for name, func in (("staging", mode_staging), ("release", mode_release)):
        sub = modes.add_parser(name)
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--write", action="store_true")
        group.add_argument("--dry-run", action="store_true")
        sub.add_argument("--assume-published", default=os.environ.get("RELAY_ASSUME_PUBLISHED"))
        if name == "release":
            sub.add_argument("--plan-only", action="store_true")
        sub.set_defaults(func=func)
    sub = modes.add_parser("verify-dist")
    sub.add_argument("--dist", required=True)
    sub.add_argument("--version", required=True)
    sub.add_argument("--forbid")
    sub.set_defaults(func=mode_verify_dist)
    sub = modes.add_parser("verify-published")
    sub.add_argument("--dist", required=True)
    sub.add_argument("--version", required=True)
    sub.add_argument("--timeout", type=int, default=600)
    sub.set_defaults(func=mode_verify_published)
    args = parser.parse_args(argv)
    if getattr(args, "assume_published", None) and getattr(args, "write", False):
        parser.error("--assume-published is a dry-run option")
    args.func(args)


if __name__ == "__main__":
    main()
