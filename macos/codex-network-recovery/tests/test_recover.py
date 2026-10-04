import errno
import importlib.util
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recover', Path(__file__).parents[1] / 'recover.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RecoveryTests(unittest.TestCase):
    def failed(self):
        return {'type': 'task_complete', 'turn_id': 'failed', 'completed_at': 100,
                'error': {'message': 'Fatal error: ' + r.ERROR}}

    def thread(self, **changes):
        return {'status': {'type': 'systemError'}, **changes}

    def test_only_exact_failure_and_no_later_work(self):
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.jsonl') as f:
            f.write(json.dumps({'type': 'event_msg', 'payload': self.failed()}) + '\n');f.flush()
            self.assertEqual(r.failed_turn(f.name)['turn_id'], 'failed')
            for payload in [{'type': 'task_started'}, {'type': 'turn_aborted'},
                            {'type': 'task_complete', 'error': {'message': 'timeout'}}]:
                f.write(json.dumps({'type': 'event_msg', 'payload': payload}) + '\n');f.flush()
                self.assertIsNone(r.failed_turn(f.name))

    def test_mentions_in_user_text_do_not_trigger(self):
        with tempfile.NamedTemporaryFile(mode='w+') as f:
            json.dump({'type': 'response_item', 'payload': {'role': 'user', 'text': r.ERROR}}, f);f.flush()
            self.assertIsNone(r.failed_turn(f.name))

    def test_active_paused_child_stale_and_settling_are_excluded(self):
        for thread in [self.thread(status={'type': 'active'}), self.thread(status={'type': 'idle'}),
                       self.thread(parentThreadId='parent'), self.thread(canAcceptDirectInput=False)]:
            self.assertFalse(r.eligible(thread, self.failed(), 140))
        self.assertFalse(r.eligible(self.thread(), self.failed(), 120))
        self.assertFalse(r.eligible(self.thread(), self.failed(), 90000))
        self.assertTrue(r.eligible(self.thread(), self.failed(), 140))

    def client(self, newer=False, fail_send=False):
        test = self
        class Fake:
            def __init__(self): self.calls=[]
            def rpc(self, method, params):
                self.calls.append((method,params))
                if method in ('thread/read','thread/resume'):return {'thread':test.thread()}
                if method=='thread/turns/list':return {'data':[{'id':'new' if newer else 'failed', 'error':test.failed()['error']}]}
                if method=='turn/start':
                    if fail_send:raise TimeoutError('lost reply')
                    return {'turn':{'id':'new'}}
        return Fake()

    def test_resume_keeps_settings_and_deduplicates(self):
        client=self.client();state={}
        self.assertEqual(r.recover(client,'id',self.failed(),state,140),'resumed')
        self.assertEqual(r.recover(client,'id',self.failed(),state,150),'already-attempted')
        start=[p for m,p in client.calls if m=='turn/start'][0]
        self.assertEqual(set(start),{'threadId','clientUserMessageId','input'})
        self.assertEqual([p for m,p in client.calls if m=='thread/resume'],[{'threadId':'id','excludeTurns':True}])

    def test_lost_reply_is_not_replayed(self):
        state={};client=self.client(fail_send=True)
        with self.assertRaises(TimeoutError):r.recover(client,'id',self.failed(),state,140)
        self.assertEqual(r.recover(client,'id',self.failed(),state,150),'already-attempted')

    def test_new_turn_and_dry_run_and_rate_limit(self):
        self.assertEqual(r.recover(self.client(newer=True),'id',self.failed(),{},140),'changed')
        c=self.client();self.assertEqual(r.recover(c,'id',self.failed(),{},140,True),'would-resume')
        self.assertFalse(any(m=='turn/start' for m,p in c.calls))
        state={'attempts':{str(i):{'thread':'id','time':130} for i in range(3)}}
        self.assertEqual(r.recover(c,'id',self.failed(),state,140),'rate-limited')

    def test_failed_child_notifies_active_parent_once(self):
        calls=[]
        failed=self.failed()
        class Fake:
            def rpc(self,method,params):
                calls.append((method,params))
                if method=='thread/read':
                    if params['threadId']=='child':return {'thread':{'status':{'type':'systemError'},'parentThreadId':'parent'}}
                    return {'thread':{'status':{'type':'active','activeFlags':[]}}}
                if method=='thread/turns/list':
                    if params['threadId']=='child':return {'data':[{'id':'failed','error':failed['error']}]}
                    return {'data':[{'id':'parent-turn','status':'inProgress'}]}
                if method=='turn/steer':return {}
                raise AssertionError(method)
        c=Fake();state={}
        self.assertEqual(r.recover(c,'child',failed,state,140),'parent-notified')
        self.assertEqual(r.recover(c,'child',failed,state,150),'already-attempted')
        self.assertFalse(any(m in ('turn/start','thread/resume') for m,p in calls))
        self.assertEqual([p['expectedTurnId'] for m,p in calls if m=='turn/steer'],['parent-turn'])

    def test_file_exhaustion_then_capacity_fix_in_isolated_process(self):
        code='''import sys,resource,os,errno
sys.path.insert(0,sys.argv[1])
from recover import raise_file_limit
resource.setrlimit(resource.RLIMIT_NOFILE,(256,resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
fds=[]
try:
 while True:fds.append(os.open(os.devnull,os.O_RDONLY))
except OSError as e:
 assert e.errno==errno.EMFILE
raise_file_limit()
fd=os.open(os.devnull,os.O_RDONLY);os.close(fd)
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0]>=4096
'''
        subprocess.run([sys.executable,'-c',code,str(Path(__file__).parents[1])],check=True)


if __name__=='__main__':unittest.main()
