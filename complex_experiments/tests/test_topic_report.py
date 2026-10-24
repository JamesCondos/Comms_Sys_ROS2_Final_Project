import unittest

from complex_experiments.topic_report import aggregate,figure_tex,RATES


class TopicReportTests(unittest.TestCase):
    def test_uniform_rate_scale_includes_video(self):
        self.assertEqual(4*sum(RATES.values()),148)
        self.assertEqual(4*sum(v*.35 for v in RATES.values()),51.8)

    def test_per_run_percentiles_are_not_pooled(self):
        values=[dict(load=1,topic='video',repetition=i,latency_p95=value,undefined=None)
                for i,value in enumerate((10,20,90),1)]
        row=aggregate(values,('load','topic'))[0]
        self.assertEqual(row['latency_p95_mean'],40)
        self.assertEqual(row['latency_p95_minimum'],10)
        self.assertEqual(row['latency_p95_maximum'],90)
        self.assertIsNone(row['undefined_mean'])

    def test_figures_use_overridable_relative_asset_macro(self):
        value=figure_tex('video_latency','A caption.','video')
        self.assertIn(r'\QOneAssetPath video_latency.pdf',value)
        self.assertNotIn('documentclass',value)
        self.assertNotIn('C:',value)


if __name__=='__main__':
    unittest.main()
