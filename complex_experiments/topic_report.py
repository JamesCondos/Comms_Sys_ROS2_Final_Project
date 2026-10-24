"""Standalone mixed-topic figures, CSV tables and embeddable Q1 LaTeX sections.

The earlier Poisson probe is retained as a separate controlled model test. This
module never creates a PDF report or substitutes theory for missing observations.
"""
from collections import defaultdict
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import statistics

from .report import distribution, get, number, read, write_csv
from .theory import mmc

TOPICS=('command','status','odometry','sensor','video')
LABELS=dict(command='Command',status='Status',odometry='Odometry',sensor='Sensor data',video='Video')
RATES=dict(command=2.,status=5.,odometry=20.,sensor=5.,video=5.)
PAYLOADS=dict(command=100,status=100,odometry=250,sensor=20000)
COLORS=dict(command='#CC79A7',status='#E69F00',odometry='#0072B2',sensor='#009E73',video='#D55E00')
MARKERS=dict(command='D',status='v',odometry='o',sensor='s',video='^')
STAGES=('gateway','edge','central')
STAGE_LABELS=dict(gateway='Four robot gateways',edge='Two site edges',central='Central shared FIFO')
STAGE_COLORS=dict(gateway='#0072B2',edge='#009E73',central='#D55E00')


def read_csv(path):
    with Path(path).open(newline='',encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def collect_runs(data,allow_partial=False):
    runs=[]
    for path in sorted(Path(data).rglob('topic_analysis.json')):
        folder=path.parent
        config=read(folder/'resolved.json')
        if config.get('traffic_mode')!='mixed_load':
            continue
        if config.get('protocol')!='h2' or config.get('workers')!=2 or not config.get('video_enabled') or not config.get('separate_telemetry_topics'):
            raise ValueError('Mixed study controls do not match: '+str(folder))
        topics,base=read(path),read(folder/'summary.json')
        if not topics.get('accounting_passed') or not base.get('accounting_passed') or not read(folder/'validation.json').get('passed'):
            raise ValueError('Invalid mixed run: '+str(folder))
        if set(topics['per_topic'])!=set(TOPICS):
            raise ValueError('Incomplete actual topic population: '+str(folder))
        runs.append(dict(folder=folder,config=config,topics=topics,base=base,
                         offered=config['nominal_offered_rps'],repetition=config['repetition']))
    keys=[(r['offered'],r['repetition']) for r in runs]
    if len(keys)!=len(set(keys)):
        raise ValueError('Duplicate mixed-load/repetition cell')
    if not allow_partial:
        cells=defaultdict(set)
        for offered,rep in keys:cells[offered].add(rep)
        if len(runs)!=15 or len(cells)!=5 or any(reps!={1,2,3} for reps in cells.values()):
            raise ValueError('Expected five mixed loads with three validated repetitions each')
    if not runs:raise ValueError('No validated mixed-topic data')
    return runs


def topic_flatten(run,kind):
    t,c=run['topics']['per_topic'][kind],run['config']
    row=dict(run_id=c.get('run_id',run['folder'].name),topic=kind,
        configured_total_rps=run['offered'],pilot_estimated_capacity_fraction=c['calibration_load_fraction'],
        repetition=run['repetition'],traffic_scale=c['traffic_scale'],
        configured_rate_per_robot_hz=RATES[kind]*c['traffic_scale'],
        configured_network_topic_rps=RATES[kind]*c['traffic_scale']*len(c['robots']),
        generated=t['generated'],successful=t['successful'],success_fraction=t['success_fraction'],
        observed_source_rate_rps=t['offered_rps'],ack_window_rate_rps=t['completed_rps'],
        offered_useful_mbit_s=t['offered_payload_mbit_s'],ack_window_useful_mbit_s=t['payload_goodput_mbit_s'],
        source_to_ack_mean_ms=t['source_to_ack_ms']['mean'],source_to_ack_p50_ms=t['source_to_ack_ms']['p50'],
        source_to_ack_p95_ms=t['source_to_ack_ms']['p95'],source_to_ack_p99_ms=t['source_to_ack_ms']['p99'],
        worker_service_mean_ms=get(t,'worker_service_ms.mean'),worker_service_cv=t.get('worker_service_cv'),
        outcomes=json.dumps(t['outcomes'],sort_keys=True))
    for stage in STAGES:
        q=t['stages'][stage]
        row.update({stage+'_mean_wait_to_service_ms':q['wait_to_service_ms']['mean'],
                    stage+'_p95_wait_to_service_ms':q['wait_to_service_ms']['p95'],
                    stage+'_mean_wait_to_queue_exit_ms':q['wait_to_queue_exit_ms']['mean'],
                    stage+'_dispatch_service_mean_ms':get(q,'service_ms.mean'),
                    stage+'_Lq_contribution':q['mean_Lq_contribution'],
                    stage+'_waiting_aborted':q['waiting_aborted'],stage+'_started':q['started'],
                    stage+'_never_admitted':q['never_admitted'],stage+'_queue_area_ns':q['queue_area_ns']})
    return row


def aggregate(rows,key_fields):
    groups=defaultdict(list)
    for row in rows:groups[tuple(row[k] for k in key_fields)].append(row)
    result=[]
    for key,sample in sorted(groups.items(),key=lambda p:str(p[0])):
        cell=dict(zip(key_fields,key))
        cell['repetitions']=len(sample)
        for name in sample[0]:
            if name in key_fields or name in ('run_id','repetition','outcomes'):continue
            if all(r[name] is None or isinstance(r[name],(int,float)) for r in sample):
                cell.update({name+'_'+stat:value for stat,value in distribution([r[name] for r in sample]).items()})
        result.append(cell)
    return result


def make_tables(runs,output):
    tables=output/'tables';tables.mkdir(parents=True,exist_ok=True)
    rows=[topic_flatten(r,k) for r in runs for k in TOPICS]
    topic_cells=aggregate(rows,('configured_total_rps','topic'))
    write_csv(tables/'mixed_per_run_topic.csv',rows)
    write_csv(tables/'mixed_topic_summary.csv',topic_cells)
    run_rows=[]
    rates=[]
    for r in runs:
        c,b,t=r['config'],r['base'],r['topics']
        central=b['central']
        models=dict(pilot_actual=mmc(central['admission_rate_per_s'],c['calibrated_capacity_rps']/2,2),
                    pilot_configured=mmc(r['offered'],c['calibrated_capacity_rps']/2,2))
        row=dict(run_id=c.get('run_id'),configured_total_rps=r['offered'],
            pilot_estimated_capacity_fraction=c['calibration_load_fraction'],repetition=r['repetition'],
            actual_source_rate_rps=b['offered_rps'],ack_window_rate_rps=b['completed_rps'],
            generated=b['generated'],successful=b['successful'],success_fraction=b['success_fraction'],
            source_to_ack_p95_ms=b['source_to_ack_ms']['p95'],
            central_admission_rate_rps=central['admission_rate_per_s'],
            central_admission_cohort_wait_mean_ms=central['queue_wait_ms']['mean'],
            central_service_mean_ms=central['service_ms']['mean'],arrival_cv=central['arrival_diagnostics']['spacing_cv'],
            frozen_pilot_service_mean_ms=2000/c['calibrated_capacity_rps'],
            service_cv=central['service_cv'],ack_window_useful_mbit_s=b['payload_goodput_mbit_s'])
        for stage in STAGES:row[stage+'_Lq']=t['stage_groups'][stage]['mean_Lq']
        for name,model in models.items():
            row[name+'_rho']=model['rho'];row[name+'_stable']=model['stable']
            row[name+'_Wq_ms']=model['Wq_s']*1000 if model['stable'] else None
            row[name+'_Lq']=model['Lq']
        run_rows.append(row)
    cells=aggregate(run_rows,('configured_total_rps',))
    write_csv(tables/'mixed_per_run.csv',run_rows)
    write_csv(tables/'mixed_load_summary.csv',cells)
    reference_fields=('run_id','configured_total_rps','repetition','central_admission_rate_rps',
        'central_admission_cohort_wait_mean_ms','central_service_mean_ms','arrival_cv','service_cv',
        'frozen_pilot_service_mean_ms','pilot_actual_rho','pilot_actual_stable','pilot_actual_Wq_ms',
        'pilot_actual_Lq','pilot_configured_Wq_ms','pilot_configured_Lq')
    write_csv(tables/'mixed_poisson_reference.csv',[{k:r[k] for k in reference_fields} for r in run_rows])
    for offered in sorted({r['offered'] for r in runs}):
        r=next(r for r in runs if r['offered']==offered)
        jpeg=statistics.mean(read(r['folder']/'logs'/robot['id']/'ready.json')['jpeg_mean_bytes'] for robot in r['config']['robots'])
        for kind in TOPICS:
            rates.append(dict(configured_total_rps=offered,
                pilot_estimated_capacity_fraction=r['config']['calibration_load_fraction'],traffic_scale=r['config']['traffic_scale'],
                topic=kind,baseline_hz_per_robot=RATES[kind],
                actual_configured_hz_per_robot=RATES[kind]*r['config']['traffic_scale'],
                actual_configured_network_rps=4*RATES[kind]*r['config']['traffic_scale'],
                mean_useful_payload_bytes=jpeg if kind=='video' else PAYLOADS[kind],
                ros_message_type='sensor_msgs/CompressedImage' if kind=='video' else 'std_msgs/String'))
    write_csv(tables/'mixed_configured_topic_rates.csv',rates)
    robot_rows=[]
    for r in runs:
        robot_rows.extend(dict(configured_total_rps=r['offered'],repetition=r['repetition'],**row)
                          for row in read_csv(r['folder']/'per_robot_class.csv'))
    write_csv(tables/'mixed_per_robot_topic.csv',robot_rows)
    return rows,topic_cells,run_rows,cells,rates


def make_figures(runs,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,
        'axes.spines.right':False,'axes.grid':True,'grid.alpha':.18,'savefig.dpi':180,'pdf.fonttype':42})
    directory=output/'figures';directory.mkdir(exist_ok=True)
    groups=defaultdict(list)
    for r in runs:groups[r['offered']].append(r)
    xs=sorted(groups)
    figures=[]
    footer='Four robots; five mixed topics including JPEG video; exponential 20 ms worker holds. Means and replicate min-max ranges.'
    def add(name,title,ylabel,series,method,interpretation,log=False,curve=False):
        fig,ax=plt.subplots(figsize=(7.4,4.5))
        any_zero=False
        for label,fn,color,marker,style in series:
            values=[]
            for x in xs:
                d=distribution([fn(r) for r in groups[x]])
                if d['n']:
                    values.append((x,d));any_zero|=d['minimum']<=0
            if values:
                ax.errorbar([x for x,d in values],[d['mean'] for x,d in values],
                    yerr=np.array([[d['mean']-d['minimum'],d['maximum']-d['mean']] for x,d in values]).T,
                    fmt=marker+style,label=label,color=color,capsize=3,markersize=5,linewidth=1.4)
        if curve:
            capacity=runs[0]['config']['calibrated_capacity_rps']
            grid=np.linspace(max(.001,min(xs)*.2),min(max(xs),capacity*.985),240)
            ax.plot(grid,[mmc(float(x),capacity/2,2)['Wq_s']*1000 for x in grid],
                '--',color='#666666',label='M/M/2 reference: frozen pilot service')
            ax.axvline(capacity,color='#777777',linestyle=':',label='Pilot-estimated capacity')
        if log:
            if any_zero:ax.set_yscale('symlog',linthresh=.1);ax.set_ylim(bottom=0)
            else:ax.set_yscale('log')
        else:ax.set_ylim(bottom=0)
        ax.set(xlabel='Total configured traffic across four robots (requests/s; all topics)',ylabel=ylabel,title=title)
        ax.legend(fontsize=8,ncol=2 if len(series)>3 else 1,loc='best')
        fig.tight_layout(rect=(0,.065,1,1),pad=1.1)
        fig.text(.5,.012,footer,ha='center',fontsize=7,color='#555555')
        fig.savefig(directory/(name+'.pdf'));fig.savefig(directory/(name+'.png'));plt.close(fig)
        figures.append(dict(name=name,title=title,method=method,interpretation=interpretation))
    def by_topic(path):
        return [(LABELS[k],lambda r,k=k:get(r['topics']['per_topic'][k],path),COLORS[k],MARKERS[k],'-') for k in TOPICS]
    repeat='Every marker is the mean of three independent per-run statistics; bars span their minimum and maximum, not a confidence interval. '
    add('mixed_throughput','Mixed traffic and delivered throughput','Requests/s',
        [('Generated (source window)',lambda r:r['base']['offered_rps'],'#555555','o','-'),
         ('Acknowledged (measurement window)',lambda r:r['base']['completed_rps'],'#0072B2','s','-'),
         ('Central admissions',lambda r:r['base']['central']['admission_rate_per_s'],'#D55E00','^','-')],
        repeat+'Source rates use publication timestamps; delivered throughput counts successful ACK timestamps inside the measurement, including warmup carry-in and excluding drain.',
        'An increasing gap between source and ACK rates reveals accumulation during the fixed observation. Successful recovery during drain is distinct from keeping up with live offered traffic.')
    add('mixed_latency_by_topic','Latency of each ROS topic in the shared workload','Successful source-to-ACK p95 latency (ms)',
        by_topic('source_to_ack_ms.p95'),
        repeat+'Each per-run p95 uses one message class among sources published inside the measurement window. Successful ACKs during drain are retained. No percentile is computed by pooling repetitions.',
        'Every class shares gateway, edge and central FIFOs with the other four classes. A point therefore measures that class under the full mixed workload, rather than an isolated single-topic test. The x coordinate is the total rate of all classes across all robots.',log=True)
    add('mixed_goodput_by_topic','Useful payload delivered by ROS topic','Acknowledged useful payload goodput (Mbit/s)',
        by_topic('payload_goodput_mbit_s'),
        repeat+'Only useful source bytes with successful ACK timestamps inside measurement are counted. JSON/base64, HTTP, TLS and transport bytes are excluded.',
        'Request rate and byte load describe different pressures. Sensor messages and JPEG frames can contribute most useful bytes even when odometry contributes the most requests.')
    add('mixed_central_wait_by_topic','Central FIFO waiting time by ROS topic','Mean wait until central service starts (ms)',
        [(LABELS[k],lambda r,k=k:get(r['topics']['per_topic'][k],'stages.central.wait_to_service_ms.mean'),COLORS[k],MARKERS[k],'-') for k in TOPICS],
        repeat+'Waiting begins at central admission and ends at service start, joined to each source-window message identity. Waiting-aborted jobs are counted separately and are excluded from wait-to-service means.',
        'These are class-conditioned observations of one shared FIFO, not five dedicated queues or priority classes. End-to-end latency additionally contains ROS delivery, other queues, transfers, service and acknowledgements.',log=True)
    add('mixed_queue_location','Where the mixed-traffic backlog accumulates','Time-mean requests waiting',
        [(STAGE_LABELS[g],lambda r,g=g:r['topics']['stage_groups'][g]['mean_Lq'],STAGE_COLORS[g],('o','^','s')[i],'-') for i,g in enumerate(STAGES)],
        repeat+'Queue sizes are exact clipped occupancy integrals over measurement, including carried-in and carried-out jobs, with in-service jobs excluded. Gateway and edge values sum four and two independent queues respectively.',
        'The location of waiting requests separates upstream transport or concurrency restrictions from central service contention. It does not infer queue size by multiplying a completed-message mean delay by a rate.',log=True)
    add('mixed_central_queue_composition','Which topics occupy the shared central queue','Time-mean central waiting requests of this class',
        [(LABELS[k],lambda r,k=k:r['topics']['per_topic'][k]['stages']['central']['mean_Lq_contribution'],COLORS[k],MARKERS[k],'-') for k in TOPICS],
        repeat+'Each class contributes its exact clipped waiting intervals to the occupancy of the single central FIFO. The five class contributions sum exactly to total central Lq. All carry-in and carry-out jobs are included, regardless of source cohort or eventual outcome.',
        'This is a decomposition of one shared queue, not five dedicated queues. Request frequency affects the number of waiting requests; payload bytes affect useful goodput. The earlier Poisson-probe figures remain the direct M/M/2 test.',log=True)
    for k in TOPICS:
        label=LABELS[k]
        add(k+'_latency',label+': latency within the mixed workload','Successful source-to-ACK latency (ms)',
            [('Median',lambda r,k=k:r['topics']['per_topic'][k]['source_to_ack_ms']['p50'],'#0072B2','o','-'),
             ('95th percentile',lambda r,k=k:r['topics']['per_topic'][k]['source_to_ack_ms']['p95'],'#D55E00','s','-')],
            repeat+'The source-time cohort contains this class only, with successful drain ACKs included; the other four classes remain active at every load.',
            'The gap between median and tail describes variability for this topic. A rising curve measures the effect of increasing the entire workload, not increasing this topic alone.',log=True)
        add(k+'_throughput',label+': offered and acknowledged request rate','Requests/s across four robots (this topic only)',
            [('Generated (source window)',lambda r,k=k:r['topics']['per_topic'][k]['offered_rps'],'#555555','o','-'),
             ('Acknowledged (measurement window)',lambda r,k=k:r['topics']['per_topic'][k]['completed_rps'],'#0072B2','s','-')],
            repeat+'Generated and delivered rates refer to this topic across all four robots. The x axis remains the configured total across all five topics. ACK-window counts include warmup carry-in and exclude drain.',
            'A delivered rate below its source rate indicates that this class accumulates or fails during the observation. Source-cohort success fractions in the tables distinguish delay from permanent loss.')
        add(k+'_stage_waiting',label+': waiting at each shared stage','Mean wait until service starts (ms)',
            [(STAGE_LABELS[g],lambda r,k=k,g=g:r['topics']['per_topic'][k]['stages'][g]['wait_to_service_ms']['mean'],STAGE_COLORS[g],('o','^','s')[i],'-') for i,g in enumerate(STAGES)],
            repeat+'For this topic, exact source identities join queue admission and service start at the gateway, edge and central FIFO. Means pool the indicated physical stage within a run; repetitions are then averaged.',
            'This decomposes waiting, rather than total residence. It excludes service and network exchange time; started-only wait means must be read with waiting-aborted counts when a source does not progress.',log=True)
    return figures


