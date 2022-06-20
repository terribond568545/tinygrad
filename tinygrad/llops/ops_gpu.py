import functools
import numpy as np
import pyopencl as cl
from typing import List, Tuple
from tinygrad.helpers import prod
from tinygrad.llops.ops_cpu import unary_op
from tinygrad.ops import UnaryOps, BinaryOps, ReduceOps, MovementOps, ProcessingOps
from tinygrad.shapetracker import ShapeTracker, View, strides_for_shape

cl_ctx, cl_queue = None, None
def get_cl_ctx(): return cl_ctx
def get_cl_queue(): return cl_queue
def require_init_gpu():
  global cl_ctx, cl_queue
  if cl_ctx is None:
    devices = cl.get_platforms()[0].get_devices(device_type=cl.device_type.GPU)
    if len(devices) == 0:  # settle for CPU
      devices = cl.get_platforms()[0].get_devices(device_type=cl.device_type.CPU)
    cl_ctx = cl.Context(devices=devices)
    cl_queue = cl.CommandQueue(cl_ctx)  # this is an in-order command queue

def roundup(x, n=4): return (x+(n-1))//n * n

class GPUBuffer:
  def __init__(self, shape, hostbuf=None):
    require_init_gpu()
    self.st = ShapeTracker(shape)
    self.cl = hostbuf.cl if isinstance(hostbuf, GPUBuffer) else cl.Buffer(cl_ctx, cl.mem_flags.READ_WRITE, 4*roundup(prod(self.shape)))  # padding
    if hostbuf is not None and not isinstance(hostbuf, GPUBuffer):
      cl.enqueue_copy(cl_queue, self.cl, hostbuf.astype(np.float32).ravel(), is_blocking=False)

  @property
  def shape(self): return self.st.shape

  @property
  def dtype(self): return np.float32

  def __repr__(self):
    return f"<GPUBuffer with shape {self.shape!r}>"

  @staticmethod
  def fromCPU(x):
    return GPUBuffer(x.shape, x.view(np.ndarray))

  def toCPU(self):
    data = np.empty(self.shape, dtype=np.float32)
    cl.enqueue_copy(cl_queue, data, contiguous(self).cl, is_blocking=True)
    return data

class CLProgram:
  def __init__(self, name, prg, options, argdtypes):
    self.built = cl.Program(cl_ctx, prg).build(options=options)
    self.clprg = self.built.__getattr__(name)
    if argdtypes is not None: self.clprg.set_scalar_arg_dtypes(argdtypes)
  def __call__(self, *args): self.clprg(cl_queue, *args)

@functools.lru_cache(maxsize=None)
def clbuild(name, prg, options=tuple(), argdtypes=None):
  #print("cache miss")
  #print(prg)
  return CLProgram(name, prg, options, argdtypes)

code_for_op = {
  UnaryOps.NOOP: "A", UnaryOps.RELU: "max(A, (float)0.)", UnaryOps.EXP: "exp(A)", UnaryOps.LOG: "log(A)", UnaryOps.NEG: "-A", UnaryOps.SIGN: "sign(A)",
  BinaryOps.ADD: "A+B", BinaryOps.SUB: "A-B", BinaryOps.MUL: "A*B", BinaryOps.DIV: "B/A", BinaryOps.POW: "pow(A,B)", BinaryOps.CMPEQ: "(A==B)"
}

def contiguous_view(x:GPUBuffer, name:str):
  return f"inline float get_{name}(__global const float *x, int gid) {{ int valid = 1; int idx = gid; {x.st.expr().replace('//', '/')}; return valid ? x[idx] : 0.0;}}"

def elementwise_op(bufs: List[Tuple[str, GPUBuffer]], code):
  assert all(buf.shape == bufs[0][1].shape for _, buf in bufs)
  ret = GPUBuffer(bufs[0][1].shape)
  ewop = clbuild("ewop", '\n'.join([contiguous_view(buf, name) for name, buf in bufs])+
    "__kernel void ewop(__global float *res_g, "+','.join([f"__global const float *{name}_g" for name, _ in bufs])+") {"+
    "int gid = get_global_id(0);"+
    '\n'.join([f"float {name} = get_{name}({name}_g, gid);" for name, _ in bufs])+
    f"res_g[gid] = {code}; }}")
  ewop([prod(ret.shape)], None, ret.cl, *[buf.cl for _, buf in bufs])
  return ret

