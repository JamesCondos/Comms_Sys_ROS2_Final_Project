"""Per-topic accounting for live, periodic mixed ROS traffic.

Source-cohort delay statistics and clipped observation-window occupancies have
different populations. Keeping both explicit prevents silently dropping jobs
which wait across the observation boundary or never enter service.
"""
from collections import Counter, defaultdict
import argparse
import json
from pathlib import Path

from .analyze import _analyze, cohort_metrics, read_json, records, stats
from .theory import coefficient_of_variation, event_area_ns, interval_area_ns, mmc


TOPICS = ('command','status','odometry','sensor','video')


def audit_ros_routes(ready,callbacks,sources):
    """Prove distinct DDS topics and exact source-to-callback route identities."""
    routes=ready.get('telemetry_topics')
    if not ready.get('separate_telemetry_topics') or not isinstance(routes,dict):
        raise ValueError('Separate actual DDS telemetry topics were not recorded')
    if set(routes)!=set(TOPICS)-{'video'} or len(set(routes.values()))!=4:
        raise ValueError('Four distinct telemetry DDS topics are required')
    routes=dict(routes,video=ready.get('video_topic'))
    if not routes['video'] or len(set(routes.values()))!=5:
        raise ValueError('Video must use its own fifth DDS topic')
    endpoint_rows=ready.get('ros_topics',[])
    endpoints={row['topic_name']:row for row in endpoint_rows}
    if set(endpoints)!=set(routes.values()) or len(endpoint_rows)!=5:
        raise ValueError('Five actual ROS discovery endpoint records are required')
    for kind,topic in routes.items():
        item=endpoints[topic]
        expected_type='sensor_msgs/msg/CompressedImage' if kind=='video' else 'std_msgs/msg/String'
        if item['publisher_count']!=1 or item['subscription_count']!=1:
            raise ValueError(topic+' must have one real publisher and one subscriber')
        for role,node_name in [('publishers','source'),('subscriptions','gateway')]:
            actors=item.get(role,[])
            if len(actors)!=1 or actors[0]['node_name']!=node_name or actors[0]['topic_type']!=expected_type:
                raise ValueError(topic+' has incorrect actual '+role+' or ROS type')
    originals={row['id']:row for row in sources}
    if len(callbacks)!=len(originals) or {row['id'] for row in callbacks}!=set(originals):
        raise ValueError('ROS callback/source identities must match exactly once')
    for callback in callbacks:
        original=originals[callback['id']]
        if callback['message_type']!=original['message_type'] or callback['ros_topic']!=routes[original['message_type']]:
            raise ValueError('ROS message callback used the wrong class/topic route')
        if callback['callback_ns']<original['source_ns']:
            raise ValueError('ROS callback precedes the actual publication timestamp')
    return routes


def class_queue_metrics(jobs, source_map, begin, end, topics=TOPICS):
    """Classify exact waiting intervals and observed source-cohort waits."""
    result = {}
    for kind in topics:
        selected = [row for row in jobs if source_map[row['id']]['message_type']==kind]
        admitted = [row for row in selected if row.get('admission_ns') is not None]
        intervals = [(row['admission_ns'],row['service_start_ns'] if row.get('service_start_ns') is not None else row['terminal_ns']) for row in admitted]
        area = interval_area_ns(intervals,begin,end)
        cohort = [row for row in selected if begin <= source_map[row['id']]['source_ns'] < end]
        cohort_admitted = [row for row in cohort if row.get('admission_ns') is not None]
        started = [row for row in cohort_admitted if row.get('service_start_ns') is not None]
        ended_waiting = [row for row in cohort_admitted if row.get('service_start_ns') is None]
        result[kind] = dict(source_cohort_jobs=len(cohort),admitted=len(cohort_admitted),
            started=len(started),waiting_aborted=len(ended_waiting),
            never_admitted=len(cohort)-len(cohort_admitted),outcomes=dict(Counter(row['outcome'] for row in cohort)),
            wait_to_service_ms=stats([(row['service_start_ns']-row['admission_ns'])/1e6 for row in started]),
            service_ms=stats([(row['terminal_ns']-row['service_start_ns'])/1e6 for row in started]),
            service_cv=coefficient_of_variation([(row['terminal_ns']-row['service_start_ns'])/1e9 for row in started]),
            wait_to_queue_exit_ms=stats([((row['service_start_ns'] if row.get('service_start_ns') is not None else row['terminal_ns'])-row['admission_ns'])/1e6 for row in cohort_admitted]),
            queue_area_ns=area,mean_Lq_contribution=area/(end-begin))
    return result


