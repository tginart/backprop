"""Run unchanged portable graph calls in an isolated pinned or Backprop process.

Architecture: the caller selects PYTHONPATH -> ordinary wandb imports -> JSON
observations of construction, attributes, failures and genuine PyTorch capture.
Main caller: test_optional_graph.py. Each probe owns its temporary offline run.
"""
import json
import tempfile
import wandb
from wandb.data_types import Graph, Node

def values():
    graph=Graph(format="custom")
    a=graph.add_node(id="a",name="input",class_name="Input",size=[2,3],parameters=[("w",[2,3])],output_shape=[1,3],is_output=False,num_parameters=6)
    b=Node(id="b",node=a,name="output",is_output=True)
    b.functions=["relu"]; b.child_parameters=4; b.is_constant=False
    graph.add_node(b); edge=graph.add_edge(a,b); edge.name="flow"
    errors=[]
    for call in (lambda:graph.add_node(a,id="x"),lambda:graph["missing"],lambda:Node(size=4),lambda:Node(node=Node()),lambda:Node.from_json({},None),lambda:Node().to_data_array(),lambda:Graph().to_data_array()):
        try:call()
        except Exception as error:errors.append(type(error).__name__)
    return {"graph":graph._to_graph_json(),"lookup":graph["a"] is a,"edge":edge.to_json(),"edge_repr":repr(edge),"node_repr":repr(b),
            "errors":errors,"suffix":Graph.with_suffix("net"),"subdir":Graph.get_media_subdir(),"defaults":Node().to_json(),"attributes":{key:getattr(b,key) for key in
            ("id","name","class_name","size","parameters","output_shape","is_output","num_parameters","functions","child_parameters","is_constant")},
            "node_suffix":Node.with_suffix("node","txt")}

def capture():
    import torch
    torch.manual_seed(7)
    with tempfile.TemporaryDirectory() as directory:
        run=wandb.init(mode="offline",project="graph-reference",dir=directory,settings={"console":"off","silent":True,"x_disable_stats":True})
        model=torch.nn.Sequential(torch.nn.Linear(2,3),torch.nn.ReLU(),torch.nn.Linear(3,1))
        criterion=torch.nn.MSELoss()
        graphs=[]
        if wandb.__version__ == "0.30.0":
            # Observe the private return without changing the public result.
            original=wandb.sdk._watch
            def capture_handles(*args,**kwargs):
                result=original(*args,**kwargs);graphs.extend(result);return result
            wandb.sdk._watch=capture_handles
        public_result=run.watch(model,criterion=criterion,log="all",log_freq=2,idx=2,log_graph=True)
        if wandb.__version__ != "0.30.0":graphs=list(run._watcher.graphs.values())
        for _ in range(2): model(torch.ones(1,2)).sum().backward()
        run.log({"loss":1.})
        graph=graphs[0]
        result={"public_return":public_result,"count":len(graphs),"criterion":graph.criterion is criterion,"format":graph.format,"nodes":[],"edges":graph._to_graph_json()["edges"]}
        for node in graph.nodes:
            attrs=dict(node.to_json());attrs.pop("id");result["nodes"].append(attrs)
        run.unwatch(model)
        result["hooks_remaining"]=sum(len(module._forward_hooks) for module in model.modules())+sum(len(parameter._backward_hooks or {}) for parameter in model.parameters())
        run.finish()
        return result

def histograms():
    import torch
    from types import SimpleNamespace
    captured={}
    def log(data,commit=None):
        captured.update({key:value.to_json() for key,value in data.items()})
    previous=wandb.run
    if wandb.__version__ == "0.30.0":
        from wandb.integration.torch.wandb_torch import TorchHistory
        wandb.run=SimpleNamespace(_log=log)
        record=TorchHistory().log_tensor_stats
    else:
        from backprop.torch_histogram import HistogramRecorder
        record=HistogramRecorder(SimpleNamespace(_closed=False,log=log)).record
    try:
        cases={"constant":torch.ones(7),"empty":torch.empty(0),"nonfinite":torch.tensor([float("nan"),float("inf")]),
               "mixed":torch.tensor([float("nan"),-3.,0.,2.,float("inf")]),"half":torch.arange(12,dtype=torch.float16),
               "double":torch.tensor([.1,.2,.3],dtype=torch.float64),"integer":torch.arange(5),
               "sparse":torch.sparse_coo_tensor([[0,3]],[2.,4.],(6,))}
        for name,tensor in cases.items():record(tensor,name)
    finally:wandb.run=previous
    return captured

if __name__ == "__main__":
    print("GRAPH_RESULT="+json.dumps({"values":values(),"capture":capture(),"histograms":histograms()},sort_keys=True))
