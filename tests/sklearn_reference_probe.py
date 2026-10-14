"""Compare sklearn helpers using actual estimators and pinned W&B processes.

Architecture: deterministic datasets and fresh fitted estimators -> public
helpers at each captured import -> actual chart/table values -> observations.
Main caller: optional sklearn contract tests. Only terminal output and run.log
are recorded instead of sent to W&B; numerical calculations execute unchanged.
Elapsed clustering times are validated and normalized, never compared as data.
"""
import contextlib
import dataclasses
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ.setdefault('OMP_NUM_THREADS','1')
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
import numpy as np
import pandas as pd
import sklearn
from sklearn.cluster import KMeans, AgglomerativeClustering
from sklearn.datasets import make_classification, make_regression, make_blobs
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.naive_bayes import MultinomialNB
from sklearn.model_selection import KFold
from scipy.sparse import csr_matrix
from joblib import parallel_backend

CONTROL_APIS={'plot_classifier','plot_clusterer','plot_silhouette','plot_elbow_curve','plot_feature_importances','plot_roc','plot_precision_recall'}
REFERENCE=len(sys.argv)>1 and sys.argv[1]=='reference'
if REFERENCE:
    import wandb as w
    assert w.__version__=='0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
ROOT=Path(__file__).resolve().parents[1]

