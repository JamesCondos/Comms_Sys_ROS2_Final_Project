"""Freeze the mixed-topic Q1 study from its own low-load mixed-traffic pilot."""
import argparse
import json
from pathlib import Path
import random


def base_config():
    return dict(protocol='h2',experiment='protocol',traffic_mode='mixed_load',traffic_scale=.35,
        separate_telemetry_topics=True,robots=[dict(id=f'robot{i}',site='A' if i<=2 else 'B') for i in range(1,5)],
        workers=2,queue_capacity=2000,capture=True,video_enabled=True,video_fps=5.,
        mean_service_s=.02,service_mode='exponential',gateway_concurrency=512,edge_concurrency=1024,
        warmup_s=10.,duration_s=45.,drain_s=30.,request_timeout_s=30.,
        sites={site:dict(forward={},reverse={}) for site in ('A','B')},
        seed=610613101,profile_name='clean_mixed_load',repetition=1)


def matrix(pilot=False,calibration=None):
    base=base_config()
    if pilot:
        return dict(purpose='Mixed-topic pilot, excluded from formal results',base=base,
            runs=[dict(name='mixed_calibration',duration_s=60.,nominal_offered_rps=51.8)])
    if calibration is None:raise ValueError('Mixed formal matrix requires its own valid pilot')
    summary=json.loads(Path(calibration).read_text())
    if not summary['accounting_passed']:raise ValueError('Invalid mixed pilot')
    mean=summary['central']['service_ms']['mean']/1000
    capacity=2/mean
    rows=[]
    for repetition in (1,2,3):
        for fraction in (.2,.5,.75,.9,1.1):
            offered=round(capacity*fraction,2)
            rows.append(dict(name=f'mixed_load{fraction:.2f}_r{repetition:02}'.replace('.','p'),
                traffic_scale=offered/148.,nominal_offered_rps=offered,
                calibration_load_fraction=fraction,calibrated_capacity_rps=capacity,
                repetition=repetition,seed=610613100+repetition))
    random.Random(61061320).shuffle(rows)
    return dict(purpose='Actual mixed ROS-topic load sweep; independent phases; finite-window queue comparison',
        baseline_total_rps=148.,pilot_capacity_rps=capacity,pilot_mean_central_service_s=mean,
        calibration_source=str(Path(calibration).resolve()),base=base,runs=rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--pilot',action='store_true')
    parser.add_argument('--calibration',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(matrix(args.pilot,args.calibration),indent=2)+'\n')
