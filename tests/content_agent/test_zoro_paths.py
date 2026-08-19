import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import supercmo_skills.stitch as stitch
from content_agent.layout import ContentAgentLayoutError, PrivacyBoundaryError
from supercmo_skills import paths


ENVIRONMENT_PATHS = {
    "SUPERCMO_OUTPUT_DIR": "",
    "SUPERCMO_SCRATCH_DIR": "",
    "SUPERCMO_CACHE_DIR": "",
    "SUPERCMO_PROJECTION_DIR": "",
}
REPOSITORY = Path(__file__).parents[2]
URL_EXTRACT_REFERENCE = (
    REPOSITORY / "skills" / "analyzing-products" / "references" / "url-extract.md"
)


class ZoroPathTests(unittest.TestCase):
    """A wrong routing branch must never write private media into public paths."""

    def make_active_root(self, temporary_directory: str) -> Path:
        root = Path(temporary_directory) / "content-agent"
        root.mkdir()
        (root / "content-agent.config.json").write_text(
            '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
            encoding="utf-8",
        )
        return root

    def test_public_defaults_remain_unchanged_without_content_agent_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                self.assertEqual(paths.output_dir(), str((root / "supercmo-media").resolve()))
                self.assertEqual(
                    paths.scratch_dir(), str(Path(tempfile.gettempdir()) / "supercmo-work")
                )
                self.assertEqual(
                    paths.cache_dir(), str(Path(tempfile.gettempdir()) / "supercmo-cache")
                )
                self.assertEqual(
                    paths.projection_dir(), str((root / ".supercmo" / "projections").resolve())
                )

    def test_zoro_defaults_land_in_private_workspace_destinations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_active_root(temporary_directory)
            with chdir(root), patch.dict(
                os.environ,
                {**ENVIRONMENT_PATHS, "CONTENT_AGENT_WORKSPACE": str(root / "public")},
                clear=False,
            ):
                workspace = root / "workspace"
                self.assertEqual(
                    paths.output_dir(), str((workspace / "media" / "generated").resolve())
                )
                self.assertEqual(
                    paths.scratch_dir(), str((workspace / "cache" / "scratch").resolve())
                )
                self.assertEqual(
                    paths.cache_dir(), str((workspace / "cache" / "runtime").resolve())
                )
                self.assertEqual(
                    paths.projection_dir(),
                    str((workspace / "projections" / "generated").resolve()),
                )

    def test_zoro_confines_explicit_destinations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_active_root(temporary_directory)
            workspace = root / "workspace"
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                for resolver, name in (
                    (paths.output_dir, "output"),
                    (paths.scratch_dir, "scratch"),
                    (paths.cache_dir, "cache"),
                    (paths.projection_dir, "projection"),
                ):
                    with self.subTest(destination=name):
                        self.assertEqual(
                            resolver(str(workspace / name)),
                            str((workspace / name).resolve()),
                        )
                        with self.assertRaises(PrivacyBoundaryError):
                            resolver(str(root / f"public-{name}"))

    def test_zoro_confines_environment_destinations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_active_root(temporary_directory)
            workspace = root / "workspace"
            for resolver, environment, name in (
                (paths.output_dir, "SUPERCMO_OUTPUT_DIR", "output"),
                (paths.scratch_dir, "SUPERCMO_SCRATCH_DIR", "scratch"),
                (paths.cache_dir, "SUPERCMO_CACHE_DIR", "cache"),
                (paths.projection_dir, "SUPERCMO_PROJECTION_DIR", "projection"),
            ):
                with self.subTest(destination=name), chdir(root), patch.dict(
                    os.environ,
                    {**ENVIRONMENT_PATHS, environment: str(workspace / name)},
                    clear=False,
                ):
                    self.assertEqual(resolver(), str((workspace / name).resolve()))
                with self.subTest(destination=f"unsafe-{name}"), chdir(root), patch.dict(
                    os.environ,
                    {**ENVIRONMENT_PATHS, environment: str(root / f"public-{name}")},
                    clear=False,
                ):
                    with self.assertRaises(PrivacyBoundaryError):
                        resolver()

    def test_wrong_basename_leaves_public_routing_active(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "supercmo-skills"
            root.mkdir()
            (root / "content-agent.config.json").write_text(
                '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
                encoding="utf-8",
            )
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                self.assertEqual(paths.output_dir(), str((root / "supercmo-media").resolve()))

    def test_malformed_active_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            (root / "content-agent.config.json").write_text("{", encoding="utf-8")
            with chdir(root):
                with self.assertRaises(ContentAgentLayoutError):
                    paths.output_dir()

    def test_zoro_rejects_sibling_prefix_and_escaping_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_active_root(temporary_directory)
            workspace = root / "workspace"
            escaped_target = Path(temporary_directory) / "public-output"
            workspace.mkdir()
            escaped_target.mkdir()
            (workspace / "escaped").symlink_to(escaped_target, target_is_directory=True)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                with self.assertRaises(PrivacyBoundaryError):
                    paths.output_dir(str(root / "workspace-evil" / "generated"))
                with self.assertRaises(PrivacyBoundaryError):
                    paths.output_dir(str(workspace / "escaped" / "generated"))

    def test_path_cli_normalizes_every_configured_destination_and_keeps_public_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            configured_root = self.make_active_root(temporary_directory)
            public_root = Path(temporary_directory) / "public"
            public_root.mkdir()
            script = Path(__file__).parents[2] / "scripts" / "content_agent_cli.py"
            environment = {
                **os.environ,
                **ENVIRONMENT_PATHS,
                "PYTHONPATH": str(script.parent),
            }

            expectations = {
                "output": (
                    configured_root / "workspace" / "media" / "generated",
                    public_root / "supercmo-media",
                ),
                "scratch": (
                    configured_root / "workspace" / "cache" / "scratch",
                    Path(tempfile.gettempdir()) / "supercmo-work",
                ),
                "cache": (
                    configured_root / "workspace" / "cache" / "runtime",
                    Path(tempfile.gettempdir()) / "supercmo-cache",
                ),
                "projection": (
                    configured_root / "workspace" / "projections" / "generated",
                    public_root / ".supercmo" / "projections",
                ),
            }
            for kind, (configured_path, public_path) in expectations.items():
                with self.subTest(kind=kind):
                    configured = subprocess.run(
                        [sys.executable, str(script), "path", "--kind", kind],
                        cwd=configured_root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    public = subprocess.run(
                        [sys.executable, str(script), "path", "--kind", kind],
                        cwd=public_root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(configured.returncode, 0, configured.stderr)
                    self.assertEqual(configured.stdout.strip(), str(configured_path.resolve()))
                    self.assertEqual(public.returncode, 0, public.stderr)
                    expected_public = (
                        public_path.resolve()
                        if kind in {"output", "projection"}
                        else public_path
                    )
                    self.assertEqual(public.stdout.strip(), str(expected_public))


class StitchConfinementTests(unittest.TestCase):
    def make_active_root(self, temporary_directory: str) -> Path:
        root = Path(temporary_directory) / "content-agent"
        root.mkdir()
        (root / "content-agent.config.json").write_text(
            '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
            encoding="utf-8",
        )
        return root

    def test_active_stitch_rejects_explicit_output_outside_workspace_before_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_active_root(temporary_directory)
            with chdir(root), patch("supercmo_skills.stitch.shutil.which", return_value="ffmpeg"):
                with self.assertRaises(PrivacyBoundaryError):
                    stitch.video_stitch(
                        ["first.mp4", "second.mp4"],
                        output=str(root / "public.mp4"),
                        dry_run=True,
                    )

    def test_active_stitch_creates_temporary_workdir_under_confined_scratch(self):
        def successful_concat(ffmpeg, clips, workdir, output):
            del ffmpeg, clips, workdir
            Path(output).write_bytes(b"stitched")
            return 0, ""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_active_root(temporary_directory)
            workspace = root / "workspace"
            clips = [root / "first.mp4", root / "second.mp4"]
            for clip in clips:
                clip.write_bytes(b"clip")
            with (
                chdir(root),
                patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False),
                patch("supercmo_skills.stitch.shutil.which", return_value="ffmpeg"),
                patch("supercmo_skills.stitch._res", return_value=None),
                patch("supercmo_skills.stitch._concat_copy", side_effect=successful_concat),
                patch("supercmo_skills.stitch._probe", return_value=(None, None, 8)),
                patch("supercmo_skills.stitch.shutil.rmtree"),
            ):
                result = stitch.video_stitch(
                    [str(clip) for clip in clips],
                    output=str(workspace / "media" / "result.mp4"),
                )
                workdirs = list((workspace / "cache" / "scratch").glob("supercmo_stitch_*"))

            self.assertTrue(result["ok"], result)
            self.assertEqual(len(workdirs), 1)
            self.assertTrue(workdirs[0].is_dir())


class UrlExtractScratchContractTests(unittest.TestCase):
    def scratch_setup(self) -> str:
        contents = URL_EXTRACT_REFERENCE.read_text(encoding="utf-8")
        block = contents.split("```", 2)[1].strip()
        setup = "\n".join(line for line in block.splitlines() if not line.startswith("curl "))
        return setup + '\nprintf "%s" "$SCRATCH"\n'

    def run_setup(self, cwd: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", "-c", self.scratch_setup()],
            cwd=cwd,
            env={**os.environ, **ENVIRONMENT_PATHS, **environment},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_public_non_checkout_uses_existing_fallback_when_cli_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fallback = root / "public-scratch"

            result = self.run_setup(root, {"SUPERCMO_SCRATCH_DIR": str(fallback)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(fallback))
            self.assertTrue(fallback.is_dir())

    def test_active_valid_marker_without_cli_fails_closed_from_nested_cwd(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            nested = root / "jobs" / "current"
            nested.mkdir(parents=True)
            (root / "content-agent.config.json").write_text(
                '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
                encoding="utf-8",
            )
            public_fallback = root / "must-not-be-created"

            result = self.run_setup(
                nested, {"SUPERCMO_SCRATCH_DIR": str(public_fallback)}
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(public_fallback.exists())

    def test_malformed_marker_without_cli_fails_closed_from_nested_cwd(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            nested = root / "jobs" / "current"
            nested.mkdir(parents=True)
            (root / "content-agent.config.json").write_text("{", encoding="utf-8")
            public_fallback = root / "must-not-be-created"

            result = self.run_setup(
                nested, {"SUPERCMO_SCRATCH_DIR": str(public_fallback)}
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(public_fallback.exists())

    def test_active_checkout_uses_validated_cli_and_malformed_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "content-agent"
            root.mkdir()
            nested = root / "jobs" / "current"
            nested.mkdir(parents=True)
            (root / "scripts").symlink_to(REPOSITORY / "scripts", target_is_directory=True)
            marker = root / "content-agent.config.json"
            marker.write_text(
                '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
                encoding="utf-8",
            )

            active = self.run_setup(nested, {})

            self.assertEqual(active.returncode, 0, active.stderr)
            self.assertEqual(
                active.stdout,
                str((root / "workspace" / "cache" / "scratch").resolve()),
            )
            marker.write_text("{", encoding="utf-8")
            public_fallback = root / "must-not-be-created"

            malformed = self.run_setup(
                nested, {"SUPERCMO_SCRATCH_DIR": str(public_fallback)}
            )

            self.assertNotEqual(malformed.returncode, 0)
            self.assertFalse(public_fallback.exists())


if __name__ == "__main__":
    unittest.main()
