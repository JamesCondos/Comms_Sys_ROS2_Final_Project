"""Mixed load must scale actual topic rates while preserving old workloads."""
import math
import unittest

from complex_experiments.components import telemetry_routes, traffic_streams
from complex_experiments.runner import validate

BASE=dict(protocol='h2',experiment='protocol',warmup_s=10,duration_s=45,drain_s=30,
          request_timeout_s=30,mean_service_s=.02,service_mode='exponential',queue_capacity=2000,
          gateway_concurrency=512,edge_concurrency=1024,video_enabled=True,video_fps=5.)


class MixedTrafficTests(unittest.TestCase):
    def test_uniform_scale_keeps_topic_mix_and_payload_sizes(self):
        base=traffic_streams(BASE)
        for scale in (.1,.5,1.,1.3):
            config=dict(BASE,traffic_mode='mixed_load',traffic_scale=scale,separate_telemetry_topics=True)
            validate(config)
            streams=traffic_streams(config)
            self.assertEqual([(k,s) for k,r,s in streams],[(k,s) for k,r,s in base])
            self.assertAlmostEqual(sum(rate for _,rate,_ in streams)*4,148*scale)
            self.assertEqual([r for _,r,_ in streams],[r*scale for _,r,_ in base])

    def test_actual_telemetry_topics_are_distinct_per_robot(self):
        config=dict(BASE,separate_telemetry_topics=True)
        routes=telemetry_routes(config,'robot3')
        self.assertEqual(routes,dict(command='/robot3/command',status='/robot3/status',
            odometry='/robot3/odometry',sensor='/robot3/sensor'))
        self.assertEqual(len(set(routes.values())),4)

    def test_legacy_shared_topic_and_probe_are_preserved(self):
        self.assertEqual(set(telemetry_routes(BASE,'robot1').values()),{'/robot1/messages'})
        self.assertEqual(traffic_streams(dict(BASE,experiment='queue',offered_rps_total=80)),[('queue_probe',20.,256)])
        self.assertEqual(sum(rate for _,rate,_ in traffic_streams(BASE)),37.)

    def test_invalid_or_incomplete_mixed_configs_rejected(self):
        config=dict(BASE,traffic_mode='mixed_load',traffic_scale=.5,separate_telemetry_topics=True)
        for override in (dict(traffic_scale=0),dict(traffic_scale=True),dict(traffic_scale=math.inf),
                         dict(video_enabled=False),dict(experiment='queue',offered_rps_total=50),
                         dict(separate_telemetry_topics=False)):
            with self.assertRaises(ValueError):validate(dict(config,**override))


if __name__=='__main__':unittest.main()
