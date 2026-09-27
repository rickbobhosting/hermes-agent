"""Native file tools must stay inside a project profile's workspace."""

from pathlib import Path

import pytest

from tools import file_tools, file_tools_paths


@pytest.fixture
def project_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile = tmp_path / "profiles" / "project-fwmb"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "project-fwmb"
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: profile)
    monkeypatch.setattr(file_tools_paths, "_uses_container_paths", lambda _task_id: False)
    monkeypatch.setattr(
        file_tools_paths, "_authoritative_workspace_root", lambda _task_id: str(workspace)
    )
    return workspace


def test_project_file_paths_reject_traversal_absolute_and_symlink_escape(
    project_workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("foreign project content")
    (project_workspace / "escape").symlink_to(outside)

    assert file_tools_paths._project_workspace_denial("notes.txt") is None
    for path in ("../outside.txt", str(outside), "escape"):
        assert file_tools_paths._project_workspace_denial(path)


def test_project_file_tool_entrypoints_fail_closed_before_io(
    project_workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")

    assert "confined" in file_tools.read_file_tool(str(outside))
    assert "confined" in file_tools.write_file_tool(str(outside), "replace")
    assert "confined" in file_tools.search_tool("keep", path=str(tmp_path))
    patch = f"*** Begin Patch\n*** Update File: {outside}\n@@\n-keep\n+replace\n*** End Patch"
    assert "confined" in file_tools.patch_tool(mode="patch", patch=patch)
    assert outside.read_text() == "keep"
