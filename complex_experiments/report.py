"""Figures, transparent CSV tables and a PDF from validated live experiment logs.

No synthetic results or missing-run substitution. Error bars describe the range
of replicate statistics, not uncertainty over individual correlated messages.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import zipfile


PROTOCOLS = ('h1', 'h2', 'h3')
PROTOCOL_LABELS = {'h1': 'HTTP/1.1', 'h2': 'HTTP/2', 'h3': 'HTTP/3'}
PROFILE_ORDER = ('clean', 'delay_30ms', 'loss_1pct', 'bandwidth_2mbit', 'combined')
PROFILE_LABELS = {'clean': 'Clean', 'delay_30ms': '30 ms / direction',
                  'loss_1pct': '1% loss / direction', 'bandwidth_2mbit': '2 Mbit/s / site',
                  'combined': 'Delay + loss + rate'}
COLORS = {'h1': '#0072B2', 'h2': '#D55E00', 'h3': '#009E73'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def get(obj, path, default=None):
    for key in path.split('.'):
        if not isinstance(obj, dict) or key not in obj:
            return default
        obj = obj[key]
    return obj


def distribution(values):
    vals = [float(value) for value in values if value is not None and math.isfinite(value)]
    return dict(n=len(vals), mean=statistics.mean(vals) if vals else None,
                minimum=min(vals) if vals else None, maximum=max(vals) if vals else None)


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)


def read_runs(data, expected_runs=60, allow_partial=False):
    runs = []
    for path in sorted(Path(data).rglob('summary.json')):
        if not (path.parent / 'resolved.json').exists():
            continue
        summary, config = read(path), read(path.parent / 'resolved.json')
        if summary.get('experiment') not in ('queue', 'protocol'):
            continue
        validation = read(path.parent / 'validation.json')
        if not summary.get('accounting_passed') or not validation.get('passed'):
            raise ValueError('Refusing an invalid run: ' + str(path.parent))
        runs.append(dict(folder=path.parent, summary=summary, config=config,
                         profile=config.get('profile_name', 'clean'),
                         repetition=config.get('repetition', config.get('rep'))))
    keys = [(r['summary']['experiment'], r['summary']['protocol'],
             r['profile'], r['config'].get('offered_rps_total'), r['repetition']) for r in runs]
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate experiment/protocol/profile/load/repetition cells')
    if not allow_partial:
        if len(runs) != expected_runs:
            raise ValueError(f'Expected {expected_runs} validated runs; found {len(runs)}')
        cells = defaultdict(list)
        for key in keys:
            cells[key[:-1]].append(key[-1])
        if len(cells) != 20 or any(set(reps) != {1, 2, 3} for reps in cells.values()):
            raise ValueError('Expected 20 cells with three independent repetitions each')
        queue_cells = [key for key in cells if key[0] == 'queue']
        protocol_cells = [key for key in cells if key[0] == 'protocol']
        if (len(queue_cells) != 5 or any(key[1:3] != ('h2', 'clean') for key in queue_cells)
                or {(key[1], key[2]) for key in protocol_cells}
                != {(protocol, profile) for protocol in PROTOCOLS for profile in PROFILE_ORDER}):
            raise ValueError('Matrix must contain five HTTP/2 clean loads and all fifteen protocol/profile pairs')
    if not runs:
        raise ValueError('No validated complex-network results found')
    return runs


def flatten_run(run):
    s, c = run['summary'], run['config']
    central = s['central']
    result = dict(run_id=c.get('run_id', run['folder'].name), experiment=s['experiment'],
                  protocol=s['protocol'], profile=run['profile'], repetition=run['repetition'],
                  configured_offered_rps=c.get('offered_rps_total'),
                  pilot_estimated_capacity_rps=c.get('calibrated_capacity_rps') if s['experiment']=='queue' else None,
                  pilot_estimated_capacity_fraction=c.get('calibration_load_fraction') if s['experiment']=='queue' else None,
                  actual_offered_rps=s['offered_rps'], completed_rps=s['completed_rps'],
                  generated=s['generated'], successful=s['successful'],
                  failure_fraction=1-s['success_fraction'], mean_latency_ms=s['source_to_ack_ms']['mean'],
                  p95_latency_ms=s['source_to_ack_ms']['p95'], p99_latency_ms=s['source_to_ack_ms']['p99'],
                  payload_goodput_mbit_s=s['payload_goodput_mbit_s'],
                  offered_payload_mbit_s=s.get('offered_payload_mbit_s'),
                  central_admission_rps=central['admission_rate_per_s'],
                  central_wait_mean_ms=central['queue_wait_ms']['mean'],
                  central_residence_mean_ms=central['residence_ms']['mean'],
                  central_service_mean_ms=central['service_ms']['mean'],
                  central_service_cv=central['service_cv'],
                  central_arrival_cv=central['arrival_diagnostics']['spacing_cv'],
                  central_Lq=s['queues']['proxy']['mean_Lq'], central_L=s['queues']['proxy']['mean_L'],
                  central_queue_growth_rps=s['queues']['proxy']['queue_growth_per_s'],
                  gateway_Lq=sum(s['queues'][robot]['mean_Lq'] for robot in s['per_robot']),
                  edge_Lq=sum(s['queues']['edge'+site]['mean_Lq'] for site in ('A', 'B')),
                  processed_unacknowledged_all=s['central_processed_unacknowledged_all'],
                  max_exact_queue_residual_ns=max(abs(q['exact_queue_residual_ns']) for q in s['queues'].values()),
                  max_exact_system_residual_ns=max(abs(q['exact_system_residual_ns']) for q in s['queues'].values()),
                  outcomes=json.dumps(s['outcomes'], sort_keys=True),
                  summary_sha256=hashlib.sha256((run['folder']/'summary.json').read_bytes()).hexdigest())
    for model in ('nominal_mmc', 'calibrated_mmc', 'pilot_mmc', 'pilot_offered_mmc'):
        value = central.get(model) or {}
        for field in ('rho', 'stable', 'Wq_s', 'W_s', 'Lq', 'L', 'capacity'):
            result[model+'_'+field] = value.get(field)
        for measured, predicted, name, scale in ((result['central_wait_mean_ms'], value.get('Wq_s'), 'Wq_relative_error_pct', 1000),
                                               (result['central_Lq'], value.get('Lq'), 'Lq_relative_error_pct', 1)):
            result[model+'_'+name] = 100*(measured-predicted*scale)/(predicted*scale) if measured is not None and predicted is not None and predicted > 0 else None
    for kind in ('video', 'command', 'status', 'odometry', 'sensor'):
        row = s.get('per_class', {}).get(kind, {})
        for key in ('offered_rps', 'completed_rps', 'payload_goodput_mbit_s', 'offered_payload_mbit_s', 'success_fraction'):
            result[kind+'_'+key] = row.get(key)
        result[kind+'_p95_latency_ms'] = get(row, 'source_to_ack_ms.p95')
    return result


def cell_key(run):
    s = run['summary']
    return (s['experiment'], s['protocol'], run['profile'], run['config'].get('offered_rps_total'))


def aggregate_rows(flat):
    groups = defaultdict(list)
    for row in flat:
        groups[(row['experiment'], row['protocol'], row['profile'], row['configured_offered_rps'])].append(row)
    output = []
    excluded = {'run_id', 'experiment', 'protocol', 'profile', 'repetition', 'outcomes', 'summary_sha256'}
    for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
        result = dict(experiment=key[0], protocol=key[1], profile=key[2], configured_offered_rps=key[3], repetitions=len(rows))
        for field in rows[0]:
            if field in excluded:
                continue
            if all(row[field] is None or isinstance(row[field], (int, float)) for row in rows):
                d = distribution([row[field] for row in rows])
                result.update({field+'_'+name: value for name, value in d.items()})
        output.append(result)
    return output


def make_figures(runs, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from .theory import mmc
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.grid': True, 'grid.alpha': .18, 'savefig.dpi': 180,
                         'pdf.fonttype': 42})
    folder = output / 'figures'
    folder.mkdir(parents=True, exist_ok=True)
    figures = []
    groups = defaultdict(list)
    for run in runs:
        groups[cell_key(run)].append(run)
    qkeys = sorted([key for key in groups if key[0] == 'queue'], key=lambda key: key[3])
    profiles = [p for p in PROFILE_ORDER if any(key[0] == 'protocol' and key[2] == p for key in groups)]

    def bounds(values):
        d = distribution(values)
        return d['mean'], [d['mean']-d['minimum'], d['maximum']-d['mean']] if d['n'] else [0, 0]

    def save(fig, name, title, method, interpretation):
        study='queue' if name.startswith('q1') else 'protocol'
        repetitions=sorted({len(values) for key,values in groups.items() if key[0]==study})
        n='/'.join(map(str,repetitions))
        context=('Q1: four clients; video off; exponential 20 ms worker holds. '
                 if study=='queue' else 'Q2: four clients; video on; fixed 4 ms worker holds. ')
        context+=f'{n} repetitions/cell; '+('one trace per repetition.' if name=='q1_overload_backlog' else 'means and replicate min-max ranges.')
        fig.tight_layout(pad=1.1,rect=(0,.045,1,1))
        fig.text(.5,.012,context,ha='center',va='bottom',fontsize=7,color='#555555')
        fig.savefig(folder/(name+'.pdf'))
        fig.savefig(folder/(name+'.png'))
        plt.close(fig)
        figures.append(dict(name=name, title=title, method=method, interpretation=interpretation))

    def qplot(name, title, ylabel, series, method, interpretation, model=None, log=False):
        if not qkeys:
            return
        fig, ax = plt.subplots(figsize=(7.2, 4.3))
        xs = [key[3] for key in qkeys]
        for label, func, color, marker in series:
            values = [[func(run['summary']) for run in groups[key]] for key in qkeys]
            valid = [(x, bounds(v)) for x, v in zip(xs, values)
                     if distribution(v)['n'] and not (label.startswith('M/M/2') and any(item is None for item in v))]
            if not valid:
                continue
            ax.errorbar([x for x, _ in valid], [v[0] for _, v in valid],
                        yerr=np.array([v[1] for _, v in valid]).T, fmt=marker+'-',
                        capsize=3, label=label, color=color, linewidth=1.6, markersize=5)
        if model:
            model(ax, xs)
        ax.set(xlabel='Configured offered traffic across four clients (requests/s)', ylabel=ylabel, title=title)
        if log:
            if name in ('q1_end_to_end_latency','q1_central_residence_theory'):
                ax.set_yscale('log')
            else:
                ax.set_yscale('symlog', linthresh=.2)
                ax.set_ylim(bottom=0)
        else:
            ax.set_ylim(bottom=0)
        ax.legend(fontsize=8, loc='best')
        primary = series[0]
        low = distribution([primary[1](r['summary']) for r in groups[qkeys[0]]])
        high = distribution([primary[1](r['summary']) for r in groups[qkeys[-1]]])
        observed = (f'Observed {primary[0].lower()}: {number(low["mean"])} at '
                    f'{number(xs[0],1)} offered requests/s and {number(high["mean"])} at '
                    f'{number(xs[-1],1)} offered requests/s ({ylabel.lower()}). ')
        if name=='q1_queue_location':
            last=[r['summary'] for r in groups[qkeys[-1]]]
            observed=(f'At the highest offered load, the central FIFO held a time-mean '
                      f'{number(statistics.mean(s["queues"]["proxy"]["mean_Lq"] for s in last))} '
                      f'waiting requests, compared with {number(high["mean"])} across all four gateway queues. ')
        if name.startswith('q1_central_'):
            stable_keys = [key for key in qkeys if all(get(r['summary'],'central.pilot_mmc.stable') for r in groups[key])]
            if stable_keys:
                key = stable_keys[-1]
                field = 'Wq_s' if 'wait' in name else 'Lq' if 'occupancy' in name else 'W_s'
                scale = 1 if field == 'Lq' else 1000
                prediction = distribution([get(r['summary'],'central.pilot_mmc.'+field)*scale for r in groups[key]])
                measured = distribution([primary[1](r['summary']) for r in groups[key]])
                observed += (f'At the highest cell with all pilot-model predictions stable '
                             f'({number(key[3],1)} offered requests/s), the measured mean is '
                             f'{number(measured["mean"])} and the independent pilot-based mean prediction is '
                             f'{number(prediction["mean"])}. ')
        interpretation = observed + interpretation
        save(fig, name, title, method, interpretation)

    repeat_note = 'Each point is the arithmetic mean of per-run statistics. Bars span the minimum to maximum across independent repetitions; they are not confidence intervals. '
    qplot('q1_throughput', 'Traffic growth and delivered throughput', 'Requests/s',
          [('Generated', lambda s:s['offered_rps'], '#555555', 'o'),
           ('Acknowledged inside measurement', lambda s:s['completed_rps'], '#0072B2', 's'),
           ('Admitted at the central FIFO', lambda s:s['central']['admission_rate_per_s'], '#D55E00', '^')],
          repeat_note+'Generated rate uses source timestamps; throughput counts ACK timestamps in the fixed observation window, including warmup carry-in.',
          'A gap between generated and acknowledged rates reveals a backlog or failed requests. The central admission rate locates whether throttling occurs before the shared FIFO. Saturation is assessed from measured throughput rather than the configured sleep alone.')
    qplot('q1_end_to_end_latency', 'Traffic growth and source-to-ACK latency', 'Latency (ms)',
          [('Mean latency', lambda s:s['source_to_ack_ms']['mean'], '#0072B2', 'o'),
           ('95th percentile', lambda s:s['source_to_ack_ms']['p95'], '#D55E00', 's')],
          repeat_note+'Every run uses messages generated inside the measurement window. Successful ACKs received during drain remain in the latency cohort. Failed messages remain in success-rate denominators.',
          'An increasing tail shows the time cost of competition across the whole network. These end-to-end values include ROS delivery, gateway and edge waiting, network transfer, central waiting, worker processing and acknowledgement; an M/M/2 formula applies only to the stated central boundary.', log=True)
    qplot('q1_queue_location', 'Where the backlog accumulates', 'Time-mean queued requests',
          [('Four gateways combined', lambda s:sum(s['queues'][r]['mean_Lq'] for r in s['per_robot']), '#0072B2', 'o'),
           ('Two edges combined', lambda s:sum(s['queues']['edge'+r]['mean_Lq'] for r in ('A','B')), '#009E73', '^'),
           ('Central shared FIFO', lambda s:s['queues']['proxy']['mean_Lq'], '#D55E00', 's')],
          repeat_note+'Queue occupancy is integrated exactly from timestamped admission/dequeue/terminal events. In-service jobs are excluded; sums refer to the indicated physical queues.',
          'This plot separates central service contention from queues upstream. A long gateway or edge queue means a central-only queueing model cannot explain all of the measured end-to-end latency.', log=True)

    def nominal_curve(ax, xs, field, scale):
        config = groups[qkeys[0]][0]['config']
        capacity = config.get('calibrated_capacity_rps', 2/config.get('q_mean_service_s', .02))
        mean_hold = 2/capacity
        grid = np.linspace(0, min(max(xs), capacity*.985), 260)
        vals = [mmc(float(x), 1/mean_hold, 2)[field]*scale for x in grid]
        ax.plot(grid, vals, '--', color='#666666', linewidth=1.2, label='M/M/2: independent pilot service mean')
        ax.axvline(capacity, color='#777777', linestyle=':', linewidth=1, label='Pilot-calibrated service capacity')

    def model_series(field, scale=1):
        def take(s):
            value = get(s, 'central.pilot_mmc.'+field)
            return value*scale if value is not None else None
        return take

    model_note = 'The dashed curve uses two identical exponential servers, a service mean frozen from an independent pilot, and configured offered rate. Model markers use the same frozen pilot service mean and each run\'s actual central admission rate. A separate post-hoc fit using in-run service means appears in the tables. No finite stationary prediction is drawn when rho >= 1. '
    qplot('q1_central_wait_theory', 'Central waiting time and M/M/2 theory', 'Mean wait in the central FIFO (ms)',
          [('Measured central wait', lambda s:s['central']['queue_wait_ms']['mean'], '#0072B2', 'o'),
           ('M/M/2: pilot service, actual arrivals', model_series('Wq_s',1000), '#D55E00', 's')],
          repeat_note+model_note+'Measured waits use the central admission-time cohort, including its drain completions.',
          'Curvature below saturation is the queueing-theory prediction. Disagreement may reflect service overhead, non-Poisson arrivals reaching the FIFO, finite observation time, or upstream flow control. Compare the arrival and service CV columns before treating M/M/2 as an exact model.',
          model=lambda ax,xs:nominal_curve(ax,xs,'Wq_s',1000), log=True)
    qplot('q1_central_occupancy_theory', 'Central queue size and M/M/2 theory', 'Time-mean central queue size (requests)',
          [('Measured central queue', lambda s:s['queues']['proxy']['mean_Lq'], '#0072B2', 'o'),
           ('M/M/2: pilot service, actual arrivals', model_series('Lq'), '#D55E00', 's')],
          repeat_note+model_note+'Measured Lq is an occupancy integral over the observation window, with carry-in and carry-out included.',
          'Queue length becomes sensitive to small changes in utilization near capacity. At overload, a growing finite-run queue is meaningful; a stationary mean is undefined. Exact sample-path occupancy audits are provided separately from model error.',
          model=lambda ax,xs:nominal_curve(ax,xs,'Lq',1), log=True)
    qplot('q1_central_residence_theory', 'Central residence time and M/M/2 theory', 'Mean central residence (ms)',
          [('Measured central residence', lambda s:s['central']['residence_ms']['mean'], '#0072B2', 'o'),
           ('M/M/2: pilot service, actual arrivals', model_series('W_s',1000), '#D55E00', 's')],
          repeat_note+model_note+'The central system boundary begins at admission and ends when the worker response is read by the proxy.',
          'Residence time combines central waiting and dispatch-to-worker service. Comparing a matched boundary avoids attributing ROS, edge or WAN delay to the central queueing model.',
          model=lambda ax,xs:nominal_curve(ax,xs,'W_s',1000), log=True)

    if qkeys:
        selected = max(qkeys, key=lambda key:key[3])
        fig, ax = plt.subplots(figsize=(7.2,4.3))
        for index, run in enumerate(sorted(groups[selected],key=lambda r:str(r['repetition']))):
            start = read(run['folder']/'start.json')
            begin, end = start['measurement_start_ns'], start['measurement_end_ns']
            rows = [json.loads(line) for line in (run['folder']/'logs'/'proxy'/'queue.jsonl').read_text().splitlines() if line.strip()]
            prior = [row for row in rows if row['time_ns']<=begin]
            actual = [row for row in rows if begin<row['time_ns']<end]
            x = [0]+[(row['time_ns']-begin)/1e9 for row in actual]+[(end-begin)/1e9]
            y = [prior[-1]['queue_len'] if prior else 0]+[row['queue_len'] for row in actual]
            y.append(y[-1])
            ax.step(x,y,where='post',label='Repetition '+str(run['repetition']),linewidth=1.2)
        ax.set(xlabel='Seconds since measurement start',ylabel='Requests waiting at the central FIFO',
               title='Central backlog at the highest offered load',ylim=(0,None))
        ax.legend(fontsize=9)
        growth = distribution([r['summary']['queues']['proxy']['queue_growth_per_s'] for r in groups[selected]])
        save(fig,'q1_overload_backlog','Central backlog at the highest offered load',
             'Every queue-changing event is plotted as a right-continuous step. The carried-in backlog at time zero accumulated during the 10-second warmup and is retained. One line is one complete repetition.',
             f'The mean net queue growth was {number(growth["mean"])} requests/s '
             f'(replicate range {number(growth["minimum"])} to {number(growth["maximum"])}). '
             'An upward trend is direct evidence of backlog accumulation during the finite observation period. A flat central trace alongside a rising upstream queue instead indicates that upstream concurrency or transport limits restrict central arrivals.')

    def protocol_plot(name,title,ylabel,func,interpretation,percent=False,log=False,reference=None):
        if not profiles:
            return
        fig,ax = plt.subplots(figsize=(8,4.4))
        xs = np.arange(len(profiles),dtype=float)
        for index, protocol in enumerate(PROTOCOLS):
            positions,means,lows,highs = [],[],[],[]
            for x,profile in zip(xs,profiles):
                sample = [func(r['summary']) for key,items in groups.items() if key[0]=='protocol' and key[1]==protocol and key[2]==profile for r in items]
                d = distribution(sample)
                if not d['n']:
                    continue
                positions.append(x+(index-1)*.22)
                means.append(d['mean'])
                lows.append(d['mean']-d['minimum'])
                highs.append(d['maximum']-d['mean'])
            ax.errorbar(positions,means,yerr=[lows,highs],fmt=['o','s','^'][index],
                        color=COLORS[protocol],capsize=3,label=PROTOCOL_LABELS[protocol],markersize=6)
        if reference is not None:
            ax.axhline(reference,color='#666666',linestyle='--',linewidth=1,label='Offered reference')
        ax.set_xticks(xs,[PROFILE_LABELS.get(p,p).replace(' / ','\nper ') for p in profiles],fontsize=9)
        ax.set(ylabel=ylabel,title=title)
        if log:
            ax.set_yscale('log')
        elif percent:
            ax.set_ylim(-1,101)
        else:
            ax.set_ylim(bottom=0)
        ax.legend(fontsize=8,loc='best',ncol=2)
        observed=[]
        for profile in dict.fromkeys((profiles[0],profiles[-1])):
            values=[]
            for protocol in PROTOCOLS:
                sample=[func(r['summary']) for key,items in groups.items()
                        if key[0]=='protocol' and key[1]==protocol and key[2]==profile for r in items]
                d=distribution(sample)
                if d['n']:
                    values.append(PROTOCOL_LABELS[protocol]+' '+number(d['mean']))
            if values:
                observed.append(PROFILE_LABELS.get(profile,profile)+': '+', '.join(values)+'.')
        interpretation='Observed replicate means ('+ylabel.lower()+'): '+' '.join(observed)+' '+interpretation
        save(fig,name,title,repeat_note+'Each point uses the same four real ROS clients, mixed telemetry and JPEG video, application concurrency, worker capacity and impairment profile. The configured protocol is used at every HTTP hop with ALPN checked and no fallback.',interpretation)

    protocol_plot('q2_latency','Protocol latency under network impairments','Mean of per-run 95th percentile latency (ms)',
                  lambda s:s['source_to_ack_ms']['p95'],
                  'Compare protocols within each profile. Increased delay contributes propagation time; loss and limited bandwidth can also increase waiting and retransmission time. Successful-message latency must be read with the failure plot because failed requests have no successful ACK latency.',log=True)
    protocol_plot('q2_throughput','Protocol throughput under network impairments','Acknowledged requests/s inside measurement',
                  lambda s:s['completed_rps'],
                  'A smaller completion rate shows fewer mixed telemetry messages and video frames delivered per second during the fixed observation. Drain completions contribute to cohort success but do not inflate this in-window rate.')
    protocol_plot('q2_goodput','Useful payload delivered under network impairments','Successful useful payload goodput (Mbit/s)',
                  lambda s:s['payload_goodput_mbit_s'],
                  'Goodput counts useful source payload bytes acknowledged inside the measurement, excluding JSON/base64 expansion, HTTP, TLS and transport headers. The 2 Mbit/s cap applies separately to each site uplink, so it is not a 2 Mbit/s cap on aggregate useful goodput.')
    protocol_plot('q2_failures','Failed or unresolved source messages','Source-cohort failure (%)',
                  lambda s:100*(1-s['success_fraction']),
                  'The denominator includes all ROS messages generated in the measurement window, including drops and timeouts. A timeout can be ambiguous if a worker processed the request but the ACK did not reach the source; those counts are retained in the per-run table.',percent=True)
    video_fps = next((r['config'].get('video_fps',5)*len(r['config']['robots']) for r in runs if r['summary']['experiment']=='protocol'),20)
    protocol_plot('q2_video_delivery','Video frame delivery under network impairments','Video frames acknowledged/s (four clients combined)',
                  lambda s:get(s,'per_class.video.completed_rps'),
                  'This is application delivery of complete JPEG frames, measured at source ACK receipt. It is not playback quality, displayed frame rate or a raw RTP video stream. The dashed line shows the total configured video frame production rate.',reference=video_fps)
    protocol_plot('q2_video_latency','Video frame latency under network impairments','Mean of per-run video 95th percentile latency (ms)',
                  lambda s:get(s,'per_class.video.source_to_ack_ms.p95'),
                  'Frame latency begins when the source publishes the compressed image and ends when central processing is acknowledged. The video and telemetry streams share the same FIFO application policy, so large frames can affect smaller messages.',log=True)
    protocol_plot('q2_odometry_latency','Odometry latency with the video stream active','Mean of per-run odometry 95th percentile latency (ms)',
                  lambda s:get(s,'per_class.odometry.source_to_ack_ms.p95'),
                  'Odometry is published at 20 Hz per robot while video and the other telemetry classes are active. This class-specific latency isolates the impact on small frequent messages; it is not a separate 20-message batch experiment.',log=True)
    protocol_plot('q2_gateway_queue','Gateway queueing under network impairments','Sum of time-mean gateway queue sizes (requests)',
                  lambda s:sum(s['queues'][r]['mean_Lq'] for r in s['per_robot']),
                  'The plotted sum covers four independent gateway queues. Growth shows that requests wait before entering the HTTP path as transport and downstream capacity consume the fixed application concurrency.',log=True)
    if profiles:
        fig,ax=plt.subplots(figsize=(8,4.4))
        stage_functions=[('Four gateways',lambda s:sum(s['queues'][r]['mean_Lq'] for r in s['per_robot']),'#0072B2'),
                         ('Two edges',lambda s:sum(s['queues']['edge'+r]['mean_Lq'] for r in ('A','B')),'#E69F00'),
                         ('Central FIFO',lambda s:s['queues']['proxy']['mean_Lq'],'#009E73')]
        for pi,protocol in enumerate(PROTOCOLS):
            positions=np.arange(len(profiles))+(pi-1)*.22
            bottom=np.zeros(len(profiles))
            samples_by_profile=[[r['summary'] for key,items in groups.items() if key[0]=='protocol' and key[1]==protocol and key[2]==profile for r in items] for profile in profiles]
            for label,function,color in stage_functions:
                values=[distribution([function(s) for s in samples])['mean'] or 0 for samples in samples_by_profile]
                ax.bar(positions,values,width=.19,bottom=bottom,label=label if pi==0 else None,color=color)
                bottom+=values
            totals=[distribution([sum(function(s) for _,function,_ in stage_functions) for s in samples]) for samples in samples_by_profile]
            ax.errorbar(positions,[v['mean'] or 0 for v in totals],
                        yerr=[[v['mean']-v['minimum'] if v['n'] else 0 for v in totals],
                              [v['maximum']-v['mean'] if v['n'] else 0 for v in totals]],
                        fmt='none',ecolor='#333333',capsize=2,linewidth=.8)
            for x in positions:
                ax.text(x,-.035,('1.1','2','3')[pi],transform=ax.get_xaxis_transform(),ha='center',va='top',fontsize=7)
        ax.set_xticks(np.arange(len(profiles)),[PROFILE_LABELS.get(p,p) for p in profiles],fontsize=8)
        ax.tick_params(axis='x',pad=17)
        ax.set(ylabel='Time-mean requests waiting (sum of application queues)',
               title='Where network impairments move the backlog',ylim=(0,None))
        ax.legend(fontsize=8,loc='upper left')
        save(fig,'q2_queue_location','Where network impairments move the backlog',
             'Within each profile, bars appear in HTTP/1.1, HTTP/2, HTTP/3 order. Stacked segments are the means across repetitions of gateway, edge and central FIFO occupancy. Error bars span the minimum and maximum total queue occupancy across repetitions; they are not confidence intervals. In-service requests are excluded.',
             'Large gateway or edge segments with a small central segment place the backlog upstream of the worker pool. An edge waiting on transport can fill its in-service slots and make later requests accumulate at robot gateways. The exact stage means are tabulated in queue_location.csv and cell_summary.csv; this evidence distinguishes the WAN constraints here from the central service bottleneck in Q1.')
    return figures


def number(value, places=2):
    return '-' if value is None else f'{value:,.{places}f}'


def build_pdf(runs,flat,aggregated,figures,output,history):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Image,Table,TableStyle,PageBreak
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle('SmallBody',parent=styles['BodyText'],fontSize=9,leading=12,spaceAfter=7))
    styles.add(ParagraphStyle('CaptionBody',parent=styles['BodyText'],fontSize=9,leading=12,spaceAfter=9))
    styles['Title'].fontSize=22
    styles['Title'].leading=26
    doc=SimpleDocTemplate(str(output/'results.pdf'),pagesize=(595.276,841.89),
                          rightMargin=45,leftMargin=45,topMargin=45,bottomMargin=42,
                          title='ROS 2 multi-site network: traffic and HTTP transport experiments',
                          author='ELEN90061 project')
    story=[]
    def p(text,style='SmallBody'):
        story.append(Paragraph(text,styles[style]))
    def table(data,widths=None):
        clean=[[Paragraph(str(cell),styles['SmallBody']) for cell in row] for row in data]
        value=Table(clean,colWidths=widths,repeatRows=1,hAlign='LEFT')
        value.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E8EDF2')),
                                  ('LINEBELOW',(0,0),(-1,0),.8,colors.HexColor('#667787')),
                                  ('LINEBELOW',(0,-1),(-1,-1),.5,colors.HexColor('#A8B0B8')),
                                  ('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),5),
                                  ('BOTTOMPADDING',(0,0),(-1,-1),2)]))
        story.append(value)
    p('ROS 2 multi-site network<br/>Traffic, queues and HTTP transports','Title')
    p(f'{len(runs)} independently validated live runs. Four actual robot clients, two site edges, two routed site uplinks, one central pooled FIFO and two worker processes. All components are emulated on one Linux host; the robot nodes publish through ROS 2 before HTTP forwarding.')
    p('What is measured','Heading2')
    p('Source-to-ACK latency spans publication of the source message through receipt of the worker acknowledgement. Source-time cohorts include successful ACKs arriving during drain. Throughput and useful payload goodput count ACKs received strictly inside the measurement window. Queue occupancy is the exact time integral of waiting requests; requests in service are excluded from Lq.')
    p('Every plotted symbol is a mean across per-run statistics. A plotted p95 is therefore the mean of each run\'s p95, rather than a percentile of pooled messages. Error bars show the smallest and largest replicate statistic. Three repetitions provide a limited estimate of variability; the bars are not confidence intervals.')
    q=[r for r in runs if r['summary']['experiment']=='queue']
    h=[r for r in runs if r['summary']['experiment']=='protocol']
    qc=q[0]['config'] if q else {}
    hc=h[0]['config'] if h else {}
    p('Controlled procedure','Heading2')
    table([['Study','Traffic and service','Observation per run'],
           ['Traffic and queueing',f'Poisson sources split across four clients; HTTP/2; video disabled; two exponential worker holds with configured mean {1000*qc.get("q_mean_service_s",.02):g} ms. The pilot-selected offered rates are frozen before collection.',f'{qc.get("warmup_s",10)} s warmup; {qc.get("duration_s",45)} s measurement; {qc.get("drain_s",30)} s drain.'],
           ['HTTP transports',f'Four telemetry classes plus {hc.get("video_fps",5):g} JPEG frames/s per robot, 320 x 180 pixels. Two fixed 4 ms worker holds. TLS 1.3 at every hop; equal application concurrency, with HTTP/1.1 connection pools and HTTP/2 or HTTP/3 multiplexed connections.',f'{hc.get("warmup_s",5)} s warmup; {hc.get("duration_s",20)} s measurement; {hc.get("drain_s",15)} s drain.']], [92,284,129])
    p('The full resolved settings, negotiated ALPN, actual service means and counts are preserved with every run. The study compares these Python implementations and this connection policy; it cannot establish a universal ranking of HTTP versions or isolate protocol design from library implementation overhead.')
    p('<b>The two studies use different workloads and worker service times.</b> Q1 has exponential 20 ms worker holds with video off; Q2 has fixed 4 ms holds with video on. Their absolute latency values cannot be treated as points on one common traffic curve. Q1 load fractions refer to pilot-estimated capacity, not a known exact utilization.')
    story.append(PageBreak())
    p('Network and impairment controls','Heading1')
    p('Robots 1 and 2 connect to edge A; robots 3 and 4 connect to edge B. Each edge forwards through its site router to the central proxy, where two fixed service loops share a FIFO and each targets one worker. ROS delivery remains local to each robot namespace. IP routing, HTTP transport and impairments operate between distinct Linux network namespaces.')
    table([['Profile','Applied to both site uplinks'],['Clean','No artificial delay, loss or rate cap.'],
           ['Delay','30 ms added in each direction, approximately 60 ms added round-trip delay.'],['Loss','1% independent packet loss in each direction, not 1% application failure.'],
           ['Bandwidth','2 Mbit/s forward cap at each site router; reverse path uncapped.'],
           ['Combined','30 ms in each direction, 1% loss in each direction, and 2 Mbit/s forward per site.']], [96,409])
    p('Workers, robot count, payload schedule and application concurrency are matched across protocol profiles. TCP and QUIC use Cubic, but kernel TCP and user-space QUIC implementations do not share identical congestion-control code. Connections are kept persistent; the fixed warmup reduces, but does not remove, connection-establishment effects.')
    p('Transport recovery is enabled; there are no application-level retries. Each robot permits eight outstanding application requests and each edge permits 32. HTTP/1.1 rotates slots through persistent connection pools in FIFO order; HTTP/2 and HTTP/3 multiplex one connection per downstream target. Pool rotation, per-connection idle intervals and transport implementation details can affect results. The comparison does not identify a single causal mechanism for protocol differences.')
    p('Video stream','Heading2')
    jpeg=[get(read(r['folder']/'logs'/robot/'ready.json'),'jpeg_mean_bytes') for r in h[:1] for robot in r['summary']['per_robot']]
    jpeg=[v for v in jpeg if v]
    p('Each source publishes actual ROS 2 sensor_msgs/CompressedImage messages on camera/image/compressed. The repeatable synthetic JPEG sequence represents a video workload. The gateway forwards the JPEG bytes in a JSON/base64 HTTP request, so wire bytes exceed useful image bytes. '+(f'The mean JPEG payload in the first protocol run was {number(statistics.mean(jpeg),0)} bytes per frame. ' if jpeg else '')+'The same FIFO serves video and telemetry. This study does not evaluate a live camera, image reconstruction quality or video playback.')
    p('Per robot, the telemetry schedule is: synthetic critical uplink events 2/s at 100 useful bytes, status 5/s at 100 bytes, odometry 20/s at 250 bytes, and sensor data 5/s at 20,000 bytes. Video adds five frames/s. Thus four clients offer 148 mixed requests/s, including 20 video frames/s; each periodic stream has an independently seeded phase.')
    p('Queueing model and finite-window audit','Heading2')
    p('The central M/M/2 reference assumes a single infinite FIFO, independent Poisson arrivals and identical exponential service. Its system boundary is central admission to proxy receipt of the worker response. Wq is central waiting; W includes dispatch-to-worker exchange and processing. Primary predictions freeze the mean central service time from a separate pilot before selecting the formal offered rates. Markers use actual central arrivals with this frozen service mean. Additional nominal-hold predictions and post-hoc predictions from in-run service means are retained in the CSV tables; the latter are diagnostic fits, not independent predictions.')
    p('A stationary M/M/2 mean exists only below rho = lambda/(2 mu) = 1. No finite theoretical latency is assigned to overload. Queue lengths are independently reconstructed from events and per-job intervals, including carry-in and carry-out, to verify the exact sample-path occupancy identity. That identity is an accounting check, distinct from validating stochastic model assumptions.')
    p('The implemented FIFO admits at most 2,000 waiting requests. This limit was not reached and no queue overflow occurred in the formal runs. Infinite buffering is an ideal M/M/2 reference assumption; the reported overload growth is observed over a finite test, not a claim of unbounded growth in the implemented finite queue.')
    story.append(PageBreak())
    if q:
        p('Traffic study: numerical results','Heading1')
        qrows=sorted([row for row in aggregated if row['experiment']=='queue'],key=lambda row:row['configured_offered_rps'])
        table([['Offered<br/>req/s','ACK/s','E2E p95<br/>ms','Central Wq<br/>ms','Central Lq','Success<br/>%']]+[
            [number(row['configured_offered_rps'],1),number(row['completed_rps_mean'],1),number(row['p95_latency_ms_mean'],1),number(row['central_wait_mean_ms_mean'],2),number(row['central_Lq_mean'],2),number(100*(1-row['failure_fraction_mean']),2)] for row in qrows], [65,65,81,91,88,95])
        p('Values are means of the run statistics. Complete precision and minimum/maximum ranges are in tables/cell_summary.csv; raw per-run statistics are in tables/per_run.csv.')
        p('M/M/2 agreement at the central boundary','Heading2')
        table([['Offered<br/>req/s','Arrival<br/>CV','Service<br/>CV','Observed<br/>service ms','Measured<br/>Wq ms','Pilot model<br/>Wq ms']]+[
            [number(row['configured_offered_rps'],1),number(row['central_arrival_cv_mean'],2),number(row['central_service_cv_mean'],2),number(row['central_service_mean_ms_mean'],2),number(row['central_wait_mean_ms_mean'],2),number(row['pilot_mmc_Wq_s_mean']*1000 if row['pilot_mmc_Wq_s_n']==row['repetitions'] else None,2)] for row in qrows], [65,65,65,102,104,104])
        p('A dash means no finite stationary prediction or no valid statistic. Per-run relative errors are in tables/theory_agreement.csv. Because near-capacity formulae are nonlinear, model means in this table average per-run predictions; they are not computed from pooled service samples.')
        story.append(PageBreak())
    if h:
        p('HTTP transport study: numerical results','Heading1')
        ordered=sorted([row for row in aggregated if row['experiment']=='protocol'],key=lambda row:(PROFILE_ORDER.index(row['profile']) if row['profile'] in PROFILE_ORDER else 99,PROTOCOLS.index(row['protocol'])))
        table([['Profile','HTTP','ACK/s','Goodput<br/>Mbit/s','p95<br/>ms','Failure<br/>%']]+[
            [PROFILE_LABELS.get(row['profile'],row['profile']),PROTOCOL_LABELS[row['protocol']],number(row['completed_rps_mean'],1),number(row['payload_goodput_mbit_s_mean'],2),number(row['p95_latency_ms_mean'],1),number(100*row['failure_fraction_mean'],2)] for row in ordered], [135,60,60,82,80,68])
        p('Successful-request latency and failure fraction are shown together to expose timeout censoring. profiles.csv records the complete resolved impairment settings; per_robot_class.csv separates message types and clients.')
        story.append(PageBreak())
        p('HTTP transport study: where requests wait','Heading1')
        table([['Profile','HTTP','Gateway Lq<br/>sum of 4','Edge Lq<br/>sum of 2','Central Lq']]+[
            [PROFILE_LABELS.get(row['profile'],row['profile']),PROTOCOL_LABELS[row['protocol']],number(row['gateway_Lq_mean'],2),number(row['edge_Lq_mean'],2),number(row['central_Lq_mean'],2)] for row in ordered], [135,65,104,104,97])
        p('Each entry is the mean across repetitions of the time-mean number of requests waiting at the indicated application queues. In-service requests are excluded. A request blocked during a WAN exchange can occupy an in-service edge slot and cause queue growth upstream at its gateway. Thus central Lq alone does not describe network-induced backlog.')
        p('The bandwidth profiles constrain each site uplink to 2 Mbit/s while the video and telemetry publication schedule remains fixed. These queue locations, the goodput plot and source failure fractions identify where demand exceeds delivered capacity. This is a different bottleneck from the deliberately slow central workers in Q1.')
        p('The aggregate forward wire cap is 4 Mbit/s across the two sites. Base64 expands payload bytes by approximately 4/3 before JSON, HTTP, TLS and transport overhead. A useful-payload goodput near 2.7 Mbit/s is therefore consistent with the configured wire capacity; it is not a failure to reach a 2 Mbit/s aggregate target.')
        story.append(PageBreak())
    for index,fig in enumerate(figures,1):
        p(f'Figure {index}. {fig["title"]}','Heading1')
        story.append(Image(str(output/'figures'/(fig['name']+'.png')),width=505,height=505*(4.4/8 if fig['name'].startswith('q2') else 4.3/7.2)))
        p('<b>Produced from:</b> '+fig['method'],'CaptionBody')
        p('<b>Interpretation:</b> '+fig['interpretation'],'CaptionBody')
        story.append(PageBreak())
    p('Data validation and scope','Heading1')
    total_generated=sum(row['generated'] for row in flat)
    total_successful=sum(row['successful'] for row in flat)
    p(f'All {len(runs)} included runs passed independent identity, source/ACK, queue-event, worker-effect, transport-negotiation and cleanup checks. Measurement source cohorts contained {total_generated:,} generated messages, of which {total_successful:,} had a successful final acknowledgement. These are message counts, not independent statistical replicates.')
    p('Packet captures are checked for valid framing, captured-record counts and zero kernel capture drops. They are not claimed to be a complete decrypted HTTP/2 or QUIC stream reconstruction. Application SHA-256 and message identities provide end-to-end payload and processing correlation. The source logs, checks and SHA-256 manifest allow the plotted statistics to be reproduced.')
    if history['excluded_attempts']:
        p(f'{len(history["excluded_attempts"])} original structurally invalid collection attempt(s) are preserved outside the formal dataset in raw/excluded_attempts. They are excluded from all plotted statistics. A shutdown race admitted a request after a queue consumer stopped, leaving a pending item during teardown. The admission gate was corrected only after all robot terminal outcomes had been recorded; the failed case was repeated. The first 49 accepted runs precede this lifecycle-only correction and the final 11 follow it. Per-run source hashes and original invalidation records are retained in lifecycle_history.json and tables/source_versions.csv.')
    p('Successful source-to-ACK latency uses the gateway terminal timestamp after ACK parsing and bookkeeping. It is distinct from the central admission-to-terminal residence used for M/M/2 comparisons and from the fixed measurement-window ACK rate.')
    p('Results describe one-host emulation with Linux namespace routing, artificial netem impairments and these protocol libraries. Finite measurement windows, three repetitions, scheduler overhead, the fixed FIFO policy and short persistent-connection experiments limit generalization. Random packet loss is not identical to congestion loss, and JPEG frames are an application workload rather than a video codec comparison.')
    p('Primary references','Heading2')
    p('M. Thomson and C. Benfield, RFC 9113, HTTP/2 (2022). <link href="https://www.rfc-editor.org/rfc/rfc9113" color="blue">rfc-editor.org/rfc/rfc9113</link>')
    p('M. Bishop, RFC 9114, HTTP/3 (2022). <link href="https://www.rfc-editor.org/rfc/rfc9114" color="blue">rfc-editor.org/rfc/rfc9114</link>')
    p('J. Iyengar and M. Thomson, RFC 9000, QUIC: A UDP-Based Multiplexed and Secure Transport (2021). <link href="https://www.rfc-editor.org/rfc/rfc9000" color="blue">rfc-editor.org/rfc/rfc9000</link>')
    p('J. D. C. Little, A Proof for the Queuing Formula: L = lambda W, Operations Research 9(3), 383-387 (1961). <link href="https://doi.org/10.1287/opre.9.3.383" color="blue">doi.org/10.1287/opre.9.3.383</link>')
    p('S. B. Gershwin, MIT 2.854, Markov Processes and Queues (2016). <link href="https://live.ocw.mit.edu/courses/2-854-introduction-to-manufacturing-systems-fall-2016/5d3e9532c2ff6e156d48e41f9bd9576f_MIT2_854F16_Queueing.pdf" color="blue">MIT OpenCourseWare queueing lecture</link>.')
    def page(canvas,document):
        canvas.setFont('Helvetica',8)
        canvas.setFillColor(colors.HexColor('#667787'))
        canvas.drawString(45,25,'ROS 2 multi-site network | measured experiment results')
        canvas.drawRightString(550,25,str(document.page))
    doc.build(story,onFirstPage=page,onLaterPages=page)


def generate(data,output,expected_runs=60,allow_partial=False):
    data,output=Path(data).resolve(),Path(output).resolve()
    runs=read_runs(data,expected_runs,allow_partial)
    output.mkdir(parents=True,exist_ok=True)
    tables=output/'tables'
    tables.mkdir(exist_ok=True)
    flat=[flatten_run(run) for run in runs]
    aggregate=aggregate_rows(flat)
    write_csv(tables/'per_run.csv',flat)
    write_csv(tables/'cell_summary.csv',aggregate)
    compact_common=('repetitions','actual_offered_rps_mean','completed_rps_mean','p95_latency_ms_mean',
                    'payload_goodput_mbit_s_mean','failure_fraction_mean')
    write_csv(tables/'q1_summary.csv',[{key:row.get(key) for key in ('configured_offered_rps',
              'pilot_estimated_capacity_fraction_mean')+compact_common+('central_wait_mean_ms_mean',
              'central_Lq_mean','central_service_mean_ms_mean','pilot_mmc_Wq_s_mean','pilot_mmc_Lq_mean',
              'central_queue_growth_rps_mean')} for row in sorted(aggregate,key=lambda r:r.get('configured_offered_rps') or 0) if row['experiment']=='queue'])
    write_csv(tables/'q2_summary.csv',[{key:row.get(key) for key in ('protocol','profile')+compact_common+
              ('video_completed_rps_mean','video_p95_latency_ms_mean','odometry_p95_latency_ms_mean',
               'gateway_Lq_mean','edge_Lq_mean','central_Lq_mean')} for row in aggregate if row['experiment']=='protocol'])
    write_csv(tables/'queue_location.csv',[{key:value for key,value in row.items() if key in ('run_id','experiment','protocol','profile','repetition','configured_offered_rps','actual_offered_rps','completed_rps','gateway_Lq','edge_Lq','central_Lq','central_L')} for row in flat])
    write_csv(tables/'theory_agreement.csv',[{key:value for key,value in row.items() if key in ('run_id','configured_offered_rps','repetition','central_admission_rps','central_wait_mean_ms','central_Lq','central_service_mean_ms','central_arrival_cv','central_service_cv','max_exact_queue_residual_ns','max_exact_system_residual_ns') or key.startswith(('nominal_mmc_','calibrated_mmc_','pilot_mmc_','pilot_offered_mmc_'))} for row in flat if row['experiment']=='queue'])
    classes=[]
    sites=[]
    provenance=[]
    source_versions=[]
    for run in runs:
        c,s=run['config'],run['summary']
        meta=dict(run_id=c.get('run_id',run['folder'].name),experiment=s['experiment'],protocol=s['protocol'],profile=run['profile'],repetition=run['repetition'])
        with (run['folder']/'per_robot_class.csv').open(newline='',encoding='utf-8') as stream:
            classes.extend({**meta,**row} for row in csv.DictReader(stream))
        for site,metrics in s.get('per_site',{}).items():
            sites.append(dict(**meta,site=site,generated=metrics['generated'],successful=metrics['successful'],completed_rps=metrics.get('completed_rps'),payload_goodput_mbit_s=metrics.get('payload_goodput_mbit_s'),success_fraction=metrics['success_fraction'],p95_latency_ms=metrics['source_to_ack_ms']['p95']))
        provenance.append(dict(**meta,raw_path=str(run['folder']),resolved_sha256=hashlib.sha256((run['folder']/'resolved.json').read_bytes()).hexdigest(),summary_sha256=hashlib.sha256((run['folder']/'summary.json').read_bytes()).hexdigest()))
        if (run['folder']/'code_sha256.json').exists():
            hashes=read(run['folder']/'code_sha256.json')
            source_versions.append({**meta,**{name:hashes.get(name) for name in ('complex_experiments/components.py','complex_experiments/transport.py','complex_experiments/runner.py')}})
    write_csv(tables/'per_robot_class.csv',classes)
    write_csv(tables/'per_site.csv',sites)
    write_csv(tables/'profiles.csv',[dict(run_id=run['config'].get('run_id'),protocol=run['summary']['protocol'],profile=run['profile'],settings=json.dumps(run['config'].get('sites',run['config'].get('profile',{})),sort_keys=True)) for run in runs])
    (output/'provenance.json').write_text(json.dumps(dict(raw_root=str(data),run_count=len(runs),partial=allow_partial,runs=provenance),indent=2)+'\n',encoding='utf-8')
    write_csv(tables/'source_versions.csv',source_versions)
    excluded_root=data.parent/'excluded_attempts'
    history=dict(excluded_attempts=[],source_versions=source_versions)
    if excluded_root.exists():
        for path in sorted(excluded_root.rglob('invalid.json')):
            history['excluded_attempts'].append(dict(raw_path=str(path.parent),reason=read(path),invalid_record_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    if (data/'shutdown_patch_provenance.json').exists():
        history['shutdown_patch_provenance']=read(data/'shutdown_patch_provenance.json')
    version_counts=defaultdict(int)
    for row in source_versions:
        version_counts[row.get('complex_experiments/components.py')]+=1
    history['components_source_version_counts']=dict(version_counts)
    (output/'lifecycle_history.json').write_text(json.dumps(history,indent=2)+'\n',encoding='utf-8')
    figures=make_figures(runs,output)
    build_pdf(runs,flat,aggregate,figures,output,history)
    (output/'figure_catalog.json').write_text(json.dumps(figures,indent=2)+'\n',encoding='utf-8')
    lines=['# Measured ROS 2 multi-site network results','',f'{len(runs)} validated live runs.','',
           'All figures are provided individually as vector PDF and PNG. Error bars are replicate minimum/maximum ranges around the mean of per-run statistics. They are not confidence intervals.','',
           '## Figures','']
    for index,fig in enumerate(figures,1):
        lines.extend([f'{index}. [{fig["title"]}](figures/{fig["name"]}.pdf)',f'   {fig["interpretation"]}',''])
    lines.extend(['## Tables','',
                  '- `tables/per_run.csv`: measured run statistics, failure counts and SHA-256 provenance.',
                  '- `tables/cell_summary.csv`: means, minima, maxima and sample counts for every cell.',
                  '- `tables/theory_agreement.csv`: matched central-boundary model predictions and relative errors.',
                  '- `tables/per_robot_class.csv`: client and message-class counts, outcomes and latency.',
                  '- `tables/per_site.csv`: independent site-level delivery and latency.',
                  '- `tables/queue_location.csv`: per-run queue occupancy at gateways, edges and the central FIFO.',
                  '- `tables/source_versions.csv`: measured-run component and transport source hashes.',
                  '- `tables/profiles.csv`: resolved impairment settings.','',
                  '`provenance.json` identifies each raw run and its configuration/summary hashes. Missing or invalid runs are never filled with predictions.'])
    (output/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    manifest=[]
    for path in sorted(output.rglob('*')):
        if path.is_file() and path.name!='MANIFEST.sha256' and 'qa' not in path.parts:
            manifest.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.relative_to(output).as_posix())
    (output/'MANIFEST.sha256').write_text('\n'.join(manifest)+'\n',encoding='utf-8')
    return dict(runs=len(runs),figures=len(figures),output=str(output))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--expected-runs',type=int,default=60)
    parser.add_argument('--allow-partial',action='store_true',help='Only for pilot layout checks; provenance is explicitly marked partial.')
    args=parser.parse_args()
    print(json.dumps(generate(args.data,args.output,args.expected_runs,args.allow_partial),indent=2))


if __name__=='__main__':
    main()
