"""Verify annotation layers converge across writers, failures and restart.

Architecture: independent SDK processes -> duplicate/reordered events -> query,
primary resume and deferred-branch reduction. Main caller: core SDK test suite.
"""
import copy
import tempfile
import unittest
from unittest.mock import patch
import backprop
from backprop.apis.public.replay import reduce_events
from annotation_writers_fixture import fixture


class AnnotationWriters(unittest.TestCase):
    def test_independent_writers_keep_siblings_and_resolve_common_layer_in_event_order(self):
        data=fixture();events=data['events']
        state=reduce_events(list(reversed(events))+events)
        labels=state['config']['_wandb']['bounding_box/class_labels']
        self.assertEqual(state['config'],data['config']);self.assertEqual(state['state'],'finished')
        self.assertEqual(state['config']['_wandb']['keep'],'reserved')
        self.assertEqual({k:v['value'] for k,v in labels.items()},
            {'primary_wandb_delimeter_truth':{'7':'primary'},'secondary_wandb_delimeter_truth':{'7':'secondary'},
             'comparison_wandb_delimeter_truth':{'7':'secondary'}})
        sink=backprop.MemorySink();sink.events=list(reversed(events))+events
        api=backprop.Api._from_store(sink,entity='e',project='annotations')
        self.assertEqual(api.run('shared-annotations').rawconfig,state['config'])
        with tempfile.TemporaryDirectory() as directory:
            with backprop.Run(id='shared-annotations',entity='e',project='annotations',mode='shared',resume='must',
                    dir=directory,_sink=sink,_deployment={},settings={'silent':True,'console':'off','disable_git':True,
                        'x_disable_stats':True,'x_primary':True,'x_label':'primary'}) as run:
                run._add_singleton('bounding_box/class_labels','restart',{'8':'resumed primary'})
        api.flush()
        after=api.run('shared-annotations').rawconfig['_wandb']
        self.assertEqual(after['keep'],'reserved')
        for key,value in labels.items():self.assertEqual(after['bounding_box/class_labels'][key],value)
        self.assertEqual(after['bounding_box/class_labels']['restart']['value'],{'8':'resumed primary'})
        from backprop.deferred_branch import _update
        restored={}
        for event in sorted(events,key=lambda e:(e['timestamp'],e['event_id'])):
            if event['type']=='config':_update(restored,event['payload'])
        self.assertEqual(restored['_wandb'],state['config']['_wandb'])

    def test_failed_append_does_not_publish_local_label_state(self):
        with tempfile.TemporaryDirectory() as directory:
            with backprop.Run(id='atomic-label',project='annotations',dir=directory,_sink=backprop.MemorySink(),_deployment={},
                    settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}) as run:
                before=copy.deepcopy(run.config._data)
                with patch.object(run._journal,'append',side_effect=OSError('journal unavailable')):
                    with self.assertRaises(OSError):run._add_singleton('mask/class_labels','first',{0:'background'})
                self.assertEqual(run.config._data,before)
                run._add_singleton('mask/class_labels','first',{0:'background'})
                self.assertEqual(run.config._data['_wandb']['mask/class_labels']['first']['value'],{'0':'background'})


if __name__=='__main__':unittest.main()
