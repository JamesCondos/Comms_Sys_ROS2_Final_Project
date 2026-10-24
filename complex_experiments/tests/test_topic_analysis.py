import unittest

from complex_experiments.topic_analysis import TOPICS, audit_ros_routes, class_queue_metrics


class TopicAccountingTests(unittest.TestCase):
    def route_fixture(self):
        routes={kind:'/robot1/'+kind for kind in TOPICS}
        endpoint_rows=[]
        for kind,topic in routes.items():
            topic_type='sensor_msgs/msg/CompressedImage' if kind=='video' else 'std_msgs/msg/String'
            endpoint_rows.append(dict(topic_name=topic,publisher_count=1,subscription_count=1,
                publishers=[dict(node_name='source',topic_type=topic_type)],
                subscriptions=[dict(node_name='gateway',topic_type=topic_type)]))
        ready=dict(separate_telemetry_topics=True,telemetry_topics={kind:topic for kind,topic in routes.items() if kind!='video'},
            video_topic=routes['video'],ros_topics=endpoint_rows)
        sources=[dict(id=kind,message_type=kind,source_ns=0) for kind in TOPICS]
        callbacks=[dict(id=kind,message_type=kind,ros_topic=routes[kind],callback_ns=1) for kind in TOPICS]
        return ready,sources,callbacks

    def test_actual_distinct_dds_routes_and_types(self):
        ready,sources,callbacks=self.route_fixture()
        self.assertEqual(len(audit_ros_routes(ready,callbacks,sources)),5)

    def test_wrong_dds_callback_route_is_rejected(self):
        ready,sources,callbacks=self.route_fixture()
        callbacks[0]['ros_topic']=ready['video_topic']
        with self.assertRaisesRegex(ValueError,'wrong class/topic'):
            audit_ros_routes(ready,callbacks,sources)

    def test_video_string_type_is_rejected(self):
        ready,sources,callbacks=self.route_fixture()
        ready['ros_topics'][-1]['publishers'][0]['topic_type']='std_msgs/msg/String'
        with self.assertRaisesRegex(ValueError,'ROS type'):
            audit_ros_routes(ready,callbacks,sources)

    def test_duplicate_callback_is_rejected(self):
        ready,sources,callbacks=self.route_fixture()
        callbacks.append(callbacks[0])
        with self.assertRaisesRegex(ValueError,'exactly once'):
            audit_ros_routes(ready,callbacks,sources)

    def test_class_areas_include_carry_in_out_and_sum(self):
        source={'a':dict(message_type='odometry',source_ns=1),
                'b':dict(message_type='video',source_ns=12),
                'c':dict(message_type='video',source_ns=14)}
        jobs=[dict(id='a',admission_ns=5,service_start_ns=12,terminal_ns=15,outcome='success'),
              dict(id='b',admission_ns=12,service_start_ns=25,terminal_ns=30,outcome='success'),
              dict(id='c',admission_ns=14,service_start_ns=None,terminal_ns=18,outcome='drain_timeout')]
        result=class_queue_metrics(jobs,source,10,20,topics=['odometry','video'])
        self.assertEqual(result['odometry']['queue_area_ns'],2)
        self.assertEqual(result['video']['queue_area_ns'],12)
        self.assertEqual(sum(row['mean_Lq_contribution'] for row in result.values()),1.4)
        self.assertEqual(result['odometry']['source_cohort_jobs'],0)
        self.assertEqual(result['video']['waiting_aborted'],1)
        self.assertEqual(result['video']['wait_to_service_ms']['n'],1)
        self.assertEqual(result['video']['wait_to_queue_exit_ms']['n'],2)

    def test_non_admitted_drop_contributes_no_queue_area(self):
        source={'a':dict(message_type='command',source_ns=11)}
        jobs=[dict(id='a',admission_ns=None,service_start_ns=None,terminal_ns=12,outcome='queue_drop')]
        result=class_queue_metrics(jobs,source,10,20,topics=['command'])['command']
        self.assertEqual(result['queue_area_ns'],0)
        self.assertEqual(result['never_admitted'],1)
        self.assertEqual(result['admitted'],0)

    def test_unknown_source_identity_fails(self):
        with self.assertRaises(KeyError):
            class_queue_metrics([dict(id='unknown')],{},10,20,topics=['sensor'])

    def test_abort_after_measurement_is_clipped(self):
        source={'a':dict(message_type='sensor',source_ns=15)}
        jobs=[dict(id='a',admission_ns=16,service_start_ns=None,terminal_ns=40,outcome='drain_timeout')]
        result=class_queue_metrics(jobs,source,10,20,topics=['sensor'])['sensor']
        self.assertEqual(result['queue_area_ns'],4)
        self.assertEqual(result['wait_to_queue_exit_ms']['mean'],24/1e6)


if __name__=='__main__':unittest.main()
