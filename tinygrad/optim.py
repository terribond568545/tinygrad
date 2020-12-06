# sorted in order of increasing complexity

import numpy as np
from tinygrad.tensor import Tensor

class Optimizer:
  def __init__(self, params):
    self.params = [x for x in params if x.requires_grad == True]

  def num(self, x):
    return Tensor([x], gpu=self.params[0].gpu, requires_grad=False)

  def zero_grad(self):
    for param in self.params:
      param.grad = None

class SGD(Optimizer):
  def __init__(self, params, lr=0.001):
    super(SGD, self).__init__(params)
    self.lr = self.num(lr)

  def step(self):
    for t in self.params:
      t -= t.grad * self.lr

class RMSprop(Optimizer):
  def __init__(self, params, lr=0.001, decay=0.9, eps=1e-8):
    super(RMSprop, self).__init__(params)
    self.lr, self.decay, self.eps, self.one, self.two = [self.num(x) for x in [lr, decay, eps, 1, 2]]

    self.v = [Tensor(np.zeros(t.shape, dtype=np.float32), gpu=params[0].gpu, requires_grad=False) for t in self.params]

  def step(self):
    for i, t in enumerate(self.params):
      self.v[i] = self.decay * self.v[i] + (self.one - self.decay) * t.grad.pow(self.two)
      t -= self.lr.div(self.v[i].sqrt() + self.eps) * t.grad

class Adam(Optimizer):
  def __init__(self, params, lr=0.001, b1=0.9, b2=0.999, eps=1e-8):
    super(Adam, self).__init__(params)
    self.lr, self.b1, self.b2, self.eps, self.t, self.one, self.two = [self.num(x) for x in [lr, b1, b2, eps, 0, 1, 2]]

    self.m = [Tensor(np.zeros(t.shape, dtype=np.float32), gpu=params[0].gpu, requires_grad=False) for t in self.params]
    self.v = [Tensor(np.zeros(t.shape, dtype=np.float32), gpu=params[0].gpu, requires_grad=False) for t in self.params]

  def step(self):
    self.t = self.t + self.one
    a = self.lr * (self.one - self.b2.pow(self.t)).sqrt().div(self.one - self.b1.pow(self.t))
    for i,t in enumerate(self.params):
      self.m[i] = self.b1 * self.m[i] + (self.one - self.b1) * t.grad
      self.v[i] = self.b2 * self.v[i] + (self.one - self.b2) * t.grad.pow(self.two)
      t -= a * self.m[i].div(self.v[i].sqrt() + self.eps)