def copy_probe(probe,output):
    probe=Path(probe)
    figures=[]
    for source in sorted((probe/'figures').glob('q1_*.pdf')):
        for suffix in ('.pdf','.png'):
            shutil.copy2(source.with_suffix(suffix),output/'figures'/('probe_'+source.stem+suffix))
        figures.append('probe_'+source.stem)
    if len(figures)!=7:raise ValueError('Expected seven preserved controlled Poisson-probe figures')
    for source,name in [('q1_summary.csv','probe_load_summary.csv'),('theory_agreement.csv','probe_theory_agreement.csv')]:
        shutil.copy2(probe/'tables'/source,output/'tables'/name)
    write_csv(output/'tables'/'probe_per_run.csv',[r for r in read_csv(probe/'tables'/'per_run.csv') if r['experiment']=='queue'])
    return read_csv(probe/'tables'/'q1_summary.csv'),figures


def tex_number(v,d=2):
    return '--' if v is None else f'{float(v):.{d}f}'


def figure_tex(name,caption,label,placement='tbp'):
    return (r'\begin{figure}['+placement+']\n'+r'\centering'+'\n'+
            r'\includegraphics[width=\linewidth]{\QOneAssetPath '+name+'.pdf}'+'\n'+
            r'\caption{'+caption+'}\n'+r'\label{fig:q1-'+label+'}\n'+r'\end{figure}'+'\n\n')


