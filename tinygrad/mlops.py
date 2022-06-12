import numpy as np    # TODO: remove this, it's used for np.prod and np.argsort
from tinygrad.helpers import prod, reduce_shape, get_conv_args
from tinygrad.ops import UnaryOps, BinaryOps, ReduceOps, MovementOps, ProcessingOps
from tinygrad.tensor import Function

# ************* unary ops *************

class _UnaryOp(Function):
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return ctx.unary_op(ctx.fop, input)

  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return ctx.binary_op(ctx.bop, input, grad_output)

class ReLU(_UnaryOp):
  fop = UnaryOps.RELU

  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    ret = ctx.unary_op(UnaryOps.SIGN, input)
    ret = ctx.unary_op(UnaryOps.RELU, ret)
    return ctx.binary_op(BinaryOps.MUL, ret, grad_output)

class Log(_UnaryOp):
  fop = UnaryOps.LOG
  bop = BinaryOps.DIV

class Exp(_UnaryOp):
  def forward(ctx, input):
    ret = ctx.unary_op(UnaryOps.EXP, input)
    ctx.save_for_backward(ret)   # we save the output here, not the input
    return ret

  bop = BinaryOps.MUL

# ************* reduce ops *************

class Sum(Function):
  def forward(ctx, input, axis=None):
    ctx.save_for_backward(input.shape)
    return ctx.reduce_op(ReduceOps.SUM, input, reduce_shape(input.shape, axis))

  def backward(ctx, grad_output):
    shape_input, = ctx.saved_tensors
    return ctx.movement_op(MovementOps.EXPAND, grad_output, shape_input)

class Max(Function):
  def forward(ctx, input, axis=None):
    ret = ctx.reduce_op(ReduceOps.MAX, input, reduce_shape(input.shape, axis))
    ctx.save_for_backward(input, ret)
    return ret

  def backward(ctx, grad_output):
    input, ret = ctx.saved_tensors

    # 1s in locations where the max was chosen (can be two locations)
    max_is_1s = ctx.binary_op(BinaryOps.CMPEQ, input, ctx.movement_op(MovementOps.EXPAND, ret, input.shape))

    # sum of locations, averaged
    div = ctx.reduce_op(ReduceOps.SUM, max_is_1s, grad_output.shape)
    div = ctx.movement_op(MovementOps.EXPAND, div, input.shape)
    max_is_amount = ctx.binary_op(BinaryOps.DIV, div, max_is_1s)

    grad_output_expanded = ctx.movement_op(MovementOps.EXPAND, grad_output, input.shape)
    return ctx.binary_op(BinaryOps.MUL, max_is_amount, grad_output_expanded)

# ************* binary ops *************

class Add(Function):
  def forward(ctx, x, y):
    return ctx.binary_op(BinaryOps.ADD, x, y)

  def backward(ctx, grad_output):
    return grad_output if ctx.needs_input_grad[0] else None, \
           grad_output if ctx.needs_input_grad[1] else None

class Sub(Function):
  def forward(ctx, x, y):
    return ctx.binary_op(BinaryOps.SUB, x, y)

  def backward(ctx, grad_output):
    return grad_output if ctx.needs_input_grad[0] else None, \
           ctx.unary_op(UnaryOps.NEG, grad_output) if ctx.needs_input_grad[1] else None

class Mul(Function):
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return ctx.binary_op(BinaryOps.MUL, x, y)

  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    grad_x = ctx.binary_op(BinaryOps.MUL, y, grad_output) if ctx.needs_input_grad[0] else None
    grad_y = ctx.binary_op(BinaryOps.MUL, x, grad_output) if ctx.needs_input_grad[1] else None
    return grad_x, grad_y

class Pow(Function):
  def forward(ctx, x, y):
    ret = ctx.binary_op(BinaryOps.POW, x, y)
    ctx.save_for_backward(x, y, ret)
    return ret

  def backward(ctx, grad_output):
    x,y,powxy = ctx.saved_tensors
    grad_x, grad_y = None, None
    if ctx.needs_input_grad[0]:
      tmp = ctx.binary_op(BinaryOps.DIV, x, powxy)      # pow(x,y)/x
      tmp = ctx.binary_op(BinaryOps.MUL, y, tmp)        # y * pow(x,y)/x
      grad_x = ctx.binary_op(BinaryOps.MUL, grad_output, tmp)
    if ctx.needs_input_grad[1]:
      tmp = ctx.binary_op(BinaryOps.MUL, ctx.unary_op(UnaryOps.LOG, x), powxy)  # log(x) * pow(x,y)
      grad_y = ctx.binary_op(BinaryOps.MUL, grad_output, tmp)
    return grad_x, grad_y

# ************* movement ops *************

# NOTE: this is sum in reverse
class Expand(Function):
  def forward(ctx, x, shape):
    ctx.save_for_backward(x.shape)
    return ctx.movement_op(MovementOps.EXPAND, x, shape)

  def backward(ctx, grad_output):
    in_shape, = ctx.saved_tensors
    return ctx.reduce_op(ReduceOps.SUM, grad_output, in_shape)

class Reshape(Function):
  def forward(ctx, x, shape):
    ctx.save_for_backward(x.shape)
    shape = tuple(-prod(x.shape) // prod(shape) if s == -1 else s for s in shape)
    return ctx.movement_op(MovementOps.RESHAPE, x, shape)

  def backward(ctx, grad_output):
    in_shape, = ctx.saved_tensors
    return ctx.movement_op(MovementOps.RESHAPE, grad_output, in_shape)

class Permute(Function):
  def forward(ctx, x, order=(1,0)):
    ctx.save_for_backward(order)
    return ctx.movement_op(MovementOps.PERMUTE, x, order)

  def backward(ctx, grad_output):
    order, = ctx.saved_tensors
    norder = np.argsort(order).tolist()
    return ctx.movement_op(MovementOps.PERMUTE, grad_output, norder)

class Slice(Function):
  def forward(ctx, x, arg=None):
    ctx.save_for_backward(x.shape, arg)
    return ctx.movement_op(MovementOps.SLICE, x, arg)

  def backward(ctx, grad_output):
    shape, arg = ctx.saved_tensors
    narg = [(0-p[0], grad_output.shape[i]+(shape[i]-p[1])) for i,p in enumerate(arg)]
    return ctx.movement_op(MovementOps.SLICE, grad_output, narg)

# ************* processing ops *************

class Conv2D(Function):
  def forward(ctx, x, w, stride=1, groups=1):
    C = get_conv_args(x.shape, w.shape, stride, groups)
    ctx.save_for_backward(x,w,(C.ys,C.xs), C.groups)
    return ctx.processing_op(ProcessingOps.CONV, x, w, (C.bs, C.groups*C.rcout, C.oy, C.ox), C)

  def backward(ctx, grad_output):
    x, w, stride, groups = ctx.saved_tensors
    C = get_conv_args(x.shape, w.shape, stride, groups)
    dx = ctx.processing_op(ProcessingOps.CONVT, grad_output, w, x.shape, C) if ctx.needs_input_grad[0] else None
    dw = ctx.processing_op(ProcessingOps.CONVDW, x, grad_output, w.shape, C) if ctx.needs_input_grad[1] else None
    return dx, dw