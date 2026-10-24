"""Run isolated complex-network trials, retaining raw evidence and failed attempts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import ssl
import subprocess
import sys
import time
import traceback
import uuid
import zipfile

from partner_network.lab.common import clock_identity, write_json
from partner_network.lab.network import LabNetwork, validate_config as validate_network

ROOT = Path(__file__).resolve().parents[1]
ROBOT_NAMES = ['robot1', 'robot2', 'robot3', 'robot4']
STAGES = [['worker1', 'worker2'], ['proxy'], ['edgeA', 'edgeB'], ROBOT_NAMES]
ACTIVE = [name for stage in STAGES for name in stage]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(config):
    if config.get('protocol') not in ('h1', 'h2', 'h3'):
        raise ValueError('protocol must be h1, h2 or h3')
    if config.get('experiment') not in ('queue', 'protocol'):
        raise ValueError('experiment must be queue or protocol')
    for name in ('warmup_s', 'duration_s', 'drain_s', 'request_timeout_s', 'mean_service_s'):
        if not isinstance(config.get(name), (int, float)) or config[name] <= 0:
            raise ValueError(name + ' must be positive')
    for name in ('queue_capacity', 'gateway_concurrency', 'edge_concurrency'):
        if not isinstance(config.get(name), int) or config[name] < 1:
            raise ValueError(name + ' must be a positive integer')
    if config.get('service_mode') not in ('fixed', 'exponential'):
        raise ValueError('service_mode must be fixed or exponential')
    if config.get('experiment') == 'queue' and config.get('offered_rps_total', 0) <= 0:
        raise ValueError('Queue study requires positive offered_rps_total')
    if config.get('video_enabled') and config.get('video_fps', 0) <= 0:
        raise ValueError('Video enabled requires positive video_fps')
    if config.get('traffic_mode') not in (None,'legacy','mixed_load'):
        raise ValueError('Unknown traffic_mode')
    if config.get('traffic_mode') == 'mixed_load':
        scale=config.get('traffic_scale')
        if isinstance(scale,bool) or not isinstance(scale,(int,float)) or not math.isfinite(scale) or scale<=0:
            raise ValueError('mixed_load requires finite positive traffic_scale')
        if config['experiment']!='protocol' or not config.get('separate_telemetry_topics') or not config.get('video_enabled'):
            raise ValueError('mixed_load requires protocol workload, separate telemetry topics and enabled video')
    result = dict(config)
    result.setdefault('seed', 610610)
    result.setdefault('workers', 2)
    result.setdefault('capture', True)
    result.setdefault('robots', [dict(id=name, site='A' if i < 2 else 'B') for i, name in enumerate(ROBOT_NAMES)])
    if result['workers'] != 2 or [r['id'] for r in result['robots']] != ROBOT_NAMES:
        raise ValueError('This study requires exactly four robot clients and two workers')
    if [r['site'] for r in result['robots']] != ['A', 'A', 'B', 'B']:
        raise ValueError('robot1/2 must be at site A and robot3/4 at site B')
    return validate_network(result)


def doctor():
    commands = {name: shutil.which(name) for name in ('ip', 'tc', 'tcpdump', 'ethtool', 'openssl', 'ping')}
    modules = {name: importlib.util.find_spec(name) is not None for name in ('rclpy', 'std_msgs', 'sensor_msgs', 'h11', 'h2', 'aioquic','PIL','numpy')}
    result = dict(linux=sys.platform.startswith('linux'), root=hasattr(os, 'geteuid') and os.geteuid() == 0,
                  commands=commands, modules=modules, platform=platform.platform(), python=sys.version,
                  executable=sys.executable, cpu_count=os.cpu_count(), shared_host=True,
                  imposed_cpu_quotas=False, clock=clock_identity())
    result['library_versions'] = {}
    for name in ('aioquic','h2','h11','numpy','Pillow'):
        try:
            result['library_versions'][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result['library_versions'][name] = None
    result['openssl_version'] = ssl.OPENSSL_VERSION
    result['ros_distribution'] = os.environ.get('ROS_DISTRO')
    if hasattr(os, 'sched_getaffinity'):
        result['cpu_affinity'] = sorted(os.sched_getaffinity(0))
    result['passed'] = result['linux'] and result['root'] and all(commands.values()) and all(modules.values())
    return result


def ensure_certificates(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory/'certificate.pem', directory/'private_key.pem'
    if cert.exists() != key.exists():
        raise RuntimeError('Incomplete existing study certificate pair: ' + str(directory))
    if not cert.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(cert), '-days', '365', '-subj', '/CN=complex.local',
            '-addext', 'subjectAltName=DNS:complex.local', '-addext', 'basicConstraints=critical,CA:TRUE'],
            check=True, capture_output=True)
        key.chmod(0o600)
    subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '300'],
                   check=True, capture_output=True)
    return dict(certfile=str(cert), keyfile=str(key), cafile=str(cert))


def proc_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


@contextmanager
def measurement_lock():
    """One scoped study driver at a time; never signal another owner."""
    path = ROOT/'.complex_measurement.lock'
    identity = dict(pid=os.getpid(), start_ticks=proc_start(os.getpid()),
                    boot_id=clock_identity()['boot_id'])
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        previous = json.loads(path.read_text())
        same_boot = previous.get('boot_id') == identity['boot_id']
        if same_boot and proc_start(previous['pid']) == previous.get('start_ticks'):
            raise RuntimeError('Another study driver holds the measurement lock: '+str(previous))
        # The recorded owner no longer exists. Remove only this stale lock.
        path.unlink()
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(identity, stream)
    try:
        yield
    finally:
        if path.exists() and json.loads(path.read_text()) == identity:
            path.unlink()


def alive(record, process=None):
    if process is not None and process.poll() is not None:
        return False
    try:
        if record['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
            return False
        if not record['start_ticks'] or proc_start(record['pid']) != record['start_ticks']:
            return False
        args = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b'\0')
        return b'complex_experiments.components' in args and record['config'].encode() in args
    except (FileNotFoundError, ProcessLookupError):
        return False


def stop_processes(records, processes):
    for record in reversed(records):
        if alive(record, processes.get(record['name'])):
            try:
                os.kill(record['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and any(alive(r, processes.get(r['name'])) for r in records):
        time.sleep(.05)
    results = []
    for record in records:
        process = processes.get(record['name'])
        forced = alive(record, process)
        if forced:
            try:
                os.kill(record['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        code = process.wait(timeout=5) if process is not None else None
        results.append(dict(name=record['name'], pid=record['pid'], forced_kill=forced, exit_code=code))
    return results


def cpu_sample(records):
    sample = dict(time_ns=time.monotonic_ns(), components={})
    for record in records:
        try:
            fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(')', 1)[1].split()
            sample['components'][record['name']] = dict(
                cpu_s=(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK'),
                rss_bytes=int(fields[21])*os.sysconf('SC_PAGE_SIZE'))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return sample


def snapshot_code(output):
    hashes = {}
    patterns = ['complex_experiments/*.py', 'complex_experiments/tests/*.py',
                'partner_network/lab/*.py', 'complex_network_ws/src/complex_network/complex_network/*.py']
    with zipfile.ZipFile(output/'code_snapshot.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for pattern in patterns:
            for path in sorted(ROOT.glob(pattern)):
                relative, data = path.relative_to(ROOT).as_posix(), path.read_bytes()
                archive.writestr(relative, data)
                hashes[relative] = hashlib.sha256(data).hexdigest()
    write_json(output/'code_sha256.json', hashes)


def manifest(output):
    values = {}
    for path in sorted(output.rglob('*')):
        if path.is_file() and path.name not in ('MANIFEST.sha256.json', 'COMPLETE.json'):
            sha = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    sha.update(chunk)
            values[path.relative_to(output).as_posix()] = sha.hexdigest()
    write_json(output/'MANIFEST.sha256.json', values)


def run_experiment(requested, output):
    config = validate(requested)
    environment = doctor()
    if not environment['passed']:
        raise RuntimeError('Study prerequisites unavailable: ' + json.dumps(environment))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'requested_config.json', requested)
    write_json(output/'environment.json', environment)
    snapshot_code(output)
    config = dict(config, run_id='complex_'+uuid.uuid4().hex[:12],
                  requested_config_sha256=digest(requested), start_file=str(output/'start.json'),
                  active_components=ACTIVE, launched_utc=datetime.now(timezone.utc).isoformat())
    if not all(config.get(key) for key in ('certfile', 'keyfile', 'cafile')):
        config.update(ensure_certificates(ROOT/'study_certificates'))
    for key in ('certfile', 'keyfile', 'cafile'):
        config[key] = str(Path(config[key]).resolve())
    network = LabNetwork(output, config)
    records, processes, streams, ready, samples = [], {}, [], {}, []
    errors = []
    try:
        network.setup()
        config['nodes'] = network.nodes
        config_path = output/'resolved.json'
        write_json(config_path, config)
        # Capture includes every TCP TLS and QUIC handshake, warmup and drain.
        network.snapshot('before')
        network.start_capture()
        for stage in STAGES:
            for name in stage:
                logdir = output/'logs'/name
                console = output/'console'/(name+'.log')
                console.parent.mkdir(exist_ok=True)
                stream = console.open('x')
                streams.append(stream)
                args = [sys.executable, '-B', '-m', 'complex_experiments.components',
                        '--config', str(config_path), '--name', name, '--output', str(logdir)]
                video_path = ROOT/'complex_network_ws/src/complex_network'
                env = dict(os.environ, ROS_DOMAIN_ID='176', ROS_LOCALHOST_ONLY='1',
                           PYTHONDONTWRITEBYTECODE='1', ROS_LOG_DIR=str(output/'ros_logs'/name),
                           PYTHONPATH=str(video_path)+os.pathsep+os.environ.get('PYTHONPATH', ''))
                process = subprocess.Popen(network.command(name, args), cwd=ROOT, env=env,
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                processes[name] = process
                records.append(dict(name=name, pid=process.pid, start_ticks=proc_start(process.pid),
                    boot_id=environment['clock']['boot_id'], config=str(config_path),
                    namespace=network.nodes[name]['namespace'], command=args))
                write_json(output/'processes.json', records)
            deadline = time.monotonic() + 90
            while True:
                for name in processes:
                    if processes[name].poll() is not None:
                        raise RuntimeError(name+' exited during startup; inspect console/'+name+'.log')
                if all((output/'logs'/name/'ready.json').exists() for name in stage):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Component readiness timed out: '+str(stage))
                time.sleep(.05)
            for name in stage:
                ready[name] = json.loads((output/'logs'/name/'ready.json').read_text())
                print('READY', name, config['protocol'], network.nodes[name]['ip'], flush=True)
        clocks = {record['clock_domain_id'] for record in ready.values()}
        if clocks != {environment['clock']['clock_domain_id']}:
            raise RuntimeError('A component uses a different monotonic clock domain')
        write_json(output/'readiness.json', ready)
        start = time.monotonic_ns()+1_000_000_000
        begin = start+round(config['warmup_s']*1e9)
        end = begin+round(config['duration_s']*1e9)
        drain = end+round(config['drain_s']*1e9)
        samples.append(cpu_sample(records))
        write_json(output/'start.json', dict(start_ns=start, measurement_start_ns=begin,
            measurement_end_ns=end, drain_deadline_ns=drain, clock_domain_id=next(iter(clocks))))
        print('MEASURE', config['experiment'], config['protocol'], config.get('case', ''),
              config['duration_s'], 'seconds', flush=True)
        while any(processes[name].poll() is None for name in ROBOT_NAMES):
            for name, process in processes.items():
                code = process.poll()
                if code is not None and (name not in ROBOT_NAMES or code != 0):
                    raise RuntimeError(f'{name} exited unexpectedly: {code}')
            samples.append(cpu_sample(records))
            if time.monotonic_ns() > drain+20_000_000_000:
                raise RuntimeError('Robot drain exceeded its bounded deadline')
            time.sleep(.2)
        if time.monotonic_ns() < end:
            raise RuntimeError('Sources finished before measurement window ended')
        if any(processes[name].returncode != 0 for name in ROBOT_NAMES):
            raise RuntimeError('A robot process failed')
        samples.append(cpu_sample(records))
        write_json(output/'run_status.json', dict(application_run_finished=True, run_id=config['run_id'],
            actual_robot_processes=4, actual_worker_processes=2, active_components=ACTIVE))
    except BaseException as exc:
        errors.append(dict(phase='application', error=repr(exc), traceback=traceback.format_exc()))
    finally:
        write_json(output/'cpu.json', samples)
        try:
            cleanup = stop_processes(records, processes)
            write_json(output/'process_cleanup.json', cleanup)
            if any(row['forced_kill'] or row['exit_code'] != 0 for row in cleanup):
                errors.append(dict(phase='process_cleanup', error='A component failed or required forced termination'))
        except BaseException as exc:
            errors.append(dict(phase='process_cleanup', error=repr(exc)))
        for stream in streams:
            stream.close()
        try:
            network.stop_capture()
        except BaseException as exc:
            errors.append(dict(phase='capture_cleanup', error=repr(exc)))
        try:
            if network.nodes:
                network.snapshot('after')
        except BaseException as exc:
            errors.append(dict(phase='network_snapshot', error=repr(exc)))
        try:
            network.cleanup()
        except BaseException as exc:
            errors.append(dict(phase='network_cleanup', error=repr(exc)))
    if errors:
        write_json(output/'invalid.json', dict(errors=errors))
        write_json(output/'validation.json', dict(passed=False, errors=errors))
        raise RuntimeError('Retained invalid run: '+str(output)+'; '+str(errors[0]))
    from .analyze import analyze
    try:
        summary = analyze(output)
        if config.get('traffic_mode') == 'mixed_load':
            from .topic_analysis import analyze_topics
            write_json(output/'topic_analysis.json',analyze_topics(output))
    except BaseException as exc:
        write_json(output/'invalid.json', dict(errors=[dict(phase='independent_validation', error=repr(exc))]))
        raise
    manifest(output)
    write_json(output/'COMPLETE.json', dict(passed=True, requested_config_sha256=digest(requested),
        finished_utc=datetime.now(timezone.utc).isoformat(), run_id=config['run_id']))
    print('VALID', output, 'generated', summary['generated'], 'success', summary['successful'], flush=True)
    return summary


def matrix_runs(matrix):
    base, runs = matrix.get('base', {}), matrix['runs']
    names = set()
    result = []
    for row in runs:
        row = dict(row)
        name = row.pop('name')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name) or name in names:
            raise ValueError('Each matrix run requires a unique safe name')
        names.add(name)
        config = dict(base, **row)
        validate(config)
        result.append((name, config))
    return result


def suite(matrix, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    definition = output/'matrix.json'
    if definition.exists():
        if digest(json.loads(definition.read_text())) != digest(matrix):
            raise ValueError('Refusing to change an existing suite matrix')
    else:
        write_json(definition, matrix)
    results = []
    for name, config in matrix_runs(matrix):
        candidates = sorted(output.glob(name+'__attempt*'))
        exact = output/name
        if exact.exists():
            candidates.insert(0, exact)
        finished = None
        for candidate in candidates:
            if (candidate/'COMPLETE.json').exists() and (candidate/'validation.json').exists():
                complete = json.loads((candidate/'COMPLETE.json').read_text())
                validation = json.loads((candidate/'validation.json').read_text())
                if complete.get('passed') and validation.get('passed') and complete.get('requested_config_sha256') == digest(config):
                    # Re-run the independent reconciliation before accepting a resume.
                    from .analyze import analyze
                    analyze(candidate)
                    finished = candidate
                    break
        if finished:
            print('RESUME_VALID', name, finished.name, flush=True)
            results.append(dict(name=name, path=str(finished), resumed=True))
            continue
        target = exact
        attempt = 2
        while target.exists():
            target = output/f'{name}__attempt{attempt:02d}'
            attempt += 1
        print('SUITE_RUN', len(results)+1, '/', len(matrix['runs']), target.name, flush=True)
        run_experiment(config, target)
        results.append(dict(name=name, path=str(target), resumed=False))
        write_json(output/'suite_progress.json', results)
    write_json(output/'SUITE_COMPLETE.json', dict(passed=True, completed_runs=len(results), runs=results))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    run = commands.add_parser('run')
    run.add_argument('--config', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    batch = commands.add_parser('suite')
    batch.add_argument('--matrix', type=Path, required=True)
    batch.add_argument('--output', type=Path, required=True)
    commands.add_parser('analyze').add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'doctor':
        print(json.dumps(doctor(), indent=2))
        return
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('Termination requested')))
    if args.command == 'analyze':
        from .analyze import analyze
        print(json.dumps(analyze(args.directory), indent=2))
    else:
        with measurement_lock():
            if args.command == 'run':
                run_experiment(json.loads(args.config.read_text()), args.output)
            else:
                suite(json.loads(args.matrix.read_text()), args.output)


if __name__ == '__main__':
    main()