def make_latex(runs,topic_cells,cells,rates,probe_rows,figures,output):
    directory=output/'latex';directory.mkdir(exist_ok=True)
    base=runs[0]['config']
    header=('% Embeddable section: no document class or standalone PDF.\n'
            '% Preamble: \\usepackage{graphicx,booktabs}; amsmath is optional.\n'
            '% Copy q1_topic_extension/ into the Overleaf project root.\n'
            '% Include setup first, then results; topic appendix is optional.\n'
            r'\providecommand{\QOneAssetPath}{q1_topic_extension/figures/}'+'\n\n')
    payload_by_kind={row['topic']:row['mean_useful_payload_bytes'] for row in rates}
    topic_names=runs[0]['topics'].get('actual_ros_topics',{})
    sample=topic_names.get('robot1',{})
    telemetry=sample.get('telemetry_topics') or {k:'/robot1/'+k for k in TOPICS if k!='video'}
    video_topic=sample.get('video_topic') or '/robot1/camera/image/compressed'
    setup=header+r'''\subsection{Experimental setup and measurement boundaries}
The network contains four independent robot clients, two site edges, two IP routers, one central load balancer with a shared FIFO, and two worker processes. Robots 1--2 use site A and robots 3--4 use site B. Components run in separate Linux network namespaces on one host. Each robot component contains two actual ROS 2 nodes, a source and a gateway, sharing local DDS. Only HTTPS crosses the robot--edge--proxy--worker path; the two site routers lie between the edges and central service. Workers are HTTP processes, and DDS does not cross the emulated WAN.

The first part of Q1 is a controlled Poisson-probe experiment: four independent sources offer fixed-size 256-byte messages, video is off, and each worker has an independent exponential processing hold with a configured mean of 20~ms. Its purpose is a matched-boundary M/M/2 test. The second part retains the topology and worker policy but publishes four telemetry topics and a compressed-image video topic concurrently. These periodic streams have independently seeded phases. The two workloads are reported separately and their message samples are never pooled. The mixed sweep changes all five rates together; it does not isolate a causal video-on/off effect, and differences from the probe also involve arrival pattern and payload size.

Each robot has one FIFO shared by all five topics. Each edge has another shared FIFO; two central service loops consume one shared FIFO, with each loop assigned to one worker. FIFO order is admission order, with no class or QoS priority. Robot gateways permit 512 outstanding application requests and edges permit 1024; these slots are not extra robot clients. Every queue has a 2000-waiting-request limit. The measured occupancy and overflow counts establish whether that finite limit matters; an infinite-buffer M/M/2 queue remains an ideal reference. Workers use the same artificial exponential processing hold for every class; the hold is a timer wait, not image perception, video decoding or a CPU benchmark.

\paragraph{Topics, payloads and video.}
The telemetry topics carry synthetic JSON messages, including upstream control-like command events rather than downlink actuation, as \texttt{std\_msgs/String}; the odometry-labelled workload is not a native \texttt{nav\_msgs/Odometry} message. Video is published as actual \texttt{sensor\_msgs/CompressedImage} messages containing a repeatable 20-frame synthetic test pattern (a yellow square moving across a textured grid, seed 61061), 320$\times$180 pixels at JPEG quality~80. Frames are generated and cached as independent JPEG images before the measurement. The sequence cycles at the scaled frame rate, so its loop duration is 20 divided by the actual video frames/s; it is not a fixed-duration camera recording. The gateway wraps useful payload bytes in JSON/base64 for HTTP, so the useful payload sizes below are smaller than the HTTP bodies and wire byte counts.

\begin{table}[tbp]
\centering
\caption{Baseline mixture per robot before applying the common traffic multiplier. Video frame size is the measured mean over the cached sequence.}
\label{tab:q1-topic-workload}
\resizebox{\linewidth}{!}{%
\begin{tabular}{lrrl}
\toprule
Topic & Baseline rate (Hz) & Useful bytes & ROS type \\
\midrule
'''
    for k in TOPICS:
        setup+=LABELS[k]+f' & {RATES[k]:g} & {payload_by_kind[k]:.0f} & '+('CompressedImage' if k=='video' else 'String')+r' \\'+'\n'
    setup+=r'''\bottomrule
\end{tabular}
}
\end{table}

For example, robot 1 publishes on '''+r'\texttt{\detokenize{'+str(telemetry.get('odometry'))+'}} and '+r'\texttt{\detokenize{'+str(video_topic)+r'}}. Analogous topic names are used by the other three robots.'+'\n\n'
    setup+=r'''\paragraph{Traffic scaling and message order.}
The baseline is a rate reference of 37 requests/s per robot, including five video frames/s, hence 148 requests/s across the network; the unscaled $s=1$ operating point is not one of the tested loads. A common multiplier $s$ scales every topic rate: robot-level rates are $(2s,5s,20s,5s,5s)$ for command, status, odometry, sensor data and video respectively. Odometry is therefore not always 20~Hz, and video is not always 5~frames/s. The mixed workload is a proportional mixture at every load, not a change in robot count or a series of isolated topic tests.

A separate mixed-traffic pilot freezes the mean dispatch-to-worker response time and estimates aggregate capacity $\widehat C=2/\overline S$. Five loads target fractions 0.20, 0.50, 0.75, 0.90 and 1.10 of that estimate. The actual configured total is rounded to 0.01 requests/s and $s=\lambda_{\mathrm{configured}}/148$. These fractions are pilot-estimated capacity fractions, not exact observed utilizations. Table~\ref{tab:q1-scaled-rates} gives the actual rate of every topic at each load.

\begin{table*}[tbp]
\centering
\caption{Configured mixed-topic rates per robot. The total column includes all five topics across four robots. Rates are configured frequencies, not message counts averaged between robots.}
\label{tab:q1-scaled-rates}
\resizebox{\linewidth}{!}{%
\begin{tabular}{rrrrrrr}
\toprule
Pilot fraction & Total (req/s) & Command (Hz) & Status (Hz) & Odom. (Hz) & Sensor (Hz) & Video (fps) \\
\midrule
'''
    for offered in sorted({r['offered'] for r in runs}):
        cfg=next(r['config'] for r in runs if r['offered']==offered)
        setup+=' & '.join([tex_number(cfg['calibration_load_fraction'],2),tex_number(offered,2)]+[tex_number(RATES[k]*cfg['traffic_scale'],3) for k in TOPICS])+r' \\'+'\n'
    setup+=r'''\bottomrule
\end{tabular}
}
\end{table*}

There is no repeated global message order or topic batch. Each periodic stream starts at an independent phase, then follows its configured period. Due messages are scheduled by time; cross-robot interleaving and ROS callback order also depend on runtime scheduling. A 20~Hz source period is 50~ms and does not imply a 50~ms network latency. Publishing continues independently of HTTP acknowledgements.

\paragraph{Procedure and statistics.}
Each load has three independent repetitions, with a 10~s warmup, 45~s observation and up to 30~s drain. Load order is reproducibly shuffled. All Q1 HTTP hops use persistent multiplexed HTTP/2 connections with TLS~1.3 and kernel TCP Cubic; the protocol is not varied within Q1. No artificial link delay, loss or bandwidth cap is added. The single host is WSL Linux. Local DDS is configured with RELIABLE, KEEP\_LAST QoS with depth 10000, independently of the 2000-request application waiting limits. Discovery and exact publication-to-callback identity reconciliation are independently checked. Protocol, FIFO policy, worker count, payload mixture and video sequence are held fixed while traffic increases. Any excluded structural failures, replacements and exact source versions are retained in the collection provenance.

Latency begins at source publication and ends at the gateway terminal timestamp after the worker ACK has been read and checked. For each topic and repetition, the median and 95th percentile use successful messages generated during the observation; their ACKs may arrive during drain. All generated messages remain in the success/failure denominator. A plotted p95 is the mean of three per-run p95 values, not a percentile over pooled runs. Error bars span the three replicate statistics' minimum and maximum and are not confidence intervals.

Throughput counts successful ACKs received inside the observation, including warmup carry-in and excluding drain completions. Useful goodput counts only those messages' source payload bytes. Queue waiting is admission to service start, joined by source identity; started-only means exclude jobs aborted before service, whose counts and observed waiting-to-exit are reported separately. A gateway or edge request already holding a service slot may await a downstream response; that time is in service, not in the local waiting queue. Queue length is a clipped time integral over all waiting jobs, including carry-in and carry-out, rather than only the successful latency cohort. Class occupancy contributions sum exactly to each shared stage's occupancy.

\paragraph{Queueing-theory scope.}
For an M/M/2 reference, $\rho=\lambda/(2\mu)$ and the stationary mean is finite only for $\rho<1$. The matched central system begins at admission and ends when the proxy receives the worker response; its service time includes HTTP exchange and software overhead as well as the configured hold. The controlled Poisson probe is the direct stochastic-model test. The independently phased periodic mixture is not Poisson, and payload-dependent overhead can alter service statistics, so its M/M/2 comparison is explicitly a reference. No finite stationary prediction is assigned to overload, and observed finite-test backlog growth is not a claim of unlimited growth in a finite buffer.
'''
    (directory/'q1_setup.tex').write_text(setup,encoding='utf-8')
    ordered=sorted(cells,key=lambda c:c['configured_total_rps'])
    lo,hi=ordered[0],ordered[-1]
    main=header+r'\subsection{Controlled Poisson probe: capacity and M/M/2 agreement}'+'\n'
    probe_rows=sorted(probe_rows,key=lambda r:float(r['configured_offered_rps']))
    pl,ph=probe_rows[0],probe_rows[-1]
    main+=(f'The controlled probe increases the configured total from {tex_number(pl["configured_offered_rps"])} to '
           f'{tex_number(ph["configured_offered_rps"])} requests/s. Mean measurement-window ACK throughput changes from '
           f'{tex_number(pl["completed_rps_mean"])} to {tex_number(ph["completed_rps_mean"])} requests/s, while '
           f'the mean per-run successful p95 latency changes from {tex_number(pl["p95_latency_ms_mean"])} to '
           f'{tex_number(ph["p95_latency_ms_mean"])}~ms. These measurements use the Poisson probe workload described above.\n\n')
    stable_probe=[r for r in probe_rows if r.get('pilot_mmc_Wq_s_mean')]
    if stable_probe:
        relative=[abs(float(r['central_wait_mean_ms_mean'])/(1000*float(r['pilot_mmc_Wq_s_mean']))-1)*100 for r in stable_probe]
        near=stable_probe[-1]
        main+=(f'Across the stable probe loads, the largest absolute relative difference between the reported mean central wait and the '
               f'frozen-pilot M/M/2 reference using actual central arrivals is {max(relative):.1f}\\%. '
               f'At {tex_number(near["configured_offered_rps"])} configured requests/s, the measured mean wait is '
               f'{tex_number(near["central_wait_mean_ms_mean"])}~ms and the model gives '
               f'{tex_number(1000*float(near["pilot_mmc_Wq_s_mean"]))}~ms. '
               'The overloaded point has no finite stationary prediction; successful drain recovery does not establish stability.\n\n')
    for name,caption in [('q1_throughput','Controlled Poisson probe: generated, central-admitted and acknowledged rates. Source rates and ACK-window throughput have different timestamp boundaries; overload drain recovery does not make the live throughput equal the offered rate.'),
                         ('q1_central_wait_theory','Controlled Poisson probe: central admission-cohort mean waiting against M/M/2. The dashed curve uses configured arrivals and the independently frozen pilot service mean; model markers use actual central arrivals with the same frozen service mean. Bars show replicate ranges. No finite steady-state prediction is plotted at overload.'),
                         ('q1_central_occupancy_theory','Controlled Poisson probe: exact time-mean central queue occupancy against M/M/2. Occupancy includes clipped carry-in and carry-out intervals; it is distinct from the completed source-time latency cohort.')]:
        main+=figure_tex('probe_'+name,caption,'probe-'+name)
    main+=r'\subsection{Mixed ROS topics: how traffic changes delivery and queueing}'+'\n'
    main+=(f'With all five topics active, total configured traffic increases from {tex_number(lo["configured_total_rps"])} to '
           f'{tex_number(hi["configured_total_rps"])} requests/s. The mean ACK-window rate changes from '
           f'{tex_number(lo["ack_window_rate_rps_mean"])} to {tex_number(hi["ack_window_rate_rps_mean"])} requests/s, '
           f'and the central time-mean queue changes from {tex_number(lo["central_Lq_mean"])} to '
           f'{tex_number(hi["central_Lq_mean"])} waiting requests. Each load changes the rates of all topics together; the robot count remains four.\n\n')
    captions={f['name']:f for f in figures}
    selected=('mixed_throughput','mixed_latency_by_topic','mixed_goodput_by_topic','mixed_central_wait_by_topic','mixed_queue_location')
    for name in selected:
        f=captions[name]
        main+=figure_tex(name,f['method']+' '+f['interpretation'],name.replace('_','-'))
        if name=='mixed_latency_by_topic':
            main+='At the lowest and highest mixed loads, the mean per-run successful p95 latencies are respectively '
            desc=[]
            for k in TOPICS:
                a=next(c for c in topic_cells if c['configured_total_rps']==lo['configured_total_rps'] and c['topic']==k)
                b=next(c for c in topic_cells if c['configured_total_rps']==hi['configured_total_rps'] and c['topic']==k)
                desc.append(LABELS[k].lower()+f' {tex_number(a["source_to_ack_p95_ms_mean"])} to {tex_number(b["source_to_ack_p95_ms_mean"])}~ms')
            main+='; '.join(desc)+'. These are observations under the same shared mixture, not five independent capacity tests.\n\n'
    main+=r'\paragraph{Reading the topic-specific results.}'+'\n'
    main+='The optional appendix gives each topic its own latency, offered-versus-delivered rate and stage-waiting figure. Exact configured per-robot rates, generated counts, failures, useful goodput and class occupancy contributions are retained in the CSV tables. Message count, byte throughput, latency and queue occupancy answer different questions and should be interpreted together. A higher request rate for odometry need not imply the largest byte load; large sensor payloads and JPEG frames can dominate useful bytes.\n'
    event_low=next(c for c in topic_cells if c['configured_total_rps']==lo['configured_total_rps'] and c['topic']=='command')
    main+=(f'At the lowest mixed load, command cohorts contain only '
           f'{tex_number(event_low["generated_minimum"],0)}--{tex_number(event_low["generated_maximum"],0)} messages per repetition across four robots. '
           'Its per-run p95 consequently depends on very few tail observations. Three-run ranges are descriptive; class-specific tail differences, particularly for rare events, should be treated as exploratory.\n')
    main+=r'''\paragraph{Quantitative Poisson reference for the mixed workload.}
Table~\ref{tab:q1-mixed-poisson-reference} compares the central admission-cohort wait with a counterfactual Poisson M/M/2 reference using actual central admission rate and the service mean frozen from the separate mixed pilot. These are matched central boundaries, not per-topic source-cohort waits. Arrival and service CVs and the observed central service mean show where the reference assumptions change. Any gap cannot be attributed entirely to periodic arrivals: phase interactions, service variation, software overhead and the finite observation also matter.

\begin{table*}[tbp]
\centering
\caption{Mixed workload: measured central waiting and counterfactual Poisson reference. Entries are means of per-run statistics; a dash indicates that not all three runs have a finite stationary reference. The controlled Poisson probe remains the direct model-validation experiment.}
\label{tab:q1-mixed-poisson-reference}
\resizebox{\linewidth}{!}{%
\begin{tabular}{rrrrrr}
\toprule
Total (req/s) & Arrival CV & Service CV & Observed service (ms) & Measured wait (ms) & Poisson reference wait (ms) \\
\midrule
'''
    for c in ordered:
        prediction=c['pilot_actual_Wq_ms_mean'] if c['pilot_actual_Wq_ms_n']==c['repetitions'] else None
        main+=' & '.join(tex_number(x) for x in (c['configured_total_rps'],c['arrival_cv_mean'],c['service_cv_mean'],
             c['central_service_mean_ms_mean'],c['central_admission_cohort_wait_mean_ms_mean'],prediction))+r' \\'+'\n'
    main+=r'''\bottomrule
\end{tabular}
}
\end{table*}
'''
    stable=[c for c in ordered if c['pilot_actual_Wq_ms_n']==c['repetitions'] and c['pilot_actual_Wq_ms_mean']]
    if stable:
        deficits=[100*(1-c['central_admission_cohort_wait_mean_ms_mean']/c['pilot_actual_Wq_ms_mean']) for c in stable]
        main+=(f'Below the pilot-estimated capacity, measured mixed-workload central waits are '
               f'{min(deficits):.1f}--{max(deficits):.1f}\\% lower than this counterfactual Poisson reference. '
               'This is materially poorer agreement than in the controlled Poisson probe. The observed arrival CVs are below one, '
               'and the service statistics and phase pattern also differ from the ideal assumptions. The measurements therefore '
               'support using the simple M/M/2 model for the controlled reference, while treating the mixed-topic case as a '
               'different arrival process rather than claiming one universal latency formula.\n')
    (directory/'q1_results.tex').write_text(main,encoding='utf-8')
    appendix=header+r'\subsection{Topic-specific mixed-workload figures}'+'\n'
    appendix+='All figures in this subsection retain the complete five-topic workload and four-client topology. Only the class selected for the plotted statistics changes. The x axis always denotes the configured total offered rate across all topics and clients.\n\n'
    for k in TOPICS:
        appendix+=r'\subsubsection{'+LABELS[k]+'}\n'
        for suffix in ('latency','throughput','stage_waiting'):
            f=captions[k+'_'+suffix]
            appendix+=figure_tex(f['name'],f['method']+' '+f['interpretation'],f['name'].replace('_','-'),placement='htbp')
        appendix+=r'\clearpage'+'\n'
    appendix+=r'\subsubsection{Class contributions to the shared central queue}'+'\n'
    f=captions['mixed_central_queue_composition']
    appendix+=figure_tex(f['name'],f['method']+' '+f['interpretation'],f['name'].replace('_','-'),placement='htbp')
    (directory/'q1_topic_appendix.tex').write_text(appendix,encoding='utf-8')
    (directory/'preamble.tex').write_text('% Add these packages to the existing document preamble.\n'+r'\usepackage{graphicx,booktabs}'+'\n',encoding='utf-8')
    (directory/'q1_section.tex').write_text('% In the existing document body, input this file after adding the preamble packages.\n'
        +r'\input{q1_topic_extension/latex/q1_setup}'+'\n'
        +r'\input{q1_topic_extension/latex/q1_results}'+'\n'
        +'% Optional appendix: \\input{q1_topic_extension/latex/q1_topic_appendix}\n',encoding='utf-8')


