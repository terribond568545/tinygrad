from __future__ import annotations
from dataclasses import replace
from typing import List, Tuple, cast, Optional, Any, Dict
import functools
from tinygrad.shape.shapetracker import ShapeTracker, View
from tinygrad.shape.symbolic import sint
from tinygrad.dtype import dtypes, DType
from tinygrad.ops import ReduceOps, KernelInfo, BinaryOps
from tinygrad.codegen.uops import BUFFER_UOPS, UOp, UOps
from tinygrad.renderer import Renderer
from tinygrad.helpers import all_int, get_contraction, prod, partition, flatten

# TODO: this needs to be replaced, there shouldn't be variables in the shapetracker, only ints and UOps
from tinygrad.shape.symbolic import Variable, NumNode, SumNode, MulNode, DivNode, ModNode, LtNode, AndNode
def variable_to_uop(x, ctx=None) -> UOp: return UOp.const(dtypes.pyint, x) if isinstance(x, int) else x.render(render_ops, ctx)
render_ops: Any = { NumNode: lambda self, ops, ctx: UOp.const(dtypes.pyint, self.b),
                    MulNode: lambda self, ops, ctx: self.a.render(ops, ctx)*variable_to_uop(self.b, ctx),
                    DivNode: lambda self, ops, ctx: self.a.render(ops, ctx)//variable_to_uop(self.b, ctx),
                    ModNode: lambda self, ops, ctx: self.a.render(ops, ctx)%variable_to_uop(self.b, ctx),
                    LtNode: lambda self, ops, ctx: self.a.render(ops, ctx).lt(variable_to_uop(self.b, ctx)),
  Variable: lambda self,ops,ctx: ctx[self] if ctx is not None and self in ctx else \
    UOp(UOps.DEFINE_VAR, dtypes.int, (UOp.const(dtypes.int, self.min), UOp.const(dtypes.int, self.max)), self),
  SumNode: lambda self,ops,ctx: functools.reduce(lambda a,b: a+b.render(ops, ctx), self.nodes[1:], self.nodes[0].render(ops,ctx)),
  AndNode: lambda self,ops,ctx: functools.reduce(lambda a,b: a*b.render(ops, ctx), self.nodes[1:], self.nodes[0].render(ops,ctx)) }

def _uop_view(view:View, idxs:List[UOp], vexpr:UOp) -> Tuple[UOp, UOp]:
  # TODO: dtypes.realint
  iexpr = variable_to_uop(view.offset)
  for idx,sh,st,m in zip(idxs, view.shape, view.strides, view.mask if view.mask is not None else [None]*len(view.shape)):
    if sh != 1 and st != 0: iexpr = iexpr + idx*variable_to_uop(st)
    if m is not None:
      if m[0] != 0: vexpr = vexpr * idx.ge(variable_to_uop(m[0]))
      if m[1] != sh: vexpr = vexpr * idx.lt(variable_to_uop(m[1]))
  return iexpr, vexpr

