"""Exercise the developer builder in disposable Git repos; never touch live stores."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

BUILDER = Path(__file__).resolve().parents[1] / 'scripts' / 'prepare_release.py'


@unittest.skipUnless(BUILDER.exists(), 'Developer builder is excluded from installed packages')
class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recall packaging ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.out = self.base / 'release'
        self.dest = self.base / 'marketplace'
        self.internal = [
            'docs/update-window-plan.md',
            'docs/update-window-history-2026-09-07.md',
            'docs/update-window-history-future.md',
            'docs/superpowers/plans/old.md',
            'docs/superpowers/specs/retired.md',
        ]
        self.files = {
            '.claude-plugin/plugin.json': '{"version":"2.5.0"}\n',
            'README.md': '# Recall\n',
            'docs/install-and-update.md': '# Upgrade\n',
            'scripts/reader.py': 'print("fixture")\n',
            'tests/test_reader.py': '# Packaged runtime tests\n',
            'benchmarks/private-probe.md': 'internal probe\n',
            **{name: 'maintainer-only marker\n' for name in self.internal},
        }
        for name, data in self.files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)
        shutil.copyfile(BUILDER, self.repo / 'scripts/prepare_release.py')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def build(self):
        return subprocess.run(
            [sys.executable, str(self.repo / 'scripts/prepare_release.py'),
             '--output', str(self.out), '--marketplace-plugin', str(self.dest)],
            capture_output=True, text=True)

    def test_public_payload_excludes_internal_records_and_matches_committed_bytes(self):
        # Even an untracked runtime-looking file must not enter a release.
        (self.repo / 'scripts/untracked.py').write_text('untracked marker')
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.out / 'BUILD.json').read_text())
        expected = {'.claude-plugin/plugin.json', 'README.md',
                    'docs/install-and-update.md', 'scripts/reader.py'}
        self.assertEqual(set(report['archive_files']), expected)
        self.assertEqual(set(report['marketplace_files']), expected | {'tests/test_reader.py'})
        self.assertEqual(report['revision'], self.git('rev-parse', 'HEAD').strip())
        archive = self.out / 'claude-recall-plugin.zip'
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), report['archive_sha256'])
        with ZipFile(archive) as z:
            self.assertEqual(set(z.namelist()), {'claude-recall-plugin/' + n for n in expected})
            for name in expected:
                self.assertEqual(z.read('claude-recall-plugin/' + name), self.files[name].encode())
        for name in self.internal:
            self.assertTrue((self.repo / name).exists())
            self.assertFalse((self.dest / name).exists())

    def test_rebuild_removes_only_previously_managed_internal_files(self):
        self.out.mkdir()
        prior = {}
        for name in self.internal:
            p = self.dest / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('previous package')
            prior[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        unmanaged = self.dest / 'docs/superpowers/local-unmanaged.md'
        unmanaged.write_text('operator-owned note')
        (self.out / 'BUILD.json').write_text(json.dumps({'marketplace_files': prior}))
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.out / 'BUILD.json').read_text())
        self.assertEqual(report['removed_marketplace_files'], sorted(self.internal))
        self.assertEqual(unmanaged.read_text(), 'operator-owned note')
        for name in self.internal:
            self.assertFalse((self.dest / name).exists())
            self.assertTrue((self.repo / name).exists())

    def test_dirty_tracked_source_refuses_before_writing_outputs(self):
        (self.repo / 'README.md').write_text('uncommitted change')
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Commit tracked changes', result.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.dest.exists())
