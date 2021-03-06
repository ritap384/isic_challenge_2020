import funcy
import numpy as np
import os
import pandas as pd
import random
import torch
import torch.nn as nn

from kornia.losses import FocalLoss
from nvidia.dali.plugin.pytorch import DALIGenericIterator
from sacred import Experiment
from sacred.observers import FileStorageObserver
from sklearn.metrics import roc_auc_score
from torch.optim.lr_scheduler import CyclicLR
from torch.utils.data import DataLoader, WeightedRandomSampler
from tensorboardX import SummaryWriter
from torch.optim import Adam
from torchvision import transforms
from tqdm import tqdm

from models import load_model
from dataset import ISICDataset, ExternalInputIterator, ExternalSourcePipeline

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.path.isdir(os.path.join(BASE_DIR, "FastAutoAugment")):
    from FastAutoAugment.data import Augmentation
    from FastAutoAugment.archive import fa_resnet50_rimagenet
else:
    raise ImportError(
        "FastAutoAugment is not available. Download the code from https://github.com/kakaobrain/fast-autoaugment"
        "and move FastAutoAugment to this directory."
    )

TRAIN = "train"
VALIDATION = "validation"
TEST = "test"

ex = Experiment()
fs_observer = FileStorageObserver.create(os.path.join(BASE_DIR, "results"))
ex.observers.append(fs_observer)

def set_seeds(worker_id):
    seed = torch.initial_seed() % 2 ** 31
    np.random.seed(seed + 1)
    random.seed(seed + 2)


def run_epoch(phase, epoch, model, pipe, optimizer, criterion, scheduler=None, writer=None, device=None):
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if isinstance(device, str):
        device = torch.device(device)

    training = phase == TRAIN
    dali_iter = DALIGenericIterator([pipe], ["inputs", "targets"])
    progress_bar = tqdm(dali_iter, desc="Epoch {} - {}".format(epoch, phase))

    if training:
        model.train()
    else:
        model.eval()

    all_targets = []
    all_outputs = []
    losses = []
    for i, it in enumerate(progress_bar):
        batch_data = it[0]
        inputs = batch_data["inputs"]
        targets = batch_data["targets"]

        inputs = inputs.to(device)
        targets = targets.to(device)

        optimizer.zero_grad()
        with torch.set_grad_enabled(training):
            outputs = model(inputs)
            loss = criterion(outputs, targets)

            if training:
                loss.backward()
                optimizer.step()

                if scheduler is not None:
                    scheduler.step()

            outputs = torch.softmax(outputs, dim=1)

            all_targets.append(targets.detach().cpu().numpy())
            all_outputs.append(outputs.detach().cpu().numpy())

            try:
                auc = roc_auc_score(np.concatenate(all_targets), np.concatenate(all_outputs)[:, 1])
            except:
                auc = np.nan

            losses.append(loss.item())
            progress_bar.set_postfix(loss=np.mean(losses), auc=auc)

    mean_loss = np.mean(losses)
    writer.add_scalar("{}.loss".format(phase), mean_loss, epoch)

    all_targets = np.concatenate(all_targets)
    all_outputs = np.concatenate(all_outputs)[:, 1]
    auc = roc_auc_score(all_targets, all_outputs)
    writer.add_scalar("{}.auc".format(phase), auc, epoch)

    return {"loss": mean_loss,
            "auc": auc}


def run_test(model, dataloader, criterion, device, threshold=None):
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if isinstance(device, str):
        device = torch.device(device)

    progress_bar = tqdm(dataloader, desc="Running test")
    model.eval()

    predictions = []
    for i, it in enumerate(progress_bar):
        batch_data = it[0]
        inputs = batch_data["inputs"]

        inputs = inputs.to(device)
        with torch.set_grad_enabled(False):
            net_outputs = model(inputs)

            net_outputs = torch.softmax(net_outputs, dim=1)
            cls_outputs = funcy.lmap(lambda t: t.item(), net_outputs[:, 1])

            if threshold is not None:
                cls_outputs = [int(out > threshold) for out in cls_outputs]

            predictions.extend(list(cls_outputs))

    return predictions


