from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "package-source" / "spk-packager.toml"
RESOURCE = ROOT / "package-source" / "resource.json"
POSTINST = ROOT / "package-source" / "scripts" / "postinst"
WIZARD = ROOT / "package-source" / "wizard" / "install_uifile"
PROVENANCE = ROOT / "package-source" / "payload" / "SOURCE_PROVENANCE.json"
BUILD_CMD = ROOT / "scripts" / "build-spk.cmd"
BUILD_SH = ROOT / "scripts" / "build-spk.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with MANIFEST.open("rb") as handle:
            cls.manifest = tomllib.load(handle)

    def test_dsm7_package_user_is_sc_prefixed(self) -> None:
        package_id = self.manifest["package"]["id"]
        privilege = self.manifest["privilege"]
        self.assertEqual(privilege["run_as"], "package")
        self.assertEqual(privilege.get("username"), f"sc-{package_id}")

    def test_install_does_not_require_or_bootstrap_a_media_root(self) -> None:
        assets = self.manifest.get("assets", {})
        scripts = self.manifest.get("scripts", {})
        self.assertNotIn("wizard_dir", assets)
        self.assertNotIn("resource", assets)
        self.assertNotIn("postinst", scripts)
        self.assertFalse(RESOURCE.exists())
        self.assertFalse(POSTINST.exists())
        self.assertFalse(WIZARD.exists())

    def test_package_revision_advances_past_single_root_installer(self) -> None:
        self.assertEqual(self.manifest["package"]["version"], "0.1.0-0002")

    def test_ci_uploads_canonical_spk_and_hides_repro_copy(self) -> None:
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("package-source/scripts/postinst", text)
        self.assertIn('PRIMARY="dist/OCD-armada38x-$VERSION.spk"', text)
        self.assertIn('REPRO="dist/OCD-repro-check.spk"', text)
        self.assertIn("cmp "$PRIMARY" "$REPRO"", text)
        self.assertIn("path: dist/OCD-armada38x-*.spk", text)
        self.assertNotIn("OCD-a.spk", text)
        self.assertNotIn("OCD-b.spk", text)

    def test_build_scripts_write_provenance_as_bytes(self) -> None:
        for script in (BUILD_CMD, BUILD_SH):
            text = script.read_text(encoding="utf-8")
            self.assertIn("write_bytes", text)
            self.assertNotIn("write_text", text)

    def test_packager_gzip_is_host_independent_stored_deflate(self) -> None:
        import gzip
        import sys

        sys.path.insert(0, str(ROOT / "tools" / "spk_packager"))
        from spk_packager.archive import ArchiveFile, deterministic_tar, deterministic_tgz

        files = [ArchiveFile("fixture.bin", b"A" * 8192)]
        blob = deterministic_tgz(files)
        self.assertEqual(blob[:3], b"\x1f\x8b\x08")
        self.assertEqual(blob[9], 255)
        self.assertEqual((blob[10] >> 1) & 0x03, 0)
        self.assertEqual(gzip.decompress(blob), deterministic_tar(files))

    def test_generated_provenance_uses_lf_only(self) -> None:
        if not PROVENANCE.exists():
            self.skipTest("provenance is generated during SPK build")
        data = PROVENANCE.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        self.assertNotIn(b"\r\n", data)


if __name__ == "__main__":
    unittest.main()
