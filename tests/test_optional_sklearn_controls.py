"""Verify every selected legacy sklearn control with numerical and UI oracles.

Architecture: real estimators/probabilities -> each native import -> independently
computed numerical expectations -> actual encoded chart specifications. Main
callers: local SDK checks and existing sklearn version-boundary CI jobs.
"""
import importlib
import importlib.util
import itertools
import unittest
from unittest.mock import patch

AVAILABLE=all(importlib.util.find_spec(name) for name in ['numpy','pandas','sklearn','scipy'])

@unittest.skipUnless(AVAILABLE,'optional sklearn numerical dependencies are unavailable')
class SklearnControls(unittest.TestCase):
    def modules(self):
        import backprop as w
        return [w.sklearn,importlib.import_module('backprop.integration.sklearn')]

    def chart(self,module,name,*args,**kwargs):
        import backprop as w
        with patch.object(w,'log') as logged:
            self.assertIsNone(getattr(module,name)(*args,**kwargs))
        self.assertEqual(logged.call_count,1)
        return next(iter(logged.call_args.args[0].values()))

    def curves(self):
        import numpy as np
        truth=np.array([0,1,2,0,1,2,0,1,2])
        scores=np.array([[.8,.1,.1],[.2,.6,.2],[.2,.3,.5],[.4,.5,.1],[.1,.8,.1],
                         [.1,.6,.3],[.7,.1,.2],[.3,.5,.2],[.1,.2,.7]])
        return truth,scores

    def test_roc_switches_class_selection_and_independent_auc(self):
        import numpy as np
        from sklearn import metrics
        import backprop as w
        truth,scores=self.curves();one_hot=truth[:,None]==np.arange(3)[None,:]
        baseline=w.plot.roc_curve(truth,scores)
        for module in self.modules():
            plain=self.chart(module,'plot_roc',truth,scores,plot_micro=False,plot_macro=False)
            self.assertEqual(plain.table.data,baseline.table.data)
            for micro,macro in itertools.product([False,True],repeat=2):
                chart=self.chart(module,'plot_roc',truth,scores,plot_micro=micro,plot_macro=macro,classes_to_plot=[1])
                grouped={str(row[0]) for row in chart.table.data}
                self.assertEqual(grouped,{'1'}|({'micro-average'} if micro else set())|({'macro-average'} if macro else set()))
                for name,average,enabled in [('micro-average','micro',micro),('macro-average','macro',macro)]:
                    if enabled:
                        rows=[row[1:] for row in chart.table.data if row[0]==name]
                        self.assertAlmostEqual(metrics.auc([v[0] for v in rows],[v[1] for v in rows]),
                            metrics.roc_auc_score(one_hot,scores,average=average),delta=.002)
            empty=self.chart(module,'plot_roc',truth,scores,classes_to_plot=[],plot_micro=False,plot_macro=False)
            self.assertEqual(empty.table.data,[])
            averages=self.chart(module,'plot_roc',truth,scores,classes_to_plot=[])
            self.assertEqual({row[0] for row in averages.table.data},{'micro-average','macro-average'})
            collision=self.chart(module,'plot_roc',truth,scores,labels=['micro-average','macro-average','third'])
            names={row[0] for row in collision.table.data}
            self.assertTrue({'micro-average (aggregate)','macro-average (aggregate)'}.issubset(names))
            for name in ['plot_micro','plot_macro']:
                with self.assertRaisesRegex(TypeError,name):self.chart(module,'plot_roc',truth,scores,**{name:'false'})

    def test_pr_micro_matches_pooled_thresholds_and_keeps_per_class_curves(self):
        import numpy as np
        from sklearn import metrics
        import backprop as w
        truth,scores=self.curves();one_hot=truth[:,None]==np.arange(3)[None,:]
        precision,recall,_=metrics.precision_recall_curve(one_hot.ravel(),scores.ravel())
        for module in self.modules():
            original=w.plot.pr_curve(truth,scores)
            plain=self.chart(module,'plot_precision_recall',truth,scores,plot_micro=False)
            self.assertEqual(plain.table.data,original.table.data)
            chart=self.chart(module,'plot_precision_recall',truth,scores)
            pooled=[row for row in chart.table.data if row[0]=='micro-average']
            self.assertEqual(chart.table.columns,['class','precision','recall'])
            np.testing.assert_allclose([row[1:] for row in pooled],np.round(np.column_stack([precision,recall]),3))
            self.assertEqual([row for row in chart.table.data if row[0]!='micro-average'],original.table.data)
            only=self.chart(module,'plot_precision_recall',truth,scores,classes_to_plot=[])
            self.assertTrue(all(row[0]=='micro-average' for row in only.table.data))
            with self.assertRaisesRegex(TypeError,'plot_micro'):
                self.chart(module,'plot_precision_recall',truth,scores,plot_micro=None)

    def test_curve_size_limit_preserves_requested_series_and_endpoint_coverage(self):
        import numpy as np
        import backprop as w
        truth,scores=self.curves()
        for module in self.modules():
            for name,flags,groups in [('plot_roc',{},5),('plot_precision_recall',{},4)]:
                full=self.chart(module,name,truth,scores,**flags)
                # A large pooled curve must not consume the whole table budget.
                with patch.object(w.Table,'MAX_ROWS',10),patch.object(w,'termwarn') as warning:
                    bounded=self.chart(module,name,truth,scores,**flags)
                self.assertEqual(len(bounded.table.data),10)
                names={row[0] for row in bounded.table.data}
                self.assertEqual(len(names),groups)
                self.assertTrue(all(isinstance(v,(int,np.integer)) for v in names if not isinstance(v,str)))
                warning.assert_called()
                for label in names:
                    coordinates=[row[1:] for row in bounded.table.data if row[0]==label]
                    original=[row[1:] for row in full.table.data if row[0]==label]
                    self.assertTrue(all(point in original for point in coordinates))
                    # Even sampling keeps both extremes for every displayed curve.
                    axis=0 if name=='plot_roc' else 1
                    self.assertEqual(min(p[axis] for p in coordinates),min(p[axis] for p in original))
                    self.assertEqual(max(p[axis] for p in coordinates),max(p[axis] for p in original))
                with patch.object(w.Table,'MAX_ROWS',groups-1),patch.object(w,'termwarn'):
                    with self.assertRaisesRegex(ValueError,'one point per requested curve'):
                        self.chart(module,name,truth,scores,**flags)

    def test_feature_limit_title_and_elbow_panel_reach_rendered_specifications(self):
        import numpy as np
        from sklearn.datasets import make_classification,make_blobs
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.cluster import KMeans
        from backprop.legacy_charts import rendering_chart
        x,y=make_classification(n_samples=80,n_features=6,random_state=5)
        model=RandomForestClassifier(n_estimators=3,random_state=6).fit(x,y)
        original=model.feature_importances_.copy()
        points,_=make_blobs(n_samples=30,centers=3,random_state=5)
        clusterer=KMeans(n_clusters=3,n_init=2,random_state=6).fit(points)
        for module in self.modules():
            for limit in [0,2,np.int64(3),None,50]:
                chart=self.chart(module,'plot_feature_importances',model,feature_names=list('abcdef'),
                                 title='Ranked features',max_num_features=limit)
                count=6 if limit is None else min(limit,6)
                self.assertEqual(len(chart.table.data),count)
                ordered=np.argsort(original)[::-1][:count]
                self.assertEqual([row[0] for row in chart.table.data],[list('abcdef')[i] for i in ordered])
                self.assertEqual(rendering_chart(chart,None).spec.custom_spec['title'],'Ranked features')
            untitled=self.chart(module,'plot_feature_importances',model,title=None)
            self.assertNotIn('title',rendering_chart(untitled,None).spec.custom_spec)
            for kwargs,error in [({'max_num_features':-1},ValueError),({'max_num_features':True},TypeError),
                                  ({'max_num_features':2.5},TypeError),({'title':[]},TypeError)]:
                with self.assertRaises(error):self.chart(module,'plot_feature_importances',model,**kwargs)
            np.testing.assert_array_equal(model.feature_importances_,original)
            for visible in [False,True]:
                chart=self.chart(module,'plot_elbow_curve',clusterer,points,cluster_ranges=[2,3],show_cluster_time=visible)
                spec=rendering_chart(chart,None).spec.custom_spec
                self.assertEqual('vconcat' in spec,visible)
                self.assertTrue(all(row[2]>=0 for row in chart.table.data))
                if visible:self.assertEqual(spec['vconcat'][1]['encoding']['y']['field'],'clustering_time')
            with self.assertRaisesRegex(TypeError,'show_cluster_time'):
                self.chart(module,'plot_elbow_curve',clusterer,points,cluster_ranges=[2],show_cluster_time='false')

    def test_cluster_names_ids_centers_metrics_and_non_kmeans_delegation(self):
        import numpy as np
        from sklearn.datasets import make_blobs
        from sklearn.cluster import KMeans,AgglomerativeClustering
        from sklearn.metrics import silhouette_score,silhouette_samples
        from scipy.sparse import csr_matrix
        x,_=make_blobs(n_samples=40,centers=3,random_state=7)
        model=KMeans(n_clusters=3,n_init=2,random_state=8).fit(x)
        for module in self.modules():
            for rename in [lambda v:v,lambda v:v*10+10,lambda v:'group-'+str(v)]:
                ids=np.array([rename(v) for v in model.labels_]);classes=np.unique(ids)
                mapping={value:'name-'+str(index) for index,value in enumerate(classes)}
                for labels in [mapping,list(mapping.values())]:
                    chart=self.chart(module,'plot_silhouette',model,csr_matrix(x),ids,labels=labels,metric='manhattan')
                    rows=chart.table.data
                    self.assertEqual([row[2] for row in rows],[mapping[v] for v in ids])
                    self.assertEqual(set(row[8] for row in rows),set(mapping.values()))
                    np.testing.assert_allclose([row[3:5] for row in rows],model.cluster_centers_[model.labels_,:2])
                    np.testing.assert_allclose([row[9] for row in rows],silhouette_score(x,ids,metric='manhattan'))
                    expected=silhouette_samples(x,ids,metric='manhattan')
                    for value,name in mapping.items():
                        np.testing.assert_allclose([row[7] for row in rows if row[8]==name],np.sort(expected[ids==value]))
            subset=model.labels_!=0
            names=['unused','North','South']
            chart=self.chart(module,'plot_silhouette',model,x[subset],model.labels_[subset],labels=names)
            self.assertEqual({row[2] for row in chart.table.data},{'North','South'})
            np.testing.assert_allclose([row[3:5] for row in chart.table.data],model.cluster_centers_[model.labels_[subset],:2])
            with self.assertRaisesRegex(TypeError,'kmeans'):
                self.chart(module,'plot_silhouette',model,x,model.labels_,kmeans='false')
            other=AgglomerativeClustering(n_clusters=3).fit(x)
            import backprop as w
            with patch.object(w,'log') as log:
                module.plot_clusterer(other,x,other.labels_,labels=['A','B','C'])
            chart=log.call_args.args[0]['silhouette_plot']
            self.assertEqual({row[2] for row in chart.table.data},{'A','B','C'})
            self.assertTrue(all(row[3:5]==[None,None] for row in chart.table.data))
            for labels in [['only-one'],{},'bad']:
                with self.assertRaises((ValueError,TypeError)):
                    self.chart(module,'plot_silhouette',model,x,model.labels_,labels=labels)
            with self.assertRaisesRegex(ValueError,'single fitted model center'):
                self.chart(module,'plot_silhouette',model,x,np.array(['a','b']*20))

    def test_binary_switch_controls_calibration_and_checks_before_logging(self):
        import backprop as w
        from sklearn.datasets import make_classification
        from sklearn.ensemble import RandomForestClassifier
        x,y=make_classification(n_samples=240,n_features=4,random_state=42)
        model=RandomForestClassifier(n_estimators=3,random_state=6).fit(x,y)
        args=(model,x,x,y,y,model.predict(x),model.predict_proba(x),['A','B'])
        for module in self.modules():
            for binary in [False,True]:
                with patch.object(w,'log') as log:
                    self.assertIsNone(module.plot_classifier(*args,is_binary=binary))
                keys={key for call in log.call_args_list for key in call.args[0]}
                self.assertEqual('calibration_curve' in keys,binary)
            invalid=list(args);invalid[3]=list(range(len(y)))
            with patch.object(w,'log') as log:
                with self.assertRaisesRegex(ValueError,'exactly two'):module.plot_classifier(*invalid,is_binary=True)
                log.assert_not_called()
            with self.assertRaisesRegex(TypeError,'is_binary'):module.plot_classifier(*args,is_binary='true')
