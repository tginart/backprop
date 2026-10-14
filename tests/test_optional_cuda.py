"""Compare actual CUDA statistics with the unchanged pinned PyTorch adapter.

Architecture: existing CUDA runtime -> hashed TorchHistory source with a local
logging sink -> identical native operations -> persisted graph/wrapper checks.
Main callers: existing GPU host verification, or optional CI with CUDA present.
No W&B service, package installation or device provisioning is performed.
"""
import ast
import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import backprop as w
from backprop.apis.public import Api
from backprop.telemetry import ModelWatcher
from backprop.torch_histogram import HistogramRecorder

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("torch"),"Optional PyTorch is not installed")
class CudaContracts(unittest.TestCase):
    def setUp(self):
        import torch
        self.torch=torch
        if not torch.cuda.is_available():self.skipTest("CUDA hardware is unavailable")
        self.assertIn("cuda",str(torch.ones(1,device="cuda").device))
        self.reference=ROOT/".reference/wandb/source/wandb/integration/torch/wandb_torch.py"
        if not self.reference.exists():self.skipTest("Pinned W&B source is required")

    def recorder(self, output, native):
        """Execute all unchanged upstream statistics and hook methods locally.

        Main callers: numerical and training comparisons. Only the hosted sink
        and unused graph factory are replaced; tensors and hooks remain real.
        """
        def log(data,commit=None):
            self.assertIs(commit,False)
            output.append({name:value.to_json() for name,value in data.items()})
        run=SimpleNamespace(_closed=False,log=log)
        if native:return run,HistogramRecorder(run)
        source=self.reference.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),"54bee05e5655a6d50f83c855e3e57b7697c7d11e4ccaa4f8a3e27f30519d195a")
        parsed=ast.parse(source)
        body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0)]
        body += [node for node in parsed.body if isinstance(node,(ast.ClassDef,ast.FunctionDef))
                 and node.name in {"TorchHistory","log_track_init","log_track_update"}]
        api=SimpleNamespace(Histogram=w.Histogram,run=SimpleNamespace(_log=log),
                            util=SimpleNamespace(get_module=lambda *args:self.torch))
        namespace={"wandb":api,"torch":self.torch,"LOG_TRACK_COUNT":0,"LOG_TRACK_THRESHOLD":1,
                   "TorchGraph":SimpleNamespace(hook_torch=None)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=body,type_ignores=[])),str(self.reference),"exec"),namespace)
        return run,namespace["TorchHistory"]()

    def test_real_cuda_histograms_match_pinned_values(self):
        torch=self.torch
        base={"constant":torch.ones(7,device="cuda"),"empty":torch.empty(0,device="cuda"),
              "nonfinite":torch.tensor([float("nan"),float("inf")],device="cuda"),
              "mixed":torch.tensor([float("nan"),-3.,0.,2.,float("inf")],device="cuda"),
              "sparse":torch.sparse_coo_tensor([[0,3]],[2.,4.],(6,),device="cuda"),
              "nested":[[torch.arange(3,device="cuda")],[torch.arange(4,device="cuda")]]}
        for dtype in (torch.float16,torch.bfloat16,torch.float32,torch.float64,torch.int32,torch.int64):
            base[str(dtype)]=torch.arange(129,device="cuda").to(dtype)
        results=[]
        for native in (False,True):
            observed=[];_,recorder=self.recorder(observed,native)
            record=recorder.record if native else recorder.log_tensor_stats
            for key,value in base.items():record(value,key)
            # Exercise the exact fallback branch as well as the detected path.
            recorder._is_cuda_histc_supported=False
            record(torch.arange(17,device="cuda",dtype=torch.float16),"fallback")
            results.append(observed)
        self.assertEqual(results[0],results[1])

    def test_cuda_parameter_and_gradient_hooks_match_all_logging_modes(self):
        torch=self.torch
        for mode in (None,"parameters","gradients","all"):
            for frequency in (1,2):
                with self.subTest(mode=mode,frequency=frequency):
                    results=[]
                    for native in (False,True):
                        torch.manual_seed(1234)
                        model=torch.nn.Linear(64,64).cuda()
                        observed=[];run,reference=self.recorder(observed,native)
                        if native:
                            watcher=ModelWatcher(run);watcher.watch(model,log=mode,log_freq=frequency)
                            cleanup=watcher.unwatch
                        else:
                            if mode in ("parameters","all"):reference.add_log_parameters_hook(model,log_freq=frequency)
                            if mode in ("gradients","all"):reference.add_log_gradients_hook(model,log_freq=frequency)
                            cleanup=reference.unhook_all
                        for _ in range(3):model(torch.ones(2,64,device="cuda")).square().sum().backward()
                        cleanup()
                        self.assertEqual(len(model._forward_hooks),0)
                        self.assertTrue(all(not p._backward_hooks for p in model.parameters()))
                        results.append(observed)
                    self.assertEqual(results[0],results[1])

    def test_cuda_wrappers_capture_graphs_and_cleanup_across_sessions(self):
        torch=self.torch
        with tempfile.TemporaryDirectory() as directory:
            wrappers=[("plain",lambda m:m),("data_parallel",torch.nn.DataParallel)]
            if torch.distributed.is_available() and torch.distributed.is_nccl_available():
                torch.cuda.set_device(0)
                torch.distributed.init_process_group("nccl",init_method=Path(directory,"rendezvous").as_uri(),rank=0,world_size=1)
                self.addCleanup(torch.distributed.destroy_process_group)
                wrappers.append(("ddp_one_rank",lambda m:torch.nn.parallel.DistributedDataParallel(m,device_ids=[0])))
            for name,wrapper in wrappers:
                model=wrapper(torch.nn.Sequential(torch.nn.Linear(2,3),torch.nn.ReLU(),torch.nn.Linear(3,1)).cuda())
                for session in range(2):
                    with self.subTest(wrapper=name,session=session):
                        store=w.MemorySink()
                        run=w.init(project="cuda",entity="e",dir=directory,_sink=store,reinit="create_new",
                                   settings={"console":"off","silent":True,"x_disable_stats":True})
                        try:
                            self.assertIsNone(run.watch(model,log="all",log_freq=1,log_graph=True))
                            model(torch.ones(1,2,device="cuda")).sum().backward();run.log({"loss":1.})
                            graph=run._watcher.graphs[id(model)]
                            self.assertTrue(graph.loaded);self.assertTrue(graph.nodes)
                            self.assertFalse(graph.metadata["capture"]["missing_modules"])
                            run.unwatch(model)
                            # DDP owns unrelated hooks. Compare our watcher state,
                            # and ensure subsequent passes add no captured data.
                            self.assertEqual(run._watcher.models,{})
                            before=run.step
                            model(torch.ones(1,2,device="cuda")).sum().backward()
                            self.assertEqual(run.step,before)
                        finally:run.finish()
                        public=Api._from_store(store,entity="e",project="cuda").run(run.id)
                        self.assertEqual(public.state,"finished")
                        self.assertTrue(public.summary["graph_0"]["capture"]["complete"])
                        self.assertTrue(any(key.startswith("gradients/") for key in public.summary))

    def test_two_cuda_devices_capture_one_logical_graph(self):
        torch = self.torch
        if torch.cuda.device_count() < 2:
            self.skipTest("Two CUDA devices are required for actual replication verification")
        import threading
        devices = set()
        lock = threading.Lock()
        def observed_device(module, inputs, output):
            with lock:
                devices.add(output.device.index)
        with tempfile.TemporaryDirectory() as directory:
            source = torch.nn.Sequential(torch.nn.Linear(2, 3), torch.nn.ReLU(), torch.nn.Linear(3, 1)).cuda(0)
            handle = source[0].register_forward_hook(observed_device)
            self.addCleanup(handle.remove)
            model = torch.nn.DataParallel(source, device_ids=[0, 1])
            store = w.MemorySink()
            run = w.init(project="cuda", entity="e", dir=directory, _sink=store, reinit="create_new",
                         settings={"console": "off", "silent": True, "x_disable_stats": True})
            try:
                run.watch(model, log="all", log_freq=1, log_graph=True)
                model(torch.ones(8, 2, device="cuda:0")).sum().backward()
                run.log({"loss": 1.})
                self.assertEqual(devices, {0, 1})
                graph = run._watcher.graphs[id(model)]
                self.assertTrue(graph.loaded)
                self.assertEqual(len(graph.nodes), 3)
                self.assertEqual(sum(sum(node.num_parameters) for node in graph.nodes), 13)
            finally:
                run.finish()
            public = Api._from_store(store, entity="e", project="cuda").run(run.id)
            self.assertTrue(public.summary["graph_0"]["capture"]["complete"])
            self.assertTrue(any(key.startswith("gradients/") for key in public.summary))