def analyze_topics(folder):
    """Read raw data without modifying source logs or existing derived files."""
    folder=Path(folder)
    config,start=read_json(folder/'resolved.json'),read_json(folder/'start.json')
    begin,end=start['measurement_start_ns'],start['measurement_end_ns']
    base,_,_,errors=_analyze(folder)
    if errors:
        raise ValueError('Base independent validation failed: '+'; '.join(errors[:12]))
    sources=[row for robot in config['robots'] for row in records(folder/'logs'/robot['id']/'source.jsonl')]
    source_map={row['id']:row for row in sources}
    observed_topics={row['message_type'] for row in sources}
    if observed_topics!=set(TOPICS):
        raise ValueError('Mixed experiment must actually publish all five configured topic classes')
    source_terminal={row['id']:row for robot in config['robots'] for row in records(folder/'logs'/robot['id']/'jobs.jsonl')}
    robots=[row['id'] for row in config['robots']]
    groups=dict(gateway=robots,edge=['edgeA','edgeB'],central=['proxy'])
    job_groups=defaultdict(list)
    component_stages={}
    audit_checks=[]
    schedule_by_robot={}
    actual_ros_topics={}
    for robot in robots:
        log=folder/'logs'/robot
        model=read_json(log/'arrival_model.json')
        schedule_by_robot[robot]=model
        expected=model.get('scheduled_count')
        actual=sum(row['robot_id']==robot for row in sources)
        if expected is not None and expected!=actual:
            raise ValueError(robot+' source count differs from complete configured schedule')
        ready=read_json(log/'ready.json')
        actual_ros_topics[robot]=audit_ros_routes(ready,records(log/'ros_callbacks.jsonl'),[row for row in sources if row['robot_id']==robot])
        audit_checks.append(robot+' all five topic classes and full publication count')
        audit_checks.append(robot+' five actual DDS topics, discovered source/gateway endpoints, exact class callback routes')
    for group,names in groups.items():
        for name in names:
            log=folder/'logs'/name
            jobs=records(log/'jobs.jsonl')
            job_groups[group].extend(jobs)
            per_topic=class_queue_metrics(jobs,source_map,begin,end)
            item_area=sum(row['queue_area_ns'] for row in per_topic.values())
            state_area=event_area_ns(records(log/'queue.jsonl'),begin,end)
            if item_area!=state_area:
                raise ValueError(name+' sum of class waiting areas differs from independent state integral')
            component_stages[name]=dict(per_topic=per_topic,queue_area_ns=state_area,
                mean_Lq=state_area/(end-begin),class_area_residual_ns=item_area-state_area)
            audit_checks.append(name+' exact class Lq contributions sum to stage Lq including carry-in/out')
    stage_groups={}
    for group,names in groups.items():
        per_topic=class_queue_metrics(job_groups[group],source_map,begin,end)
        area=sum(component_stages[name]['queue_area_ns'] for name in names)
        if sum(row['queue_area_ns'] for row in per_topic.values())!=area:
            raise ValueError(group+' class aggregation changes total queue area')
        stage_groups[group]=dict(components=names,per_topic=per_topic,
            queue_area_ns=area,mean_Lq=area/(end-begin),class_area_residual_ns=0)
    workers=[row for name in ('worker1','worker2') for row in records(folder/'logs'/name/'worker.jsonl')]
    per_topic={}
    for kind in TOPICS:
        per_topic[kind]=cohort_metrics([row for row in sources if row['message_type']==kind],source_terminal,begin,end)
        per_topic[kind]['stages']={group:stage_groups[group]['per_topic'][kind] for group in groups}
        worker_cohort=[row for row in workers if source_map[row['id']]['message_type']==kind and
            begin<=source_map[row['id']]['source_ns']<end and row.get('service_start_ns') is not None]
        per_topic[kind]['worker_wait_ms']=stats([(row['service_start_ns']-row['admission_ns'])/1e6 for row in worker_cohort])
        processed=[row for row in worker_cohort if row.get('processed_ns') is not None]
        per_topic[kind]['worker_service_ms']=stats([(row['processed_ns']-row['service_start_ns'])/1e6 for row in processed])
        per_topic[kind]['worker_service_cv']=coefficient_of_variation([(row['processed_ns']-row['service_start_ns'])/1e9 for row in processed])
    pilot_capacity=config.get('calibrated_capacity_rps')
    mm2_reference=dict(pilot_actual_arrivals=mmc(base['central']['admission_rate_per_s'],pilot_capacity/2,2) if pilot_capacity else None,
        pilot_configured_arrivals=mmc(config.get('nominal_offered_rps',config.get('offered_rps_total',148*config.get('traffic_scale',1))),pilot_capacity/2,2) if pilot_capacity else None,
        assumption_note='Reference only: independently phased periodic mixed-class arrivals and measured service are not iid Poisson/exponential assumptions.')
    return dict(schema_version=1,accounting_passed=True,successful_checks=len(audit_checks),
        run_id=config.get('run_id'),traffic_scale=config.get('traffic_scale',config.get('mixed_traffic_scale')),
        mixed_traffic_scale=config.get('traffic_scale',config.get('mixed_traffic_scale')),
        calibration_load_fraction=config.get('calibration_load_fraction'),
        calibrated_capacity_rps=config.get('calibrated_capacity_rps'),
        nominal_offered_rps=config.get('nominal_offered_rps',148*config.get('traffic_scale',1)),
        duration_s=(end-begin)/1e9,actual_robot_clients=len(robots),protocol=config['protocol'],
        offered_rps=base['offered_rps'],completed_rps=base['completed_rps'],
        payload_goodput_mbit_s=base['payload_goodput_mbit_s'],generated=base['generated'],
        successful=base['successful'],outcomes=base['outcomes'],
        source_to_ack_ms=base['source_to_ack_ms'],central_reference=base['central'],mm2_reference=mm2_reference,
        per_topic=per_topic,stage_groups=stage_groups,component_stages=component_stages,
        arrival_models=schedule_by_robot,actual_ros_topics=actual_ros_topics,
        audit_checks=audit_checks,
        interpretation='Independent-phase periodic mixed topics are not a Poisson M/M/2 workload. Any M/M/2 curve is an approximation or reference; the earlier Poisson probe remains the controlled theory test. Source-cohort queue waits include drain; wait_to_service_ms excludes jobs aborted while waiting, and wait_to_queue_exit_ms includes their observed waiting duration. Class Lq contributions instead integrate every clipped waiting interval within measurement, including carry-in and carry-out; their exact sum equals stage queue occupancy. Throughput/goodput count successful ACKs inside measurement, not drain completions.')


def write_topic_analysis(folder,output):
    result=analyze_topics(folder)
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('folder',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=write_topic_analysis(args.folder,args.output)
    print(json.dumps(dict(passed=True,generated=result['generated'],topics=list(result['per_topic'])),indent=2))
