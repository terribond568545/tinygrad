import warnings
import numpy as np
from .tensor import Function, register

# ************* basic ops *************

class Add(Function):
  @staticmethod
  def forward(ctx, x, y):
    return x+y

  @staticmethod
  def backward(ctx, grad_output):
    return grad_output, grad_output
register('add', Add)

class Sub(Function):
  @staticmethod
  def forward(ctx, x, y):
    return x-y

  @staticmethod
  def backward(ctx, grad_output):
    # this right?
    return grad_output, -grad_output
register('sub', Sub)

class Mul(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return x*y

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    return y*grad_output, x*grad_output
register('mul', Mul)

class Div(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return x / y

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    return grad_output / y, -x * grad_output / y**2
# TODO: registering this breaks the default div on the GPU
#register('div', Div)

class Pow(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return x ** y

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    return y * (x**(y-1.0)) * grad_output, (x**y) * np.log(x) * grad_output
register('pow', Pow)

class Sum(Function):
  @staticmethod
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return np.array([input.sum()])

  @staticmethod
  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return grad_output * np.ones_like(input)
register('sum', Sum)


# ************* GEMM *************

class Dot(Function):
  @staticmethod
  def forward(ctx, input, weight):
    ctx.save_for_backward(input, weight)
    return input.dot(weight)

  @staticmethod
  def backward(ctx, grad_output):
    input, weight = ctx.saved_tensors
    grad_input = grad_output.dot(weight.T)
    grad_weight = input.T.dot(grad_output)
    return grad_input, grad_weight
register('dot', Dot)
register('matmul', Dot)


# ************* simple ops *************

class Pad2D(Function):
  @staticmethod
  def forward(ctx, x, padding=None):
    ctx.save_for_backward(padding)
    return np.pad(x,
      ((0,0), (0,0),
       (padding[0], padding[1]), (padding[2], padding[3])))

  @staticmethod
  def backward(ctx, grad_output):
    padding, = ctx.saved_tensors
    return grad_output[..., padding[0]:-padding[1], padding[2]:-padding[3]]
register('pad2d', Pad2D)

class Reshape(Function):
  @staticmethod
  def forward(ctx, x, shape):
    ctx.save_for_backward(x.shape)
    return x.reshape(shape)

  @staticmethod
  def backward(ctx, grad_output):
    in_shape, = ctx.saved_tensors
    return grad_output.reshape(in_shape)
register('reshape', Reshape)


# ************* activation ops *************

class ReLU(Function):
  @staticmethod
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return np.maximum(input, 0)

  @staticmethod
  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    grad_input = grad_output * (input >= 0)
    return grad_input
register('relu', ReLU)

class Sigmoid(Function):
  @staticmethod
  def forward(ctx, input):
    with np.warnings.catch_warnings():
      np.warnings.filterwarnings('ignore')
      ret = np.where(
          input >= 0,1/(1 + np.exp(-input)),np.exp(input)/(1 + np.exp(input))
      )
    ctx.save_for_backward(ret)
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    ret, = ctx.saved_tensors
    grad_input = grad_output * (ret * (1 - ret))
    return grad_input
register('sigmoid', Sigmoid)

class LogSoftmax(Function):
  @staticmethod
  def forward(ctx, input):
    def logsumexp(x):
      #return np.log(np.exp(x).sum(axis=1))
      c = x.max(axis=1)
      return c + np.log(np.exp(x-c.reshape((-1, 1))).sum(axis=1))
    output = input - logsumexp(input).reshape((-1, 1))
    ctx.save_for_backward(output)
    return output

  @staticmethod
  def backward(ctx, grad_output):
    output, = ctx.saved_tensors
    return grad_output - np.exp(output)*(grad_output.sum(axis=1).reshape((-1, 1)))
register('logsoftmax', LogSoftmax)


# ************* conv ops *************

class Conv2D(Function):
  @staticmethod
  def forward(ctx, x, w, stride=1, groups=1):
    if type(ctx.stride) == int:
      ctx.stride = (ctx.stride, ctx.stride)
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_ = x.shape[0], x.shape[1]
    oy,ox = (x.shape[2]-(H-ys))//ys, (x.shape[3]-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    gx = x.reshape(bs,ctx.groups,cin,x.shape[2],x.shape[3])
    tx = np.lib.stride_tricks.as_strided(gx,
           shape=(bs, ctx.groups, cin, oy, ox, H, W),
           strides=(gx.strides[0], gx.strides[1], gx.strides[2],
                    gx.strides[3]*ys, gx.strides[4]*xs,
                    gx.strides[3], gx.strides[4]),
           writeable=False,
         )
    tw = w.reshape(ctx.groups, rcout, cin, H, W)
    ctx.save_for_backward(tx, tw, x.shape)

    ret = np.zeros((bs,ctx.groups,oy,ox,rcout),dtype=x.dtype)
    for g in range(ctx.groups):
      #ijYXyx,kjyx -> iYXk ->ikYX
      ret[:,g] += np.tensordot(tx[:,g], tw[g], ((1,4,5),(1,2,3)))
    return np.moveaxis(ret,4,2).reshape(bs, cout, oy, ox)


  @staticmethod
  def backward(ctx, grad_output):
    bs,_,oy,ox = grad_output.shape
    tx, tw, x_shape = ctx.saved_tensors
    _,rcout,cin,H,W = tw.shape
    ys,xs = ctx.stride
    OY,OX = x_shape[2:4]

    ggg = grad_output.reshape(bs,ctx.groups,rcout,oy,ox)

    gdw = np.zeros((ctx.groups,rcout,cin,H,W), dtype=tx.dtype)
    for g in range(ctx.groups):
      #'ikYX,ijYXyx -> kjyx'
      gdw[g] += np.tensordot(ggg[:,g], tx[:,g], ((0,2,3),(0,2,3)))

    # needs to be optimized
    gdx = np.zeros((bs,ctx.groups,cin,OY,OX), dtype=tx.dtype)
    for Y in range(grad_output.shape[2]):
      for X in range(grad_output.shape[3]):
        iY,iX = Y*ys, X*xs
        #gdx[:,:,: , iY:iY+H, iX:iX+W] += np.einsum('igk,gkjyx->igjyx', ggg[:,:,:,Y,X], tw)
        for g in range(ctx.groups):
          tg = np.dot(ggg[:,g,:,Y,X].reshape(bs, -1), tw[g].reshape(rcout, -1))
          gdx[:, g, :, iY:iY+H, iX:iX+W] += tg.reshape((bs, cin, H, W))

    return gdx.reshape((bs, ctx.groups*cin, OY, OX)), gdw.reshape((ctx.groups*rcout, cin, H, W))
register('conv2d', Conv2D)


# ************* pooling ops *************

def stack_for_pool(x, kernel_size, stride, fill_value=0):
  (ky, kx), (py, px) = kernel_size, stride
  my, mx = (x.shape[2]-ky)//py+1, (x.shape[3]-kx)//px+1
  stack = fill_value*np.ones((ky, kx, *x.shape[:2], my+ky, mx+kx), dtype=x.dtype)
  for Y in range(ky):
    for X in range(kx):
      sl = x[..., Y:Y+my*py+ky:py, X:X+mx*px+kx:px]
      stack[Y, X, ..., :sl.shape[2], :sl.shape[3]] = sl
  return stack.reshape(-1, *stack.shape[2:]), (my, mx)

def unstack_for_pool(fxn, s, kernel_size, stride):
  (ky, kx), (py, px) = kernel_size, stride
  for Y in range(ky):
    for X in range(kx):
      ll = fxn(Y*kx+X)
      if X == 0 and Y == 0:
        ret = np.zeros((*s[:2], s[2]+ky, s[3]+kx), dtype=ll.dtype)
      ret[..., Y:Y+ll.shape[2]*py:py, X:X+ll.shape[3]*px:px] = ll
  return ret[..., :s[2], :s[3]]

class MaxPool2D(Function):
  @staticmethod
  def forward(ctx, x, kernel_size=(2, 2), stride=None):
    if not stride:
      ctx.stride = stride = kernel_size
    stack, (my, mx) = stack_for_pool(x, kernel_size, stride, fill_value=-np.inf)
    idxs = np.nanargmax(stack, axis=0)[..., :my, :mx]
    ctx.save_for_backward(idxs, x.shape)
    return np.amax(stack, axis=0)[..., :my, :mx]

  @staticmethod
  def backward(ctx, grad_output):
    # TODO implement for stride != kernel_size
    if ctx.kernel_size != ctx.stride:
      raise NotImplementedError("CPU MaxPool2D.backward() with stride != kernel_size not implemented")
    idxs,s = ctx.saved_tensors
    return unstack_for_pool(
      lambda idx: grad_output * (idxs == idx),
      s, ctx.kernel_size, ctx.stride)
register('max_pool2d', MaxPool2D)

class AvgPool2D(Function):
  @staticmethod
  def forward(ctx, x, kernel_size=(2, 2), stride=None):
    if not stride:
      ctx.stride = stride = kernel_size
    stack, (my, mx) = stack_for_pool(x, kernel_size, stride, fill_value=np.nan)
    ctx.save_for_backward(x.shape)
    with warnings.catch_warnings():
      warnings.simplefilter("ignore")
      return np.nanmean(stack, axis=0)[...,:my, :mx]

  @staticmethod
  def backward(ctx, grad_output):
    # TODO implement for stride != kernel_size
    if ctx.kernel_size != ctx.stride:
      raise NotImplementedError("CPU AvgPool2D.backward() with stride != kernel_size not implemented")
    s, = ctx.saved_tensors
    py, px = ctx.kernel_size
    return unstack_for_pool(
      lambda idx: grad_output/py/px,
      s, ctx.kernel_size, ctx.stride)
register('avg_pool2d', AvgPool2D)

