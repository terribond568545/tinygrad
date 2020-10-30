#!/usr/bin/env python
import unittest
import numpy as np
from tinygrad.nn import *
import torch

class TestNN(unittest.TestCase):
  def test_batchnorm2d(self):
    sz = 4

    # create in tinygrad
    bn = BatchNorm2D(sz, eps=1e-5)
    bn.weight = Tensor.randn(sz)
    bn.bias = Tensor.randn(sz)
    bn.running_mean = Tensor.randn(sz)
    bn.running_var = Tensor.randn(sz)
    bn.running_var.data[bn.running_var.data < 0] = 0

    # create in torch
    tbn = torch.nn.BatchNorm2d(sz).eval()
    tbn.weight[:] = torch.tensor(bn.weight.data)
    tbn.bias[:] = torch.tensor(bn.bias.data)
    tbn.running_mean[:] = torch.tensor(bn.running_mean.data)
    tbn.running_var[:] = torch.tensor(bn.running_var.data)

    # trial
    inn = Tensor.randn(2, sz, 3, 3)

    # in tinygrad
    outt = bn(inn)

    # in torch
    toutt = tbn(torch.tensor(inn.data))

    # close
    np.testing.assert_allclose(outt.data, toutt.detach().numpy(), rtol=1e-5)


if __name__ == '__main__':
  unittest.main()