@ex.automain
def main(_run, architecture, batch_size, n_epochs, learning_rate, weight_decay, patience, input_size,
         datapath, train_fpath, valid_fpath, test_fpath):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    writer = SummaryWriter(os.path.join(BASE_DIR, "runs", "experiment-{}".format(_run._id)))
    best_model_path = os.path.join(fs_observer.dir, "best_model.pth")
    last_model_path = os.path.join(fs_observer.dir, "last_model.pth")

    # transform = Augmentation(fa_resnet50_rimagenet())

    transform = transforms.Compose([
        transforms.RandomHorizontalFlip(p=0.5),
        transforms.RandomVerticalFlip(p=0.5),
        transforms.RandomRotation((-45, 45)),
        transforms.ColorJitter(brightness=0.05, contrast=0.05, saturation=0.05),
        transforms.RandomAffine(degrees=5, translate=(0.05, 0.05), shear=10)
    ])

    train_valid_datadir = os.path.join(datapath, "train_512")

    train_eii = iter(ExternalInputIterator(train_valid_datadir, train_fpath, batch_size))
    train_pipe = ExternalSourcePipeline(data_iterator=train_eii, batch_size=batch_size, num_threads=2)
    train_pipe.build()

    valid_eii = iter(ExternalInputIterator(train_valid_datadir, valid_fpath, batch_size))
    valid_pipe = ExternalSourcePipeline(data_iterator=valid_eii, batch_size=batch_size, num_threads=2)
    valid_pipe.build()

    # train_dataset = ISICDataset(train_valid_datadir, train_fpath, transform, size=input_size)
    # valid_dataset = ISICDataset(train_valid_datadir, valid_fpath, transform, size=input_size)

    # sampler = torch.utils.data.sampler.WeightedRandomSampler(
    #     train_dataset.class_weights, len(train_dataset.class_weights)
    # )

    # train_dataloader = DataLoader(
    #     train_dataset, batch_size=batch_size, shuffle=False, num_workers=0, worker_init_fn=set_seeds, sampler=sampler
    # )
    # valid_dataloader = DataLoader(
    #     valid_dataset, batch_size=batch_size, shuffle=False, num_workers=0, worker_init_fn=set_seeds
    # )

    model = load_model(architecture, 2)
    model = model.to(device)
    optimizer = Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    scheduler = CyclicLR(optimizer, base_lr=learning_rate, max_lr=10 * learning_rate, cycle_momentum=False)
    # loss_fn = nn.CrossEntropyLoss()
    loss_fn = FocalLoss(alpha=0.5, reduction="sum")

    info = {}
    epochs = range(1, n_epochs + 1)
    best_metric = 0.0
    epochs_since_best = 0

    for epoch in epochs:
        info[TRAIN] = run_epoch(TRAIN, epoch, model, train_pipe, optimizer, loss_fn, scheduler, writer)
        info[VALIDATION] = run_epoch(VALIDATION, epoch, model, valid_pipe, optimizer, loss_fn, scheduler, writer)

        if info[VALIDATION]["auc"] > best_metric:
            best_metric = info[VALIDATION]["auc"]
            torch.save(model.state_dict(), best_model_path)
            epochs_since_best = 0
        else:
            epochs_since_best += 1

        torch.save(model.state_dict(), last_model_path)

        if epochs_since_best > patience:
            break

    if test_fpath is not None:
        test_datadir = os.path.join(datapath, "test_512")
        test_eii = iter(ExternalInputIterator(test_datadir, test_fpath, batch_size))
        test_pipe = ExternalSourcePipeline(data_iterator=test_eii, batch_size=batch_size, num_threads=2)
        test_pipe.build()

        # test_dataset = ISICDataset(test_datadir, test_fpath, transform, size=input_size)
        # test_dataloader = DataLoader(
        #     test_dataset, batch_size=batch_size, shuffle=False, num_workers=0, worker_init_fn=set_seeds
        # )

        best_model_state_dict = torch.load(best_model_path, map_location=device)
        best_model = load_model(architecture, 2, best_model_state_dict).to(device)
        predictions = run_test(best_model, test_pipe, loss_fn, device, threshold=0.5)

        df = pd.DataFrame(predictions, columns=["image_name", "target"])
        df.to_csv(os.path.join(fs_observer.dir, "submission.csv"), index=False)

        print("Finished running test")
