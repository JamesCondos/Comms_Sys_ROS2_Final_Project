import json
from pathlib import Path
import tempfile
import unittest

from complex_experiments.analyze import analyze, cohort_metrics, replay_queue


def event(action,stamp,identity,q,b):
    return dict(event=action,time_ns=stamp,id=identity,queue_len=q,in_service=b)


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')


def write_records(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')


def minimal_run(folder):
    config = dict(experiment='queue',protocol='h2',workers=2,queue_capacity=2000,
        gateway_concurrency=32,edge_concurrency=64,offered_rps_total=50,
        q_mean_service_s=.02,video_enabled=False,capture=False,
        robots=[dict(id='robot'+str(i),site='A' if i<3 else 'B') for i in range(1,5)])
    write_json(folder/'resolved.json',config)
    write_json(folder/'start.json',dict(start_ns=0,measurement_start_ns=100,
        measurement_end_ns=1000,drain_deadline_ns=1500,clock_domain_id='testclock'))
    source=[]
    job_groups={}
    workers={name:[] for name in ['worker1','worker2']}
    for index,robot in enumerate(config['robots']):
        stamp=110+index*100
        row=dict(id=robot['id']+':0',robot_id=robot['id'],message_type='odom',seq=0,
            source_ns=stamp,scheduled_ns=stamp,payload_bytes=250,http_body_bytes=500)
        source.append(row)
        write_records(folder/'logs'/robot['id']/'source.jsonl',[row])
        worker_id='worker'+str(index%2+1)
        worker=dict(**row,outcome='processed',worker_id=worker_id,admission_ns=stamp+30,
            service_start_ns=stamp+31,processed_ns=stamp+40,requested_service_s=9e-9)
        workers[worker_id].append(worker)
        for name,admit,started,terminal in [(robot['id'],1,2,70),('edge'+robot['site'],10,11,60),('proxy',20,21,50)]:
            job_groups.setdefault(name,[]).append(dict(**row,admission_ns=stamp+admit,
                service_start_ns=stamp+started,terminal_ns=stamp+terminal,outcome='success',
                http_status=200,negotiated_protocol='h2',worker_id=worker_id,worker_processed_ns=stamp+40))
    names=list(job_groups)+list(workers)
    for name in names:
        log=folder/'logs'/name
        ready=dict(clock_domain_id='testclock')
        if name.startswith('robot'):
            ready.update(actual_source_node_count=1,actual_gateway_node_count=1,video_enabled=False)
        write_json(log/'ready.json',ready)
        write_json(log/'final.json',dict(pending=0,ros_missing_ids=[],generated=1,observed=1))
        write_records(log/'connections.jsonl',[dict(event='connection',protocol='h2',alpn='h2',tls='TLSv1.3',congestion='cubic')])
        if name.startswith('worker'):
            write_records(log/'worker.jsonl',workers[name])
            continue
        rows=job_groups[name]
        write_records(log/'jobs.jsonl',rows)
        events=[event('initial',0,None,0,0)]
        for row in rows:
            events.extend([event('admit',row['admission_ns'],row['id'],1,0),
                event('start',row['service_start_ns'],row['id'],0,1),
                event('complete',row['terminal_ns'],row['id'],0,0)])
        write_records(log/'queue.jsonl',sorted(events,key=lambda row:row['time_ns']))
    write_json(folder/'network'/'cleanup.json',dict(success=True))
    write_json(folder/'process_cleanup.json',[dict(exit_code=0,forced_kill=False) for name in names])


class AnalyzeTest(unittest.TestCase):
    def test_fifo_and_exact_carry_in(self):
        events=[event('initial',0,None,0,0),event('admit',5,'a',1,0),
            event('admit',6,'b',2,0),event('start',12,'a',1,1),
            event('complete',15,'a',1,0),event('start',18,'b',0,1),
            event('complete',25,'b',0,0)]
        queue,intervals,dropped=replay_queue(events,10,20,10,1)
        self.assertEqual(queue['max_waiting'],2)
        self.assertEqual(queue['mean_Lq'],1)
        self.assertEqual(queue['mean_L'],1.5)
        self.assertEqual(queue['exact_system_residual_ns'],0)
        self.assertEqual(queue['waiting_at_start'],2)
        self.assertEqual(queue['waiting_at_end'],0)

    def test_fifo_tamper_rejected(self):
        events=[event('initial',0,None,0,0),event('admit',5,'a',1,0),
            event('admit',6,'b',2,0),event('start',12,'b',1,1)]
        with self.assertRaisesRegex(ValueError,'FIFO'):
            replay_queue(events,10,20,10,1)

    def test_two_server_pool_accepts_completion_reordering(self):
        events=[event('initial',0,None,0,0),event('admit',1,'a',1,0),
            event('admit',2,'b',2,0),event('admit',3,'c',3,0),
            event('start',4,'a',2,1),event('start',5,'b',1,2),
            event('complete',7,'b',1,1),event('start',8,'c',0,2),
            event('complete',10,'c',0,1),event('complete',20,'a',0,0)]
        queue,_,_=replay_queue(events,0,25,10,2)
        self.assertEqual(queue['admitted_all'],3)
        self.assertEqual(queue['exact_system_residual_ns'],0)

    def test_completion_window_does_not_count_drain_as_throughput(self):
        source=[dict(id='carry',source_ns=-5,scheduled_ns=-5,payload_bytes=100),
                dict(id='drain',source_ns=5,scheduled_ns=5,payload_bytes=100)]
        terminals={'carry':dict(**source[0],outcome='success',terminal_ns=1),
                   'drain':dict(**source[1],outcome='success',terminal_ns=25)}
        result=cohort_metrics(source,terminals,0,20)
        self.assertEqual(result['generated'],1)
        self.assertEqual(result['successful'],1)
        self.assertEqual(result['completed_rps'],50_000_000)
        self.assertEqual(result['source_to_ack_ms']['mean'],20/1e6)

    def test_drop_and_abort(self):
        events=[event('initial',0,None,0,0),event('admit',5,'a',1,0),
            event('drop',6,'b',1,0),event('abort',15,'a',0,0)]
        queue,_,drops=replay_queue(events,10,20,1,1)
        self.assertEqual(queue['mean_Lq'],.5)
        self.assertEqual(drops,{'b'})

    def test_failed_messages_remain_in_denominator(self):
        source=[dict(id=str(i),source_ns=10,scheduled_ns=10,payload_bytes=100) for i in range(3)]
        terminals={'0':dict(**source[0],outcome='success',terminal_ns=12),
            '1':dict(**source[1],outcome='queue_drop',terminal_ns=11)}
        result=cohort_metrics(source,terminals,0,20)
        self.assertEqual(result['generated'],3)
        self.assertEqual(result['success_fraction'],1/3)
        self.assertEqual(result['outcomes'],dict(success=1,queue_drop=1,ros_missing=1))

    def test_complete_chain_and_persisted_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            minimal_run(folder)
            result=analyze(folder)
            self.assertTrue(result['accounting_passed'])
            self.assertEqual(result['successful'],4)
            self.assertTrue(json.loads((folder/'validation.json').read_text())['passed'])
            self.assertTrue((folder/'per_robot_class.csv').exists())

    def test_forged_ack_is_detected_and_saved(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            minimal_run(folder)
            path=folder/'logs'/'robot1'/'jobs.jsonl'
            row=json.loads(path.read_text())
            row['worker_processed_ns']+=1
            write_records(path,[row])
            with self.assertRaisesRegex(ValueError,'Exact worker ACK'):
                analyze(folder)
            self.assertFalse(json.loads((folder/'validation.json').read_text())['passed'])

    def test_parser_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with self.assertRaises(FileNotFoundError):
                analyze(folder)
            self.assertTrue(json.loads((folder/'validation.json').read_text())['fatal_parser_error'])


if __name__=='__main__':
    unittest.main()
