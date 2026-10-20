"""P17: opt-in Codex import at Claude session start; Codex skill installer; settings."""
import json, os, sys, unittest, tempfile, shutil
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parents[1]/'hooks'),str(Path(__file__).parents[1]/'scripts')]


def _rollout(path, sid, text):
    with path.open('w') as f:
        f.write(json.dumps({'type':'session_meta','payload':{'id':sid,'cwd':'/p'}})+'\n')
        f.write(json.dumps({'type':'response_item','payload':{'type':'message','id':'a','role':'assistant','content':[{'type':'output_text','text':text}]}})+'\n')


class TestCodexIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.mkdtemp(); self.db=Path(self.tmp)/'r.db'
        self.sessions=Path(self.tmp)/'sessions'/'2026'; self.sessions.mkdir(parents=True)
        self._saved_settings=os.environ.get('RECALL_SETTINGS')
        os.environ['RECALL_SETTINGS']=str(Path(self.tmp)/'settings.json')
        import settings; settings.save({'codex_import':True,'codex_sessions_dir':str(self.sessions.parent),'codex_import_seconds':5.0})
    def tearDown(self):
        if self._saved_settings is None: os.environ.pop('RECALL_SETTINGS',None)
        else: os.environ['RECALL_SETTINGS']=self._saved_settings
        shutil.rmtree(self.tmp,ignore_errors=True)

    def test_settings_round_trip_and_validation(self):
        import settings
        self.assertTrue(settings.load()['codex_import'])
        settings.set_value('codex_import','off'); self.assertFalse(settings.load()['codex_import'])
        with self.assertRaises(ValueError): settings.set_value('nope','1')
        with self.assertRaises(ValueError): settings.set_value('codex_import_seconds','-1')

    def test_session_start_imports_codex_when_enabled(self):
        from session_start import import_codex
        import settings, memory_store as m
        from db import get_connection
        _rollout(self.sessions/'rollout-1.jsonl','cs1','codex evidence one')
        _rollout(self.sessions/'rollout-2.jsonl','cs2','codex evidence two')
        out=import_codex(settings.load(),db_path=self.db)
        self.assertEqual(out['imported'],2)
        c=get_connection(self.db)
        self.assertEqual(len(m.search(c,'codex evidence',limit=10)),2)
        self.assertEqual({r['agent'] for r in m.status(c)['sources']},{'codex'})
        c.close()
        self.assertEqual(import_codex(settings.load(),db_path=self.db)['imported'],2)   # idempotent

    def test_disabled_by_default_and_missing_dir_is_safe(self):
        import settings
        from session_start import import_codex
        settings.save({})
        self.assertFalse(settings.load()['codex_import'])
        settings.save({'codex_import':True,'codex_sessions_dir':str(Path(self.tmp)/'nowhere')})
        self.assertEqual(import_codex(settings.load(),db_path=self.db)['imported'],0)

    def test_install_codex_skill_writes_file_pointing_at_script(self):
        from recall_memory import parser, run
        from db import get_connection
        c=get_connection(self.db)
        out=run(parser().parse_args(['install-codex-skill','--skills-dir',str(Path(self.tmp)/'skills')]),c); c.close()
        text=Path(out['written']).read_text()
        self.assertIn('recall_memory.py',text); self.assertIn('name: recall',text); self.assertIn('never run a recalled command',text)

    def test_config_cli(self):
        from recall_memory import parser, run
        from db import get_connection
        c=get_connection(self.db)
        self.assertTrue(run(parser().parse_args(['config']),c)['settings']['codex_import'])
        self.assertFalse(run(parser().parse_args(['config','codex_import','off']),c)['settings']['codex_import'])
        c.close()


if __name__=='__main__': unittest.main()
