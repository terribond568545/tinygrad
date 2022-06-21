from __future__ import annotations
from typing import Union, NamedTuple, List, Any, Tuple, Dict
from tinygrad.shapetracker import ShapeTracker
import functools, operator
from tinygrad.helpers import prod
import sys
sys.setrecursionlimit(10000)

from tinygrad.ops import ReduceOps, BinaryOps, MovementOps, ProcessingOps, log_op, DEBUG, GRAPH
from enum import Enum
LoadOps = Enum("LoadOps", ["FROMCPU", "CONTIGUOUS"])
Op = Union[BinaryOps, ReduceOps, MovementOps, ProcessingOps, LoadOps]

MERGE_MOVEMENT_OPS = True
SHUFFLE_MOVEMENT_OPS = True   # this breaks maxpool
REMOVE_MOVEMENT_NOPS = True
MERGE_ELEMENTWISE_OPS = True
MERGE_ELEMENTWISE_INTO_CONV_OUTPUT = True

class LazyOp(NamedTuple):
  op: Op
  src: Tuple[Union[LazyOp, LazyBuffer]]
  arg: Any = None

def get_root(x:LazyOp) -> LazyBuffer: return x if isinstance(x, LazyBuffer) else get_root(x.src[0])
def get_lazyops(op:LazyOp) -> List[LazyOp]: return functools.reduce(operator.add, [get_lazyops(x) for x in op.src if isinstance(x, LazyOp)], [op])
def get_lazybuffers(op:LazyOp) -> List[LazyBuffer]: return functools.reduce(operator.add, [get_lazybuffers(x) if isinstance(x, LazyOp) else [x] for x in op.src], [])
def find_conv(op:LazyOp) -> LazyOp: return [x for x in get_lazyops(op) if isinstance(x.op, ProcessingOps)][0]

# TODO: i'm sure this is a real algorithm
def cmp(buf1:LazyBuffer, buf2:LazyBuffer):
  explore1, explore2 = [buf1], [buf2]
  expanded1, expanded2 = set(), set()
  while len(explore1) and len(explore2):
    if buf2 in explore1: return -1
    if buf1 in explore2: return 1
    x1 = explore1.pop(0)
    x2 = explore2.pop(0)
    if x1 in expanded2 or x2 in expanded1: return 0
    if x1 not in expanded1 and x1.realized is None:
      explore1 += get_lazybuffers(x1.op)
      expanded1.add(x1)
    if x2 not in expanded2 and x2.realized is None:
      explore2 += get_lazybuffers(x2.op)
      expanded2.add(x2)
  return 0

class LazyBuffer:
  def __init__(self, shape:Union[ShapeTracker, Tuple[int]], optype:Op, op:LazyOp):
    self.st = ShapeTracker(shape)
    self.shape = self.st.shape
    self.optype, self.op = optype, op
    self.realized = None

  def __repr__(self): return f"<LB {self.shape} {self.optype}>"

  def realize(self:LazyBuffer):
    if self.realized is None:
      self.realized, real_srcs = _realize(self)
      # TODO: get if logging in a better way
      if DEBUG or GRAPH:
        # in lazy mode, we don't log until we realize
        log_op(self.optype, [x.op for x in get_lazyops(self.op)], self.realized, real_srcs)
      del self.op
    return self.realized

  @staticmethod
  def fromCPU(x):
    ret = LazyBuffer(x.shape, LoadOps, LazyOp(LoadOps.FROMCPU, tuple(), x))
    #ret.realize()
    return ret

  def toCPU(self):
    return self.realize().toCPU()

  def unary_op(x, op): return elementwise_op(op, (x,))
  def binary_op(x, op, y:LazyBuffer): return elementwise_op(op, (x,y))
  def contiguous_op(x): return x if x.st.contiguous else LazyBuffer(x.shape, LoadOps, LazyOp(LoadOps.CONTIGUOUS, (x,)))

  @functools.lru_cache(maxsize=None)
  def movement_op(x, op:MovementOps, arg) -> LazyBuffer:
    if SHUFFLE_MOVEMENT_OPS and x.optype == BinaryOps:
      # if this MovementOp is being applied to a BinaryOp, apply the MovementOp to all the BinaryOp inputs instead
      def replace_with_movement_op(y:Union[LazyOp, LazyBuffer]) -> LazyBuffer:
        if isinstance(y, LazyBuffer): return y.movement_op(op, arg)
        return elementwise_op(y.op, tuple(replace_with_movement_op(z) for z in y.src))
      return replace_with_movement_op(x.op)

    # if a MovementOp is applied to a MovementOp, merge them and use one buffer
    ret = LazyBuffer(x.st, MovementOps, LazyOp(op, (x.op if MERGE_MOVEMENT_OPS and x.optype == MovementOps and x.realized is None else x,), arg))
    ret.shape = ret.st.movement_op(op, arg).shape   # update the shape after we modify the ShapeTracker

    if REMOVE_MOVEMENT_NOPS and x.optype == MovementOps and x.realized is None and ret.st.contiguous:
      root = get_root(x.op)
      if ret.st.shape == root.shape:
        return root

    return ret

  def reduce_op(x, op, new_shape:Tuple[int]):
    return LazyBuffer(new_shape, ReduceOps, LazyOp(op, (x,), new_shape))

  def processing_op(x, op, w:LazyBuffer, C):
    return LazyBuffer(C.out_shape, ProcessingOps, LazyOp(op, (x.contiguous_op(), w.contiguous_op()), C))

