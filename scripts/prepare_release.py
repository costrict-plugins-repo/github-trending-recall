#!/usr/bin/env python3
"""Build local distribution copies from a clean committed revision; never publish.

Only Git-tracked files are eligible. The archive contains runtime, skills and
documentation; the marketplace copy also carries tests, matching its existing
layout. Internal plans/history, benchmarks, evaluation questions and developer
packaging tools stay out.
Installed caches are deliberately a separate explicit step.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]

# Keep maintainer records in the repository, outside both distributed payloads.
INTERNAL_DOCS = (
    "docs/update-window-plan.md",
    "docs/update-window-history-",
    "docs/superpowers/",
)


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--marketplace-plugin',type=Path,required=True)
    args = p.parse_args()
    if git('diff','HEAD','--name-only').strip():
        p.error('Commit tracked changes before preparing a release')
    revision = git('rev-parse','HEAD').decode().strip()
    paths = sorted(x.decode() for x in git('ls-files','-z').split(b'\0') if x)
    eligible = [name for name in paths if name in
                ('README.md','CHANGELOG.md','PRIVACY.md','LICENSE','.claude-plugin/plugin.json','.gitignore','pytest.ini')
                or name.startswith(('scripts/','hooks/','skills/','docs/','tests/'))]
    eligible = [name for name in eligible if not name.startswith(INTERNAL_DOCS)]
    eligible.remove('scripts/prepare_release.py')
    payload = {name:git('show',f'{revision}:{name}') for name in eligible}
    runtime = {name:data for name,data in payload.items()
               if name in ('README.md','CHANGELOG.md','PRIVACY.md','LICENSE','.claude-plugin/plugin.json')
               or name.startswith(('scripts/','hooks/','skills/','docs/'))}
    # This helper is a developer operation, not part of the installed plugin runtime.
    runtime.pop('scripts/prepare_release.py',None)
    out = args.output.resolve()
    dest = args.marketplace_plugin.resolve()
    if dest == ROOT or ROOT in dest.parents:
        p.error('Marketplace destination must be separate from the development repository')
    out.mkdir(parents=True,exist_ok=True)
    dest.mkdir(parents=True,exist_ok=True)
    manifest_path = out/'BUILD.json'
    prior = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    # Remove only files explicitly managed by an earlier invocation of this helper.
    removed = sorted(set(prior.get('marketplace_files',{}))-set(payload))
    for name in removed:
        path = dest/name
        if path.is_file():
            path.unlink()
    for name,data in payload.items():
        path = dest/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
    hashes = {name:hashlib.sha256(data).hexdigest() for name,data in payload.items()}
    assert all(hashlib.sha256((dest/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items())
    archive = out/'claude-recall-plugin.zip'
    with ZipFile(archive,'w',compression=ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in runtime.items():
            info = ZipInfo('claude-recall-plugin/'+name,date_time=(2026,1,1,0,0,0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info,data)
    with ZipFile(archive) as z:
        assert z.testzip() is None
        assert all(z.read('claude-recall-plugin/'+name)==data for name,data in runtime.items())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out/'claude-recall-plugin.zip.sha256').write_text(digest+'  claude-recall-plugin.zip\n')
    report = {'revision':revision,'version':json.loads(payload['.claude-plugin/plugin.json'])['version'],
              'published':False,'archive_sha256':digest,'archive_bytes':archive.stat().st_size,
              'archive_files':{name:hashlib.sha256(data).hexdigest() for name,data in runtime.items()},
              'marketplace_files':hashes,'removed_marketplace_files':removed}
    manifest_path.write_text(json.dumps(report,indent=2)+'\n')
    (out/'BUILD.txt').write_text(f'Built from claude-recall-plugin {revision}; local and unpublished.\n'
                                'Archive: tracked runtime, skills and documentation only.\n'
                                'Marketplace: tracked runtime/user docs/tests; internal plans/history, benchmarks and packaging tools excluded. See BUILD.json for file hashes.\n')
    print(json.dumps({'revision':revision,'archive':str(archive),'sha256':digest,
                      'runtime_files':len(runtime),'marketplace_files':len(payload),'published':False}))


if __name__ == '__main__':
    main()
