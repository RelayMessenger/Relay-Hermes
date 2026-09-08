"""The release derivation decides versions the way Relay-SDK's bump script does.

scripts/release_version.py is loaded by path because ``scripts/`` is not a
package; every function here is pure, so no network and no build runs.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_version.py"
spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
rv = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = rv
spec.loader.exec_module(rv)


def published(*versions: str):
    held = set(versions)
    return lambda candidate: candidate in held


@pytest.mark.parametrize(
    ("text", "target", "manifest"),
    [
        ("1.0.0", "1.0.0", "1.0.0"),
        ("1.0.0rc2", "1.0.0rc2", "1.0.0-rc.2"),
        ("1.0.0rc3.dev0", "1.0.0rc3", "1.0.0-rc.3.dev.0"),
        ("1.0.0.dev4", "1.0.0", "1.0.0-dev.4"),
        ("2.10.3rc12.dev7", "2.10.3rc12", "2.10.3-rc.12.dev.7"),
    ],
)
def test_parse_derive_and_manifest_form_round_trip(text, target, manifest):
    assert str(rv.parse_version(text)) == text
    assert rv.derive_release(text) == target
    assert rv.manifest_form(text) == manifest
    assert rv.pep440_form(manifest) == text


@pytest.mark.parametrize(
    "text",
    ["1.0.0-rc.2", "1.0", "1.0.0a1", "1.0.0rc", "1.0.0.dev", "v1.0.0", "1.0.0.post1", ""],
)
def test_other_shapes_fail_closed(text):
    with pytest.raises(ValueError):
        rv.parse_version(text)


@pytest.mark.parametrize("text", ["1.0.0-rc.03", "1.0.0.rc.3", "1.0.0-rc.3-dev.0", "1.0.0.dev.0"])
def test_non_canonical_manifest_forms_fail_closed(text):
    with pytest.raises(ValueError):
        rv.pep440_form(text)


def test_next_target_after_a_candidate_and_after_a_final():
    assert str(rv.parse_version("1.0.0rc2").next_target) == "1.0.0rc3"
    assert str(rv.parse_version("1.0.0").next_target) == "1.0.1"
    assert str(rv.parse_version("1.0.0rc2.dev5").next_target) == "1.0.0rc3"


def test_todays_tree_starts_the_next_candidate_line():
    # PyPI holds 1.0.0rc1 and 1.0.0rc2; the tree still names 1.0.0rc2.
    decision = rv.next_staging_version(
        "1.0.0rc2", False, True, None, published("1.0.0rc1", "1.0.0rc2")
    )
    assert (decision.action, decision.version) == ("bump", "1.0.0rc3.dev0")


def test_a_bare_unpublished_target_gets_its_first_dev():
    decision = rv.next_staging_version("1.0.0", False, False, None, published())
    assert (decision.action, decision.version) == ("bump", "1.0.0.dev0")


def test_same_content_under_a_published_dev_ships_nothing():
    decision = rv.next_staging_version("1.0.0rc3.dev0", True, False, False, published("1.0.0rc3.dev0"))
    assert decision.action == "none"
    assert decision.version == "1.0.0rc3.dev0"


def test_changed_content_moves_to_the_next_dev():
    decision = rv.next_staging_version("1.0.0rc3.dev0", True, False, True, published("1.0.0rc3.dev0"))
    assert (decision.action, decision.version) == ("bump", "1.0.0rc3.dev1")


def test_an_abandoned_candidate_is_skipped():
    decision = rv.next_staging_version(
        "1.0.0rc3.dev0", True, False, True, published("1.0.0rc3.dev0", "1.0.0rc3.dev1")
    )
    assert decision.version == "1.0.0rc3.dev2"


def test_changed_content_after_promotion_starts_the_next_target():
    decision = rv.next_staging_version(
        "1.0.0rc3.dev4", True, True, True, published("1.0.0rc3.dev4", "1.0.0rc3")
    )
    assert (decision.action, decision.version) == ("bump", "1.0.0rc4.dev0")
    decision = rv.next_staging_version("1.0.0.dev2", True, True, True, published("1.0.0.dev2", "1.0.0"))
    assert (decision.action, decision.version) == ("bump", "1.0.1.dev0")


def test_an_unpublished_dev_of_an_unpublished_target_is_kept():
    # The owner edited pyproject.toml to 1.1.0.dev0 by hand: honoured as written.
    decision = rv.next_staging_version("1.1.0.dev0", False, False, None, published("1.0.0"))
    assert (decision.action, decision.version) == ("keep", "1.1.0.dev0")


def test_an_unpublished_dev_of_a_published_target_moves_on():
    decision = rv.next_staging_version("1.0.0rc3.dev9", False, True, None, published("1.0.0rc3"))
    assert (decision.action, decision.version) == ("bump", "1.0.0rc4.dev0")


def make_sdist(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(f"relay_hermes-1.0.0rc3.dev0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_sdist_comparison_ignores_setuptools_metadata_only(tmp_path):
    left = make_sdist(
        tmp_path / "a.tar.gz",
        {"PKG-INFO": b"a", "relay_hermes.egg-info/PKG-INFO": b"a", "adapter.py": b"x"},
    )
    right = make_sdist(
        tmp_path / "b.tar.gz",
        {"PKG-INFO": b"b", "relay_hermes.egg-info/SOURCES.txt": b"b", "adapter.py": b"x"},
    )
    assert rv.differing_files(rv.sdist_file_hashes(left), rv.sdist_file_hashes(right)) == []
    changed = make_sdist(tmp_path / "c.tar.gz", {"adapter.py": b"y", "state.py": b"z"})
    assert rv.differing_files(rv.sdist_file_hashes(left), rv.sdist_file_hashes(changed)) == [
        "adapter.py",
        "state.py",
    ]


def test_metadata_headers_count_but_long_descriptions_and_markdown_do_not(tmp_path):
    # Header says the target; the body is the README, which names a dev
    # version as an example: prose, not an embedding.
    clean = b"Name: relay-hermes\nVersion: 1.0.0rc3\n\npip install --pre relay-hermes==1.0.0rc3.dev0\n"
    (tmp_path / "PKG-INFO").write_bytes(clean)
    (tmp_path / "METADATA").write_bytes(clean)
    (tmp_path / "README.md").write_text("`1.0.0rc3.dev0` is a staging build\n")
    assert rv.files_carrying_version(tmp_path, "1.0.0rc3.dev0") == []
    # A header that still says the dev version is the embedding the scan exists for.
    stale = b"Name: relay-hermes\nVersion: 1.0.0rc3.dev0\n\nbody\n"
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "PKG-INFO").write_bytes(stale)
    (tmp_path / "sub" / "METADATA").write_bytes(stale)
    # Any other file is scanned in full.
    (tmp_path / "plugin.yaml").write_text("version: 1.0.0rc3.dev0\n")
    assert rv.files_carrying_version(tmp_path, "1.0.0rc3.dev0") == [
        "plugin.yaml",
        "sub/METADATA",
        "sub/PKG-INFO",
    ]


def test_files_carrying_version_finds_every_embedding(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "META").write_text("Version: 1.0.0rc3.dev0\n")
    (tmp_path / "clean.py").write_text("print('1.0.0rc3')\n")
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_x.py").write_text("v = '1.0.0rc3.dev0'\n")
    assert rv.files_carrying_version(tmp_path, "1.0.0rc3.dev0") == ["a/META", "pkg/tests/test_x.py"]
    assert rv.files_carrying_version(tmp_path, "1.0.0rc3.dev0", ("tests",)) == ["a/META"]
    assert rv.files_carrying_version(tmp_path, "9.9.9") == []
    assert rv.VERBATIM_SOURCE_DIRS == ("scripts", "tests")


def tree(tmp_path: Path, version: str, manifest: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "relay-hermes"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "plugin.yaml").write_text(
        f"manifest_version: 1\nname: relay-hermes\nversion: {manifest}\n", encoding="utf-8"
    )
    return tmp_path


def test_write_tree_rewrites_both_carriers_in_their_own_forms(tmp_path):
    root = tree(tmp_path, "1.0.0rc2", "1.0.0-rc.2")
    assert rv.check_tree(root) == "1.0.0rc2"
    assert rv.write_tree(root, "1.0.0rc3.dev0") == ["pyproject.toml", "plugin.yaml"]
    assert 'version = "1.0.0rc3.dev0"' in (root / "pyproject.toml").read_text()
    assert "version: 1.0.0-rc.3.dev.0\n" in (root / "plugin.yaml").read_text()
    assert "manifest_version: 1\n" in (root / "plugin.yaml").read_text()
    assert rv.check_tree(root) == "1.0.0rc3.dev0"


def test_check_tree_refuses_a_manifest_that_disagrees(tmp_path):
    root = tree(tmp_path, "1.0.0rc3.dev0", "1.0.0-rc.3")
    with pytest.raises(ValueError, match="plugin.yaml says 1.0.0-rc.3"):
        rv.check_tree(root)


def test_the_real_tree_is_consistent():
    assert rv.check_tree(SCRIPT.parents[1]) == rv.read_tree(SCRIPT.parents[1])[0]
