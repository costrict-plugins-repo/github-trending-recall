"""P18: repository identity across worktrees, remotes, clones, missing dirs, overrides."""
import json, os, subprocess, sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).parents[1]/'scripts')]
import memory_store as m
from db import get_connection
from recall_memory import parser, run


def git(cwd,*a): return subprocess.check_output(['git','-C',str(cwd),*a],stderr=subprocess.DEVNULL,text=True).strip()


def make_repo(path,remote=None):
    path.mkdir(); git(path,'init','-q'); (path/'f').write_text('x')
    git(path,'-c','user.email=t@t','-c','user.name=t','add','.'); git(path,'-c','user.email=t@t','-c','user.name=t','commit','-q','-m','i')
    if remote: git(path,'remote','add','origin',remote)
    return path


def rec(role,text,key):
    return {'type':role,'uuid':key,'timestamp':'2026-09-05T12:00:00Z','message':{'role':role,'content':[{'type':'text','text':text}]}}


def test_worktree_shares_identity_with_main_checkout(tmp_path):
    main=make_repo(tmp_path/'main'); wt=tmp_path/'wt'
    git(main,'worktree','add','-q',str(wt),'-b','side')
    assert m.repository_identity(main)==m.repository_identity(wt)
    assert m.repository_identity(main).startswith('local-git:')


def test_subdirectory_resolves_to_its_repository(tmp_path):
    main=make_repo(tmp_path/'main'); (main/'sub').mkdir()
    assert m.repository_identity(main/'sub')==m.repository_identity(main)


def test_ssh_and_https_clones_of_same_remote_match_and_differ_from_other_repo(tmp_path):
    a=make_repo(tmp_path/'a','git@github.com:owner/project.git')
    b=make_repo(tmp_path/'b','https://github.com/owner/project.git')
    other=make_repo(tmp_path/'c','https://github.com/owner/other.git')
    assert m.repository_identity(a)==m.repository_identity(b)=='github.com/owner/project'
    assert m.repository_identity(other)!=m.repository_identity(a)


def test_changed_remote_keeps_existing_source_scope_until_rescoped(tmp_path):
    repo=make_repo(tmp_path/'r','https://github.com/owner/old.git')
    conn=get_connection(tmp_path/'m.db'); p=tmp_path/'t.jsonl'
    p.write_text(json.dumps(rec('assistant','evidence','a'))+'\n')
    m.index_file(conn,p,session_id='s',cwd=str(repo)); conn.commit()
    assert m.status(conn)['sources'][0]['repo_id']=='github.com/owner/old'
    git(repo,'remote','set-url','origin','https://github.com/owner/new.git')
    m.index_file(conn,p,session_id='s',cwd=str(repo)); conn.commit()          # same cwd: scope unchanged (stable)
    assert m.status(conn)['sources'][0]['repo_id']=='github.com/owner/old'
    out=run(parser().parse_args(['rescope','claude:s','--cwd',str(repo)]),conn)   # explicit correction
    assert out['now']['repo_id']=='github.com/owner/new'
    assert m.search(conn,'evidence',repo_id='github.com/owner/new')
    conn.close()


def test_one_repository_does_not_contaminate_anothers_brief_or_search(tmp_path):
    a=make_repo(tmp_path/'a','https://github.com/o/a.git'); b=make_repo(tmp_path/'b','https://github.com/o/b.git')
    conn=get_connection(tmp_path/'m.db')
    for repo,sid,text in ((a,'sa','alpha decision'),(b,'sb','beta decision')):
        p=tmp_path/f'{sid}.jsonl'; p.write_text(json.dumps(rec('user',text,'k'))+'\n')
        m.index_file(conn,p,session_id=sid,cwd=str(repo)); conn.commit()
    ra=m.repository_identity(a)
    assert [h['text'] for h in m.search(conn,'decision',repo_id=ra)]==['alpha decision']
    assert [e['agent']+':'+e['session_id'] for e in m.brief(conn,repo_id=ra)['evidence']]==['claude:sa']
    assert len(m.search(conn,'decision'))==2      # unscoped sees both
    conn.close()


def test_missing_directory_and_non_repo_fall_back_to_stable_hashes(tmp_path):
    missing=tmp_path/'gone'
    assert m.repository_identity(missing).startswith('directory:')
    plain=tmp_path/'plain'; plain.mkdir()
    assert m.repository_identity(plain).startswith('directory:')
    assert m.repository_identity(plain)==m.repository_identity(plain)


def test_cwd_override_corrects_unsuitable_source_metadata(tmp_path):
    repo=make_repo(tmp_path/'r','https://github.com/o/r.git')
    conn=get_connection(tmp_path/'m.db'); p=tmp_path/'t.jsonl'
    p.write_text(json.dumps({'type':'user','uuid':'k','cwd':'/nonexistent/elsewhere','sessionId':'s','timestamp':'t',
                             'message':{'role':'user','content':'hello'}})+'\n')
    m.index_file(conn,p); conn.commit()                       # metadata from the trace: unsuitable cwd
    assert m.status(conn)['sources'][0]['repo_id'].startswith('directory:')
    m.index_file(conn,p,cwd=str(repo)); conn.commit()         # explicit --cwd corrects it
    assert m.status(conn)['sources'][0]['repo_id']=='github.com/o/r'
    conn.close()