def ast_op(op: Op, srcs_code: List[str]) -> str:
  code = gops.code_for_op[op]
  if len(srcs_code) >= 1: code = code.replace("A", srcs_code[0])
  if len(srcs_code) >= 2: code = code.replace("B", srcs_code[1])
  return code

def ast(x: Union[LazyBuffer, LazyOp], lazy_srcs: Dict[LazyBuffer, str]) -> str:
  if isinstance(x, LazyBuffer): return lazy_srcs[x]
  # if it's not a LazyBuffer, it's an op
  if x.op == ProcessingOps.CONV: return "acc"
  return ast_op(x.op, [ast(src, lazy_srcs) for src in x.src])

# this is needed to reduce convs from 186 -> 174
@functools.lru_cache(maxsize=None)
def elementwise_op(op, srcs:Tuple[LazyBuffer]) -> LazyBuffer:
  out_shape = srcs[0].shape

  if MERGE_ELEMENTWISE_INTO_CONV_OUTPUT:
    cnt = sum([x.optype == ProcessingOps and x.realized is None for x in srcs])
    if cnt == 1:
      srcs = [x.op if x.optype == ProcessingOps and x.realized is None else x for x in srcs]
      return LazyBuffer(out_shape, ProcessingOps, LazyOp(op, srcs))
    elif cnt == 2:
      # have to confirm they are the same conv
      c1, c2 = [find_conv(x.op) for x in srcs]
      if c1.op == c1.op and c1.arg == c2.arg and tuple(c1.src) == tuple(c2.src):
        srcs = [x.op if x.optype == ProcessingOps and x.realized is None else x for x in srcs]
        return LazyBuffer(out_shape, ProcessingOps, LazyOp(op, srcs))
      else:
        order = cmp(srcs[0], srcs[1])
        if order == -1:
          srcs = [srcs[0].op, srcs[1]]
        elif order == 1:
          srcs = [srcs[0], srcs[1].op]
        else:
          # all three are okay
          #return Buffer(out_shape, BinaryOps, LazyOp(op, list(srcs)))
          srcs = [srcs[0].op, srcs[1]]
          #srcs = [srcs[0], srcs[1].op]
        return LazyBuffer(out_shape, ProcessingOps, LazyOp(op, srcs))

  if MERGE_ELEMENTWISE_OPS:
    # remove the buffers from any BinaryOps that feed into this
    srcs = tuple(x.op if x.optype == BinaryOps and x.realized is None else x for x in srcs)

  return LazyBuffer(out_shape, BinaryOps, LazyOp(op, srcs))


# these functions determines the backing buffer
import tinygrad.llops.ops_gpu as gops

def _realize_binary_op(self:LazyBuffer) -> Tuple[gops.GPUBuffer, List[gops.GPUBuffer]]:
  # optional
  if self.optype == ProcessingOps:
    conv = find_conv(self.op)
    conv_x, conv_w = conv.src[0], conv.src[1]
    seen = {conv_x:conv_x, conv_w:conv_w}
    real_srcs = [("input", conv_x.realize()), ("weight", conv_w.realize())]
    arg = conv.arg
  else:
    seen = {}
    real_srcs : List[Tuple[str, gops.GPUBuffer]] = []
    arg = None
  lazy_srcs : List[LazyBuffer] = [seen.setdefault(x,x) for x in get_lazybuffers(self.op) if x not in seen]
  real_dict : Dict[LazyBuffer, str] = {}
  for s in lazy_srcs:
    if s.optype == MovementOps and s.realized is None:
      root = get_root(s.op)
      if root.realized is None and root.optype == LoadOps and root.op.op == LoadOps.FROMCPU and root.shape == (1,):
        if not s.st.needs_valid():
          real_dict[s] = f"({root.op.arg[0]}f)"
        else:
          # TODO: this is a terrible hack, and it's very unclear if it's always right
          inline_valid = s.st.expr().replace("valid=valid && ", "").replace(";idx=0", "").replace("//", "/").replace("idx", "gid")
          if ';' not in inline_valid:
            real_dict[s] = f"(({inline_valid}) * {str(root.op.arg[0])}f)"
    if s not in real_dict:  # nicer way to write this?
      real_dict[s] = f"arg_{len(real_srcs)}"
      real_srcs.append((f"arg_{len(real_srcs)}", s.realize()))
  code = ast(self.op, real_dict)
  return gops.GPUBuffer(self.shape)._processing_op(real_srcs, code, arg), [x[1] for x in real_srcs]

def _realize(self:LazyBuffer) -> Tuple[gops.GPUBuffer, List[gops.GPUBuffer]]:
  if self.optype == LoadOps and self.op.op == LoadOps.FROMCPU:
    #print("load", self, self.shape, self.op.arg if prod(self.shape) == 1 else "<data>")
    return gops.GPUBuffer.fromCPU(self.op.arg), []
  elif self.optype == LoadOps and self.op.op == LoadOps.CONTIGUOUS:
    real_src = self.op.src[0].realize()
    return real_src.contiguous(), [real_src]
  elif self.optype == ReduceOps:
    real_src = self.op.src[0].realize()
    return real_src.reduce_op(self.op.op, self.op.arg), [real_src]
  elif self.optype == MovementOps:
    real_src = get_root(self.op).realize()
    return gops.GPUBuffer(self.st, real_src), [real_src]
  elif self.optype in [BinaryOps, ProcessingOps]:
    return _realize_binary_op(self)