def generate(data,probe,output,allow_partial=False):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    runs=collect_runs(data,allow_partial)
    rows,topic_cells,run_rows,cells,rates=make_tables(runs,output)
    figures=make_figures(runs,output)
    probe_rows,probe_figures=copy_probe(probe,output)
    make_latex(runs,topic_cells,cells,rates,probe_rows,figures,output)
    (output/'figure_catalog.json').write_text(json.dumps(dict(mixed_figures=figures,controlled_probe_figures=probe_figures),indent=2)+'\n')
    provenance=dict(raw_mixed_root=str(Path(data).resolve()),controlled_probe_artifact_root=str(Path(probe).resolve()),
        mixed_runs=len(runs),partial=allow_partial,runs=[dict(run_id=r['config'].get('run_id'),raw_path=str(r['folder']),
        topic_analysis_sha256=hashlib.sha256((r['folder']/'topic_analysis.json').read_bytes()).hexdigest(),
        resolved_sha256=hashlib.sha256((r['folder']/'resolved.json').read_bytes()).hexdigest()) for r in runs])
    (output/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    paths=[p for p in sorted(output.rglob('*')) if p.is_file() and 'qa' not in p.relative_to(output).parts and p.name!='MANIFEST.sha256']
    (output/'MANIFEST.sha256').write_text('\n'.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(output).as_posix() for p in paths)+'\n')
    return dict(mixed_runs=len(runs),mixed_figures=len(figures),controlled_probe_figures=len(probe_figures),output=str(output))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mixed',required=True,type=Path)
    parser.add_argument('--probe',required=True,type=Path,help='Existing validated complex-network report artifact directory')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--allow-partial',action='store_true')
    args=parser.parse_args()
    print(json.dumps(generate(args.mixed,args.probe,args.output,args.allow_partial),indent=2))