def unary_op(op, x): return elementwise_op([("A", x)], code_for_op[op])
def binary_op(op, x, y): return elementwise_op([("A", x), ("B", y)], code_for_op[op])
def contiguous(x:GPUBuffer): return x if x.st.contiguous else unary_op(UnaryOps.NOOP, x)

def reduce_op(op, inp, new_shape):
  ret = GPUBuffer(new_shape)
  if op == ReduceOps.SUM: code, start = "out += a", "0.0"
  elif op == ReduceOps.MAX: code, start = "out = max(a,out)", "-INFINITY"
  else: raise Exception(f"{op} isn't supported")

  # reverse operation of expand, this validates inputs
  st = ShapeTracker(ret.shape).movement_op(MovementOps.EXPAND, inp.shape)
  # this takes a ret index to an inp index, indexing 0 on the reduced strides
  view = View(ret.shape, strides_for_shape(inp.shape))

  # generate loops with combined adjacent reduce axis
  acc = 1
  loop_start, loop_end = [], []
  for shp,stride in st.views[-1].shape_strides[::-1]:
    if stride == 0:
      loop_start.append(f"for (int axis_{len(loop_start)} = 0; axis_{len(loop_start)} < {shp}; axis_{len(loop_start)}++) {{")
      loop_end.append(f"idx += {acc}; }} idx -= {shp*acc};")
    acc *= shp

  # TODO: support multistage reduces
  prg = contiguous_view(inp, 'A')+"""
  __kernel void reduce(__global const float *a_g, __global float *res_g) {
    int gid = get_global_id(0); int idx = gid;"""+view.expr.replace('//', '/')+""";
    float out = """+start+""";\n"""+ \
      '\n'.join(loop_start[::-1])+"""
        float a = get_A(a_g, idx);
        """+code+""";\n"""+ \
      '\n'.join(loop_end)+"""
    res_g[gid] = out;
  }"""
  clbuild("reduce", prg)([prod(ret.shape)], None, inp.cl, ret.cl)
  return ret

def movement_op(op, x, arg):
  ret = GPUBuffer(x.st, x)
  ret.st.movement_op(op, arg)
  return ret

def processing_op(op,x,w,C):
  ret = GPUBuffer((C.bs, C.cout, C.oy, C.ox))
  assert op == ProcessingOps.CONV, f"{op} isn't supported"
  ints = ''.join(f"int {x} = {getattr(C, x)};" for x in ["H", "W", "cin", "ys", "xs", "dx", "dy", "px", "py"])
  params = [(f"int {x}", getattr(C, x)) for x in ["groups", "rcout", "oy", "ox", "iy", "ix"]]
  conv_prg = clbuild("conv", """
  __kernel void conv(__global const float* restrict input, __global const float* restrict weight, __global float* restrict output,
    """+','.join([x[0] for x in params])+""") {
    """+ints+"""
    int B = get_global_id(0)/(groups*rcout);  // range 0-bs
    int g = (get_global_id(0)/rcout)%groups;
    int c = get_global_id(0) % rcout;

    int Y = get_global_id(1);  // range 0-oy
    int X = get_global_id(2);  // range 0-ox
    int IY = Y*ys;
    int IX = X*xs;

    int gid = get_global_id(0)*oy*ox + Y*ox + X;

    float acc = 0.0;
    for (int ci = 0; ci < cin; ci++) {
      for (int y = 0; y < H; y++) { for (int x = 0; x < W; x++) {
        int idx_y = y*dy + IY - py;
        int idx_x = x*dx + IX - px;
#ifdef ALLVALID
        acc += input[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + idx_y*ix + idx_x] * \
          weight[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
#else
        int valid = (idx_y >= 0 && idx_y < iy && idx_x >= 0 && idx_x < ix);
        acc += valid * input[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + clamp(idx_y, 0, iy-1)*ix + clamp(idx_x, 0, ix-1)] * \
          weight[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
#endif
      } }
    }
    output[gid] = acc;
  }""",
  options=tuple(["-DALLVALID"]) if C.px == 0 and C.py == 0 else tuple(),
  argdtypes=tuple([None, None, None] + [np.int32]*len(params)))
  conv_prg([C.bs*C.cout, C.oy, C.ox], None, contiguous(x).cl, contiguous(w).cl, ret.cl, *[x[1] for x in params])
  return ret
