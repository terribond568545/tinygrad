# this is focused on speed
# it may not run everything

import pathlib
import numpy as np
from tinygrad.ops import MovementOps, ProcessingOps
from tinygrad.llops.ops_gpu import require_init_gpu, clbuild, get_cl_queue, get_cl_ctx
from tinygrad.llops.ops_gpu import contiguous
from tinygrad.llops.ops_gpu import unary_op as unary_op_gpu, binary_op as binary_op_gpu, reduce_op as reduce_op_gpu
from tinygrad.helpers import prod
from tinygrad.shapetracker import ShapeTracker
import pyopencl as cl
from copy import deepcopy

def roundup(x, n=4): return (x+(n-1))//n * n
def flip(x): return (x[1], x[0])
class OpenCLBuffer:
  def __init__(self, shape, hostbuf=None, _buf=None, _image=None):
    require_init_gpu()
    self.shapetracker = deepcopy(shape) if isinstance(shape, ShapeTracker) else ShapeTracker(*shape)
    self._buf = _buf
    self._image = _image
    self.dtype = np.float32
    if hostbuf is not None:
      # TODO: lazy?
      self._buf = cl.Buffer(get_cl_ctx(), cl.mem_flags.READ_WRITE, 4*roundup(prod(shape)))
      cl.enqueue_copy(get_cl_queue(), self._buf, hostbuf.astype(np.float32).ravel())

  def clone(self):
    return OpenCLBuffer(self.shapetracker, _buf=self._buf, _image=self._image)

  @property
  def shape(self): return self.shapetracker.shape

  @staticmethod
  def fromCPU(x):
    return OpenCLBuffer(x.shape, x)

  def toCPU(self):
    data = np.empty(self.shape, dtype=np.float32)
    if self.shapetracker.contiguous == False:
      tmp = OpenCLBuffer(self.shapetracker.shape)
      contiguous(None, self, self.shapetracker, tmp)
    else:
      tmp = self
    cl.enqueue_copy(get_cl_queue(), data, tmp.cl, is_blocking=True)
    return data

  @property
  def cl(self):
    if self._buf is None:
      self._buf = cl.Buffer(get_cl_ctx(), cl.mem_flags.READ_WRITE, 4*roundup(prod(self.shape)))
      if self._image is not None:
        assert prod(self.shape) == prod(self._image.shape)*4
        print(f"converting {self.shape} back to buffer, image shape is {self._image.shape}")
        clbuild("from_image", """
          __kernel void from_image(
              read_only image2d_t in,
              __global float4 *out) {
            const sampler_t smp = CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP | CLK_FILTER_NEAREST;
            int2 l;
            l.y = get_global_id(1);
            l.x = get_global_id(0);
            int W = get_image_width(in);
            out[l.y*W + l.x] = read_imagef(in, smp, l);
          }
        """)(self._image.shape, None, self._image, self._buf)
        self._image = None
    return self._buf

  @property
  def image(self):
    if self._image is None:
      assert self.shape[2] == 4 and len(self.shape) == 3
      fmt = cl.ImageFormat(cl.channel_order.RGBA, cl.channel_type.FLOAT)
      self._image = cl.Image(get_cl_ctx(), cl.mem_flags.READ_WRITE, fmt, shape=flip(self.shape))
      if self._buf is not None:
        assert prod(self.shape) == prod(self._image.shape)*4
        print(f"converting {self.shape} to image with shape {self._image.shape}")
        clbuild("to_image", """
          __kernel void to_image(
              __global const float4 *in,
              write_only image2d_t out) {
            int2 l;
            l.y = get_global_id(1);
            l.x = get_global_id(0);
            int W = get_image_width(out);
            write_imagef(out, l, in[l.y*W + l.x]);
          }
        """)(self._image.shape, None, self._buf, self._image)
      self._buf = None
    return self._image

def unary_op(ctx, op, x):
  # TODO: this doesn't actually have to be contiguous
  x = contiguous(ctx, x, x.shapetracker) if not x.shapetracker.contiguous else x
  return unary_op_gpu(ctx, op, x)

def binary_op(ctx, op, x, y):
  x = contiguous(ctx, x, x.shapetracker) if not x.shapetracker.contiguous else x
  y = contiguous(ctx, y, y.shapetracker) if not y.shapetracker.contiguous else y
  return binary_op_gpu(ctx, op, x, y)

def reduce_op(ctx, op, x, new_shape):
  x = contiguous(ctx, x, x.shapetracker) if not x.shapetracker.contiguous else x
  return reduce_op_gpu(ctx, op, x, new_shape)

def movement_op(ctx, op, x, arg=None):
  xc = x.clone()
  # convert from image if the buffer can change shape
  if op in [MovementOps.EXPAND, MovementOps.SLICE]: xc.cl
  xc.shapetracker.movement_op(op, arg)
  if not xc.shapetracker.contiguous: return contiguous(ctx, xc, xc.shapetracker)
  else: return xc

def load(x):
  with open(x) as f:
    ret = f.read()
  return ret

def conv(x,w,ret,C):
  print(x.shapetracker.expr(), w.shapetracker.expr())
  print(x.shape, w.shape, ret.shape)
  options = []
  if C.cin == 1: options.append("-DDEPTHWISE")
  if C.bs > 1:
    options.append("-DBATCH")
    assert C.py == 0, "batched conv doesn't work with y-padding"
  conv_prg = clbuild("conv", load(pathlib.Path(__file__).parent.parent.parent / 'accel/opencl/conv.cl'), tuple(options))
  assert C.cout%4 == 0
  kernel_args = [C.cout//4, (C.ox+3)//4, C.bs*C.oy]
  conv_args = [max(1, C.cin//4), C.groups*C.cin//4, max(1, C.rcout//4), C.cout//4, C.ox, C.oy, C.iy, C.W, C.H, C.px, C.py, C.xs, C.ys, C.dx, C.dy]
  print(conv_args, kernel_args)
  conv_prg(kernel_args, None, x.image, w.image, ret.image, *[np.int16(x) for x in conv_args])


def processing_op(ctx,op,x,w,out_shape,C):
  assert op == ProcessingOps.CONV, f"{op} isn't supported"
  ret = ctx.buffer((C.bs*C.oy, C.ox*C.cout//4, 4))
  conv(x, w, ret, C)
  return ret


def test_image():
  hostbuf = np.random.randn(5,8,4).astype(np.float32)
  x = OpenCLBuffer((5,8,4), hostbuf)
  assert np.allclose(x.toCPU(), hostbuf)
  print(x.image)
  assert np.allclose(x.toCPU(), hostbuf)

if __name__ == "__main__":
  test_image()





