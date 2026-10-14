"""Collect local training telemetry using optional framework/runtime libraries.

Architecture: explicit watch hooks or periodic system sampling -> ordinary
histogram/system events -> existing journal. Hooks are removed at unwatch/finish;
the monitor stops before final events. No remote process executes user code.
Main callers: Run.watch, Run initialization and Run.finish.
"""
import importlib
import platform
import shutil
import threading
import time
import warnings


from .torch_watch import ModelWatcher


class SystemMonitor:
    """Sample psutil and optional NVML on a stoppable daemon.

    Main callers: Run lifecycle. Missing collectors produce a warning once;
    sampling errors are reported rather than manufacturing zero measurements.
    """
    def __init__(self, run):
        self.run, self.stop_event, self.thread = run, threading.Event(), None
        self.psutil = self.nvml = None
        if run.disabled or run._settings.x_disable_stats: return
        try:
            self.psutil = importlib.import_module("psutil")
            self.process = self.psutil.Process()
            self.psutil.cpu_percent()
        except ImportError:
            run._warn("Automatic CPU/memory metrics require optional psutil; collector unavailable")
        try:
            self.nvml = importlib.import_module("pynvml")
            self.nvml.nvmlInit()
        except ImportError:
            if platform.system() != "Darwin" and shutil.which("nvidia-smi"):
                run._warn("NVIDIA metrics require optional nvidia-ml-py and an NVIDIA driver; GPU collector unavailable")
            self.nvml = None
        except Exception as error:
            run._warn(f"GPU collector unavailable: {type(error).__name__}")
            self.nvml = None

    def sample(self):
        values = {}
        if self.psutil:
            values.update(cpu=self.psutil.cpu_percent(), memory=self.psutil.virtual_memory().percent)
            values["proc.memory.rssMB"] = self.process.memory_info().rss / 1024**2
        if self.nvml:
            for index in range(self.nvml.nvmlDeviceGetCount()):
                device = self.nvml.nvmlDeviceGetHandleByIndex(index)
                usage, memory = self.nvml.nvmlDeviceGetUtilizationRates(device), self.nvml.nvmlDeviceGetMemoryInfo(device)
                values.update({f"gpu.{index}.gpu": usage.gpu, f"gpu.{index}.memory": usage.memory,
                               f"gpu.{index}.memoryAllocated": 100 * memory.used / memory.total})
        if values:
            with self.run._lock:
                if not self.run._closed and not self.stop_event.is_set():
                    self.run._emit("system", {"metrics": values, "step": self.run.step})
        return values

    def start(self):
        if not self.psutil and not self.nvml: return
        self.thread = threading.Thread(target=self._loop, name=f"backprop-stats-{self.run.id}", daemon=True)
        self.thread.start()

    def _loop(self):
        try:
            while not self.stop_event.wait(self.run._settings.x_stats_sampling_interval):
                try: self.sample()
                except Exception as error:
                    self.run._warn(f"System metric sampling failed: {type(error).__name__}: {error}")
                    return
        finally:
            self._close_gpu()

    def _close_gpu(self):
        """Release this monitor's NVML session after its sampling loop exits."""
        if self.nvml is not None:
            collector, self.nvml = self.nvml, None
            try: collector.nvmlShutdown()
            except Exception as error: self.run._warn(f"GPU collector cleanup failed: {error}")

    def stop(self):
        self.stop_event.set()
        # Run.finish holds the run lock. Joining here could deadlock a sample
        # waiting to publish; the loop owns cleanup when a thread was started.
        if self.thread is None: self._close_gpu()
