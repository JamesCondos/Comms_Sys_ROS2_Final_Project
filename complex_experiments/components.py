"""Live ROS publishers, bounded queues and common application logic for H1/H2/H3."""
import argparse
import asyncio
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import sys
import threading
import time

from partner_network.lab.common import JsonlLog, clock_identity, stream_seed, write_json
from .transport import create_client, serve


TELEMETRY_STREAMS = [('command',2.,100),('status',5.,100),('odometry',20.,250),('sensor',5.,20000)]


def traffic_streams(config):
    """Useful sizes and per-robot rates; opt-in mixed sweeps preserve the mix."""
    if config['experiment'] == 'queue':
        return [('queue_probe',config['offered_rps_total']/4,256)]
    streams = list(TELEMETRY_STREAMS)
    if config['video_enabled']:
        streams.append(('video',config['video_fps'],0))
    if config.get('traffic_mode') == 'mixed_load':
        scale = config['traffic_scale']
        streams = [(kind,rate*scale,size) for kind,rate,size in streams]
    return streams


def telemetry_routes(config, robot_name):
    kinds = [kind for kind,_,_ in TELEMETRY_STREAMS]
    if not config.get('separate_telemetry_topics',False):
        kinds.append('queue_probe')
    return {kind:f"/{robot_name}/{kind if config.get('separate_telemetry_topics',False) else 'messages'}"
            for kind in kinds}


def encode(value):
    return json.dumps(value, separators=(',', ':')).encode()


