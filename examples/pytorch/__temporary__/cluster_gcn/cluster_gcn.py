import torch
import torch.nn as nn
import torch.nn.functional as F
import torchmetrics.functional as MF
import dgl
import dgl.nn as dglnn
import time
import numpy as np
from ogb.nodeproppred import DglNodePropPredDataset

class SAGE(nn.Module):
    def __init__(self, in_feats, n_hidden, n_classes):
        super().__init__()
        self.layers = nn.ModuleList()
        self.layers.append(dglnn.SAGEConv(in_feats, n_hidden, 'mean'))
        self.layers.append(dglnn.SAGEConv(n_hidden, n_hidden, 'mean'))
        self.layers.append(dglnn.SAGEConv(n_hidden, n_classes, 'mean'))
        self.dropout = nn.Dropout(0.5)

    def forward(self, sg, x):
        h = x
        for l, layer in enumerate(self.layers):
            h = layer(sg, h)
            if l != len(self.layers) - 1:
                h = F.relu(h)
                h = self.dropout(h)
        return h

dataset = DglNodePropPredDataset('ogbn-products')
graph, labels = dataset[0]
graph.ndata['label'] = labels
split_idx = dataset.get_idx_split()
train_idx, valid_idx, test_idx = split_idx['train'], split_idx['valid'], split_idx['test']
graph.ndata['train_mask'] = torch.zeros(graph.num_nodes(), dtype=torch.bool).index_fill_(0, train_idx, True)
graph.ndata['valid_mask'] = torch.zeros(graph.num_nodes(), dtype=torch.bool).index_fill_(0, valid_idx, True)
graph.ndata['test_mask'] = torch.zeros(graph.num_nodes(), dtype=torch.bool).index_fill_(0, test_idx, True)

model = SAGE(graph.ndata['feat'].shape[1], 256, dataset.num_classes).cuda()
opt = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=5e-4)

num_partitions = 1000
sampler = dgl.dataloading.ClusterGCNSampler(
        graph, num_partitions,
        prefetch_node_feats=['feat', 'label', 'train_mask', 'valid_mask', 'test_mask'])
# DataLoader for generic dataloading with a graph, a set of indices (any indices, like
# partition IDs here), and a graph sampler.
# NodeDataLoader and EdgeDataLoader are simply special cases of DataLoader where the
# indices are guaranteed to be node and edge IDs.
dataloader = dgl.dataloading.DataLoader(
        graph,
        torch.arange(num_partitions),
        sampler,
        device='cuda',
        batch_size=100,
        shuffle=True,
        drop_last=False,
        num_workers=0,
        use_uva=True)

durations = []
for _ in range(10):
    t0 = time.time()
    model.train()
    for it, sg in enumerate(dataloader):
        x = sg.ndata['feat']
        y = sg.ndata['label'][:, 0]
        m = sg.ndata['train_mask']
        y_hat = model(sg, x)
        loss = F.cross_entropy(y_hat[m], y[m])
        opt.zero_grad()
        loss.backward()
        opt.step()
        if it % 20 == 0:
            acc = MF.accuracy(y_hat[m], y[m])
            mem = torch.cuda.max_memory_allocated() / 1000000
            print('Loss', loss.item(), 'Acc', acc.item(), 'GPU Mem', mem, 'MB')
    tt = time.time()
    print(tt - t0)
    durations.append(tt - t0)

    model.eval()
    with torch.no_grad():
        val_preds, test_preds = [], []
        val_labels, test_labels = [], []
        for it, sg in enumerate(dataloader):
            x = sg.ndata['feat']
            y = sg.ndata['label'][:, 0]
            m_val = sg.ndata['valid_mask']
            m_test = sg.ndata['test_mask']
            y_hat = model(sg, x)
            val_preds.append(y_hat[m_val])
            val_labels.append(y[m_val])
            test_preds.append(y_hat[m_test])
            test_labels.append(y[m_test])
        val_preds = torch.cat(val_preds, 0)
        val_labels = torch.cat(val_labels, 0)
        test_preds = torch.cat(test_preds, 0)
        test_labels = torch.cat(test_labels, 0)
        val_acc = MF.accuracy(val_preds, val_labels)
        test_acc = MF.accuracy(test_preds, test_labels)
        print('Validation acc:', val_acc.item(), 'Test acc:', test_acc.item())

print(np.mean(durations[4:]), np.std(durations[4:]))
