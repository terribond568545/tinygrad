#!/usr/bin/env python3
# tinygrad implementation of https://github.com/tysam-code/hlb-CIFAR10/blob/main/main.py
# https://myrtle.ai/learn/how-to-train-your-resnet-8-bag-of-tricks/
# https://siboehm.com/articles/22/CUDA-MMM
# TODO: gelu is causing nans!
import numpy as np
import time
from datasets import fetch_cifar
from tinygrad import nn
from tinygrad.nn import optim
from tinygrad.tensor import Tensor
from tinygrad.helpers import getenv
from extra.training import train, evaluate
from extra.utils import get_parameters
from tinygrad.ops import GlobalCounters

num_classes = 10

# TODO: eval won't work with track_running_stats=False
class ConvGroup:
  def __init__(self, channels_in, channels_out, short, se=True):
    self.short, self.se = short, se and not short
    self.conv = [nn.Conv2d(channels_in if i == 0 else channels_out, channels_out, kernel_size=3, padding=1, bias=False) for i in range(1 if short else 3)]
    self.norm = [nn.BatchNorm2D(channels_out, track_running_stats=False) for _ in range(1 if short else 3)]
    if self.se: self.se1, self.se2 = nn.Linear(channels_out, channels_out//16), nn.Linear(channels_out//16, channels_out)

  def __call__(self, x):
    x = self.conv[0](x).max_pool2d(2)
    x = self.norm[0](x).relu()
    if self.short: return x
    residual = x
    mult = self.se2(self.se1(residual.mean((2,3))).relu()).sigmoid().reshape(x.shape[0], x.shape[1], 1, 1) if self.se else 1.0
    x = self.norm[1](self.conv[1](x)).relu()
    x = self.norm[2](self.conv[2](x) * mult).relu()
    return x + residual

class SpeedyResNet:
  def __init__(self):
    # TODO: add whitening
    self.net = [
      nn.Conv2d(3, 64, kernel_size=1),
      nn.BatchNorm2D(64, track_running_stats=False),
      lambda x: x.relu(),
      ConvGroup(64, 128, short=False),
      ConvGroup(128, 256, short=True),
      ConvGroup(256, 512, short=False),
      lambda x: x.max((2,3)),
      nn.Linear(512, num_classes, bias=False)
    ]

  # note, pytorch just uses https://pytorch.org/docs/stable/generated/torch.nn.CrossEntropyLoss.html instead of logsoftmax
  def __call__(self, x): return x.sequential(self.net).logsoftmax()

# TODO: this will become @tinygrad.jit
first, cl_cache, loss = True, None, None
from tinygrad.llops.ops_gpu import CL
def train_step_jitted(model, optimizer, X, Y, enable_jit=False):
  global cl_cache, first, loss

  if not cl_cache:
    GlobalCounters.global_ops = 0
    if not first:
      CL.CACHE = []
    if enable_jit: first = False
    out = model(X)
    loss = out.mul(Y).mean()
    if not getenv("DISABLE_BACKWARD"):
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()
      loss.realize()
    if not first:
      cl_cache = CL.CACHE
      CL.CACHE = None

  if cl_cache:
    GlobalCounters.global_ops = 0
    for prg, args in cl_cache:
      prg.clprg(CL().cl_queue, *args)
      GlobalCounters.global_ops += prg.op_estimate
  return loss

def fetch_batch(X_train, Y_train, BS):
  # fetch a batch
  samp = np.random.randint(0, X_train.shape[0], size=(BS))
  Y = np.zeros((BS, num_classes), np.float32)
  Y[range(BS),Y_train[samp]] = -1.0*num_classes
  X = Tensor(X_train[samp])
  Y = Tensor(Y.reshape(BS, num_classes))
  return X.realize(), Y.realize()

def train_cifar():
  Tensor.training = True
  BS = getenv("BS", 512)
  X_train,Y_train = fetch_cifar(train=True)
  print(X_train.shape, Y_train.shape)
  X_test,Y_test = fetch_cifar(train=False)
  Xt, Yt = fetch_batch(X_test, Y_test, BS=BS)
  model = SpeedyResNet()
  def make_lr(x): return Tensor([x]).realize()
  optimizer = optim.SGD(get_parameters(model), lr=0.001)
  #optimizer = optim.Adam(get_parameters(model), lr=3e-4)

  # 97 steps in 2 seconds = 20ms / step
  # step is 1163.42 GOPS = 56 TFLOPS!!!, 41% of max 136
  # 4 seconds for tfloat32 ~ 28 TFLOPS, 41% of max 68
  # 6.4 seconds for float32 ~ 17 TFLOPS, 50% of max 34.1
  # 4.7 seconds for float32 w/o channels last. 24 TFLOPS. we get 50ms then i'll be happy. only 64x off

  # https://www.anandtech.com/show/16727/nvidia-announces-geforce-rtx-3080-ti-3070-ti-upgraded-cards-coming-in-june
  # 136 TFLOPS is the theoretical max w float16 on 3080 Ti

  X, Y = fetch_batch(X_train, Y_train, BS=BS)
  for i in range(getenv("STEPS", 10)):
    #new_lr = (0.003 * i/300) if i < 300 else min(0.00001, 0.003 - 0.003 * i/300)
    #optimizer.lr = Tensor([new_lr]).realize()

    if i%10 == 0:
      # use training batchnorm (and no_grad would change the kernels)
      outs = model(Xt).numpy().argmax(axis=1)
      correct = outs == Yt.numpy().argmin(axis=1)
      print(f"eval {sum(correct)}/{len(correct)} {sum(correct)/len(correct)*100.0:.2f}%")
    GlobalCounters.time_sum = 0
    CL.kernel_count = -1
    CL.ops_sum = 0  # TODO: this should be GlobalCounters.global_ops
    st = time.monotonic()
    loss = train_step_jitted(model, optimizer, X, Y, enable_jit=getenv("CLCACHE"))
    et = time.monotonic()
    X, Y = fetch_batch(X_train, Y_train, BS=BS)  # do this here
    loss_cpu = loss.numpy()[0]
    cl = time.monotonic()
    print(f"{i:3d} {(cl-st)*1000.0:7.2f} ms run, {(et-st)*1000.0:7.2f} ms python, {(cl-et)*1000.0:7.2f} ms CL, {loss_cpu:7.2f} loss, {CL.mem_used/1e9:.2f} GB used, {GlobalCounters.global_ops*1e-9/(cl-st):9.2f} GFLOPS")

  #train(model, X, Y, optimizer, steps=X.shape[0]//BS, BS=BS)
  #evaluate(model, X_test, Y_test)

if __name__ == "__main__":
  train_cifar()
