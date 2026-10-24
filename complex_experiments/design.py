"""Freeze matched, reproducibly shuffled experiment matrices after a separate pilot."""
import argparse
import json
from pathlib import Path
import random


def sites(delay=0, loss=0, rate=None):
    return {site:{'forward':{'delay_ms':delay,'loss_pct':loss,'rate_mbit':rate,'limit_packets':1000},
                  'reverse':{'delay_ms':delay,'loss_pct':loss,'rate_mbit':None,'limit_packets':1000}}
            for site in ('A','B')}


PROFILES = {'clean':sites(), 'delay_30ms':sites(delay=30), 'loss_1pct':sites(loss=1),
            'bandwidth_2mbit':sites(rate=2), 'combined':sites(delay=30,loss=1,rate=2)}


def matrix(pilot=False, calibration=None):
    base={'robots':[{'id':f'robot{i}','site':'A' if i<=2 else 'B'} for i in range(1,5)],
          'workers':2,'queue_capacity':2000,'capture':True,'video_fps':5.,
          'protocol':'h2','experiment':'queue','mean_service_s':.02,'q_mean_service_s':.02,
          'service_mode':'exponential','offered_rps_total':70.,'video_enabled':False,
          'gateway_concurrency':512,'edge_concurrency':1024,
          'warmup_s':10.,'duration_s':45.,'drain_s':30.,'request_timeout_s':30.,
          'seed':610613001,'sites':sites(),'profile_name':'clean','repetition':1}
    if pilot:
        runs=[{'name':'queue_calibration','warmup_s':10.,'duration_s':60.}]
        for protocol in ('h1','h2','h3'):
            for profile in ('clean','combined'):
                runs.append({'name':f'pilot_{protocol}_{profile}', 'experiment':'protocol',
                  'protocol':protocol,'profile_name':profile,'sites':PROFILES[profile],
                  'mean_service_s':.004,'service_mode':'fixed','video_enabled':True,
                  'gateway_concurrency':8,'edge_concurrency':32,'request_timeout_s':8.,
                  'warmup_s':2.,'duration_s':5.,'drain_s':15.})
        return {'purpose':'Separate feasibility and rate calibration; excluded from formal results',
                'base':base,'runs':runs}
    if not calibration:
        raise ValueError('Formal matrix requires the independently validated queue pilot summary')
    evidence=json.loads(Path(calibration).read_text())
    if not evidence['accounting_passed']:
        raise ValueError('Cannot calibrate from an invalid pilot')
    mean_s=evidence['central']['service_ms']['mean']/1000
    capacity=2/mean_s
    queue_runs=[]
    for repetition in (1,2,3):
        for fraction in (.2,.5,.75,.9,1.1):
            rate=round(capacity*fraction,2)
            queue_runs.append({'name':f'queue_rho{fraction:.2f}_r{repetition:02}'.replace('.', 'p'),
              'offered_rps_total':rate,'calibration_load_fraction':fraction,
              'calibrated_capacity_rps':capacity,'repetition':repetition,
              'seed':610613000+repetition})
    protocol_runs=[]
    for repetition in (1,2,3):
        for protocol in ('h1','h2','h3'):
            for profile,network in PROFILES.items():
                protocol_runs.append({'name':f'protocol_{protocol}_{profile}_r{repetition:02}',
                  'experiment':'protocol','protocol':protocol,'profile_name':profile,'sites':network,
                  'mean_service_s':.004,'service_mode':'fixed','video_enabled':True,
                  'gateway_concurrency':8,'edge_concurrency':32,'request_timeout_s':8.,
                  'warmup_s':5.,'duration_s':20.,'drain_s':15.,'repetition':repetition,
                  'seed':610613000+repetition})
    random.Random(61061310).shuffle(queue_runs)
    random.Random(61061311).shuffle(protocol_runs)
    return {'purpose':'Live two-site load/queue study and matched HTTP protocol comparison',
       'pilot_capacity_rps':capacity,'pilot_mean_central_service_s':mean_s,
       'calibration_source':str(Path(calibration).resolve()),
       'base':base,'runs':queue_runs+protocol_runs}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--pilot',action='store_true')
    parser.add_argument('--calibration',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():raise FileExistsError(args.output)
    args.output.write_text(json.dumps(matrix(args.pilot,args.calibration),indent=2)+'\n')
