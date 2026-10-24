"""Independent source/ACK/FIFO reconciliation for the complex live network.

Only raw logs and resolved configuration are read. Failed runs retain an explicit
validation record; no measured samples are replaced with theoretical values.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import csv
import json
from pathlib import Path
import struct

from .theory import audit_little, coefficient_of_variation, mmc, rate_and_cv_ns


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def records(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stats(values):
    values = sorted(values)
    if not values:
        return dict(n=0, mean=None, p50=None, p95=None, p99=None, max=None)
    def percentile(p):
        index = (len(values)-1)*p
        lo = int(index)
        return values[lo] + (values[min(lo+1, len(values)-1)]-values[lo])*(index-lo)
    return dict(n=len(values), mean=sum(values)/len(values), p50=percentile(.5),
                p95=percentile(.95), p99=percentile(.99), max=values[-1])


def unique(rows, label):
    result = {}
    for row in rows:
        if row['id'] in result:
            raise ValueError('Duplicate ' + label + ' identity: ' + row['id'])
        result[row['id']] = row
    return result


def pcap_record_count(path):
    """Check full PCAP framing, not decrypted HTTP semantics."""
    with Path(path).open('rb') as stream:
        header = stream.read(24)
        endians = {b'\xd4\xc3\xb2\xa1':'<', b'\x4d\x3c\xb2\xa1':'<',
                   b'\xa1\xb2\xc3\xd4':'>', b'\xa1\xb2\x3c\x4d':'>'}
        if len(header) != 24 or header[:4] not in endians:
            raise ValueError('Invalid PCAP header')
        endian = endians[header[:4]]
        snaplen = struct.unpack(endian+'I',header[16:20])[0]
        count = 0
        while True:
            record = stream.read(16)
            if not record:
                return count
            if len(record) != 16:
                raise ValueError('Truncated PCAP record header')
            _, _, included, original = struct.unpack(endian+'IIII',record)
            if included > snaplen or included > original or len(stream.read(included)) != included:
                raise ValueError('Truncated or invalid PCAP packet')
            count += 1


def replay_queue(events, begin, end, capacity, concurrency):
    """Rebuild the FIFO, then independently integrate item and event histories."""
    waiting, active = deque(), set()
    intervals, dropped = {}, set()
    maximum = 0
    previous = None
    if not events or events[0]['event'] != 'initial':
        raise ValueError('Missing initial queue state')
    for event in events:
        stamp, action, identity = event['time_ns'], event['event'], event.get('id')
        if previous is not None and stamp < previous:
            raise ValueError('Queue events are not chronological')
        previous = stamp
        if action == 'initial':
            if intervals or active or waiting or event is not events[0]:
                raise ValueError('Queue initial state repeated')
        elif action == 'admit':
            if identity in intervals or identity in dropped or len(waiting) >= capacity:
                raise ValueError('Duplicate admission or capacity violation')
            intervals[identity] = dict(admission_ns=stamp)
            waiting.append(identity)
        elif action == 'start':
            if not waiting or waiting[0] != identity or len(active) >= concurrency:
                raise ValueError('FIFO order or concurrency violation')
            waiting.popleft()
            active.add(identity)
            intervals[identity]['service_start_ns'] = stamp
        elif action == 'complete':
            if identity not in active:
                raise ValueError('Completion without active job')
            active.remove(identity)
            intervals[identity]['terminal_ns'] = stamp
        elif action == 'abort':
            if identity in active:
                active.remove(identity)
            elif identity in waiting:
                waiting.remove(identity)
            else:
                raise ValueError('Abort without admitted job')
            intervals[identity]['terminal_ns'] = stamp
        elif action == 'drop':
            if identity in intervals or identity in dropped:
                raise ValueError('Repeated or admitted dropped identity')
            dropped.add(identity)
        else:
            raise ValueError('Unknown queue action: ' + action)
        if event['queue_len'] != len(waiting) or event['in_service'] != len(active):
            raise ValueError('Logged queue population differs from replay')
        if begin <= stamp < end:
            maximum = max(maximum, len(waiting))
    if waiting or active:
        raise ValueError('Unresolved jobs at finalization')
    queued = [(row['admission_ns'], row.get('service_start_ns') if row.get('service_start_ns') is not None else row['terminal_ns'])
              for row in intervals.values()]
    system = [(row['admission_ns'],row['terminal_ns']) for row in intervals.values()]
    total_events = [dict(time_ns=e['time_ns'],system=e['queue_len']+e['in_service']) for e in events]
    queue_audit = audit_little(queued,events,begin,end)
    system_audit = audit_little(system,total_events,begin,end,value_key='system')
    if not queue_audit['passed'] or not system_audit['passed']:
        raise ValueError('Clipped item residence differs from occupancy integral')
    # Include carry-in occupancy, even if the first in-window event removes it.
    carry_in = [e for e in events if e['time_ns'] <= begin]
    maximum = max(maximum, carry_in[-1]['queue_len'] if carry_in else 0)
    at_end = [e for e in events if e['time_ns'] < end]
    q_begin = carry_in[-1]['queue_len'] if carry_in else 0
    q_end = at_end[-1]['queue_len'] if at_end else 0
    return dict(mean_Lq=queue_audit['mean_occupancy'],mean_L=system_audit['mean_occupancy'],
                mean_in_service=system_audit['mean_occupancy']-queue_audit['mean_occupancy'],
                max_waiting=maximum,exact_queue_residual_ns=queue_audit['residual_ns'],
                exact_system_residual_ns=system_audit['residual_ns'],
                admitted_all=len(intervals),dropped_all=len(dropped),
                waiting_at_start=q_begin,waiting_at_end=q_end,
                queue_growth_per_s=(q_end-q_begin)*1e9/(end-begin)), intervals, dropped


def cohort_metrics(source, terminals, begin, end):
    cohort = [row for row in source if begin <= row['source_ns'] < end]
    successful = [terminals[row['id']] for row in cohort
                  if row['id'] in terminals and terminals[row['id']]['outcome'] == 'success']
    outcomes = Counter(terminals[row['id']]['outcome'] if row['id'] in terminals
                       else 'ros_missing' for row in cohort)
    source_ids = {row['id'] for row in source}
    completions = [row for identity,row in terminals.items() if identity in source_ids and
                   row['outcome']=='success' and begin <= row['terminal_ns'] < end]
    duration = (end-begin)/1e9
    return dict(generated=len(cohort),successful=len(successful),outcomes=dict(outcomes),
                success_fraction=len(successful)/len(cohort) if cohort else None,
                source_to_ack_ms=stats([(row['terminal_ns']-row['source_ns'])/1e6 for row in successful]),
                scheduling_lag_ms=stats([(row['source_ns']-row['scheduled_ns'])/1e6 for row in cohort]),
                offered_rps=len(cohort)*1e9/(end-begin),
                cohort_success_payload_bytes=sum(row['payload_bytes'] for row in successful),
                completed_rps=len(completions)/duration,
                payload_goodput_mbit_s=sum(row['payload_bytes']*8 for row in completions)/duration/1e6,
                offered_payload_mbit_s=sum(row['payload_bytes']*8 for row in cohort)/duration/1e6)


IDENTITY_FIELDS = ('robot_id','message_type','source_ns','scheduled_ns','payload_bytes','http_body_bytes')


def _analyze(folder):
    config, start = read_json(folder/'resolved.json'),read_json(folder/'start.json')
    begin, end = start['measurement_start_ns'],start['measurement_end_ns']
    duration = (end-begin)/1e9
    checks, errors = [], []
    def check(ok, message):
        (checks if ok else errors).append(message)
    robots = [row['id'] for row in config['robots']]
    names = robots + ['edgeA','edgeB','proxy'] + [f'worker{i+1}' for i in range(config['workers'])]
    sources, terminals, jobs, queues, intervals = {},{}, {},{},{}
    all_sources = []
    for robot in robots:
        rows = records(folder/'logs'/robot/'source.jsonl')
        sources[robot] = unique(rows,robot+' source')
        all_sources.extend(rows)
        check(bool(rows),robot+' published actual source messages')
        check(all(row['source_ns'] >= row['scheduled_ns'] for row in rows),robot+' scheduling chronology')
    source_map = unique(all_sources,'global source')
    worker_rows = []
    for name in names:
        log = folder/'logs'/name
        check(not (log/'failure.json').exists(),name+' has no component failure record')
        check((log/'ready.json').exists(),name+' readiness exists')
        check((log/'final.json').exists(),name+' normal finalization exists')
        if (log/'ready.json').exists():
            ready = read_json(log/'ready.json')
            check(ready.get('clock_domain_id') == start['clock_domain_id'],name+' common clock')
            if name in robots:
                check(ready.get('actual_source_node_count')==1 and ready.get('actual_gateway_node_count')==1,name+' owns real source and gateway ROS nodes')
                check(ready.get('video_enabled')==config.get('video_enabled',False),name+' configured video state')
        if (log/'final.json').exists():
            final = read_json(log/'final.json')
            check(final.get('pending',0) == 0,name+' no pending jobs')
            if name in robots:
                check(final.get('ros_missing_ids')==[],name+' no missing ROS publications')
                check(final.get('generated')==len(sources[name]) and final.get('observed')==len(sources[name]),name+' generated/observed counts reconcile')
        connections = records(log/'connections.jsonl')
        connection_rows = [row for row in connections if row.get('event')=='connection']
        check(bool(connection_rows),name+' actual negotiated transport connections exist')
        expected_alpn = dict(h1='http/1.1',h2='h2',h3='h3')[config['protocol']]
        check(all(row.get('protocol')==config['protocol'] and row.get('alpn')==expected_alpn for row in connection_rows),name+' protocol and ALPN match without fallback')
        check(all(row.get('tls')=='TLSv1.3' for row in connection_rows),name+' encrypted TLS 1.3 transport')
        check(all(row.get('congestion')=='cubic' for row in connection_rows),name+' controlled Cubic congestion algorithm')
        if config['protocol']=='h3':
            check(all(row.get('quic_version')==1 for row in connection_rows),name+' actual QUIC v1 transport')
        if name.startswith('worker'):
            worker_rows.extend(records(log/'worker.jsonl'))
            continue
        job_rows = records(log/'jobs.jsonl')
        jobs[name] = unique(job_rows,name+' terminal')
        check(bool(job_rows),name+' handled requests')
        concurrency = 2 if name == 'proxy' else config['edge_concurrency'] if name.startswith('edge') else config['gateway_concurrency']
        capacity = config.get(name+'_queue_capacity',config.get('proxy_queue_capacity' if name=='proxy' else 'edge_queue_capacity' if name.startswith('edge') else 'gateway_queue_capacity',config.get('queue_capacity',10000)))
        try:
            queues[name],intervals[name],dropped = replay_queue(records(log/'queue.jsonl'),begin,end,capacity,concurrency)
            admitted = {identity for identity,row in jobs[name].items() if row.get('admission_ns') is not None}
            check(admitted == set(intervals[name]),name+' all admitted terminal identities match event log')
            check(dropped == {identity for identity,row in jobs[name].items() if row['outcome']=='queue_drop'},name+' all queue-drop identities match event log')
            for identity,row in intervals[name].items():
                terminal = jobs[name][identity]
                check(all(terminal.get(key)==stamp for key,stamp in row.items()),name+' exact event/job boundaries '+identity)
        except (ValueError,KeyError) as exc:
            errors.append(name+' queue validation: '+str(exc))
        for row in job_rows:
            original = source_map.get(row['id'])
            check(original is not None,name+' known source identity '+row['id'])
            if original is not None:
                check(all(row.get(key)==original.get(key) for key in IDENTITY_FIELDS),name+' source fields preserved '+row['id'])
                if 'body_sha256' in original:
                    check(row.get('body_sha256')==original['body_sha256'],name+' exact body SHA256 '+row['id'])
                check(row['terminal_ns'] >= original['source_ns'],name+' terminal chronology '+row['id'])
            if row['outcome']=='success':
                check(row.get('http_status')==200,name+' successful HTTP status '+row['id'])
                check(row.get('negotiated_protocol')==expected_alpn,name+' actual successful response protocol '+row['id'])
            if row['outcome']=='queue_drop':
                check(row.get('admission_ns') is None and row.get('service_start_ns') is None,name+' drop not admitted '+row['id'])
    check(all(row.get('outcome','processed') in ('processed','aborted') for row in worker_rows),'Known worker terminal outcomes')
    processed_rows = [row for row in worker_rows if row.get('outcome','processed')=='processed']
    processed = unique(processed_rows,'central processing')
    check(len({row['id'] for row in worker_rows})==len(worker_rows),'No duplicate worker effects including aborts')
    for identity,row in processed.items():
        original = source_map.get(identity)
        check(original is not None,'Worker known source identity '+identity)
        if original is not None:
            check(all(row.get(key)==original.get(key) for key in IDENTITY_FIELDS),'Worker identity preserved '+identity)
            if 'body_sha256' in original:
                check(row.get('body_sha256')==original['body_sha256'],'Worker exact body SHA256 '+identity)
            check(original['source_ns'] <= row['admission_ns'] <= row['service_start_ns'] <= row['processed_ns'],'Worker chronology '+identity)
    all_terminals = []
    per_robot, class_rows = {},[]
    for robot in config['robots']:
        name,site = robot['id'],robot['site']
        robot_source = list(sources[name].values())
        terminal = jobs.get(name,{})
        terminals[name] = terminal
        check(set(terminal)==set(sources[name]),name+' all ROS publications reached gateway and resolved')
        for identity,row in terminal.items():
            if row['outcome']!='success':
                continue
            worker = processed.get(identity)
            check(worker is not None,'Successful ACK has unique central processing '+identity)
            if worker is not None:
                check(row.get('worker_id')==worker['worker_id'] and row.get('worker_processed_ns')==worker['processed_ns'],'Exact worker ACK '+identity)
                check(worker['processed_ns'] <= row['terminal_ns'],'Processing precedes source ACK '+identity)
            for hop in ('edge'+site,'proxy'):
                upstream = jobs.get(hop,{}).get(identity)
                check(upstream is not None and upstream['outcome']=='success','Successful full processing chain '+hop+' '+identity)
            edge = jobs.get('edge'+site,{}).get(identity)
            proxy = jobs.get('proxy',{}).get(identity)
            if worker is not None and edge is not None and proxy is not None:
                chronology = [row['source_ns'],row['admission_ns'],row['service_start_ns'],
                    edge['admission_ns'],edge['service_start_ns'],proxy['admission_ns'],
                    proxy['service_start_ns'],worker['admission_ns'],worker['service_start_ns'],
                    worker['processed_ns'],proxy['terminal_ns'],edge['terminal_ns'],row['terminal_ns']]
                check(chronology==sorted(chronology),'Whole multihop successful chronology '+identity)
        per_robot[name] = cohort_metrics(robot_source,terminal,begin,end)
        per_robot[name]['site'] = site
        per_robot[name]['queue'] = queues.get(name)
        for kind in sorted({row['message_type'] for row in robot_source}):
            class_rows.append(dict(robot=name,site=site,message_type=kind,**cohort_metrics(
                [row for row in robot_source if row['message_type']==kind],terminal,begin,end)))
        all_terminals.extend(terminal.values())
    global_terminals = unique(all_terminals,'global terminal')
    summary = cohort_metrics(all_sources,global_terminals,begin,end)
    site_by_robot = {row['id']:row['site'] for row in config['robots']}
    summary['per_site'] = {site:cohort_metrics([row for row in all_sources if site_by_robot[row['robot_id']]==site],global_terminals,begin,end) for site in sorted(set(site_by_robot.values()))}
    summary['per_class'] = {kind:cohort_metrics([row for row in all_sources if row['message_type']==kind],global_terminals,begin,end) for kind in sorted({row['message_type'] for row in all_sources})}
    summary['per_site_class'] = {site:{kind:cohort_metrics([row for row in all_sources if site_by_robot[row['robot_id']]==site and row['message_type']==kind],global_terminals,begin,end) for kind in summary['per_class']} for site in summary['per_site']}
    completed = [row for row in all_terminals if row['outcome']=='success' and begin <= row['terminal_ns'] < end]
    summary.update(schema_version=1,experiment=config['experiment'],protocol=config['protocol'],
        offered_rps_total=config.get('offered_rps_total'),duration_s=duration,
        actual_robot_clients=len(robots),actual_worker_processes=config['workers'],
        completed_rps=len(completed)/duration,
        payload_goodput_mbit_s=sum(row['payload_bytes']*8 for row in completed)/duration/1e6,
        per_robot=per_robot,queues=queues,
        central_processed_all=len(processed),
        central_processed_unacknowledged_all=sum(identity not in global_terminals or global_terminals[identity]['outcome']!='success' for identity in processed),
        duplicate_central_effects=0)
    central = list(jobs.get('proxy',{}).values())
    central_cohort = [row for row in central if row.get('admission_ns') is not None and begin <= row['admission_ns'] < end]
    served = [row for row in central_cohort if row.get('service_start_ns') is not None]
    service_s = [(row['terminal_ns']-row['service_start_ns'])/1e9 for row in served]
    queue_s = [(row['service_start_ns']-row['admission_ns'])/1e9 for row in served]
    central_lambda = len(central_cohort)/duration
    calibrated_mu = 1/(sum(service_s)/len(service_s)) if service_s and sum(service_s)>0 else None
    central_stats = dict(admitted=len(central_cohort),admission_rate_per_s=central_lambda,
        queue_wait_ms=stats([value*1000 for value in queue_s]),
        service_ms=stats([value*1000 for value in service_s]),
        residence_ms=stats([(row['terminal_ns']-row['admission_ns'])/1e6 for row in central_cohort]),
        service_cv=coefficient_of_variation(service_s),
        arrival_diagnostics=rate_and_cv_ns(sorted(row['admission_ns'] for row in central_cohort)),
        calibrated_mmc=mmc(central_lambda,calibrated_mu,2) if calibrated_mu and config['experiment']=='queue' else None,
        nominal_mmc=mmc(config.get('offered_rps_total',central_lambda),1/config.get('mean_service_s',config.get('q_mean_service_s',.02)),2) if config['experiment']=='queue' else None,
        pilot_mmc=mmc(central_lambda,config['calibrated_capacity_rps']/2,2) if config['experiment']=='queue' and config.get('calibrated_capacity_rps') else None,
        pilot_offered_mmc=mmc(config.get('offered_rps_total',central_lambda),config['calibrated_capacity_rps']/2,2) if config['experiment']=='queue' and config.get('calibrated_capacity_rps') else None,
        measurement_note='Admission-time cohort; dispatch-to-terminal service includes worker HTTP exchange and software overhead. M/M/2 assumptions are approximate.')
    summary['central'] = central_stats
    summary['worker_service_ms'] = stats([(row['processed_ns']-row['service_start_ns'])/1e6 for row in processed_rows if begin <= row['service_start_ns'] < end])
    # Each fixed proxy service loop targets one worker: overlapping worker service
    # intervals would invalidate the intended two-server boundary.
    for worker in [f'worker{i+1}' for i in range(config['workers'])]:
        ordered = sorted([row for row in processed_rows if row['worker_id']==worker],key=lambda row:row['service_start_ns'])
        check(all(a['processed_ns']<=b['service_start_ns'] for a,b in zip(ordered,ordered[1:])),worker+' serial service')
    cleanup_path = folder/'network'/'cleanup.json'
    check(cleanup_path.exists(),'Network cleanup record exists')
    if cleanup_path.exists():
        check(read_json(cleanup_path).get('success') is True,'Owned network cleanup succeeded')
    if config.get('sites'):
        from partner_network.lab.network import verify_installed_profile
        for tag in ('before','after'):
            snapshot_path = folder/'network'/('snapshot_'+tag+'.json')
            check(snapshot_path.exists(),'Network '+tag+' snapshot exists')
            if not snapshot_path.exists():
                continue
            snapshot = read_json(snapshot_path)
            check(set(snapshot.get('nodes',{}))==set(names+['routerA','routerB']),'All eleven isolated network nodes in '+tag+' snapshot')
            for site in ('A','B'):
                qdiscs = snapshot.get('nodes',{}).get('router'+site,{}).get('qdisc',[])
                for direction,device in [('forward','wan0'),('reverse','lan0')]:
                    try:
                        observed = [row for row in qdiscs if row.get('dev')==device]
                        verify_installed_profile(config['sites'][site][direction],observed)
                        check(True,tag+' site '+site+' '+direction+' requested netem settings match kernel')
                    except (ValueError,RuntimeError,KeyError) as exc:
                        errors.append(tag+' site '+site+' '+direction+' netem verification: '+str(exc))
    process_path = folder/'process_cleanup.json'
    check(process_path.exists(),'Process cleanup record exists')
    if process_path.exists():
        entries = read_json(process_path)
        check(all(not row.get('forced_kill') and row.get('exit_code')==0 for row in entries),'All components exited normally')
    if config.get('capture',True):
        metadata_path = folder/'network'/'capture_metadata.json'
        check(metadata_path.exists(),'Capture metadata exists')
        if metadata_path.exists():
            metadata = read_json(metadata_path)
            check(set(metadata.get('sites',{}))=={'A','B'} and not metadata.get('errors'),'Both site capture records exist without lifecycle errors')
            for site,record in metadata.get('sites',{}).items():
                check(record.get('statistics_complete') and record.get('capture_complete') and record.get('packets_dropped_by_kernel')==0,'Site '+site+' capture finalized without kernel drops')
                try:
                    count = pcap_record_count(folder/'network'/('site'+site+'_wan.pcap'))
                    check(count>0 and count==record.get('packets_captured'),'Site '+site+' PCAP framing/count match')
                except (OSError,ValueError) as exc:
                    errors.append('Site '+site+' PCAP: '+str(exc))
    summary.update(accounting_passed=not errors,validation_errors=errors,successful_checks=len(checks),
        measurement_note='Source-time cohort; successful latency includes drain. Completion/goodput rates use ACK timestamps inside measurement. Queue means are exact clipped occupancy integrals. Failed source messages remain in the denominator. Captures are integrity checked, not decrypted protocol reconstruction.')
    return summary,class_rows,checks,errors


def analyze(folder):
    folder = Path(folder).resolve()
    try:
        summary,class_rows,checks,errors = _analyze(folder)
    except Exception as exc:
        validation = dict(passed=False,successful_checks=0,errors=[type(exc).__name__+': '+str(exc)],fatal_parser_error=True)
        (folder/'validation.json').write_text(json.dumps(validation,indent=2)+'\n',encoding='utf-8')
        raise
    (folder/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'validation.json').write_text(json.dumps(dict(passed=not errors,successful_checks=len(checks),errors=errors),indent=2)+'\n',encoding='utf-8')
    flat = []
    for row in class_rows:
        flat.append({key:value for key,value in row.items() if not isinstance(value,dict)} | dict(
            latency_mean_ms=row['source_to_ack_ms']['mean'],latency_p95_ms=row['source_to_ack_ms']['p95'],
            latency_p99_ms=row['source_to_ack_ms']['p99'],outcomes=json.dumps(row['outcomes'],sort_keys=True)))
    if flat:
        with (folder/'per_robot_class.csv').open('w',newline='',encoding='utf-8') as stream:
            writer = csv.DictWriter(stream,fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    if errors:
        raise ValueError('Independent validation failed: '+'; '.join(errors[:12]))
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder',type=Path)
    args = parser.parse_args()
    summary = analyze(args.folder)
    print(json.dumps(dict(passed=summary['accounting_passed'],generated=summary['generated'],successful=summary['successful'],completed_rps=summary['completed_rps']),indent=2))


if __name__ == '__main__':
    main()