def portable(value):
    if hasattr(value,'table') and hasattr(value,'spec'):
        spec=dataclasses.asdict(value.spec)
        # Backprop extends CustomChartSpec with an optional native definition.
        # None adds no rendering behavior; nonempty definitions remain visible.
        if spec.get('custom_spec',False) is None:spec.pop('custom_spec')
        result={'table':portable(value.table),'spec':portable(spec)}
        if hasattr(value,'_backprop_render_options'):result['render_options']=portable(value._backprop_render_options)
        return result
    if isinstance(value,w.Table):
        data=portable(value.data)
        if 'clustering_time' in value.columns:
            position=value.columns.index('clustering_time')
            for row in data:
                assert isinstance(row[position],(float,int)) and math.isfinite(row[position]) and row[position]>=0
                row[position]='<measured elapsed seconds>'
        return {'columns':portable(value.columns),'data':data}
    if isinstance(value,dict):return {str(k):portable(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [portable(v) for v in value]
    if isinstance(value,np.ndarray):return portable(value.tolist())
    if isinstance(value,np.generic):return portable(value.item())
    if isinstance(value,float) and not math.isfinite(value):return str(value)
    return value

def attempt(call):
    try:return {'value':portable(call())}
    except Exception as error:return {'error':type(error).__name__,'message':str(error)}

def classification(classes=2,features=4,kind='forest',fitted=True):
    x,y=make_classification(n_samples=240,n_features=features,n_informative=features,n_redundant=0,
        n_classes=classes,n_clusters_per_class=1,random_state=42)
    model=RandomForestClassifier(n_estimators=3,max_depth=3,random_state=5)
    if kind=='linear':model=LogisticRegression(random_state=5)
    if kind=='nb':x=np.abs(x);model=MultinomialNB()
    if fitted:model.fit(x,y)
    return model,x,y

def regression(features=4,fitted=True):
    x,y=make_regression(n_samples=60,n_features=features,noise=1,random_state=42)
    model=LinearRegression()
    if fitted:model.fit(x,y)
    return model,x,y

def clusters(kind='kmeans',fitted=True,legacy_role=True):
    x,y=make_blobs(n_samples=40,n_features=2,centers=3,random_state=42)
    model=KMeans(n_clusters=3,n_init=2,random_state=3) if kind=='kmeans' else AgglomerativeClustering(n_clusters=3)
    if fitted:model.fit(x)
    # Pinned W&B checks this legacy role attribute; modern sklearn uses tags.
    # Keep an actual compatible estimator for numerical comparisons and test
    # unmodified current estimators in a separate explicit repair case.
    if legacy_role:model._estimator_type='clusterer'
    return model,x,y

def cases():
    """Make fresh model/data values so mutations cannot leak between cases."""
    output={}
    def add(api,label,build):output.setdefault(api,{})[label]=build
    for label,classes in [('binary',2),('multiclass',3)]:
        for api in ['plot_roc','plot_precision_recall']:
            def build(classes=classes):
                model,x,y=classification(classes);return (y,model.predict_proba(x)),{}
            add(api,label,build)
        def classifier(classes=classes):
            model,x,y=classification(classes);return (model,x,x,y,y,model.predict(x),model.predict_proba(x),['a','b','c'][:classes]),{}
        add('plot_classifier',label,classifier)
    for api in ['plot_roc','plot_precision_recall']:
        def options(api=api):
            model,x,y=classification(3);kw={'labels':['cat','dog','owl'],'classes_to_plot':[0,2],'plot_micro':False}
            if api=='plot_roc':kw['plot_macro']=False
            return (y,model.predict_proba(x)),kw
        add(api,'options',options)
        add(api,'bad_probabilities',lambda:(([0,1,0],[.1,.9,.2]),{}))
        add(api,'empty_classes',lambda:(([0,1],[[.9,.1],[.1,.9]]),{'classes_to_plot':[]}))
        add(api,'string_classes',lambda:((['a','b','a'],[[.8,.2],[.1,.9],[.6,.4]]),{}))
    for label,kw in [('default',{}),('named',{'labels':['cat','dog','owl']}),('normalized',{'normalize':True}),
            ('true_subset',{'true_labels':[0,2]}),('pred_subset',{'pred_labels':[1]}),('invalid_subset',{'true_labels':[99]}),
            ('both_subsets',{'true_labels':[0,2],'pred_labels':[1]})]:
        add('plot_confusion_matrix',label,lambda kw=kw:(([0,0,1,1,2,2],[0,1,0,1,1,2]),kw))
    add('plot_confusion_matrix','named_subset',lambda:(([0,0,1,1,2,2],[0,1,0,1,1,2]),{'labels':['cat','dog','owl'],'true_labels':['cat','owl'],'pred_labels':['dog']}))
    add('plot_confusion_matrix','mismatched',lambda:(([0,1],[1]),{}))
    add('plot_confusion_matrix','strings',lambda:((['cat','dog','cat'],['cat','dog','dog']),{}))
    for api in ['plot_summary_metrics','plot_learning_curve','plot_feature_importances','plot_calibration_curve']:
        def default(api=api):
            model,x,y=classification();return ((model,x,y,x,y) if api=='plot_summary_metrics' else (model,) if api=='plot_feature_importances' else (model,x,y)),{}
        add(api,'default',default)
        def unfitted(api=api):
            model,x,y=classification(fitted=False);return ((model,x,y,x,y) if api=='plot_summary_metrics' else (model,) if api=='plot_feature_importances' else (model,x,y)),{}
        add(api,'unfitted',unfitted)
    def learning_options():
        model,x,y=classification();return (model,pd.DataFrame(x),pd.Series(y)),{'cv':KFold(2,shuffle=True,random_state=3),'shuffle':True,'random_state':4,'train_sizes':[.4,.8],'n_jobs':2,'scoring':'accuracy'}
    add('plot_learning_curve','all_options',learning_options)
    add('plot_learning_curve','bad_size',lambda:((LinearRegression(),[[0],[1]],[0,1]),{'train_sizes':[-1],'cv':2}))
    def feature_options():
        model,x,y=classification();return (model,),{'feature_names':['a','b','c','d'],'title':'Ranked features','max_num_features':2}
    add('plot_feature_importances','all_options',feature_options)
    for label,kw in [('bad_names',{'feature_names':[]}),('zero_limit',{'max_num_features':0})]:
        add('plot_feature_importances',label,lambda kw=kw:((classification()[0],),kw))
    for classes in [2,3]:
        add('plot_feature_importances','linear_'+str(classes),lambda classes=classes:((classification(classes,kind='linear')[0],),{}))
    for label,classes in [('named',2),('multiclass',3)]:
        def calibration(classes=classes):
            model,x,y=classification(classes);return (model,x,y),{'clf_name':'Custom classifier'}
        add('plot_calibration_curve',label,calibration)
    def sparse_calibration():
        model,x,y=classification();return (model,csr_matrix(x),y),{}
    add('plot_calibration_curve','sparse',sparse_calibration)
    add('plot_calibration_curve','strings',lambda:((classification()[0],classification()[1],['cat','dog']*120),{}))
    for api in ['plot_summary_metrics','plot_residuals','plot_outlier_candidates']:
        def reg(api=api):
            model,x,y=regression();return ((model,x,y,x,y) if api=='plot_summary_metrics' else (model,x,y)),{}
        add(api,'regression',reg)
    for api in ['plot_residuals','plot_outlier_candidates']:
        def unfitted(api=api):
            model,x,y=regression(fitted=False);return (model,x,y),{}
        add(api,'unfitted',unfitted)
        def frame(api=api):
            model,x,y=regression();return (model,pd.DataFrame(x),pd.Series(y)),{}
        add(api,'dataframe',frame)
    for label,args in [('default',([0,1,1,2],[0,0,2])),('names',([0,1,1,2],[0,0,2],['cat','dog','owl'])),
        ('strings',(['a','b','a'],['b','b'])),('short_labels',([0,1],[0,1],['cat'])),('empty',([],[]))]:
        add('plot_class_proportions',label,lambda args=args:(args,{}))
    for label,kw in [('default',{}),('options',{'cluster_ranges':[4,2,3],'n_jobs':2,'show_cluster_time':False}),
        ('empty_ranges',{'cluster_ranges':[]}),('invalid_ranges',{'cluster_ranges':[0]})]:
        def elbow(kw=kw):
            model,x,y=clusters();return (model,x),kw
        add('plot_elbow_curve',label,elbow)
    add('plot_elbow_curve','no_clusters',lambda:((LinearRegression(),[[0],[1]]),{}))
    for label,kind,kw in [('default','kmeans',{}),('options','kmeans',{'labels':['A','B','C'],'metric':'manhattan','kmeans':True}),
        ('non_kmeans','other',{'kmeans':False}),('invalid_metric','kmeans',{'metric':'bad'})]:
        def silhouette(kind=kind,kw=kw):
            model,x,y=clusters(kind);return (model,pd.DataFrame(x),model.labels_),kw
        add('plot_silhouette',label,silhouette)
    def one_cluster():
        model,x,y=clusters();return (model,x,[0]*len(x)),{}
    add('plot_silhouette','one_cluster',one_cluster)
    for api in ['plot_silhouette','plot_clusterer']:
        def modern_clusterer():
            model,x,y=clusters(legacy_role=False);return (model,x,model.labels_),{}
        add(api,'modern_tags_repair',modern_clusterer)
    for kind in ['kmeans','other']:
        def clusterer(kind=kind):
            model,x,y=clusters(kind);return (model,x,model.labels_),{'labels':['A','B','C'],'model_name':'Custom clustering'}
        add('plot_clusterer',kind,clusterer)
    def all_classifier():
        model,x,y=classification();return (model,x,x,y,y,model.predict(x),model.predict_proba(x),['A','B']),{'is_binary':True,'model_name':'Custom forest','feature_names':['a','b','c','d'],'log_learning_curve':True}
    add('plot_classifier','all_options',all_classifier)
    def naive_bayes():
        model,x,y=classification(kind='nb');return (model,x,x,y,y,model.predict(x),model.predict_proba(x),['A','B']),{}
    add('plot_classifier','naive_bayes',naive_bayes)
    def regressor():
        model,x,y=regression();return (model,x,x,y,y),{'model_name':'Custom regression'}
    add('plot_regressor','all_options',regressor)
    # The pinned fitted check forgets to return True when its three-column
    # prediction succeeds. Record that existing native repair separately.
    for api in ['plot_summary_metrics','plot_feature_importances','plot_calibration_curve']:
        def three(api=api):
            model,x,y=classification(features=3);return ((model,x,y,x,y) if api=='plot_summary_metrics' else (model,) if api=='plot_feature_importances' else (model,x,y)),{}
        add(api,'three_feature_repair',three)
    for api, group in output.items():
        group['defaults']=lambda:((),{})
        group['unknown_argument']=lambda:((),{'unexpected':True})
    return output

def main():
    results={'versions':{'sklearn':sklearn.__version__,'numpy':np.__version__,'pandas':pd.__version__},'portable':{},'controls':{},'repairs':{},'source_sha256':{}}
    if REFERENCE:
        package=importlib.import_module('wandb.integration.sklearn')
        folder=Path(package.__file__).parent
        for file in folder.rglob('*.py'):
            relative=file.relative_to(folder);expected=ROOT/'.reference/wandb/source/wandb/integration/sklearn'/relative
            assert file.read_bytes()==expected.read_bytes(),str(relative)
            results['source_sha256'][str(relative)]=hashlib.sha256(file.read_bytes()).hexdigest()
    with parallel_backend('threading'):
        for prefix in ['wandb.sklearn','wandb.integration.sklearn']:
            module=w.sklearn if prefix=='wandb.sklearn' else importlib.import_module(prefix)
            results['portable'][prefix]={};results['controls'][prefix]={};results['repairs'][prefix]={}
            for name,group in cases().items():
                function=getattr(module,name)
                record={'signature':[(p.name,p.kind.name,'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(function).parameters.values()],'cases':{}}
                results['controls' if name in CONTROL_APIS else 'portable'][prefix][name]=record
                for label,build in group.items():
                    np.random.seed(123);args,kwargs=build();calls=[]
                    estimators=[v for v in args if hasattr(v,'get_params')]
                    def model_state():
                        return [{name:portable(getattr(model,name)) for name in ['coef_','intercept_','labels_','cluster_centers_'] if hasattr(model,name)} for model in estimators]
                    before=model_state()
                    with contextlib.ExitStack() as stack:
                        for method in ['log','termwarn','termerror','termlog']:
                            stack.enter_context(patch.object(w,method,lambda *a,_method=method,**k:calls.append([_method,portable(a),portable(k)])))
                        observed={'result':attempt(lambda:function(*args,**kwargs)),'calls':calls,'model_before':before,'model_after':model_state()}
                    if label.endswith('_repair') or (name=='plot_confusion_matrix' and 'subset' in label):
                        results['repairs'][prefix][name+'.'+label]=observed
                    else:record['cases'][label]=observed
    print(json.dumps(results,sort_keys=True))

if __name__=='__main__':main()
