# inspired by https://github.com/karpathy/micrograd/blob/master/micrograd/engine.py
import os, atexit, time, inspect, functools, importlib
from collections import defaultdict
import numpy as np
from tinygrad.helpers import prod

# **** profiler ****

GRAPH = os.getenv("GRAPH", None) is not None
if GRAPH:
  import networkx as nx
  G = nx.DiGraph()
  def save_graph_exit():
    print("saving", G)
    nx.drawing.nx_pydot.write_dot(G, '/tmp/net.dot')
  atexit.register(save_graph_exit)

DEBUG = os.getenv("DEBUG", None) is not None
if DEBUG:
  debug_counts, debug_times = defaultdict(int), defaultdict(float)
  def print_debug_exit():
    for name, _ in sorted(debug_times.items(), key=lambda x: -x[1]):
      print(f"{name:>20} : {debug_counts[name]:>6} {debug_times[name]:>10.2f} ms")
  atexit.register(print_debug_exit)

global_num_max = 0
class ProfileOp:
  def __init__(self, ctx, name, x, backward=False):
    self.ctx, self.name, self.x, self.output, self.backward = ctx, f"back_{name}" if backward else name, x, None, backward
  def __enter__(self):
    if DEBUG: self.st = time.time()
    return self
  def __exit__(self, *junk):
    if GRAPH:
      def nm(x):
        global global_num_max
        if getattr(x, 'global_num', None) is None:
          setattr(x, 'global_num', global_num_max)
          global_num_max += 1
        return f"<<< {x.global_num} >>>"
      # connect inputs to outputs
      for x in self.x:
        for y in self.output:
          G.add_edge(nm(x.data), nm(y.data), label=self.name, color="blue" if self.backward else "black")
          G.nodes[nm(x.data)]['label'], G.nodes[nm(y.data)]['label'] = str(x.shape), str(y.shape)
      # which saved tensors does this backward depend on?
      saved_tensors = filter(lambda x: any(isinstance(x, v) for v in Device.buffers.values()), self.ctx.saved_tensors)
      if self.backward:
        for x in saved_tensors:
          for y in self.output:
            G.add_edge(nm(x), nm(y.data), label=self.name, color="red")
      # did this forward create any intermediate tensors?
      if not self.backward:
        x_data = [nm(x.data) for x in self.x] + [nm(x.data) for x in self.output]
        for y in saved_tensors:
          if nm(y) not in x_data:    # if intermediate tensors are inputs they don't count
            for x in self.x:
              G.add_edge(nm(x.data), nm(y), label=self.name, color="purple")
    if DEBUG:
      self.output[0].data.toCPU()
      et = (time.time()-self.st)*1000.
      debug_counts[self.name] += 1
      debug_times[self.name] += et
      print(f"{self.name:>20} : {et:>7.2f} ms {str([y.shape for y in self.x]):>40} -> {str([y.shape for y in self.output])}")

# **** enumerate supported devices ****

class Device:
  _ops = sorted(os.listdir(os.path.join(os.path.dirname(os.path.realpath(__file__)), "llops")))
  imports = dict(enumerate([os.path.splitext(x)[0] for x in _ops if x.startswith("ops_")]))
  DEFAULT = None
  buffers, llops = {}, {}
  for i,op in imports.items():
    name = op[len("ops_"):].upper()
    vars()[name] = i
    DEFAULT = i if os.environ.get(name, 0) == "1" else DEFAULT
    try:
      llops[i] = importlib.import_module('tinygrad.llops.'+op)
      buffers[i] = [cls for name, cls in inspect.getmembers(llops[i], inspect.isclass) if name.endswith("Buffer")][0]
    except ImportError as e:
      print(op, "not available", e)
  DEFAULT = CPU if DEFAULT is None else DEFAULT

# **** start with two base classes, Tensor and Function ****