# TODO: change this once UOps is ready to replace symbolic
def st_to_uops(st:ShapeTracker, idxs:List[UOp]) -> Tuple[UOp, UOp]:
  idx, valid = _uop_view(st.views[-1], idxs, UOp.const(dtypes.bool, True))
  for view in reversed(st.views[0:-1]):
    view = view.minify()
    acc, idxs = 1, []
    for _d in reversed(view.shape):
      d = variable_to_uop(_d)
      idxs.append((idx//acc)%d)
      acc *= d
    idx, valid = _uop_view(view, idxs[::-1], valid)
  return idx, valid

def _limit_dims(dims:Tuple[sint, ...], max_sizes:Tuple[int, ...]):
  # TODO: symbolic shape
  if not all_int(dims): return dims
  while len(dims) > len(max_sizes) or any(d > m for d,m in zip(dims, max_sizes)):
    for i,m in enumerate(max_sizes):
      if dims[i] * dims[i+1] <= m:
        dims = dims[:i] + (dims[i]*dims[i+1],) + dims[i+2:]
        break
    else: raise RuntimeError(f"cannot limit dim {dims=}, {max_sizes=}")
  return dims

def get_grouped_dims(prefix, dims:Tuple[sint, ...], max_sizes:Optional[Tuple[int, ...]], reverse=False) -> List[UOp]:
  if reverse: dims = dims[::-1]
  limited = _limit_dims(dims, max_sizes) if max_sizes is not None else dims
  ret = raw_idxs = [UOp(UOps.SPECIAL, dtypes.pyint, (), (f"{prefix}{i}", s)) for i,s in enumerate(limited)]
  if limited != dims:
    ret = []
    # cast for mypy, get_contraction won't be None
    for idx, contraction in zip(raw_idxs, cast(List[List[int]], get_contraction(dims, limited))):
      if len(contraction) == 1: ret.append(idx)
      else:
        for c in contraction:
          ret.append(idx % dims[c])
          idx //= dims[c]
  return ret[::-1] if reverse else ret

class IndependentLowerer:
  def lower(self, ast:UOp, opts:Renderer) -> UOp:
    self.output_count = len(ast.src)

    ki = ast.arg if isinstance(ast.arg, KernelInfo) else KernelInfo()
    # NOTE: assumes the shape is <global dims> <local dims> <group_for_reduces> <reduces> <upcasts/unrolls>
    full_shape = ast.full_shape
    first_upcasted = len(full_shape)-ki.upcasted
    first_output_st: ShapeTracker = ast.src[0].src[-1].arg
    # if there's no reduce, this is first_upcasted
    first_reduce = [x!=y for x,y in zip(first_output_st.shape[:first_upcasted]+(0,), full_shape[:first_upcasted]+(1,))].index(True)
    local_loads = [x for x in ast.parents if x.op is UOps.LOAD and x.src[0].op is UOps.DEFINE_LOCAL]
    # NOTE: this is taking the first one...there may be subtlelies here with multireduces
    group_for_reduces = sum([x!=y for x,y in zip(
      local_loads[0].src[-1].arg.shape[first_reduce:first_upcasted], first_output_st.shape[first_reduce:first_upcasted])]) if local_loads else 0
    global_dims = first_reduce-ki.local_dims

    if opts.has_local:
      if ki.dont_use_locals:
        assert ki.local_dims == 0, "can't use locals if there's no local dims"
        self.idxs = get_grouped_dims("idx", full_shape[:global_dims], opts.global_max, reverse=True)
      else:
        # define indexes for GPU-like execution
        self.idxs = get_grouped_dims("gidx", full_shape[:global_dims], opts.global_max, reverse=True) + \
                    get_grouped_dims("lidx", full_shape[global_dims:first_reduce+group_for_reduces], opts.local_max)
    else:
      # all loops are RANGES
      self.idxs = [UOp(UOps.RANGE, dtypes.pyint, (UOp.const(dtypes.pyint, 0), variable_to_uop(g)), (i, False))
                   for i,g in enumerate(full_shape[:first_reduce])]

    # reduce loops
    self.idxs += [UOp(UOps.RANGE, dtypes.pyint, (UOp.const(dtypes.pyint, 0), variable_to_uop(g)), (i, True))
      for i,g in enumerate(full_shape[first_reduce+group_for_reduces:first_upcasted], start=first_reduce+group_for_reduces)]

    # upcast loops
    for i,g in enumerate(full_shape[first_upcasted:], start=first_upcasted):
      assert isinstance(g, int), "needs to be int to upcast/unroll"
      self.idxs.append(UOp(UOps.EXPAND, dtypes.pyint, tuple(UOp.const(dtypes.pyint, j) for j in range(0, g)), ((i,g),)))

    # late indexes (group for reduce)
    self.ridxs = self.idxs[:]
    for a in range(first_reduce, first_reduce+group_for_reduces):
      self.ridxs[a] = UOp(UOps.RANGE, dtypes.pyint, (UOp.const(dtypes.pyint, 0), variable_to_uop(full_shape[a])), (1000+a, True))

    self.uop_cache: Dict[UOp, UOp] = {}
    return self.to_uop(ast)

  def to_uop(self, x:UOp) -> UOp:
    if uop:=self.uop_cache.get(x, None): return uop
    ret = self._to_uop(x)
    self.uop_cache[x] = ret
    return ret

  def _to_uop(self, x:UOp) -> UOp:
    if x.op in BUFFER_UOPS:
      idx, valid = st_to_uops(x.src[-1].arg, self.ridxs if x.op is UOps.LOAD and x.src[0].op is UOps.DEFINE_LOCAL else self.idxs)
      # TODO: check has_valid in UPat, not here
      has_valid = valid.op is not UOps.CONST or valid.arg is not True
      if x.op is UOps.CONST: return valid.where(UOp.const(x.dtype, x.arg), UOp.const(x.dtype, 0))
      buf = x.src[0]
      if x.op is UOps.LOAD:
        barrier = (UOp(UOps.BARRIER, None, (self.to_uop(x.src[1]),)),) if x.src[0].op is UOps.DEFINE_LOCAL else ()
        return UOp(UOps.LOAD, x.dtype, (buf, idx) + ((UOp.const(x.dtype, 0), valid) if has_valid else ()) + barrier)
      # NOTE: only store the local reduceop in the first thread (this is wrong for non group for reduces!)
      if x.src[0].op is UOps.DEFINE_GLOBAL:
        for oidx, ridx in zip(self.idxs, self.ridxs):
          if oidx != ridx: valid = valid * oidx.eq(0)
        has_valid = valid.op is not UOps.CONST or valid.arg is not True
      return UOp(UOps.STORE, None, (buf, idx, self.to_uop(x.src[2])) + ((valid,) if has_valid else ()))

    in_uops = tuple(self.to_uop(y) for y in x.src)
    if x.op is UOps.REDUCE_AXIS:
      if x.arg[0] is ReduceOps.WMMA:
        upcast_axes = x.arg[1][-2]
        wmma_sz = [prod(x[1] for x in l) for l in upcast_axes]
        ret = UOp(UOps.WMMA, dtype=cast(DType, x.dtype).vec(wmma_sz[2]), src=(
          UOp(UOps.CONTRACT, dtype=cast(DType, in_uops[0].dtype).vec(wmma_sz[0]), src=(in_uops[0],), arg=upcast_axes[0]),
          UOp(UOps.CONTRACT, dtype=cast(DType, in_uops[1].dtype).vec(wmma_sz[1]), src=(in_uops[1],), arg=upcast_axes[1]),
          UOp.const(cast(DType, x.dtype).vec(wmma_sz[2]), 0.0)), arg=x.arg[1])
        return UOp(UOps.EXPAND, x.dtype, tuple(UOp(UOps.GEP, x.dtype, (ret,), i) for i in range(wmma_sz[2])), arg=upcast_axes[2])
      # NOTE: always using ridxs is fine here
      reduce_range, reduce_expand = partition([self.ridxs[i] for i in x.arg[1]], lambda y: y.op is UOps.RANGE)
      alu_op = {ReduceOps.SUM:BinaryOps.ADD, ReduceOps.MAX:BinaryOps.MAX}[cast(ReduceOps, x.arg[0])]
      ret = in_uops[0]
      if len(contract_axis:=flatten(x.arg for x in reduce_expand)):
        ret = UOp(UOps.CONTRACT, cast(DType, x.dtype).vec(prod(x[1] for x in contract_axis)), (ret,), tuple(contract_axis))
        ret = functools.reduce(lambda x,y: x.alu(alu_op, y), [ret.gep(i) for i in range(cast(DType, ret.dtype).count)])
      return UOp(UOps.REDUCE, x.dtype, (ret,) + tuple(reduce_range), alu_op) if len(reduce_range) else ret
    return replace(x, src=in_uops)

def ast_to_uop(ast:UOp, opts:Renderer) -> UOp: return IndependentLowerer().lower(ast, opts)
