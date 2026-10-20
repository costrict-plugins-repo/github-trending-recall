#!/usr/bin/env python3
"""Prepare opt-in Codex lifecycle capture config; never install or grant hook trust."""
import argparse,json,shlex,sys
from pathlib import Path
from recall_codex_hook import EVENTS


def prepare(output, db, root, cwd=None, existing=None, python=None):
    output=Path(output).expanduser().resolve()
    if output.exists():raise ValueError('Refusing to overwrite the prepared file')
    root=Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():raise ValueError('Capture root must be a directory')
    data=json.loads(Path(existing).read_text()) if existing else {'hooks':{}}
    if not isinstance(data,dict) or not isinstance(data.get('hooks',{}),dict):raise ValueError('Invalid existing hook configuration')
    data.setdefault('hooks',{})
    executable=Path(python or sys.executable).resolve(strict=True)
    command=[str(executable),str(Path(__file__).with_name('recall_codex_hook.py').resolve()),'--db',str(Path(db).expanduser().resolve()),'--root',str(root)]
    if cwd:command+=['--cwd',str(Path(cwd).expanduser().resolve(strict=True))]
    command=shlex.join(command)
    for event in EVENTS:
        groups=data['hooks'].setdefault(event,[])
        if not isinstance(groups,list):raise ValueError('Invalid event hook list')
        if any(h.get('command')==command for g in groups if isinstance(g,dict) for h in g.get('hooks',[]) if isinstance(h,dict)):
            continue
        groups.append({'hooks':[{'type':'command','command':command,'timeout':3 if event in ('SessionEnd','Interrupt') else 5}]})
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(data,indent=2)+'\n')
    return {'prepared':str(output),'installed':False,'trusted':False,
            'next_action':'Review this exact config, merge at the selected Codex config layer, and approve its hook definitions through Codex /hooks. No trust bypass was used.',
            'scope':'Only matching root transcript events under the selected root; no subagent identity inheritance or private session binding.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--db',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--cwd',type=Path)
    p.add_argument('--existing',type=Path);p.add_argument('--python',type=Path)
    a=p.parse_args()
    try:print(json.dumps(prepare(a.output,a.db,a.root,a.cwd,a.existing,a.python),indent=2));return 0
    except (ValueError,OSError) as exc:print(json.dumps({'error':str(exc)}),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
