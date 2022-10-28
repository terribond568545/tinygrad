from __future__ import annotations
import os
import hashlib
import math
from typing import Tuple, Union
from tinygrad.helpers import prod
from tinygrad.shapetracker import ShapeTracker, ZeroView
from tinygrad.ops import LazyOp
import ctypes
import numpy as np
from ctypes import CFUNCTYPE
from tinygrad.ops import DEBUG, UnaryOps, BinaryOps, ReduceOps, MovementOps, get_buffers, get_lazyops, ExplicitExecAST, get_lazyop_shape
from llvmlite import ir  # type: ignore
import llvmlite.binding as llvm  # type: ignore

def int_const(x): return ir.Constant(ir.IntType(64), x)
def idx_deref(builder, buf, ptr, eidx):
  if eidx[2] == 1 and eidx[3] is None:
    idx = eidx[1]
  else:
    idx = builder.add(builder.mul(eidx[1], int_const(eidx[2])), eidx[3], name="idx")

  if DEBUG >= 1:
    print("viewcount:", len(buf.st.views), buf.st.expr(), ptr)
  # TODO: unify this with expr in ShapeTracker
  valid = None
  for v in buf.st.views[::-1]:
    if isinstance(v, ZeroView):
      if valid is None:
        valid = ir.Constant(ir.IntType(1), 1)
      acc = 1
      for s,(x,y) in list(zip(v.old_shape, v.arg))[::-1]:
        if x < 0 or y > s:
          lr = idx
          if acc != 1:
            lr = builder.sdiv(lr, int_const(acc))
          lr = builder.srem(lr, int_const(y-x))
          if x != 0:
            lr = builder.add(lr, int_const(x))
          if x < 0:
            valid = builder.and_(valid, builder.icmp_signed(">=", lr, int_const(0)))
          if y > s:
            valid = builder.and_(valid, builder.icmp_signed("<", lr, int_const(s)))
        acc *= y-x
    else:
      acc = 1
      ret = int_const(v.offset)
      if DEBUG >= 2:
        print(f"expanding index {v.shape_strides}")
      for i,(d,s) in enumerate(v.shape_strides[::-1]):
        if d != 1 and s != 0:
          if acc%eidx[2] == 0 and len(buf.st.views) == 1:
            # the inner one doesn't matter
            lr = eidx[1]
            if acc//eidx[2] != 1:
              lr = builder.sdiv(lr, int_const(acc//eidx[2]))
            if (acc//eidx[2])*d != eidx[0]:
              lr = builder.srem(lr, int_const(d))
          elif acc*d <= eidx[2] and eidx[3] is not None and len(buf.st.views) == 1:
            # the outer one doesn't matter
            lr = eidx[3]
            if acc != 1:
              lr = builder.sdiv(lr, int_const(acc))
            if acc*d != eidx[2]:
              lr = builder.srem(lr, int_const(d))
          else:
            # slow path
            lr = idx
            if acc != 1:
              lr = builder.sdiv(lr, int_const(acc))
            if acc*d != (eidx[0]*eidx[2]):
              lr = builder.srem(lr, int_const(d))
          if s != 1:
            lr = builder.mul(lr, int_const(s))
          ret = builder.add(ret, lr)
        acc *= d
      idx = ret
  if valid is not None:
    return builder.select(valid, builder.load(builder.gep(ptr, [idx])), ir.Constant(ir.FloatType(), 0))
  else:
    return builder.load(builder.gep(ptr, [idx]))

class LLVM:
  target_machine = None
  engine = None
  optimizer = None
  def __init__(self):
    if LLVM.engine is not None:
      return
    llvm.initialize()
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()  # yes, even this one
    target = llvm.Target.from_default_triple()
    LLVM.target_machine = target.create_target_machine()
    LLVM.engine = llvm.create_mcjit_compiler(llvm.parse_assembly(""), LLVM.target_machine)
    LLVM.optimizer = llvm.ModulePassManager()
    builder = llvm.PassManagerBuilder()
    builder.opt_level = 3
    builder.loop_vectorize = True
    builder.populate(LLVM.optimizer)

    # cache
    def notify_func(module, buffer):
      #print("notify", module.name)
      with open(f"/tmp/llvmcache/{module.name}", "wb") as f:
        f.write(buffer)
    def getbuffer_func(module):
      #print("getbuffer", module.name)
      try:
        with open(f"/tmp/llvmcache/{module.name}", "rb") as f:
          return f.read()
      except FileNotFoundError:
        return None
    # enable cache
    if int(os.getenv("LLVMCACHE", "0")):
      LLVM.engine.set_object_cache(notify_func, getbuffer_func)

  def exec(self, module, bufs):
    llvm_ir = str(module)
    if DEBUG >= 2:
      print(llvm_ir)

    mod = llvm.parse_assembly(llvm_ir)
    mod.verify()
    LLVM.optimizer.run(mod)
    mod.name = hashlib.sha1(llvm_ir.encode('utf-8')).hexdigest()
    if DEBUG >= 3:
      print(LLVM.target_machine.emit_assembly(mod))
    LLVM.engine.add_module(mod)
    LLVM.engine.finalize_object()

    # call function
    cfunc = CFUNCTYPE(ctypes.c_int, *[ctypes.POINTER(ctypes.c_float) for _ in bufs])(LLVM.engine.get_function_address('exec'))
    cfunc(*[x._buf for x in bufs])

    # we are done
    LLVM.engine.remove_module(mod)


# TODO: Refactor LLVMBuffer and GPUBuffer into ShapeTrackedBuffer
class LLVMBuffer(ExplicitExecAST):
  op_lookup = {
    UnaryOps.NOOP: lambda builder,x: x,
    UnaryOps.NEG: lambda builder,x: builder.fneg(x),
    UnaryOps.RELU: lambda builder,x: builder.select(builder.fcmp_ordered("<=", ir.Constant(ir.FloatType(), 0), x), x, ir.Constant(ir.FloatType(), 0)),
    UnaryOps.EXP: lambda builder,x: builder.call(builder._block.module.declare_intrinsic('llvm.exp', [ir.FloatType()]), [x]),
    UnaryOps.LOG: lambda builder,x: builder.call(builder._block.module.declare_intrinsic('llvm.log', [ir.FloatType()]), [x]),
    UnaryOps.SIGN: lambda builder,x: builder.select(builder.fcmp_ordered("==", x, ir.Constant(ir.FloatType(), 0)), ir.Constant(ir.FloatType(), 0),
                                                    builder.select(builder.fcmp_ordered("<=", ir.Constant(ir.FloatType(), 0), x), ir.Constant(ir.FloatType(), 1), ir.Constant(ir.FloatType(), -1))),
    UnaryOps.RECIPROCAL: lambda builder,x: builder.fdiv(ir.Constant(ir.FloatType(), 1), x),
    BinaryOps.ADD: lambda builder,x,y: builder.fadd(x,y),
    BinaryOps.SUB: lambda builder,x,y: builder.fsub(x,y),
    BinaryOps.MUL: lambda builder,x,y: builder.fmul(x,y),
    BinaryOps.DIV: lambda builder,x,y: builder.fdiv(x,y),
    BinaryOps.POW: lambda builder,x,y: builder.call(builder._block.module.declare_intrinsic('llvm.pow', [ir.FloatType()]), [x,y]),
    BinaryOps.CMPEQ: lambda builder,x,y: builder.uitofp(builder.fcmp_ordered("==", x, y), ir.FloatType()),
    MovementOps.RESHAPE: lambda builder,x: x,
  }
  def __init__(self, shape:Union[ShapeTracker, Tuple[int, ...]], hostbuf=None):
    super().__init__(shape, hostbuf)
    self._buf = (ctypes.c_float * (prod(self.shape)))() if hostbuf is None else hostbuf._buf

  def __repr__(self): return f"LLVMBuffer {str(self.shape)}"

  @staticmethod
  def fromCPU(x):
    x = x.astype(np.float32)
    ret = LLVMBuffer(x.shape)
    ctypes.memmove(ret._buf, x.ctypes.data, prod(ret.shape)*4)
    return ret
  
  def toCPU(x): return np.ctypeslib.as_array(x.contiguous_op()._buf)[:prod(x.shape)].reshape(x.shape).copy()

  # ast can contain one ReduceOp with arbitrary Binary/Unary ops
  @classmethod
  def exec_ast(cls, ast:LazyOp) -> LLVMBuffer:
    # get the real buffers from the ast
    bufs = get_buffers(ast)
    reduceops = [x for x in get_lazyops(ast) if isinstance(x.op, ReduceOps)]
    assert len(reduceops) <= 1, "max one reduce op in an ast"
    earlybufs = get_buffers(reduceops[0]) if len(reduceops) > 0 else []
    ret = cls(get_lazyop_shape(ast))

    module = ir.Module(name=__file__)
    func = ir.Function(module, ir.FunctionType(ir.VoidType(), [ir.PointerType(ir.FloatType())]*(1+len(bufs))), name='exec')

    # enter
    start_builder = ir.IRBuilder(func.append_basic_block(name="entry"))
    body_builder = ir.IRBuilder(func.append_basic_block(name="inner_loop"))
    start_builder.branch(body_builder._block)

    idx = body_builder.phi(ir.IntType(64))
    idx.add_incoming(int_const(0), start_builder._block)

    reduce_builder = ir.IRBuilder(func.append_basic_block(name="reduce_loop"))
    store_builder = ir.IRBuilder(func.append_basic_block(name="store_block"))

    def ast_parse(builder, x, idx, reduce_result=None, depth=0):
      if DEBUG >= 1:
        print("  "*depth+"ast:", reduce_result, x)
      if not isinstance(x, LazyOp):
        return idx_deref(builder, x, func.args[bufs.index(x)+1], idx)
      if isinstance(x.op, ReduceOps):
        if reduce_result is None:
          raise Exception("no reduce")
        return reduce_result
      values = [ast_parse(builder, v, idx, reduce_result, depth=depth+1) for v in x.src]
      return LLVMBuffer.op_lookup[x.op](builder, *values)

    if len(reduceops) > 0:
      assert len(earlybufs[0].shape) == len(reduceops[0].arg), "reduce only possible on matching shapes"
      if DEBUG >= 1:
        print(f"reduce {earlybufs[0].shape} -> {reduceops[0].arg}")
      red = prod([s for s,n in zip(earlybufs[0].shape, reduceops[0].arg) if n == 1])
      red_idx = reduce_builder.phi(ir.IntType(64))
      red_idx.add_incoming(int_const(0), body_builder._block)
      val = reduce_builder.phi(ir.FloatType())
      reduce_input = ast_parse(reduce_builder, reduceops[0].src[0], (prod(reduceops[0].arg), idx, red, red_idx))

      if reduceops[0].op == ReduceOps.SUM:
        val.add_incoming(ir.Constant(ir.FloatType(), 0), body_builder._block)
        reduce_result = reduce_builder.fadd(reduce_input, val)
      elif reduceops[0].op == ReduceOps.MAX:
        val.add_incoming(ir.Constant(ir.FloatType(), -math.inf), body_builder._block)
        reduce_result = reduce_builder.call(ir.Function(module, ir.FunctionType(ir.FloatType(), [ir.FloatType(), ir.FloatType()]), name="llvm.maxnum.f32"), [reduce_input, val])
      else:
        raise Exception(f"unknown ReduceOps {ast.op}")
      val.add_incoming(reduce_result, reduce_builder._block)

      red_idx_p1 = reduce_builder.add(red_idx, int_const(1))
      red_idx.add_incoming(red_idx_p1, reduce_builder._block)
      reduce_builder.cbranch(reduce_builder.icmp_unsigned("==", red_idx_p1, int_const(red)), store_builder._block, reduce_builder._block)
    else:
      reduce_result = None
      reduce_builder.branch(store_builder._block)

    body_builder.branch(reduce_builder._block)
    result = ast_parse(store_builder, ast, (prod(ret.shape), idx, 1, None), reduce_result)
    store_builder.store(result, store_builder.gep(func.args[0], [idx]))
    idx_p1 = store_builder.add(idx, int_const(1))
    idx.add_incoming(idx_p1, store_builder._block)

    exit_builder = ir.IRBuilder(func.append_basic_block(name="exit"))
    exit_builder.ret_void()

    store_builder.cbranch(store_builder.icmp_unsigned("==", idx_p1, int_const(prod(ret.shape))), exit_builder._block, body_builder._block)

    # **** llvm running ****
    LLVM().exec(module, [ret] + bufs)
    return ret
