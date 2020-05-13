import math as m
import torch as t
import torch.nn as nn
from typing import Union

from .base import NeuralNetworkModule


class CasualConv1d(NeuralNetworkModule):
    def __init__(self, in_channels, out_channels, kernel_size,
                 stride=1, dilation=1, groups=1, bias=True, device="cpu"):
        super(CasualConv1d, self).__init__()
        self.stride = stride
        padding = dilation * (kernel_size - 1)
        self.conv1d = nn.Conv1d(in_channels, out_channels, kernel_size, stride,
                                padding, dilation, groups, bias).to(device)

        self.set_input_module(self.conv1d)

    def forward(self, input: t.Tensor):
        # Takes something of shape (N, in_channels, T),
        # returns (N, out_channels, T) if stride = 1.
        # removed length should be dilation * (kernel_size - 1) if stride = 1.
        length = input.shape[-1]
        out_length = (length + self.stride - 1) // self.stride
        out = self.conv1d(input)
        return out[:, :, :out_length].contiguous()


class DenseBlock(NeuralNetworkModule):
    def __init__(self, in_channels, dilation, filters, kernel_size=2, device="cpu"):
        super(DenseBlock, self).__init__()
        self.casualconv1 = CasualConv1d(in_channels, filters, kernel_size,
                                        dilation=dilation, device=device)
        self.casualconv2 = CasualConv1d(in_channels, filters, kernel_size,
                                        dilation=dilation, device=device)

        self.set_input_module(self.casualconv1)

    def forward(self, input: t.Tensor):
        # input is dimensions (N, in_channels, T)
        xf = self.casualconv1(input)
        xg = self.casualconv2(input)
        activations = t.tanh(xf) * t.sigmoid(xg)   # shape: (N, filters, T)
        return t.cat((input, activations), dim=1)  # shape: (N, in_channels + filters, T)


class TCBlock(NeuralNetworkModule):
    def __init__(self, in_channels, seq_length, filters, device):
        super(TCBlock, self).__init__()
        self.dense_blocks = nn.ModuleList(
            [DenseBlock(in_channels + i * filters, 2 ** (i + 1), filters, device=device)
            for i in range(int(m.ceil(m.log(seq_length, 2))))]
        )

        self.set_input_module(self.dense_blocks[0])

    def forward(self, input: t.Tensor):
        # input is dimensions (N, T, in_channels)
        input = t.transpose(input, 1, 2)
        for block in self.dense_blocks:
            input = block(input)
        return t.transpose(input, 1, 2)


class AttentionBlock(NeuralNetworkModule):
    def __init__(self, in_channels, key_size, value_size, device):
        super(AttentionBlock, self).__init__()
        self.linear_query = nn.Linear(in_channels, key_size).to(device)
        self.linear_keys = nn.Linear(in_channels, key_size).to(device)
        self.linear_values = nn.Linear(in_channels, value_size).to(device)
        self.sqrt_key_size = m.sqrt(key_size)

        self.set_input_module(self.linear_query)

    def forward(self, input: t.Tensor, time_steps: Union[t.Tensor, None]):
        # input is dim (N, T, in_channels) where N is the batch_size, and T is the sequence length
        # time steps is dim (N, T)

        # mask_ij = (1 if i >= j else 0), i and j are time steps
        # Note: sequence index may not be equal to time steps, eg: time steps could be:
        # 1, 1, 1, 2, 2, 3, 3, 3, 3, ...., n
        # each sequence element has its matching time step

        length = input.shape[1]

        if time_steps is None:
            # when each sequence element has a different time step, use a diagonal matrix
            mask = t.ones([length, length], dtype=t.uint8, device=input.device).triu(diagonal=1)
        else:
            # upper diagnoal
            mask = time_steps.unsqueeze(dim=2) < time_steps.unsqueeze(dim=1)

        # import pdb; pdb.set_trace()
        keys = self.linear_keys(input)  # shape: (N, T, key_size)
        query = self.linear_query(input)  # shape: (N, T, key_size)
        values = self.linear_values(input)  # shape: (N, T, value_size)
        raw = t.bmm(query, t.transpose(keys, 1, 2))  # shape: (N, T, T)
        tmp = raw.clone()
        # fill with -inf so in softmax, elements will be 0
        tmp.masked_fill_(mask, -float('inf'))
        rel = t.softmax(tmp / self.sqrt_key_size,
                        dim=1)  # shape: (N, T, T), broadcasting over any slice [:, x, :], each row of the matrix
        tmp = t.bmm(rel, values)  # shape: (N, T, value_size)

        # shapes: (N, T, in_channels + value_size), (N, T, T), (N, T, T)
        return t.cat((input, tmp), dim=2), rel.detach(), raw.detach()


class TCDNNet(NeuralNetworkModule):
    def __init__(self, in_channels, out_channels, seq_length, additional_length=0,
                 att_layer=(16, 16),
                 tc_layers=(16, 16),
                 fc_layers=(),
                 activation=t.relu,
                 final_process=None,
                 device="cuda:0"):
        super(TCDNNet, self).__init__()
        num_filters = int(m.ceil(m.log(seq_length, 2)))

        self.layers = nn.ModuleDict()
        channels = in_channels

        for i in range(len(tc_layers)):
            self.layers.add_module("tconv_{}".format(i),
                                   TCBlock(channels, seq_length, tc_layers[i], device))
            channels += num_filters * tc_layers[i]

        self.layers.add_module("attention", AttentionBlock(channels, att_layer[0], att_layer[1], device))
        channels += att_layer[1]

        channels += additional_length
        fc_layers = list(fc_layers) + [out_channels]

        self.layers.add_module("fc_amalgamate", nn.Linear(seq_length, 1).to(device))
        for i in range(len(fc_layers)):
            self.layers.add_module("fc_{}".format(i), nn.Linear(channels, fc_layers[i]).to(device))
            channels = fc_layers[i]

        self.activation = activation if activation is not None else lambda x: x
        self.device = device
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.seq_length = seq_length
        self.tc_num = len(tc_layers)
        self.fc_num = len(fc_layers)
        self.final_procecss = final_process

        self.set_input_module(self.layers["tconv_0"])

    def forward(self, input: t.Tensor, time_steps=None, additional=None):
        # input is dim (N, T, in_channels)
        x = input
        for i in range(self.tc_num):
            x = self.layers["tconv_{}".format(i)](x)

        x, rel, raw = self.layers["attention"](x, time_steps)

        t.set_printoptions(linewidth=200,)
        print(rel)

        # we do not need to output a sequence, so do an amalgamation
        x = t.transpose(x, 1, 2)
        x = t.squeeze(self.activation(self.layers["fc_amalgamate"](x)), dim=-1)

        if additional is not None:
            # additional should be (B, additional_size)
            x = t.cat((x, additional), dim=1)

        for i in range(self.fc_num):
            x = self.layers["fc_{}".format(i)](x)
            if i != self.fc_num - 1:
                x = self.activation(x)

        if self.final_procecss is not None:
            if self.final_procecss == "softmax":
                x = t.softmax(x, dim=-1).clone()
            elif self.final_procecss == "tanh":
                x = t.tanh(x).clone()
            elif self.final_procecss == "sigmoid":
                x = t.sigmoid(x).clone()
            else:
                x = self.final_procecss(x).clone()

        return x, rel, raw