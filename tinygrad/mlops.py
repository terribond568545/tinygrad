import math
from typing import Tuple, Optional, cast
from tinygrad.helpers import argsort, DType, least_upper_float, dtypes, least_upper_dtype
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, ReduceOps
from tinygrad.tensor import Function
from tinygrad.lazy import LazyBuffer
from tinygrad.shape.symbolic import sint

class Contiguous(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x.contiguous()
  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output

class ContiguousBackward(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x
  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.contiguous()

class Cast(Function):
  def forward(self, x:LazyBuffer, dtype:DType, bitcast:bool=False) -> LazyBuffer:
    self.input_dtype, self.bitcast = x.dtype, bitcast
    return x.cast(dtype, bitcast)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.cast(self.input_dtype, self.bitcast)

# ************* unary ops *************

class Zero(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x.const(0)
  def backward(self, grad:LazyBuffer) -> LazyBuffer: return grad.const(0)

class Neg(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x.e(UnaryOps.NEG)
  def backward(self, grad:LazyBuffer) -> LazyBuffer: return grad.e(UnaryOps.NEG)

class Sin(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.x = x
    return x.cast(least_upper_float(x.dtype)).e(UnaryOps.SIN)

  def backward(self, grad:LazyBuffer) -> LazyBuffer:
    return self.x.const(math.pi / 2).e(BinaryOps.SUB, self.x).e(UnaryOps.SIN).e(BinaryOps.MUL, grad)

# NOTE: maximum(x, 0) behaves differently where x=0
class Relu(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.e(BinaryOps.MAX, x.const(0))
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.const(0).e(BinaryOps.CMPLT, self.ret).e(BinaryOps.MUL, grad_output)

class Log(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.x = x
    return x.cast(ftype:= least_upper_float(x.dtype)).e(UnaryOps.LOG2).e(BinaryOps.MUL, x.cast(ftype).const(math.log(2)))

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.e(BinaryOps.DIV, self.x)

class Exp(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.cast(ftype:=least_upper_float(x.dtype)).e(BinaryOps.MUL, x.cast(ftype).const(1/math.log(2))).e(UnaryOps.EXP2)
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.e(BinaryOps.MUL, grad_output)

class Sqrt(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.cast(least_upper_float(x.dtype)).e(UnaryOps.SQRT)
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.e(BinaryOps.DIV, self.ret.e(BinaryOps.MUL, self.ret.const(2)))

# NOTE: the implicit derivative of sigmoid is not stable
# https://towardsdatascience.com/derivative-of-the-sigmoid-function-536880cf918e
# TODO: have the backend automatically find this
class Sigmoid(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    x = x.cast(least_upper_float(x.dtype))
    self.ret = x.const(1).e(BinaryOps.DIV, x.const(1).e(BinaryOps.ADD, x.e(BinaryOps.MUL, x.const(-1/math.log(2))).e(UnaryOps.EXP2)))
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.e(BinaryOps.MUL, self.ret.const(1).e(BinaryOps.SUB, self.ret)).e(BinaryOps.MUL, grad_output)

# ************* binary ops *************

class Less(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    output_dtype = least_upper_dtype(x.dtype, y.dtype)
    return x.cast(output_dtype).e(BinaryOps.CMPLT, y.cast(output_dtype))

class Xor(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    output_dtype = least_upper_dtype(x.dtype, y.dtype)
    return x.cast(output_dtype).e(BinaryOps.XOR, y.cast(output_dtype))

class Add(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    self.x_dtype, self.y_dtype, output_dtype = x.dtype, y.dtype, least_upper_dtype(x.dtype, y.dtype)
    return x.cast(output_dtype).e(BinaryOps.ADD, y.cast(output_dtype))

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return grad_output.cast(self.x_dtype) if self.needs_input_grad[0] else None, \
           grad_output.cast(self.y_dtype) if self.needs_input_grad[1] else None

class Sub(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    self.x_dtype, self.y_dtype, output_dtype = x.dtype, y.dtype, least_upper_dtype(x.dtype, y.dtype)
    return x.cast(output_dtype).e(BinaryOps.SUB, y.cast(output_dtype))

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return grad_output.cast(self.x_dtype) if self.needs_input_grad[0] else None, \
           grad_output.cast(self.y_dtype).e(UnaryOps.NEG) if self.needs_input_grad[1] else None

class Mul(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    self.x, self.y, output_dtype = x, y, least_upper_dtype(x.dtype, y.dtype)
    return x.cast(output_dtype).e(BinaryOps.MUL, y.cast(output_dtype))

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return self.y.cast(self.x.dtype).e(BinaryOps.MUL, grad_output.cast(self.x.dtype)) if self.needs_input_grad[0] else None, \
           self.x.cast(self.y.dtype).e(BinaryOps.MUL, grad_output.cast(self.y.dtype)) if self.needs_input_grad[1] else None

class Div(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    self.x, self.y, output_dtype = x, y, least_upper_dtype(x.dtype, y.dtype)
    return x.cast(output_dtype).e(BinaryOps.DIV, y.cast(output_dtype))

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return grad_output.cast(self.x.dtype).e(BinaryOps.DIV, self.y.cast(self.x.dtype)) if self.needs_input_grad[0] else None, \
           grad_output.cast(self.y.dtype).e(UnaryOps.NEG).e(BinaryOps.MUL, self.x.cast(self.y.dtype)).e(BinaryOps.DIV, self.y.e(BinaryOps.MUL, self.y)) if self.needs_input_grad[1] else None  # noqa: E501

# ************* ternary ops *************

class Where(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer, z:LazyBuffer) -> LazyBuffer:
    self.x, self.y_dtype, self.z_dtype, output_type = x.cast(dtypes.bool), y.dtype, z.dtype, least_upper_dtype(y.dtype, z.dtype)
    return self.x.e(TernaryOps.WHERE, y.cast(output_type), z.cast(output_type))

  def backward(self, grad_output:LazyBuffer) -> Tuple[None, Optional[LazyBuffer], Optional[LazyBuffer]]:
    return None, \
      self.x.e(TernaryOps.WHERE, grad_output.cast(self.y_dtype), grad_output.cast(self.y_dtype).const(0)) if self.needs_input_grad[1] else None, \
      self.x.e(TernaryOps.WHERE, grad_output.const(0), grad_output) if self.needs_input_grad[2] else None

# ************* reduce ops *************

class Sum(Function):
  def forward(self, x:LazyBuffer, new_shape:Tuple[int, ...]) -> LazyBuffer:
    self.input_shape = x.shape
    return x.r(ReduceOps.SUM, new_shape)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.expand(self.input_shape)

class Max(Function):
  def forward(self, x:LazyBuffer, new_shape:Tuple[int, ...]) -> LazyBuffer:
    self.x, self.ret = x, x.r(ReduceOps.MAX, new_shape)
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    # 1s in locations where the max was chosen (can be two locations)
    max_is_1s = self.x.const(1.0).e(BinaryOps.SUB, self.x.e(BinaryOps.CMPLT, self.ret.expand(self.x.shape)))
    div = max_is_1s.r(ReduceOps.SUM, grad_output.shape).expand(self.x.shape)
    return max_is_1s.e(BinaryOps.DIV, div).e(BinaryOps.MUL, grad_output.expand(self.x.shape))

# ************* movement ops *************

# NOTE: this is sum in reverse
class Expand(Function):
  def forward(self, x:LazyBuffer, shape:Tuple[int, ...]) -> LazyBuffer:
    self.input_shape = x.shape
    return x.expand(shape)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.r(ReduceOps.SUM, self.input_shape)

class Reshape(Function):
  def forward(self, x:LazyBuffer, shape:Tuple[int, ...]) -> LazyBuffer:
    self.input_shape = x.shape
    return x.reshape(shape)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.reshape(self.input_shape)

class Permute(Function):
  def forward(self, x:LazyBuffer, order:Tuple[int, ...]) -> LazyBuffer:
    self.input_order = order
    return x.permute(order)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.permute(argsort(self.input_order))

class Pad(Function):
  def forward(self, x:LazyBuffer, arg:Tuple[Tuple[int, int], ...]) -> LazyBuffer:
    self.narg = tuple([(p[0], s+p[0]) for s,p in zip(x.shape, arg)])
    return x.pad(arg)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.shrink(self.narg)

class Shrink(Function):
  def forward(self, x:LazyBuffer, arg:Tuple[Tuple[sint, sint], ...]) -> LazyBuffer:
    self.narg = tuple([(p[0], s-p[1]) for s,p in zip(x.shape, arg)])
    return x.shrink(arg)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    assert all(isinstance(x[0], int) and isinstance(x[1], int) for x in self.narg), "symbolic shrink does not support backward"
    # need this cast because mypy cannot narrow the type even with assert
    return grad_output.pad(cast(Tuple[Tuple[int, int], ...], self.narg))

class Flip(Function):
  def forward(self, x:LazyBuffer, axis:Tuple[int, ...]) -> LazyBuffer:
    self.arg = tuple([-1 if i in set(axis) else 1 for i in range(len(x.shape))])
    return x.stride(self.arg)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.stride(self.arg)
