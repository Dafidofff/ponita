import pytorch_lightning as pl
import torch
import torch.nn as nn
from models.ponita import PonitaFiberBundle
import torchmetrics
import numpy as np
from .scheduler import CosineWarmupScheduler
from torch_geometric.data import Batch
from ponita.transforms.random_rotate import RandomRotate3D


class PONITA_MD17(pl.LightningModule):
    """
    """

    def __init__(self, args):
        super().__init__()

        # Store some of the relevant args
        self.repeats = args.repeats
        self.lr = args.lr
        self.weight_decay = args.weight_decay
        self.epochs = args.epochs
        self.warmup = args.warmup
        self.lambda_F = args.lambda_F
        if args.layer_scale == 0.:
            args.layer_scale = None

        # For rotation augmentations during training and testing
        self.train_augm = args.train_augm
        self.rotation_transform = RandomRotate3D(['pos','force'])
        
        # Shift and scale before callibration
        self.shift = 0.
        self.scale = 1.

        # The metrics to log
        self.train_metric = torchmetrics.MeanAbsoluteError()
        self.train_metric_force = torchmetrics.MeanAbsoluteError()
        self.valid_metric = torchmetrics.MeanAbsoluteError()
        self.valid_metric_force = torchmetrics.MeanAbsoluteError()
        self.test_metrics_energy = nn.ModuleList([torchmetrics.MeanAbsoluteError() for r in range(self.repeats)])
        self.test_metrics_force = nn.ModuleList([torchmetrics.MeanAbsoluteError() for r in range(self.repeats)])

        # Input/output specifications:
        in_channels_scalar = 9  # Charge, Velocity norm
        in_channels_vec = 0  # Velocity, rel_pos
        out_channels_scalar = 1  # None
        out_channels_vec = 0  # Output velocity

        # Make the model
        self.model = PonitaFiberBundle(in_channels_scalar + in_channels_vec,
                        args.hidden_dim,
                        out_channels_scalar,
                        args.layers,
                        output_dim_vec=out_channels_vec,
                        radius=args.radius,
                        num_ori=args.num_ori,
                        basis_dim=args.basis_dim,
                        degree=args.degree,
                        widening_factor=args.widening_factor,
                        layer_scale=args.layer_scale,
                        task_level='graph',
                        multiple_readouts=args.multiple_readouts)

    def set_dataset_statistics(self, dataset):
        ys = np.array([data.energy.item() for data in dataset])
        forces = np.concatenate([data.force.numpy() for data in dataset])
        self.shift = np.mean(ys)
        self.scale = np.sqrt(np.mean(forces**2))
        self.min_dist = 1e10
        self.max_dist = 0
        for data in dataset:
            pos = data.pos
            edm = np.linalg.norm(pos[:,None,:] - pos[None,:,:],axis=-1)
            min_dist = np.min(edm + np.eye(edm.shape[0]) * 1e10)
            max_dist = np.max(edm)
            if min_dist < self.min_dist:
                self.min_dist = min_dist 
            if max_dist > self.max_dist:
                self.max_dist = max_dist 
        print('Min-max range of distances between atoms in the dataset:', self.min_dist, '-', self.max_dist)

    def forward(self, graph):
        # Only utilize the scalar (energy) prediction
        pred, _ = self.model(graph)
        return pred.squeeze(-1)

    @torch.enable_grad()
    def pred_energy_and_force(self, graph):
        graph.pos = torch.autograd.Variable(graph.pos, requires_grad=True)
        pred_energy = self(graph)
        sign = -1.0
        pred_force = sign * torch.autograd.grad(
            pred_energy,
            graph.pos,
            grad_outputs=torch.ones_like(pred_energy),
            create_graph=True,
            retain_graph=True
        )[0]
        # Return result
        return pred_energy, pred_force

    def training_step(self, graph):
        if self.train_augm:
            graph = self.rotation_transform(graph)
        pred_energy, pred_force = self.pred_energy_and_force(graph)
        
        energy_loss = torch.mean((pred_energy - (graph.energy - self.shift) / self.scale)**2)
        force_loss = torch.mean(torch.sum((pred_force - graph.force / self.scale)**2,-1)) / 3.
        loss = energy_loss / self.lambda_F + force_loss

        self.train_metric(pred_energy * self.scale + self.shift, graph.energy)
        self.train_metric_force(pred_force * self.scale, graph.force)

        return loss

    def on_train_epoch_end(self):
        self.log("train MAE (energy)", self.train_metric, prog_bar=True)
        self.log("train MAE (force)", self.train_metric_force, prog_bar=True)

    def validation_step(self, graph, batch_idx):
        pred_energy, pred_force = self.pred_energy_and_force(graph)
        self.valid_metric(pred_energy * self.scale + self.shift, graph.energy)
        self.valid_metric_force(pred_force * self.scale, graph.force)        

    def on_validation_epoch_end(self):
        self.log("valid MAE (energy)", self.valid_metric, prog_bar=True)
        self.log("valid MAE (force)", self.valid_metric_force, prog_bar=True)
    
    def test_step(self, graph, batch_idx):
        # Repeat the prediction self.repeat number of times and average (makes sense due to random grids)
        batch_size = graph.batch.max() + 1
        batch_length = graph.batch.shape[0]
        graph_repeated = Batch.from_data_list([graph] * self.repeats)
        # Random rotate graph
        rot = self.rotation_transform.random_rotation(graph_repeated)
        graph_repeated = self.rotation_transform.rotate_graph(graph_repeated, rot)
        # Compute results
        pred_energy_repeated, pred_force_repeated = self.pred_energy_and_force(graph_repeated)
        # Unrotate results
        rot_T = rot.transpose(-2,-1)
        pred_force_repeated = self.rotation_transform.rotate_attr(pred_force_repeated, rot_T)
        # Unwrap predictions
        pred_energy_repeated = pred_energy_repeated.unflatten(0, (self.repeats, batch_size))
        pred_force_repeated = pred_force_repeated.unflatten(0, (self.repeats, batch_length))
        # Compute the averages
        for r in range(self.repeats):
            pred_energy, pred_force = pred_energy_repeated[:r+1].mean(0), pred_force_repeated[:r+1].mean(0)
            self.test_metrics_energy[r](pred_energy * self.scale + self.shift, graph.energy)
            self.test_metrics_force[r](pred_force * self.scale, graph.force)

    def on_test_epoch_end(self):
        for r in range(self.repeats):
            self.log("test MAE (energy) x"+str(r+1), self.test_metrics_energy[r])
            self.log("test MAE (force) x"+str(r+1), self.test_metrics_force[r])

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        scheduler = CosineWarmupScheduler(optimizer, self.warmup, self.trainer.max_epochs)
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "val_loss"}