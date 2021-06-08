import numpy as np
from tinygrad.tensor import Function
from extra.risk import *

# ************* unary ops *************

class ReLU(Function):
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return risk_unop(input, UnaryOps.RELU)

  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return risk_binop(grad_output, risk_unop(input, UnaryOps.GT0), BinaryOps.MUL)

class Log(Function):
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return risk_unop(input, UnaryOps.LOG)

  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return risk_binop(grad_output, input, BinaryOps.DIV)

class Exp(Function):
  def forward(ctx, input):
    ret = risk_unop(input, UnaryOps.EXP)
    ctx.save_for_backward(ret)
    return ret

  def backward(ctx, grad_output):
    ret, = ctx.saved_tensors
    return risk_binop(grad_output, ret, BinaryOps.MUL)

# ************* processing ops *************

class Matmul(Function):
  def forward(ctx, input, weight):
    ctx.save_for_backward(input, weight)
    return risk_matmul(input, weight)

  def backward(ctx, grad_output):
    input, weight = ctx.saved_tensors
    grad_input = risk_matmul(grad_output, weight, transpose_w=True)
    grad_weight = risk_matmul(input, grad_output, transpose_x=True)
    return grad_input, grad_weight

class Conv2D(Function):
  def forward(ctx, x, w, stride=1, groups=1):
    if type(ctx.stride) == int:
      ctx.stride = (ctx.stride, ctx.stride)
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_ = x.shape[0], x.shape[1]
    iy,ix = x.shape[2],x.shape[3]
    oy,ox = (x.shape[2]-(H-ys))//ys, (x.shape[3]-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    # if H == 1 and W == 1 and ctx.groups == 1 and ctx.stride == (1,1):

    gx = x.reshape(bs,ctx.groups,cin,x.shape[2],x.shape[3])
    tx = np.lib.stride_tricks.as_strided(gx,
      shape=(bs, ctx.groups, cin, oy, ox, H, W),
      strides=(*gx.strides[0:3], gx.strides[3]*ys, gx.strides[4]*xs, *gx.strides[3:5]),
      writeable=False,
    )
    tw = w.reshape(ctx.groups, rcout, cin, H, W)
    ctx.save_for_backward(tx, tw, x.shape)

    print((*gx.strides[0:3], gx.strides[3]*ys, gx.strides[4]*xs, *gx.strides[3:5]))

    """
    ret = np.zeros((bs,ctx.groups,oy,ox,rcout),dtype=x.dtype)
    for g in range(ctx.groups):
      #ijYXyx,kjyx -> iYXk ->ikYX
      ret[:,g] += np.tensordot(tx[:,g], tw[g], ((1,4,5),(1,2,3)))

    print(bs, ctx.groups, cin)
    return np.moveaxis(ret,4,2).reshape(bs, cout, oy, ox)
    """

    riski_dmar(SLOT(0), x)   # bs, groups, cin, x.shape[2], x.shape[3]
    riski_dmar(SLOT(1), w)   # groups, rcout, cin, H, W

    risk_reset_counts()
    print(bs, ctx.groups, rcout, oy, ox, cin, H, W)

    for B in range(0, bs):
      if cin == 1 and rcout == 1 and ctx.groups > 1:
        # hmm, this doesn't work, it's not a matmul
        # you always have to loop over the groups, since they aren't joint
        # the idea would be to collapse the HxW into the matmul, but you'd be limited to 9 for 3x3
        # and while the load is easy in the weight matrix, it's hard in the image matrix (3 strides)
        # and only the diagonal of the matrix would be useful! groups aren't channels!
        # [(1, 144, 58, 58), (144, 1, 3, 3)] -> (1, 144, 56, 56)

        # what does a grouped 1x1 conv look like?
        #    bs x groups x yx -- groups x 1 --> bs x groups x yx
        #    it looks like a broadcasted multiply

        print("opt1")

        # x:   bs x groups x iy x ix
        # w:        groups x H  x W
        # out: bs x groups x oy x ox
        # ix x groups x groups
        for g in range(0, groups, SZ):
          for Y in range(0, oy):
            for X in range(0, ox, SZ):
              IY,IX = Y*ys,X*xs
              riski_mov(Reg.MATMUL_OUTPUT, Reg.ZERO)
              for y in range(IY, IY+H):
                for x in range(IX, IX+W):
                  riski_load(Reg.MATMUL_INPUT,
                    SLOT(0) + B*groups*iy*ix + g*iy*ix + y*ix + x,
                    xs, iy*ix, min(SZ, ox-X), min(SZ, groups-g))
                  # 0 here is for broadcasting
                  riski_load(Reg.MATMUL_WEIGHTS,
                    SLOT(1) + g*H*W + (y-IY)*W + (x-IX),
                    0, H*W, SZ, min(SZ, groups-g))
                  riski_mulacc()
                  #risk_regdump()
              riski_store(Reg.MATMUL_OUTPUT,
                SLOT(2) + B*groups*oy*ox + g*oy*ox + Y*ox + X,
                1, oy*ox, min(SZ, ox-X), min(SZ, groups-g))

      elif H == 1 and W == 1 and xs == 1 and ys == 1:
        print("opt2")
        # oxy x cin x rcout -- unstrided 1x1
        # this is a simple matmul
        for g in range(0, groups):
          for c in range(0, rcout, SZ):
            yx = oy*ox
            assert yx == iy*ix
            for YX in range(0, oy*ox, SZ):   # these are next to each other
              # inner conv
              riski_mov(Reg.MATMUL_OUTPUT, Reg.ZERO)
              for ci in range(0, cin, SZ):
                riski_load(Reg.MATMUL_INPUT,
                  SLOT(0) + B*groups*cin*yx + g*cin*yx + ci*yx + YX,
                  1, yx, min(SZ, yx-YX), min(SZ, cin-ci))
                riski_load(Reg.MATMUL_WEIGHTS,
                  SLOT(1) + g*rcout*cin + c*cin + ci,
                  1, cin, min(SZ, cin-ci), min(SZ, rcout-c))
                riski_matmul()
              riski_store(Reg.MATMUL_OUTPUT,
                SLOT(2) + B*groups*rcout*yx + g*rcout*yx + c*yx + YX,
                1, yx, min(SZ, yx-YX), min(SZ, rcout-c))
      else:
        print("unoptimized")
        # ox x cin x rcout -- unoptimized
        for g in range(0, groups):
          for c in range(0, rcout, SZ):
            for Y in range(0, oy):
              for X in range(0, ox, SZ):
                IY,IX = Y*ys,X*xs

                # inner conv
                riski_mov(Reg.MATMUL_OUTPUT, Reg.ZERO)
                for ci in range(0, cin, SZ):
                  # not a loop in 1x1 convs, 9 in 3x3, 25 in 5x5
                  for y in range(IY, IY+H):
                    for x in range(IX, IX+W):
                      riski_load(Reg.MATMUL_INPUT,
                        SLOT(0) + B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + y*ix + x,
                        xs, iy*ix, min(SZ, ox-X), min(SZ, cin-ci))
                      riski_load(Reg.MATMUL_WEIGHTS,
                        SLOT(1) + g*rcout*cin*H*W + c*cin*H*W + ci*H*W + (y-IY)*W + (x-IX),
                        H*W, cin*H*W, min(SZ, cin-ci), min(SZ, rcout-c))
                      riski_matmul()
                riski_store(Reg.MATMUL_OUTPUT,
                  SLOT(2) + B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X,
                  1, oy*ox, min(SZ, ox-X), min(SZ, rcout-c))
    risk_print_counts()

    #print(x.shape, w.shape, "->", ret.shape)
    return riski_dmaw(SLOT(2), (bs, cout, oy, ox))

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
    for k in range(oy*ox):
      Y, X = k//ox, k%ox
      iY,iX = Y*ys, X*xs
      #gdx[:,:,: , iY:iY+H, iX:iX+W] += np.einsum('igk,gkjyx->igjyx', ggg[:,:,:,Y,X], tw)
      for g in range(ctx.groups):
        tg = np.dot(ggg[:,g,:,Y,X].reshape(bs, -1), tw[g].reshape(rcout, -1))
        gdx[:, g, :, iY:iY+H, iX:iX+W] += tg.reshape((bs, cin, H, W))

    return gdx.reshape((bs, ctx.groups*cin, OY, OX)), gdw.reshape((ctx.groups*rcout, cin, H, W))