def identity(body):
    value = json.loads(body)
    result = {key: val for key, val in value.items() if key != 'payload_b64'}
    result.update(http_body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
    return result


class WorkQueue:
    def __init__(self, owner, capacity):
        self.owner, self.capacity = owner, capacity
        self.waiting = asyncio.Queue()
        self.active = {}
        self.accepting = True
        self.admissions_closed_ns = None
        self.events = owner.log('queue')
        self.jobs = owner.log('jobs')
        self.event('initial', None)

    def event(self, kind, job, **extra):
        stamp = time.monotonic_ns()
        self.events.write({'event': kind, 'time_ns': stamp,
                           'id': job['meta']['id'] if job else None,
                           'queue_len': self.waiting.qsize(), 'in_service': len(self.active), **extra})
        return stamp

    def close_admissions(self):
        # Called on the same asyncio loop as admit(), without an intervening
        # await: late complete HTTP bodies cannot enter an unserviced queue.
        if not self.accepting:
            return
        self.accepting = False
        self.admissions_closed_ns = time.monotonic_ns()
        write_json(self.owner.output/'admission_gate.json',
                   {'closed_ns':self.admissions_closed_ns, 'reason':'component_shutdown',
                    'late_request_http_status':503})

    def admit(self, body):
        job = {'body': body, 'meta': identity(body), 'future': asyncio.get_running_loop().create_future(),
               'admission_ns': None, 'service_start_ns': None}
        if not self.accepting or self.waiting.qsize() >= self.capacity:
            reason = 'shutdown_rejected' if not self.accepting else 'queue_capacity'
            self.event('drop', job, drop_reason=reason)
            self.finish(job, {'outcome': 'queue_drop', 'drop_reason':reason, 'http_status': 503})
            return job
        self.waiting.put_nowait(job)
        job['admission_ns'] = self.event('admit', job)
        return job

    async def get(self):
        job = await self.waiting.get()
        self.active[job['meta']['id']] = job
        job['service_start_ns'] = self.event('start', job)
        return job

    def finish(self, job, result):
        if job.get('terminal_ns') is not None:
            return
        if job['admission_ns'] is not None:
            self.active.pop(job['meta']['id'], None)
            stamp = self.event('complete' if result['outcome'] != 'drain_timeout' else 'abort', job)
            self.waiting.task_done()
        else:
            stamp = time.monotonic_ns()
        job['terminal_ns'] = stamp
        self.jobs.write({**job['meta'], 'admission_ns': job['admission_ns'],
                         'service_start_ns': job['service_start_ns'], 'terminal_ns': stamp, **result})
        if not job['future'].done():
            job['future'].set_result((result.get('http_status', 503),
                                      job.get('response_body', encode({'error': result['outcome']}))))

    def abort_waiting(self):
        while not self.waiting.empty():
            job = self.waiting.get_nowait()
            # A waiting job leaves without ever occupying a server.
            self.finish(job, {'outcome': 'drain_timeout', 'http_status': 503})


class Component:
    def __init__(self, config, name, output):
        self.config, self.name = config, name
        self.node = config['nodes'][name]
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.logs, self.log_lock = {}, threading.Lock()
        self.clock = clock_identity()
        self.stop = asyncio.Event()
        self.connections = self.log('connections')
        self.clients = []

    def log(self, name):
        with self.log_lock:
            if name not in self.logs:
                self.logs[name] = JsonlLog(self.output / (name + '.jsonl'))
            return self.logs[name]

    def ready(self, **extra):
        write_json(self.output / 'ready.json', {'name': self.name, 'pid': os.getpid(),
                   **self.clock, 'protocol': self.config['protocol'], **extra})

    async def client(self, target, concurrency):
        client = await create_client(self.config['protocol'], self.config['nodes'][target]['ip'],
                  8443, self.config['cafile'], server_name='complex.local',
                  max_concurrency=concurrency, event_callback=self.connections.write)
        self.clients.append(client)
        return client

    async def request(self, client, job):
        try:
            response = await asyncio.wait_for(client.post(job['body'], job['meta']['id']),
                                               self.config['request_timeout_s'])
            result = {'http_status': response.status, 'negotiated_protocol': response.protocol,
                      'connection_id': response.connection_id, 'stream_id': response.stream_id}
            if response.status != 200:
                return dict(result, outcome='http_error')
            ack = json.loads(response.body)
            if ack['id'] != job['meta']['id'] or ack['outcome'] != 'processed':
                raise ValueError('Central ACK identity does not match')
            job['response_body'] = response.body
            return dict(result, outcome='success', worker_id=ack['worker_id'],
                        worker_processed_ns=ack['worker_processed_ns'])
        except asyncio.TimeoutError:
            return {'outcome': 'timeout', 'http_status': 504}
        except Exception as exc:
            return {'outcome': 'request_error', 'http_status': 502, 'error': repr(exc)}

    async def service_loop(self, queue, client):
        while True:
            job = await queue.get()
            try:
                result = await self.request(client, job)
            except asyncio.CancelledError:
                queue.finish(job, {'outcome': 'drain_timeout', 'http_status': 503})
                raise
            queue.finish(job, result)

    async def stop_services(self, queue, tasks, timeout):
        try:
            await asyncio.wait_for(queue.waiting.join(), timeout=max(.01, timeout))
        except asyncio.TimeoutError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        queue.abort_waiting()

    async def close(self, **extra):
        cleanup = await asyncio.gather(*(client.close() for client in self.clients), return_exceptions=True)
        cleanup_errors = [repr(value) for value in cleanup if isinstance(value, BaseException)]
        write_json(self.output / 'client_cleanup.json', {'reports':[value for value in cleanup if not isinstance(value,BaseException)],
                   'errors':cleanup_errors})
        write_json(self.output / 'final.json', {'name': self.name, 'finished_ns': time.monotonic_ns(),
                   'clock_domain_id': self.clock['clock_domain_id'], 'client_cleanup_errors':cleanup_errors, **extra})
        for log in self.logs.values():
            log.close()
        if cleanup_errors:
            raise RuntimeError('Client cleanup failed: '+repr(cleanup_errors))


class Forwarder(Component):
    async def run(self):
        queue = WorkQueue(self, self.config['queue_capacity'])
        targets = ['worker1', 'worker2'] if self.name == 'proxy' else ['proxy']
        tasks = []
        if self.name == 'proxy':
            for target in targets:
                client = await self.client(target, 1)
                tasks.append(asyncio.create_task(self.service_loop(queue, client)))
        else:
            client = await self.client('proxy', self.config['edge_concurrency'])
            tasks = [asyncio.create_task(self.service_loop(queue, client))
                     for _ in range(self.config['edge_concurrency'])]

        async def app(body, headers):
            job = queue.admit(body)
            return await asyncio.shield(job['future'])

        server = await serve(self.config['protocol'], self.node['ip'], 8443,
                   self.config['certfile'], self.config['keyfile'], app,
                   event_callback=self.connections.write)
        self.ready(queue_servers=len(tasks), queue_capacity=self.config['queue_capacity'])
        await self.stop.wait()
        queue.close_admissions()
        await self.stop_services(queue, tasks, 20)
        await server.close()
        await self.close(pending=queue.waiting.qsize()+len(queue.active))


class Worker(Component):
    async def run(self):
        rng = random.Random(stream_seed(self.config['seed'], self.name, 'service'))
        lock = asyncio.Lock()
        self.worker_log = self.log('worker')
        count = 0
        inflight = {}

        async def app(body, headers):
            nonlocal count
            meta, admitted = identity(body), time.monotonic_ns()
            inflight[meta['id']] = meta
            started, hold = None, None
            try:
                async with lock:
                    started = time.monotonic_ns()
                    mean = self.config['mean_service_s']
                    hold = rng.expovariate(1/mean) if self.config['service_mode'] == 'exponential' else mean
                    await asyncio.sleep(hold)
                    processed = time.monotonic_ns()
                    self.worker_log.write({**meta, 'worker_id': self.name, 'admission_ns': admitted,
                         'service_start_ns': started, 'processed_ns': processed, 'requested_service_s': hold,
                         'outcome': 'processed'})
                    count += 1
                    return 200, encode({'id': meta['id'], 'outcome': 'processed', 'worker_id': self.name,
                                       'worker_processed_ns': processed})
            except asyncio.CancelledError:
                self.worker_log.write({**meta,'worker_id':self.name,'admission_ns':admitted,
                    'service_start_ns':started,'requested_service_s':hold,'terminal_ns':time.monotonic_ns(),
                    'outcome':'aborted'})
                raise
            finally:
                inflight.pop(meta['id'], None)

        server = await serve(self.config['protocol'], self.node['ip'], 8443,
                   self.config['certfile'], self.config['keyfile'], app,
                   event_callback=self.connections.write)
        self.ready(actual_service_threads=1)
        await self.stop.wait()
        await server.close()
        await self.close(pending=len(inflight), processed=count)


class Robot(Component):
    async def run(self):
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import CompressedImage
        from std_msgs.msg import String

        rclpy.init()
        source = Node('source', namespace='/' + self.name)
        gateway = Node('gateway', namespace='/' + self.name)
        qos = QoSProfile(depth=10000, reliability=ReliabilityPolicy.RELIABLE)
        routes = telemetry_routes(self.config,self.name)
        if self.config.get('separate_telemetry_topics',False):
            publishers = {kind:source.create_publisher(String,kind,qos) for kind in routes}
        else:
            publisher = source.create_publisher(String,'messages',qos)
            publishers = {kind:publisher for kind in routes}
        video_publisher = source.create_publisher(CompressedImage, 'camera/image/compressed', qos)
        queue = WorkQueue(self, self.config['queue_capacity'])
        source_log, callback_log = self.log('source'), self.log('ros_callbacks')
        generated, observed, video_meta = set(), set(), {}
        loop = asyncio.get_running_loop()
        state_lock = threading.Lock()

        def admit(body, ros_topic=None):
            metadata = identity(body)
            observed.add(metadata['id'])
            callback_log.write({'id': metadata['id'], 'callback_ns': time.monotonic_ns(),
                                'message_type':metadata['message_type'],'ros_topic':ros_topic})
            queue.admit(body)

        def callback(message, topic_name):
            loop.call_soon_threadsafe(admit, message.data.encode(),topic_name)

        def video_callback(message):
            with state_lock:
                metadata = video_meta.pop((message.header.stamp.sec, message.header.stamp.nanosec))
            body = encode({**metadata, 'payload_b64': base64.b64encode(bytes(message.data)).decode()})
            loop.call_soon_threadsafe(admit, body,video_publisher.topic_name)

        subscriptions = []
        if self.config.get('separate_telemetry_topics',False):
            for kind,topic_name in routes.items():
                subscriptions.append(gateway.create_subscription(String,kind,
                    lambda message,topic=topic_name:callback(message,topic),qos))
        else:
            subscriptions.append(gateway.create_subscription(String,'messages',
                lambda message:callback(message,f'/{self.name}/messages'),qos))
        if self.config['video_enabled']:
            subscriptions.append(gateway.create_subscription(CompressedImage, 'camera/image/compressed', video_callback, qos))
            from complex_network.video_frames import generate_jpeg_frames
            frames = generate_jpeg_frames(320, 180, frame_count=20, quality=80)
        else:
            frames = []
        executor = SingleThreadedExecutor()
        executor.add_node(source)
        executor.add_node(gateway)
        ros_thread = threading.Thread(target=executor.spin, daemon=True)
        ros_thread.start()
        target = 'edgeA' if self.node['site'] == 'A' else 'edgeB'
        client = await self.client(target, self.config['gateway_concurrency'])
        tasks = [asyncio.create_task(self.service_loop(queue, client))
                 for _ in range(self.config['gateway_concurrency'])]
        ready_deadline = time.monotonic() + 15
        while any(pub.get_subscription_count()!=1 for pub in publishers.values()) or (frames and video_publisher.get_subscription_count() != 1):
            if time.monotonic() > ready_deadline:
                raise RuntimeError('ROS subscriptions not discovered')
            await asyncio.sleep(.05)
        unique_publishers={pub.topic_name:pub for pub in publishers.values()}
        if frames:
            unique_publishers[video_publisher.topic_name]=video_publisher
        def endpoints(items):
            return [{'node_name':item.node_name,'node_namespace':item.node_namespace,
                     'topic_type':item.topic_type,'qos_depth':item.qos_profile.depth,
                     'qos_reliability':item.qos_profile.reliability.name} for item in items]
        ros_topics=[{'topic_name':topic,'subscription_count':pub.get_subscription_count(),
                     'publisher_count':gateway.count_publishers(topic),
                     'publishers':endpoints(source.get_publishers_info_by_topic(topic)),
                     'subscriptions':endpoints(gateway.get_subscriptions_info_by_topic(topic))}
                    for topic,pub in unique_publishers.items()]
        self.ready(actual_source_node_count=1, actual_gateway_node_count=1,
                   telemetry_subscriptions=min(pub.get_subscription_count() for pub in publishers.values()),
                   video_subscriptions=video_publisher.get_subscription_count(),
                   video_enabled=bool(frames), jpeg_mean_bytes=sum(map(len,frames))/len(frames) if frames else 0,
                   separate_telemetry_topics=self.config.get('separate_telemetry_topics',False),
                   telemetry_routes=routes,telemetry_topics=routes,
                   video_topic=video_publisher.topic_name,ros_topics=ros_topics)
        start_path = Path(self.config['start_file'])
        while not start_path.exists():
            await asyncio.sleep(.05)
        start = json.loads(start_path.read_text())
        duration = self.config['warmup_s'] + self.config['duration_s']
        streams, schedule = traffic_streams(self.config), []
        for kind, rate, size in streams:
            rng = random.Random(stream_seed(self.config['seed'], self.name, kind))
            offset, sequence = 0., 0
            if self.config['experiment'] == 'queue':
                offset = rng.expovariate(rate)
                while offset < duration:
                    schedule.append((round(offset*1e9),kind,sequence,size))
                    offset += rng.expovariate(rate)
                    sequence += 1
            else:
                offset = rng.random()/rate
                while offset + sequence/rate < duration:
                    schedule.append((round((offset+sequence/rate)*1e9),kind,sequence,size))
                    sequence += 1
        schedule.sort()
        write_json(self.output/'arrival_model.json', {'pattern':'Poisson' if self.config['experiment']=='queue' else 'periodic_independent_phase',
                   'streams':streams,'scheduled_count':len(schedule),'seed':self.config['seed'],
                   'traffic_mode':self.config.get('traffic_mode','legacy'),
                   'traffic_scale':self.config.get('traffic_scale',1.),
                   'ros_topic_routes':dict(routes,video=video_publisher.topic_name)})
        for offset, kind, sequence, size in schedule:
            stamp = start['start_ns'] + offset
            await asyncio.sleep(max(0,(stamp-time.monotonic_ns())/1e9))
            payload = frames[sequence % len(frames)] if kind == 'video' else b'x'*size
            metadata = {'id': f"{self.config['run_id']}:{self.name}:{kind}:{sequence}",
                        'robot_id':self.name,'message_type':kind,'seq':sequence,
                        'source_ns':time.monotonic_ns(),'scheduled_ns':stamp,'payload_bytes':len(payload)}
            body = encode({**metadata,'payload_b64':base64.b64encode(payload).decode()})
            source_log.write(identity(body))
            generated.add(metadata['id'])
            if kind == 'video':
                message = CompressedImage()
                message.header.stamp = source.get_clock().now().to_msg()
                message.header.frame_id = self.name + '/camera'
                message.format, message.data = 'jpeg', payload
                with state_lock:
                    video_meta[(message.header.stamp.sec,message.header.stamp.nanosec)] = metadata
                video_publisher.publish(message)
            else:
                message = String()
                message.data = body.decode()
                publishers[kind].publish(message)
        await asyncio.sleep(max(0,(start['measurement_end_ns']-time.monotonic_ns())/1e9))
        # Give DDS callbacks bounded time to deliver the final publications.
        ros_deadline = min(start['drain_deadline_ns'], time.monotonic_ns()+2_000_000_000)
        while generated != observed and time.monotonic_ns() < ros_deadline:
            await asyncio.sleep(.02)
        await self.stop_services(queue, tasks, (start['drain_deadline_ns']-time.monotonic_ns())/1e9)
        executor.shutdown(timeout_sec=3)
        ros_thread.join(timeout=3)
        source.destroy_node()
        gateway.destroy_node()
        rclpy.shutdown()
        await self.close(pending=queue.waiting.qsize()+len(queue.active),generated=len(generated),
                         observed=len(observed),ros_missing_ids=sorted(generated-observed))


async def main_async(args):
    config = json.loads(Path(args.config).read_text())
    role = config['nodes'][args.name]['role']
    cls = Robot if role == 'robot' else Worker if role == 'worker' else Forwarder
    component = cls(config,args.name,args.output)
    for sig in (signal.SIGINT,signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig,component.stop.set)
    try:
        await component.run()
    except BaseException as exc:
        write_json(Path(args.output)/'failure.json',{'error':repr(exc)})
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--output',required=True)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == '__main__':
    main()
