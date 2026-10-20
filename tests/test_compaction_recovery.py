"""P16: bounded verbatim compaction recovery from durable blocks (SessionStart/compact)."""
import json, shutil, sys, tempfile, unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parents[1]/'hooks'),str(Path(__file__).parents[1]/'scripts')]
from post_compact import run_hook, build_recovery_context, RECOVERY_MAX_CHARS
from prompt_submit import run_hook as prompt_hook
from db import get_connection


def _entry(role,text,ts,key):
    return {'type':role,'uuid':key,'timestamp':ts,'message':{'role':role,'content':[{'type':'text','text':text}]}}


class TestCompactionRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.mkdtemp(); self.db=Path(self.tmp)/'t.db'; self.tp=str(Path(self.tmp)/'t.jsonl')
    def tearDown(self): shutil.rmtree(self.tmp,ignore_errors=True)

    def _capture(self,*entries):
        with open(self.tp,'a') as f:
            for e in entries: f.write(json.dumps(e)+'\n')
        prompt_hook({'session_id':'s','transcript_path':self.tp,'prompt':'x','cwd':self.tmp},db_path=self.db)

    def test_verbatim_cited_and_bounded(self):
        long_reply='notes '*300+'FINAL: batching rejected because of latency.'
        self._capture(_entry('user','Objective: make the parser streaming','2026-09-01T10:00:00Z','u1'),
                      _entry('assistant',long_reply,'2026-09-01T10:05:00Z','a1'),
                      _entry('user','next question','2026-09-01T11:00:00Z','u2'),
                      _entry('assistant','A short answer.','2026-09-01T11:01:00Z','a2'))
        out=run_hook({'session_id':'s','source':'compact'},db_path=self.db)
        ctx=out['hookSpecificOutput']['additionalContext']
        self.assertIn('[Context Compacted]',ctx)
        self.assertIn('Objective: make the parser streaming',ctx)          # opening ask, verbatim
        self.assertIn('batching rejected because of latency',ctx)         # tail of the long reply survives
        self.assertNotIn('notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes notes',ctx)
        self.assertRegex(ctx,r'get [0-9a-f]{32} --start \d+')                 # citations
        self.assertLessEqual(len(ctx),RECOVERY_MAX_CHARS)
        self.assertNotIn('systemMessage',out)

    def test_not_repeated_for_unchanged_state(self):
        self._capture(_entry('user','ask','t','u1'),_entry('assistant','reply','t','a1'))
        first=run_hook({'session_id':'s'},db_path=self.db); self.assertIn('hookSpecificOutput',first)
        self.assertEqual(run_hook({'session_id':'s'},db_path=self.db),{})      # same state: silent
        self._capture(_entry('user','new ask','t','u2'),_entry('assistant','new reply','t','a2'))
        self.assertIn('new reply',run_hook({'session_id':'s'},db_path=self.db)['hookSpecificOutput']['additionalContext'])

    def test_falls_back_to_legacy_nudge_without_durable_blocks(self):
        conn=get_connection(self.db)
        from db import insert_session, insert_exchanges, update_session_offset
        insert_session(conn,'legacy','/p','h','2026-01-01')
        insert_exchanges(conn,'legacy',[{'idx':1,'timestamp':'2026-01-01T00:00:00Z','preview':'old preview','user_text':'q','assistant_text':'a'}])
        update_session_offset(conn,'legacy',100,1)
        conn.close()
        ctx=run_hook({'session_id':'legacy'},db_path=self.db)['hookSpecificOutput']['additionalContext']
        self.assertIn('1 exchanges indexed',ctx); self.assertIn('old preview',ctx)

    def test_cap_holds_for_huge_blocks(self):
        self._capture(_entry('user','x'*5000,'t','u1'),_entry('assistant','y'*5000,'t','a1'))
        ctx=run_hook({'session_id':'s'},db_path=self.db)['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(ctx),RECOVERY_MAX_CHARS)


if __name__=='__main__': unittest.main()
