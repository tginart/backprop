"""Build tiny real fastai 1 learners for prediction and cancellation tests.

Architecture: frozen CPU inputs -> genuine DataBunch -> genuine Learner with a
small torch model. Main callers: framework_gap_probe and FastaiCleanup tests.
Why this exists: shared construction keeps source/native observations and error
recovery tests on the same actual framework objects, without framework stubs.
"""
from pathlib import Path


def tensor_learner(directory):
    """Create the CPU regression learner used to exercise training failures.

    Main callers: exceptional cancellation and interrupted checkpoint tests.
    """
    import torch
    from torch.utils.data import DataLoader, TensorDataset
    from fastai.basic_data import DataBunch
    from fastai.basic_train import Learner
    torch.manual_seed(7)
    dataset = TensorDataset(torch.ones(8, 2), torch.ones(8, 1))
    data = DataBunch(DataLoader(dataset, batch_size=4), DataLoader(dataset, batch_size=4),
                     device=torch.device('cpu'), path=directory)
    return Learner(data, torch.nn.Linear(2, 1), loss_func=torch.nn.MSELoss(),
                   add_time=False, silent=True)


def vision_learner(directory, segmentation):
    """Create real classification or segmentation data and an untrained model.

    Main callers: pinned image-prediction comparisons and figure cleanup tests.
    Classification produces category captions; segmentation produces the input,
    prediction and truth panels, using fastai's real Learner.predict path.
    """
    import numpy as np
    import torch
    from fastai.basic_train import Learner
    from fastai.vision import ImageDataBunch, SegmentationItemList
    from PIL import Image
    folder = Path(directory) / 'input'
    for index in range(8):
        pixels = np.full((8, 8, 3), index * 16, dtype=np.uint8)
        destination = folder / ('images' if segmentation else str(index % 2)) / f'{index}.png'
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(pixels).save(destination)
        if segmentation:
            mask = folder / 'masks' / f'{index}.png'
            mask.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.full((8, 8), index % 2, dtype=np.uint8)).save(mask)
    torch.manual_seed(7)
    if segmentation:
        data = (SegmentationItemList.from_folder(folder / 'images')
            .split_by_rand_pct(.5, seed=7)
            .label_from_func(lambda path: folder / 'masks' / path.name, classes=['zero', 'one'])
            .databunch(bs=2, num_workers=0, device=torch.device('cpu')))
        model = torch.nn.Conv2d(3, 2, kernel_size=1)
    else:
        data = ImageDataBunch.from_folder(folder, train='.', valid_pct=.5, seed=7, bs=2,
                                         num_workers=0, device=torch.device('cpu'))
        model = torch.nn.Sequential(torch.nn.Flatten(), torch.nn.Linear(192, 2))
    return Learner(data, model, add_time=False, silent=True)
