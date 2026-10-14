"""Preserve pinned PyTorch histogram measurements across CPU and CUDA tensors.

Architecture: detached tensor -> finite values plus counted sparse zeros ->
PyTorch histogram -> ordinary run log with commit=False. Hooks own frequency;
this helper owns only numerical behavior, without retaining training tensors.
Main callers: ModelWatcher parameter and gradient callbacks.
Adapted from W&B 0.30.0 wandb/integration/torch/wandb_torch.py (MIT).
Upstream SHA-256: 54bee05e5655a6d50f83c855e3e57b7697c7d11e4ccaa4f8a3e27f30519d195a
"""
from __future__ import annotations

from .data_types import Histogram


class HistogramRecorder:
    def __init__(self, run):
        self.run = run
        self._num_bins = 64
        self._is_cuda_histc_supported = None

    def record(self, tensor, name):  # noqa: C901
        """Add distribution statistics on a tensor's elements to the current History entry."""
        import torch
        if self.run._closed:
            return
        # Preserve sparse implicit zeros and the pinned constant-value bin.
        if isinstance(tensor, (tuple, list)):
            while isinstance(tensor, (tuple, list)) and isinstance(
                tensor[0], (tuple, list)
            ):
                tensor = [item for sublist in tensor for item in sublist]
            tensor = torch.cat([t.detach().clone().reshape(-1) for t in tensor])

        tensor = tensor.detach().clone()
        # checking for inheritance from _TensorBase didn't work for some reason
        if not hasattr(tensor, "shape"):
            cls = type(tensor)
            raise TypeError(f"Expected Tensor, not {cls.__module__}.{cls.__name__}")

        # Sparse tensors have a bunch of implicit zeros. In order to histo them correctly,
        # we have to count them up and add them to the histo ourselves.
        sparse_zeros = None
        if tensor.is_sparse:
            # Have to call this on a sparse tensor before most other ops.
            tensor = tensor.cpu().coalesce()

            backing_values = tensor._values()
            sparse_zeros = tensor.numel() - backing_values.numel()
            tensor = backing_values

        flat = tensor.reshape(-1)

        if flat.is_cuda:
            if self._is_cuda_histc_supported is None:
                try:
                    flat.histc(bins=self._num_bins)
                except RuntimeError:
                    self._is_cuda_histc_supported = False
                else:
                    self._is_cuda_histc_supported = True

            # As of torch 1.0.1.post2+nightly, float16 cuda summary ops are not supported (convert to float32)
            if not self._is_cuda_histc_supported:
                flat = flat.cpu()
            elif not isinstance(
                flat, (torch.cuda.FloatTensor, torch.cuda.DoubleTensor)
            ):
                flat = flat.type(torch.cuda.FloatTensor)

        # Since we use histc, we need to make sure that torch supports the operation on CPU,
        # otherwise we'll get a runtime error. Hence, we need to upcast to float32.
        if not flat.is_cuda and not isinstance(
            flat, (torch.FloatTensor, torch.DoubleTensor)
        ):
            flat = flat.type(torch.FloatTensor)

        # Skip logging if all values are nan or inf or the tensor is empty.
        if self._no_finite_values(flat):
            return

        # Remove nans and infs if present. There's no good way to represent that in histograms.
        flat = self._remove_infs_nans(flat)

        tmin = flat.min().item()
        tmax = flat.max().item()
        if sparse_zeros:
            # If we've got zeros to add in, make sure zero is in the hist range.
            tmin = 0 if tmin > 0 else tmin
            tmax = 0 if tmax < 0 else tmax
        # Anecdotally, this can somehow happen sometimes. Maybe a precision error
        # in min()/max() above. Swap here to prevent a runtime error.
        # If all values are equal, just return a single bin.
        if tmin > tmax:
            tmin, tmax = tmax, tmin
        if tmin == tmax:
            tensor = torch.Tensor([flat.numel()])
            tensor = tensor.cpu().clone().detach()
            bins = torch.Tensor([tmin, tmax])
        else:
            tensor = flat.histc(bins=self._num_bins, min=tmin, max=tmax)
            tensor = tensor.cpu().detach().clone()
            bins = torch.linspace(tmin, tmax, steps=self._num_bins + 1)

        # Add back zeroes from a sparse tensor.
        if sparse_zeros:
            bins_np = bins.numpy()
            tensor_np = tensor.numpy()
            bin_idx = 0
            num_buckets = len(bins_np) - 1
            for i in range(num_buckets):
                start = bins_np[i]
                end = bins_np[i + 1]
                # There are 3 cases to consider here, all of which mean we've found the right bucket
                # 1. The bucket range contains zero.
                # 2. The bucket range lower bound *is* zero.
                # 3. This is the last bucket and the bucket range upper bound is zero.
                if (start <= 0 and end > 0) or (i == num_buckets - 1 and end == 0):
                    bin_idx = i
                    break

            tensor_np[bin_idx] += sparse_zeros
            tensor = torch.Tensor(tensor_np)
            bins = torch.Tensor(bins_np)

        self.run.log(
            {name: Histogram(np_histogram=(tensor.tolist(), bins.tolist()))},
            commit=False,
        )

    def _no_finite_values(self, tensor: Tensor) -> bool:
        import torch
        return tensor.shape == torch.Size([0]) or (~torch.isfinite(tensor)).all().item()

    def _remove_infs_nans(self, tensor: Tensor) -> Tensor:
        import torch
        if not torch.isfinite(tensor).all():
            tensor = tensor[torch.isfinite(tensor)]

        return tensor
