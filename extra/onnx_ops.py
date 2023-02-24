from tinygrad.tensor import Tensor
from tinygrad.helpers import prod
from extra.onnx import safe_numpy
import numpy as np

def Unsqueeze(data, axes):
  axes = [len(data.shape) + int(x) if x < 0 else int(x) for x in safe_numpy(axes)]
  ptr = 0
  new_shape = []
  for i in range(len(data.shape) + len(axes)):
    if i in axes: new_shape.append(1)
    else:
      new_shape.append(data.shape[ptr])
      ptr += 1
  return data.reshape(new_shape)

def Gemm(A, B, C=None, alpha=1.0, beta=1.0, transA=0, transB=0):
  ret = alpha * ((A.transpose() if transA == 1 else A) @ (B.transpose() if transB == 1 else B))
  if C is not None: ret += beta * C
  return ret

# TODO: this is copied from tinygrad/nn/__init__.py
# spatial is from opset 7 and has since been removed
def BatchNormalization(X, scale, B, input_mean, input_var, epsilon=1e-05, momentum=0.9, training_mode=0, spatial=1):
  if training_mode:
    x_detached = X.detach()
    current_mean = x_detached.mean(axis=(0,2,3))
    y = (x_detached - current_mean.reshape(shape=[1, -1, 1, 1]))
    current_var = (y*y).mean(axis=(0,2,3))
    current_invstd = current_var.add(epsilon).pow(-0.5)

    running_mean = input_mean * momentum + current_mean * (1 - momentum)
    running_var = input_var * momentum + current_var * (1 - momentum)

    return X.batchnorm(scale, B, current_mean, current_invstd), running_mean, running_var
  else:
    invstd = (input_var + epsilon)**-0.5
    return X.batchnorm(scale, B, input_mean, invstd)

def _padding(pads=None, auto_pad="NOTSET"):
  assert auto_pad == "NOTSET"  # TODO: write this
  return (pads[1], pads[3], pads[0], pads[2]) if pads is not None else (0,0,0,0)

def AveragePool(X, kernel_shape, auto_pad="NOTSET", ceil_mode=0, count_include_pad=0, dilations=1, pads=None, strides=1):
  # TODO: the padding shouldn't be counted in the average! this is causing a test failure
  assert ceil_mode == 0 and count_include_pad == 0 and dilations == 1
  return X.pad2d(_padding(pads, auto_pad)).avg_pool2d(kernel_shape, stride=strides)

def MaxPool(X, kernel_shape, auto_pad="NOTSET", ceil_mode=0, dilations=1, pads=None, storage_order=0, strides=1):
  # TODO: the padding should be infinity, not 0!
  assert ceil_mode == 0 and storage_order == 0 and dilations == 1
  return X.pad2d(_padding(pads, auto_pad)).max_pool2d(kernel_shape, stride=strides)

def Conv(X, W, B=None, auto_pad="NOTSET", dilations=1, group=1, kernel_shape=None, pads=None, strides=1):
  return X.conv2d(W, B, stride=strides, groups=group, dilation=dilations, padding=_padding(pads, auto_pad))

# TODO: copied from tensor.py
def Dropout(data, ratio=0.5, training_mode=False, seed=None):
  # TODO: mask should be a boolean tensor
  if not training_mode: return data, Tensor.ones(*data.shape)  # if mask is requested as output it will contain all ones.
  if seed is not None: Tensor.manual_seed(seed)
  _mask : np.ndarray = np.asarray(Tensor._rng.binomial(1, 1.0-ratio, size=data.shape), dtype=data.dtype)
  mask = Tensor(_mask, requires_grad=False, device=data.device)
  return data * mask * (1/(1.0 - ratio)), mask

def Shape(data, end=None, start=0): return list(data.shape)[start:end]

# TODO: this doesn't match Tensor.flatten behavior
def Flatten(input, axis=1):
  new_shape = (1, -1) if axis == 0 else (prod(input.shape[0:axis]), -1)
  return input.reshape(new_shape)

# TODO: abstract out the broadcast logic in tensor
def Expand(input, shape):
  x_shape, y_shape = input.shape, [int(x) for x in safe_numpy(shape)]
  # copied from _broadcasted
  x_shape, y_shape = [([1]*(max(len(x_shape), len(y_shape))-len(t_shape)) + list(t_shape)) for t_shape in [x_shape, y_shape]]
  shape_ret = tuple(max(sx, sy) for sx,sy in zip(x_shape, y_shape))
  # TODO: openpilot is broken if we actually do the expand!!
  return input.reshape(x_shape) #.expand(shape_ret)

def Exp(input): return input.exp()
def Softmax(input, axis=-1): return input.softmax(axis)

def _axes(axes, noop_with_empty_axes): return [int(x) for x in safe_numpy(axes)] if axes is not None else ([] if noop_with_empty_axes else None)

def ReduceMax(data, axes=None, keepdims=1, noop_with_empty_axes=0): return data.max(_axes(axes, noop_with_empty_axes), keepdim=keepdims)
def ReduceSum(data, axes=None, keepdims=1, noop_with_empty_axes=0): return data.sum(_axes(axes, noop_with_empty_axes), keepdim=keepdims)
def ReduceSumSquare(data, axes=None, keepdims=1, noop_with_empty_axes=0): return data.square().sum(_axes(axes, noop_with_empty_axes), keepdim=keepdims)
def ReduceL2(data, axes=None, keepdims=1, noop_with_empty_axes=0): return data.square().sum(_axes(axes, noop_with_empty_axes), keepdim=keepdims).sqrt()