class Tensor:
  did_float_warning = False
  training = False

  def __init__(self, data, device=Device.DEFAULT, requires_grad=True):
    self.device, self.data = device, self._move_data(data, device)

    self.grad, self.requires_grad = None, requires_grad

    # internal variables used for autograd graph construction
    self._ctx = None

  def __repr__(self):
    return f"<Tensor {self.data!r} with grad {(self.grad.data if self.grad else None)!r}>"

  def assign(self, x):
    if not isinstance(x, Tensor):
      x = Tensor(x)
    assert self.shape == x.shape
    self.data = x.data

  @property
  def shape(self):
    return self.data.shape

  @staticmethod
  def _get_data_dtype(data):
    return data.getdtype() if getattr(data, 'getdtype', None) else data.dtype

  @property
  def dtype(self):
    return Tensor._get_data_dtype(self.data)

  # ***** creation helper functions *****

  @classmethod
  def zeros(cls, *shape, **kwargs):
    return cls(np.zeros(shape, dtype=np.float32), **kwargs)

  @classmethod
  def ones(cls, *shape, **kwargs):
    return cls(np.ones(shape, dtype=np.float32), **kwargs)

  @classmethod
  def randn(cls, *shape, **kwargs):
    return cls(np.random.randn(*shape).astype(np.float32), **kwargs)
  
  @classmethod
  def arange(cls, stop, start=0, **kwargs):
    return cls(np.arange(start=start, stop=stop).astype(np.float32), **kwargs)

  @classmethod
  def uniform(cls, *shape, **kwargs):
    return cls((np.random.uniform(-1., 1., size=shape)/np.sqrt(prod(shape))).astype(np.float32), **kwargs)

  @classmethod
  def eye(cls, dim, **kwargs):
    return cls(np.eye(dim).astype(np.float32), **kwargs)

  # ***** toposort and backward pass *****

  def deepwalk(self):
    def _deepwalk(node, visited, nodes):
      visited.add(node)
      if node._ctx:
        [_deepwalk(i, visited, nodes) for i in node._ctx.parents if i not in visited]
        nodes.append(node)
      return nodes
    return _deepwalk(self, set(), [])

  def backward(self):
    assert self.shape == (1,)

    # fill in the first grad with one
    # this is "implicit gradient creation"
    self.grad = Tensor.ones(*self.shape, device=self.device, requires_grad=False)

    for t0 in reversed(self.deepwalk()):
      if not any(x.requires_grad for x in t0._ctx.parents):
        continue
      assert (t0.grad is not None)
      with ProfileOp(t0._ctx, t0._ctx.__class__.__name__, [t0.grad], backward=True) as po:
        grads = t0._ctx.backward(t0._ctx, t0.grad.data)
        grads = [Tensor(g, device=self.device, requires_grad=False) if g is not None else None
          for g in ([grads] if len(t0._ctx.parents) == 1 else grads)]
        po.output = [x for x in grads if x is not None]   # backward can return None if no required gradient, don't profile it
      for t, g in zip(t0._ctx.parents, grads):
        if g is not None and t.requires_grad:
          assert g.shape == t.shape, \
            f"grad shape must match tensor shape in {self._ctx!r}, {g.shape!r} != {t.shape!r}"
          t.grad = g if t.grad is None else (t.grad + g)

  # ***** tinygrad supports many devices *****

  @staticmethod
  def _move_data(data, device):
    if isinstance(data, list):
      data = np.array(data, dtype=np.float32)
    if isinstance(data, np.ndarray):
      data = data.view(Device.buffers[Device.CPU])
    if isinstance(data, Device.buffers[device]):
      return data

    if Tensor._get_data_dtype(data) != np.float32 and not Tensor.did_float_warning:
      # warning? float64 is actually needed for numerical jacobian
      print(f"warning, {data.shape!r} isn't float32, it's {data.dtype}")
      Tensor.did_float_warning = True

    data = data.toCPU().view(Device.buffers[Device.CPU])
    return Device.buffers[device].fromCPU(data)

  def to_(self, device):
    self.data, self.device = self._move_data(self.data, device), device
    if self.grad: self.grad.to_(device)

  def to(self, device):
    ret = Tensor(self.data, device)
    if self.grad: ret.grad = self.grad.to(device)
    return ret

  def detach(self):
    return Tensor(self.data, device=self.device, requires_grad=False)

  # ***** non first class ops *****
  
  def __getitem__(self, val):
    arg = []
    new_shape = []
    if val is not None:
      for i, s in enumerate(val if isinstance(val, (list, tuple)) else [val]):
        if isinstance(s, int):
          arg.append((s, s + 1))
        else:
          arg.append((s.start if s.start is not None else 0,
            (s.stop if s.stop >=0 else self.shape[i]+s.stop) if s.stop is not None else self.shape[i]))
          new_shape.append(arg[-1][1] - arg[-1][0])
          assert s.step is None or s.step == 1
    new_shape += self.shape[len(arg):]
    ret = self.slice(arg = arg + [(0,self.shape[i]) for i in range(len(arg), len(self.shape))])
    return ret.reshape(shape=new_shape) if tuple(ret.shape) != tuple(new_shape) else ret

  def cat(self, y, dim=0):
    assert len(self.shape) == len(y.shape)
    dim = (dim + len(self.shape)) if dim < 0 else dim
    s1, s2 = [], []
    for i in range(len(self.shape)):
      if i != dim:
        assert self.shape[i] == y.shape[i]
        s1.append((0, self.shape[i]))
        s2.append((0, self.shape[i]))
      else:
        s1.append((0, self.shape[i]+y.shape[i]))
        s2.append((-self.shape[i], y.shape[i]))
    return self.slice(arg=s1) + y.slice(arg=s2)

  def pad2d(self, padding):
    return self[:, :, -padding[2]:self.shape[2]+padding[3], -padding[0]:self.shape[3]+padding[1]]

  def matmul(x, w):
    bs, groups = prod(x.shape[0:-2]), prod(w.shape[0:-2])
    cin, cout = w.shape[-2], w.shape[-1]
    out_shape_t = tuple(list(x.shape[0:-2])+[cout,-1])
    order = tuple(list(range(len(x.shape)-2))+[len(x.shape)-1, len(x.shape)-2])
    worder = tuple(list(range(len(w.shape)-2))+[len(w.shape)-1, len(w.shape)-2])

    # NOTE: with NHWC we can remove the transposes
    # bs x groups*cin x H x W
    cx = x.transpose(order=order).reshape(shape=(bs//groups, groups*cin, -1, 1))
    # groups*cout x cin x H, W
    cw = w.transpose(order=worder).reshape(shape=(groups*cout, cin, 1, 1))
    return cx.conv2d(cw, groups=groups).reshape(shape=out_shape_t).transpose(order=order)

  dot = matmul

  def _canonicalize_reduce_axis(self, axis):
    if axis is None: axis = range(len(self.shape))
    if isinstance(axis, int): axis = [axis]
    axis = tuple([x if x >= 0 else x+len(self.shape) for x in axis])
    shape = [self.shape[i] for i in range(len(self.shape)) if i not in axis]
    shape = [1] if shape == [] else shape
    return axis, shape

  def sum(self, axis=None, keepdim=False):
    axis, out_shape = self._canonicalize_reduce_axis(axis)
    ret = self._sum(axis=axis)
    return ret if keepdim or ret.shape == out_shape else ret.reshape(shape=out_shape)

  def max(self, axis=None, keepdim=False):
    axis, out_shape = self._canonicalize_reduce_axis(axis)
    ret = self._max(axis=axis)
    return ret if keepdim or ret.shape == out_shape else ret.reshape(shape=out_shape)

  def mean(self, axis=None, keepdim=False):
    out = self.sum(axis=axis, keepdim=keepdim)
    return out * (prod(out.shape)/prod(self.shape))

  def sqrt(self):
    return self.pow(0.5)

  def div(self, y):
    return self * (y ** -1.0)
  __truediv__ = div

  def sigmoid(self):
    #e = self.exp(); return e.div(1 + e)
    return (1.0 + (0.0-self).exp()) ** -1.0

  def swish(self):
    return self * self.sigmoid()

  def relu6(self):
    return self.relu() - (self-6).relu()

  def hardswish(self):
    return self * (self+3).relu6() * (1/6)

  def tanh(self):
    return 2.0 * ((2.0 * self).sigmoid()) - 1.0

  def gelu(x):
    # https://github.com/huggingface/transformers/blob/master/src/transformers/activations.py
    #import torch; return Tensor(torch.nn.functional.gelu(torch.tensor(x.data)).numpy())
    return 0.5 * x * (1 + (x * 0.7978845608 * (1 + 0.044715 * x * x)).tanh())

  def leakyrelu(self, neg_slope=0.01):
    return self.relu() - (-neg_slope*self).relu()

  def softmax(self):
    m = self.max(axis=len(self.shape)-1, keepdim=True)
    e = (self - m).exp()
    ss = e.sum(axis=len(self.shape)-1, keepdim=True)
    return e.div(ss)

  def logsoftmax(self):
    m = self.max(axis=len(self.shape)-1, keepdim=True)
    ss = m + (self-m).exp().sum(axis=len(self.shape)-1, keepdim=True).log()
    return self - ss

  def dropout(self, p=0.5):
    if Tensor.training:
      _mask = np.asarray(np.random.binomial(1, 1.0-p, size=self.shape), dtype=self.dtype)
      return self * Tensor(_mask, requires_grad=False, device=self.device) * (1/(1.0 - p))
    else:
      return self

  def softplus(self, limit=20, beta=1):
    # safe softplus - 1/beta*log(1 + exp(beta*x)) (PyTorch)
    eb = (self*beta).exp()
    ret = (1 + eb).log()
    return (1/beta)*ret

  def mish(self):
    return self * (self.softplus().tanh()) # x*tanh(softplus(x))

  def abs(self):
    return self.relu() + (-1.0*self).relu()

  def sign(self):
    return self / (self.abs() + 1e-10)

  def _pool2d(self, py, px):
    xup = self[:, :, :self.shape[2]-self.shape[2]%py, :self.shape[3]-self.shape[3]%px] if (self.shape[2]%py != 0) or (self.shape[3]%px != 0) else self
    return xup.reshape(shape=(xup.shape[0], xup.shape[1], xup.shape[2]//py, py, xup.shape[3]//px, px))

  def avg_pool2d(self, kernel_size=(2,2)):
    return self._pool2d(*kernel_size).mean(axis=(3,5))

  def max_pool2d(self, kernel_size=(2,2)):
    return self._pool2d(*kernel_size).max(axis=(3,5))

  def conv2d(self, weight, bias=None, stride=1, groups=1):
    ret = self._conv2d(weight, stride=stride, groups=groups)
    return ret if bias is None else ret.add(bias.reshape(shape=[1, -1, 1, 1]))

  # ***** functional nn ops *****

  def linear(self, weight, bias):
    shp = [1] * (len(self.shape)-1) + [-1]
    ret = self.mul(weight.reshape(shape=shp)) if len(weight.shape) == 1 else self.dot(weight)
    return ret.add(bias.reshape(shape=shp))

  def sequential(self, ll):
    for l in ll: self = l(self)
    return self

  def layernorm(x, eps=1e-5):
    y = (x - x.mean(axis=-1, keepdim=True))
    return y.div((y*y).mean(axis=-1, keepdim=True).add(eps).sqrt())

# An instantiation of the Function is the Context
class Function:
  def __new__(cls, *args, **kwargs):
    cls.forward = staticmethod(cls.forward)
    cls.backward = staticmethod(cls.backward)
    return super().__new__(cls)

  def __init__(self, device, *tensors):
    self.device = device
    self.parents = tensors
    self.needs_input_grad = [t.requires_grad for t in tensors]
    self.requires_grad = any(self.needs_input_grad)
    self.saved_tensors = []

  buffer = property(lambda self: Device.buffers[self.device])
  op = property(lambda self: Device.llops[self.device])

  def save_for_backward(self, *x):
    if self.requires_grad:
      self.saved_tensors.extend(x)

  @classmethod
  def apply(cls, *x, **kwargs):
    tt = [arg for arg in x if isinstance(arg, Tensor)][0]  # this is the prototype tensor

    # create tensors from number arguments
    x = [Tensor(np.array([arg], dtype=tt.dtype), device=tt.device, requires_grad=False) if not isinstance(arg, Tensor) else arg for arg in x]
    assert all([tt.device == t.device for t in x]), "All tensors are not on the same device"

    ctx = cls(tt.device, *x)
    with ProfileOp(ctx, ctx.__class__.__name__, x) as po:
      ret = Tensor(cls.forward(ctx, *[t.data for t in x], **kwargs),
                   device=ctx.device, requires_grad=ctx.requires_grad)
      po.output = [ret]
    if ret.requires_grad:
      ret._ctx = ctx    # used by autograd engine
    return ret

def register(name, fxn):
  def dispatch(*x, **kwargs): return fxn.apply(*x, **kwargs)   # TODO: there's probably a very pythonic thing to replace this with
  setattr(Tensor, "_"+name if (getattr(Tensor, name, None) is not None) else name, dispatch)
  if name in ['add', 'sub', 'mul', 'pow', 'matmul']:
    setattr(Tensor, f"__{name}__", dispatch)
    setattr(Tensor, f"__i{name}__", lambda self,x: self.assign(dispatch(self,x)))
    setattr(Tensor, f"__r{name}__", lambda self,x: dispatch(x,self))

# register functions to move between devices
for device in [device for device in Device.__dict__.keys() if device[0] != "_"]:
  setattr(Tensor, f"{device.lower()}", functools.partialmethod(Tensor.to, Device.__dict__[device]))
  setattr(Tensor, f"{device.lower()}_", functools.partialmethod(Tensor.to_, Device.__dict__[device]))

# this registers all the mlops "math" operations
for name, cls in inspect.getmembers(importlib.import_module('tinygrad.mlops'), inspect.isclass):
  if name[0] != "_" and name != "Function" and not name.endswith("Ops"): register(name.lower(), cls)
