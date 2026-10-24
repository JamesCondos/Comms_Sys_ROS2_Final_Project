"""Regression for a late HTTP body arriving after a forwarder's queue drained."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from complex_experiments.components import WorkQueue, encode
from partner_network.lab.common import JsonlLog


class Owner:
    def __init__(self, output):
        self.output=Path(output)
        self.logs={}
    def log(self, name):
        if name not in self.logs:self.logs[name]=JsonlLog(self.output/(name+'.jsonl'))
        return self.logs[name]


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_http_body_after_empty_queue_gets_explicit_503_and_no_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            owner=Owner(directory)
            queue=WorkQueue(owner,10)
            first=queue.admit(encode(dict(id='first',payload_b64='')))
            self.assertIs(await queue.get(),first)
            queue.finish(first,dict(outcome='success',http_status=200))
            # Same ordering as Forwarder shutdown: close before join and stop.
            queue.close_admissions()
            await asyncio.wait_for(queue.waiting.join(),1)
            await asyncio.sleep(.001)  # Network body may finish during teardown.
            late=queue.admit(encode(dict(id='late',payload_b64='')))
            status,_=await late['future']
            self.assertEqual(status,503)
            self.assertIsNone(late['admission_ns'])
            self.assertEqual(queue.waiting.qsize()+len(queue.active),0)
            await asyncio.wait_for(queue.waiting.join(),1)
            for log in owner.logs.values():log.close()
            jobs=[json.loads(line) for line in (Path(directory)/'jobs.jsonl').read_text().splitlines()]
            self.assertEqual(jobs[-1]['outcome'],'queue_drop')
            self.assertEqual(jobs[-1]['drop_reason'],'shutdown_rejected')
            events=[json.loads(line) for line in (Path(directory)/'queue.jsonl').read_text().splitlines()]
            self.assertEqual(events[-1]['event'],'drop')
            self.assertEqual(events[-1]['drop_reason'],'shutdown_rejected')
            self.assertEqual([row['id'] for row in events if row['event']=='admit'],['first'])


if __name__=='__main__':unittest.main()
