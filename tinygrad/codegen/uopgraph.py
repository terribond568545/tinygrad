from __future__ import annotations
from typing import Iterator, Optional, Tuple, Dict, List, Set, Union, cast, TYPE_CHECKING
import functools, itertools, heapq, math
from tinygrad.dtype import dtypes, PtrDType, ImageDType
from tinygrad.shape.symbolic import Variable
from tinygrad.ops import UnaryOps, BinaryOps, ReduceOps, exec_alu
from tinygrad.helpers import DEBUG, getenv, flatten, dedup, TRANSCENDENTAL, prod, CI
from tinygrad.codegen.uops import UOp, NOp, UOps, UPat, PatternMatcher, END_FOR_UOP, type_verify
from tinygrad.codegen.transcendental import xexp2, xlog2, xsin, TRANSCENDENTAL_SUPPORTED_DTYPES
if TYPE_CHECKING: from tinygrad.renderer import Renderer

# ***** image handling *****

def image_contract_load(buf, idx, idy, id4, ls):
  # TODO: there's no contract on the gate, is this okay?
  if len(ls.src) > 3: extra = (ls.src[2], UOp(UOps.VECTORIZE, ls.dtype.vec(4), (ls.src[3],)*4))
  else: extra = ls.src[2:]  # NOTE: image load shouldn't have barrier and this shouldn't matter
  vec_load = UOp(UOps.LOAD, ls.dtype.vec(4), (buf, UOp(UOps.VECTORIZE, dtypes.int.vec(2), (idx, idy))) + extra)
  return functools.reduce(lambda ret, i: id4.ne(i).where(ret, UOp(UOps.GEP, ls.dtype, (vec_load,), i)), range(4), ls.const(float('nan')))

def image_contract_store(buf, ex, idx, idy, ls, var):
  new_var = UOp(UOps.CONTRACT, var.dtype.vec(4), (var,), ((ex.arg[0][0],4),))
  return UOp(UOps.STORE, None, (buf, UOp(UOps.VECTORIZE, dtypes.int.vec(2), (idx, idy)), new_var) + ls.src[3:])

# ***** float4 handling *****

def float4_expand_load(load, buf, ex, idx=UOp.const(dtypes.int, 0), idx2=None, idx3=None):
  if len(ex.src) not in [2, 4]: return None
  if tuple(x.arg for x in ex.src if x.op is UOps.CONST) != tuple(range(len(ex.src))): return None
  if buf.dtype != PtrDType(dtypes.float) and buf.dtype != PtrDType(dtypes.half) and not isinstance(buf.dtype, ImageDType): return None
  if idx2 is not None: idx = idx + idx2
  if idx3 is not None: idx = idx + idx3
  if idx.divides(len(ex.src)) is None: return None

  if load.dtype.scalar() != load.dtype: return None  # how does this happen?
  vec_load = UOp(UOps.LOAD, load.dtype.vec(len(ex.src)), (buf, idx))
  return UOp(UOps.EXPAND, load.dtype, tuple(UOp(UOps.GEP, load.dtype, (vec_load,), i) for i in range(len(ex.src))), ex.arg)

def float4_contract_store(buf, ex, var, store, idx=UOp.const(dtypes.int, 0), idx2=None, idx3=None):
  if len(ex.src) not in [2, 4]: return None
  if tuple(x.arg for x in ex.src if x.op is UOps.CONST) != tuple(range(len(ex.src))): return None
  if buf.dtype != PtrDType(dtypes.float) and buf.dtype != PtrDType(dtypes.half) and not isinstance(buf.dtype, ImageDType): return None
  if idx2 is not None: idx = idx + idx2
  if idx3 is not None: idx = idx + idx3
  if idx.divides(len(ex.src)) is None: return None

  new_var = UOp(UOps.CONTRACT, var.dtype.vec(len(ex.src)), (var,), ((ex.arg[0][0],len(ex.src)),))
  return UOp(UOps.STORE, None, (buf, idx, new_var) + store.src[3:])

float4_folding = PatternMatcher([
  # reorder index to bring const closer to store
  (NOp(UOps.STORE, src=(NOp.var("buf"), NOp.var("idx")+
    (NOp(UOps.EXPAND, src=tuple(NOp.const(dtypes.int, i) for i in range(4)), name="ex")+NOp.var("idx2")), NOp.var("var")), name="store"),
    lambda buf, store, idx, idx2, ex, var: UOp(UOps.STORE, store.dtype, (buf, idx+idx2+ex, var), store.arg)),
  # float(2,4) load
  (NOp(UOps.LOAD, src=(NOp.var("buf"), NOp(UOps.EXPAND, name="ex")+NOp.var("idx")+NOp.var("idx2")+NOp.var("idx3")), name="load"), float4_expand_load),
  (NOp(UOps.LOAD, src=(NOp.var("buf"), NOp(UOps.EXPAND, name="ex")+NOp.var("idx")+NOp.var("idx2")), name="load"), float4_expand_load),
  (NOp(UOps.LOAD, src=(NOp.var("buf"), NOp(UOps.EXPAND, name="ex")+NOp.var("idx")), name="load"), float4_expand_load),
  (NOp(UOps.LOAD, src=(NOp.var("buf"), NOp(UOps.EXPAND, name="ex")), name="load"), float4_expand_load),
  # float(2,4) store
  # TODO: fold ADDs into one UOp and remove add chains
  (NOp(UOps.STORE, src=(NOp.var("buf"),
    NOp(UOps.EXPAND, name="ex")+NOp.var("idx")+NOp.var("idx2")+NOp.var("idx3"), NOp.var("var")), name="store", allow_any_len=True),
    float4_contract_store),
  (NOp(UOps.STORE, src=(NOp.var("buf"),
    NOp(UOps.EXPAND, name="ex")+NOp.var("idx")+NOp.var("idx2"), NOp.var("var")), name="store", allow_any_len=True),
    float4_contract_store),
  (NOp(UOps.STORE, src=(NOp.var("buf"),
    NOp(UOps.EXPAND, name="ex")+NOp.var("idx"), NOp.var("var")), name="store", allow_any_len=True), float4_contract_store),
  (NOp(UOps.STORE, src=(NOp.var("buf"),
    NOp(UOps.EXPAND, name="ex"), NOp.var("var")), name="store", allow_any_len=True), float4_contract_store),
  # image handling
  (NOp(UOps.LOAD, src=(NOp.var("buf"), NOp(UOps.VECTORIZE, dtypes.int.vec(3), (NOp.var('idx'), NOp.var('idy'),
     NOp.var('id4')))), name="ls", allow_any_len=True), image_contract_load),
  (NOp(UOps.STORE, src=(NOp.var("buf"), NOp(UOps.VECTORIZE, dtypes.int.vec(3), (NOp.var('idx'), NOp.var('idy'),
     NOp(UOps.EXPAND, src=tuple(NOp.const(dtypes.int, i) for i in range(4)), name="ex"))), NOp.var("var")), name="ls", allow_any_len=True),
     image_contract_store),
])

# ***** transcendental *****

transcendental_folding = PatternMatcher([
  (UPat(UOps.ALU, dtype=TRANSCENDENTAL_SUPPORTED_DTYPES, src=(UPat(name="x"),), arg=UnaryOps.EXP2), xexp2),
  (UPat(UOps.ALU, dtype=TRANSCENDENTAL_SUPPORTED_DTYPES, src=(UPat(name="d"),), arg=UnaryOps.LOG2), xlog2),
  (UPat(UOps.ALU, dtype=TRANSCENDENTAL_SUPPORTED_DTYPES, src=(UPat(name="d"),), arg=UnaryOps.SIN), xsin),
])

# ***** threefry *****

def threefry2x32(x: UOp, seed: UOp):
  # split x into two uint32, since x in a uint64
  x0, x1 = (x & 0xffffffff).cast(dtypes.uint32), ((x // 2**32) & 0xffffffff).cast(dtypes.uint32)

  rotations = [[13, 15, 26, 6], [17, 29, 16, 24]]
  ks = [0x0, (seed := seed.cast(dtypes.uint32)) ^ 0x1BD11BDA, seed]
  xr = [x0 + ks[-1], x1 + ks[0]]
  for i in range(5):
    for r in rotations[i % 2]: xr[0], xr[1] = (x0 := xr[0] + xr[1]), x0 ^ ((xr[1] * 2**r) + (xr[1] // 2**(32 - r)))
    xr = [(xr[0] + ks[i % 3]), (xr[1] + ks[(i + 1) % 3] + i + 1)]

  return xr[1].cast(dtypes.uint64) * 2**32 | xr[0].cast(dtypes.uint64)

# ***** main rewriter *****

def reduce_before_expand(reduce, expand, x):
  # if the expand is being reduced, you can't push it through
  # NOTE: could do a partial push here in some cases
  expands = flatten([x.arg for x in reduce.src[1:] if x.op is UOps.EXPAND])
  if any(x in expands for x in expand.arg): return None
  red = UOp(UOps.REDUCE, x.dtype, (x,)+reduce.src[1:], reduce.arg)
  gep = tuple(UOp(UOps.GEP, reduce.dtype, (red,), i) for i in range(x.dtype.count))
  return UOp(expand.op, expand.dtype, gep, expand.arg)

def sum_collapse(phi_input, loop, val1, val2):
  for v1,v2 in [(val1, val2), (val2, val1)]:
    if loop not in v1.parents:
      return UOp(UOps.PHI, phi_input.dtype, (phi_input, v2))+v1*(loop.src[1]-loop.src[0]).cast(v1.dtype)
  return None

def loop_collapse(loop_start, loop_end, compval, idx, mval, multconst, rng, reduce, idx2=None, idx3=None):
  if getenv("DISABLE_LOOP_COLLAPSE") or rng not in reduce.src: return None  # must be the right REDUCE
  if mval.arg >= 0 or loop_start.arg != 0:
    # TODO: support and test this with other mvals and loop_starts
    if DEBUG >= 1: print(f"WARNING, NOT FOLDING: mval:{mval.arg} loop_start:{loop_start.arg}")
    return None
  if idx2 is not None: idx = idx + idx2
  if idx3 is not None: idx = idx + idx3
  comprange = UOp.min(loop_end, UOp.max((idx-compval-mval)//mval + (loop_end-loop_start), loop_start))
  return UOp(UOps.REDUCE, reduce.dtype, (comprange.cast(multconst.dtype) * multconst,) +
             tuple(x for x in reduce.src[1:] if x is not rng), reduce.arg)

def index_collapse(idx,rng,buf,add,mul,ld,reduce):
  if rng not in reduce.src: return None
  return UOp(reduce.op, reduce.dtype, (UOp(ld.op, ld.dtype, (buf, add+mul*idx)),)+
             tuple(x for x in reduce.src[1:] if x is not rng), reduce.arg)

# this is symbolic 2.0
constant_folder = PatternMatcher([
  # CONTRACT before ALU/REDUCE/CAST
  (UPat(UOps.CONTRACT, name="con", src=(UPat(UOps.ALU, name="alu"),)),
   lambda con, alu: UOp(alu.op, con.dtype, tuple(UOp(UOps.CONTRACT, x.dtype.vec(con.dtype.count), (x,), con.arg) for x in alu.src), alu.arg)),
  (UPat(UOps.CONTRACT, name="con", src=(UPat(UOps.REDUCE, dtype={dtypes.half, dtypes.bfloat16, dtypes.float}, name="red"),)),
   lambda con, red: UOp(UOps.REDUCE, con.dtype, (UOp(UOps.CONTRACT, con.dtype, red.src[0:1], con.arg),)+red.src[1:], red.arg)),
  (UPat(UOps.CONTRACT, name="con", src=(UPat(UOps.CAST, dtype={dtypes.half, dtypes.bfloat16, dtypes.float}, src=(UPat(name="casted"),)),)),
   lambda con, casted: UOp(UOps.CAST, con.dtype, (UOp(UOps.CONTRACT, casted.dtype.vec(con.dtype.count), (casted,), con.arg),))),
  # bigint is rewritten to int32
  (UPat({UOps.CONST, UOps.ALU, UOps.SPECIAL, UOps.RANGE, UOps.EXPAND}, dtype=dtypes.bigint, name="x"),
   lambda x: UOp(x.op, dtypes.int32, x.src, x.arg)),
  # VECTORIZE/GEP
  (NOp(UOps.GEP, src=(NOp(UOps.VECTORIZE, name="cast"),), name="gep"), lambda gep, cast: cast.src[gep.arg]),
  *[(NOp(UOps.VECTORIZE, dtypes.float.vec(i), tuple(NOp(UOps.GEP, dtypes.float,
                         src=(NOp.var('x', dtype=dtypes.float.vec(i)),), arg=j) for j in range(i))), lambda x: x) for i in [2, 4, 8, 16]],
  *[(NOp(UOps.VECTORIZE, dtypes.half.vec(i), tuple(NOp(UOps.GEP, dtypes.half,
                         src=(NOp.var('x', dtype=dtypes.half.vec(i)),), arg=j) for j in range(i))), lambda x: x) for i in [2, 4, 8, 16]],
  # tensor core with a 0 input is acc
  (NOp(UOps.WMMA, src=(NOp.const(None, 0.0), NOp.var(), NOp.var('acc'))), lambda acc: acc),
  (NOp(UOps.WMMA, src=(NOp.var(), NOp.const(None, 0.0), NOp.var('acc'))), lambda acc: acc),
  # tensor core cleanups
  *[(NOp(UOps.REDUCE, src=(NOp(UOps.EXPAND, src=tuple(NOp(UOps.GEP, dtypes.float, src=(NOp.var('x'),), arg=i) for i in range(j)), name="expand"),)
    ,name="reduce", allow_any_len=True), reduce_before_expand) for j in [2,4,8]],
  (NOp.var("add") + NOp(UOps.WMMA, name="wmma"),
    lambda add, wmma: UOp(wmma.op, wmma.dtype, (wmma.src[0], wmma.src[1], wmma.src[2]+add), wmma.arg)),
  # threefry
  (NOp(UOps.ALU, dtype=dtypes.uint64, src=(NOp.var("x"), NOp.var("seed")), arg=BinaryOps.THREEFRY), threefry2x32),
  # sum collapse to mul (with possible GEP)
  (UPat(UOps.PHI, src=(UPat(UOps.DEFINE_ACC, name="phi_input", src=[UPat(UOps.CONST), UPat(UOps.RANGE, name="loop")]),
                       UPat(UOps.ALU, BinaryOps.ADD, src=(UPat(name="val1"), UPat(name="val2"))))), sum_collapse),
  (UPat(UOps.PHI, src=(UPat(UOps.GEP, name="phi_input", src=(UPat(UOps.DEFINE_ACC, src=[UPat(UOps.CONST), UPat(UOps.RANGE, name="loop")]),)),
                       UPat(UOps.ALU, BinaryOps.ADD, src=(UPat(name="val1"), UPat(name="val2"))))), sum_collapse),
  # extra arange loop folding because we don't fold adds. TODO: fold adds
  (NOp(UOps.REDUCE, src=((NOp.var("idx") + NOp.cvar("mval") * NOp(UOps.RANGE, src=(NOp.var("loop_start"), NOp.var("loop_end")), name="rng") +
                          NOp.var("idx2") + NOp.var("idx3"))
   .lt(NOp.cvar("compval")).where(NOp.cvar("multconst"), NOp.const(None, 0)),), arg=ReduceOps.SUM, name="reduce", allow_any_len=True), loop_collapse),
  (NOp(UOps.REDUCE, src=((NOp.var("idx") + NOp.cvar("mval") * NOp(UOps.RANGE, src=(NOp.var("loop_start"), NOp.var("loop_end")), name="rng") +
                          NOp.var("idx2"))
   .lt(NOp.cvar("compval")).where(NOp.cvar("multconst"), NOp.const(None, 0)),), arg=ReduceOps.SUM, name="reduce", allow_any_len=True), loop_collapse),
  # arange loop folding (reduce)
  (NOp(UOps.REDUCE, src=((NOp.var("idx") + NOp.cvar("mval") * NOp(UOps.RANGE, src=(NOp.var("loop_start"), NOp.var("loop_end")), name="rng"))
   .lt(NOp.cvar("compval")).where(NOp.cvar("multconst"), NOp.const(None, 0)),), arg=ReduceOps.SUM, name="reduce", allow_any_len=True), loop_collapse),
  (NOp(UOps.REDUCE, src=((NOp.var("idx") - NOp(UOps.RANGE, src=(NOp.var("loop_start"), NOp.var("loop_end")), name="rng"))
    .lt(NOp.cvar("compval")).where(NOp.cvar("multconst"), NOp.const(None, 0)),), arg=ReduceOps.SUM, name="reduce", allow_any_len=True),
    lambda **kwargs: loop_collapse(mval=UOp.const(dtypes.int, -1), **kwargs)),
  # indexing (with a multiply offset)!
  (NOp(UOps.REDUCE, src=(NOp.var('idx').eq(NOp(UOps.RANGE, name="rng")).cast()*
    NOp(UOps.LOAD, src=(NOp.var("buf"), NOp.var('add')+NOp.var('mul')*NOp(UOps.RANGE, name="rng")), name="ld"),),
    arg=ReduceOps.SUM, name="reduce", allow_any_len=True), index_collapse),
  (NOp(UOps.REDUCE, src=(NOp.var('idx').eq(NOp(UOps.RANGE, name="rng")).where(
    NOp(UOps.LOAD, src=(NOp.var("buf"), NOp.var('add')+NOp.var('mul')*NOp(UOps.RANGE, name="rng")), name="ld"), NOp.const(None, 0.0)),),
    arg=ReduceOps.SUM, name="reduce", allow_any_len=True), index_collapse),
  # other arange folders
  (NOp.cvar("c1") - (NOp.var("x") + NOp.cvar("c2")), lambda c1, c2, x: (c1-c2)-x),  # c1 - (x + c2) -> (c1-c2) - x
  # max folding
  (NOp.max(NOp.var('x'), NOp.var('y')), lambda x,y: x if x.vmin.arg >= y.vmax.arg else y if x.vmax.arg <= y.vmin.arg else None),
  # const rules
  (NOp(UOps.GEP, src=(NOp.cvar("c"),), name="root"), lambda root, c: root.const(c.arg)),
  (UPat(UOps.CAST, name="root", src=UPat(UOps.CONST, name="c")), lambda root, c: root.const(c.arg)),
  (UPat(UOps.VECTORIZE, name="root", src=UPat(UOps.CONST, name="c")), lambda root, c: root.const(c.arg)),
  # a phi on a DEFINE_ACC without loops or a CONST is a noop. this is for correctness, not just speed
  (NOp(UOps.PHI, src=(NOp(UOps.DEFINE_ACC, name="acc"), NOp.var("acc"))), lambda acc: UOp.cast(acc.src[0], acc.dtype)),
  (NOp(UOps.PHI, src=(NOp(UOps.DEFINE_ACC, src=(NOp.cvar(),)), NOp.var("x"))), lambda x: x),
  (NOp(UOps.PHI, src=(NOp.cvar(), NOp.var("x"))), lambda x: x),
  # a DEFINE_ACC without inputs is a const + GEP on a const is the const
  (NOp(UOps.DEFINE_ACC, src=(NOp.cvar(),), name="root"), lambda root: UOp.cast(root.src[0], root.dtype)),
  (NOp(UOps.GEP, src=(NOp.cvar("x"),), name="root"), lambda root,x: root.const(x.arg)),
  # a conditional with the same results either way is a noop, also fold const conditionals
  (NOp.var().where(NOp.var("val"), NOp.var("val")), lambda val: val),
  (NOp.cvar('gate').where(NOp.var('c0'), NOp.var('c1')), lambda gate, c0, c1: c0 if gate.arg else c1),
  # ** constant folding **
  (UPat(UOps.ALU, name="root", src=UPat(UOps.CONST)), lambda root: root.const(exec_alu(root.arg, root.dtype, [x.arg for x in root.src]))),
  # ** self folding **
  (-(-NOp.var('x')), lambda x: x),    # -(-x) -> x
  (NOp.var('x') + 0, lambda x: x),    # x+0 -> x
  (NOp.var('x') * 1, lambda x: x),    # x*1 -> x
  (NOp.var('x') * -1, lambda x: -x),  # x*-1 -> -x
  (NOp.var('x') // NOp.var('x'), lambda x: x.const(1)), # x//x -> 1
  (NOp.var('x') // 1, lambda x: x),   # x//1 -> x
  (NOp.var('x') // -1, lambda x: -x), # x//-1 -> -x
  (NOp.var('x') / NOp.var('x'), lambda x: x.const(1)), # x/x -> 1
  (NOp.var('x') / NOp.cvar('c'), lambda x,c: x*exec_alu(UnaryOps.RECIP, c.dtype, [c.arg])),    # x/c -> x*(1/c)
  (NOp.var('x', dtype=dtypes.bool).max(NOp.const(dtypes.bool, False)), lambda x: x),  # max(x, False) -> x
  # ** zero folding **
  #x*0 -> 0 or 0*x -> 0
  #if x is nan or inf it should render the nan value.
  # NOTE: this can be wrong for loaded NaN
  (NOp.var('x') * 0, lambda x: x.const(float('nan') if isinstance(x.arg, float) and (math.isnan(x.arg) or math.isinf(x.arg)) else 0)),
  (NOp.var('x') - NOp.var('x'), lambda x: x.const(0)),   # x-x -> 0
  # lt folding
  (NOp.var('x').lt(NOp.var('y')),
   lambda x,y: NOp.const(dtypes.bool, True) if x.vmax.arg < y.vmin.arg else NOp.const(dtypes.bool, False) if x.vmin.arg >= y.vmax.arg else None),
  # c0*x<c1 for positive int c0,c1
  ((NOp.cvar('c0',dtypes.int)*NOp.var('x')).lt(NOp.cvar('c1',dtypes.int)),
   lambda x,c0,c1: x.lt(math.ceil(c1.arg/c0.arg)) if c0.arg > 0 and c1.arg > 0 else None),
  # mul -> add -> lt
  (((NOp.cvar('c0')*NOp.var('x'))+NOp.var('x2')).lt(NOp.cvar('c1')),
   lambda x,x2,c0,c1: x.lt(c1.arg//c0.arg) if c1.arg % c0.arg == 0 and c0.arg > x2.vmax.arg and x2.vmin.arg >= 0 else None),
  # ** load/store folding **
  (NOp.store(NOp.var("buf"), NOp.var("idx"), NOp.load(NOp.var("buf"), NOp.var("idx"))), lambda buf,idx:UOp(UOps.NOOP)),
  # ** two stage add/mul folding **
  ((NOp.var('x') + NOp.cvar('c1')) + NOp.cvar('c2'), lambda x,c1,c2: x+x.const(exec_alu(BinaryOps.ADD, x.dtype, [c1.arg, c2.arg]))),
  ((NOp.var("x") * NOp.cvar("c1")) * NOp.cvar("c2"), lambda x,c1,c2: x*x.const(exec_alu(BinaryOps.MUL, x.dtype, [c1.arg, c2.arg]))),
  # *** rules from symbolic ***
  # div folding
  (NOp.var('x') // NOp.cvar('c'), lambda x,c: x.const(x.vmin.arg//c.arg) if c.arg > 0 and x.vmin.arg//c.arg == x.vmax.arg//c.arg else None),
  (NOp.var('x') // NOp.cvar('c'), lambda x,c: d if c.arg > 0 and (d:=x.divides(c.arg)) is not None else None),
  # mod folding
  (NOp.var('x') % NOp.cvar('c'), lambda x,c: x if 0 <= x.vmin.arg <= x.vmax.arg < c.arg else None),
  # mod reduction
  (NOp.var('x') % NOp.cvar('c'), lambda x,c: (x-(x.vmin.arg//c.arg)*c.arg)%c if 0 < c.arg <= x.vmin.arg else None),
  # mul -> mod
  ((NOp.cvar('c0')*NOp.var('x')) % NOp.cvar('c1'), lambda x,c0,c1:\
   x*(c0.arg%c1.arg)%c1 if c0.arg >= c1.arg > 0 else (x%(c1.arg//c0.arg))*c0 if c1.arg%c0.arg==0 else None),
  # mul -> add -> mod
  (((NOp.cvar('c0')*NOp.var('x'))+NOp.var('x2')) % NOp.cvar('c1'),
   lambda x,x2,c0,c1: x2%c1 if (r:=c0.arg%c1.arg) == 0 else (x*r+x2)%c1 if c0.arg >= c1.arg > 0 else None),
  # mul -> add -> div
  (((NOp.cvar('c0')*NOp.var('x'))+NOp.var('x2')) // NOp.cvar('c1'), lambda x,x2,c0,c1:\
   x*(c0.arg//g)//(c1.arg//g) if c0.arg > 0 and c1.arg > 0 and (g:=math.gcd(c0.arg,c1.arg)) > 1 and g > x2.vmax.arg and x2.vmin.arg >= 0 else None),
  # mod mod
  ((NOp.var('x') % NOp.cvar('c0')) % NOp.cvar('c1'), lambda x,c0,c1: x % c0 if 0 < c0.arg < c1.arg else x % c1 if c0.arg % c1.arg == 0 else None),
  (((NOp.var('x') * NOp.cvar('c0')) % NOp.cvar('c1')) % NOp.cvar('c0'), lambda x,c0,c1: x.const(0)),
  # -(x+y) -> -x + -y
  #(-(NOp.var("x") + NOp.var("y")), lambda x,y: (-x)+(-y)),
  # x%1 -> 0
  (NOp.var("x") % NOp.const(None, 1), lambda x: x.const(0)),
  # (x*c0)+(x*c1) -> x*(c0+c1)
  (NOp.var("x") * NOp.cvar("c0") + NOp.var("x") * NOp.cvar("c1"), lambda x,c0,c1: x*exec_alu(BinaryOps.ADD, x.dtype, [c0.arg, c1.arg])),
  # (x*c0)+(y*c0) -> (x+y)*c0
  #((NOp.var("x") * NOp.cvar("c0")) + (NOp.var("y") * NOp.cvar("c0")), lambda x,y,c0: c0*(x+y)),
  # mul div
  ((NOp.var("x") * NOp.cvar("c0")) // NOp.cvar("c1"),
   lambda x,c0,c1: x*(c0.arg//gcd)//(c1.arg//gcd) if c1.arg!=0 and (gcd:=math.gcd(c0.arg,c1.arg))> 1 else None),
  # (x*x2)/x2 -> x
  ((NOp.var("x") * NOp.var("x2")) / NOp.var("x2"), lambda x,x2: x),
  # (x//c0)//c1 -> x//(c0*c1)
  ((NOp.var("x") // NOp.cvar("c0")) // NOp.cvar("c1"), lambda x,c0,c1: x//x.const(exec_alu(BinaryOps.MUL, x.dtype, [c0.arg, c1.arg]))),
  # (x/x1)/x2 -> x/(x1*x2)
  ((NOp.var("x") / NOp.var("x2")) / NOp.var("x3"), lambda x,x2,x3: x/(x2*x3)),
  # c0 + x < c1 -> x < c1 - c0
  ((NOp.cvar("c0") + NOp.var("x")).lt(NOp.cvar("c1")), lambda x,c0,c1: UOp.lt(x, x.const(exec_alu(BinaryOps.ADD, x.dtype, [c1.arg, -c0.arg])))),
  # (x+x*c0)-> x*(c0+1)
  (NOp.var("x") + NOp.var("x") * NOp.cvar("c0"), lambda x,c0: x*(c0.arg+1)),
  # x!=0 -> (bool)x
  (NOp.var("x").ne(0), lambda x: x.cast(dtypes.bool)),
  # bool != 1 -> not bool
  (NOp.var("x", dtype=dtypes.bool).ne(1), lambda x: -x),
  # TODO: can do the invert of this (flip alt/load) when we fix double ops
  (NOp.store(NOp.var("buf"), NOp.var("idx"), NOp.var("gate").where(NOp.var("alt"), NOp.load(NOp.var("buf"), NOp.var("idx")))),
   lambda buf, idx, gate, alt: UOp.store(buf, idx, alt, gate)),
  # VECTORIZE-PHI-GEP -> PHI-VECTORIZE
  (NOp(UOps.VECTORIZE, src=tuple(NOp(UOps.PHI, src=(NOp(UOps.GEP, src=(NOp.var("val"),), arg=i), NOp.var(f"v{i}"))) for i in range(4)), name="root"),
   lambda root, val, v0, v1, v2, v3: UOp(UOps.PHI, root.dtype, (val, UOp(UOps.VECTORIZE, val.dtype, (v0, v1, v2, v3))))),
  (NOp(UOps.VECTORIZE, src=tuple(NOp(UOps.PHI, src=(NOp(UOps.GEP, src=(NOp.var("val"),), arg=i), NOp.var(f"v{i}"))) for i in range(2)), name="root"),
   lambda root, val, v0, v1: UOp(UOps.PHI, root.dtype, (val, UOp(UOps.VECTORIZE, val.dtype, (v0, v1))))),
  # NEG/CMPLT -> CMPLT
  (NOp.lt(-NOp.var('x'), NOp.cvar('c', dtypes.int)), lambda c,x: UOp.lt(c.const(-c.arg), x)),
  # cast NOOP (NOTE: it's str to deal with PtrDType)
  (NOp(UOps.CAST, name="root"), lambda root: root.src[0] if str(root.dtype) == str(root.src[0].dtype) else None),
  (NOp(UOps.VECTORIZE, name="root"), lambda root: root.src[0] if str(root.dtype) == str(root.src[0].dtype) else None),
  # fold gated LOAD/STORE
  (NOp.load(NOp.var("buf"), NOp.var("idx"), NOp.const(dtypes.bool, True), NOp.cvar("var")), lambda buf,idx,var: UOp.load(buf, idx, dtype=var.dtype)),
  (NOp.load(NOp.var("buf"), NOp.var("idx"), NOp.const(dtypes.bool, True), NOp.cvar("var"), NOp.var("barrier")),
   lambda buf,idx,var,barrier: UOp.load(buf, idx, barrier, dtype=var.dtype)),
  (NOp.load(NOp.var(), NOp.var(), NOp.const(dtypes.bool, False), NOp.cvar("var")), lambda var: var),
  (NOp.load(NOp.var(), NOp.var(), NOp.const(dtypes.bool, False), NOp.cvar("var"), NOp.var()), lambda var: var),
  (NOp.store(NOp.var("buf"), NOp.var("idx"), NOp.var("val"), NOp.const(dtypes.bool, True)), UOp.store),
  (NOp.store(NOp.var(), NOp.var(), NOp.var(), NOp.const(dtypes.bool, False)), lambda: UOp(UOps.NOOP)),
  # remove NOOPs from SINK
  (NOp(UOps.SINK, name="root"),
    lambda root: UOp(UOps.SINK, root.dtype, a, root.arg) if len(a:=tuple(x for x in root.src if x.op is not UOps.NOOP)) != len(root.src) else None),
  # ** move add consts to end **
  #(UPat(UOps.ALU, BinaryOps.ADD, src=(UPat(UOps.CONST, name='c1'), UPat(name='x'))), lambda c1,x: x+c1),
  (UPat(UOps.ALU, BinaryOps.ADD, src=(UPat(UOps.ALU, BinaryOps.ADD, src=(UPat(name='x'), UPat(UOps.CONST, name='c1'))), UPat(name='y'))),
    lambda x,c1,y: (x+y)+c1),
])

# *** uop expander ***

def _expand_arg_to_idx(args:Tuple[Tuple[int, int], ...], rpk:Dict[int, int]) -> int:
  idx, mul = 0, 1
  for axis,m in args[::-1]:
    idx += rpk[axis] * mul
    mul *= m
  return idx

def _choices_from_args(args:Tuple[Tuple[int, int], ...]) -> List[Dict[int, int]]:
  return [dict(x) for x in itertools.product(*[zip(itertools.repeat(axis), range(m)) for axis,m in args])]

def do_expand(root:UOp):
  if root.op is UOps.REDUCE:
    if root.src[0].op is not UOps.EXPAND: return None
    reduce_expand_args = flatten([x.arg for x in root.src[1:] if x.op is UOps.EXPAND])
    expand_args = tuple(x for x in root.src[0].arg if x not in reduce_expand_args)
    if len(expand_args) == 0: return None
    dont_expand_args = tuple(x for x in root.src[0].arg if x in reduce_expand_args)
  else:
    expands = [x for x in root.src if x.op is UOps.EXPAND]
    if len(expands) == 0: return None
    expand_args = tuple(sorted(dedup(flatten([x.arg for x in expands]))))
    if root.op is UOps.WMMA:
      # both the reduce and upcast args are not expanded here
      dont_expand_args = tuple(x for x in expand_args if x[0] in root.arg[-1] or x[0] in [y[0] for y in flatten(root.arg[-2])])
      expand_args = tuple(x for x in expand_args if x not in dont_expand_args)
    else:
      dont_expand_args = ()
  new_srcs: List[UOp] = []
  lrpks = _choices_from_args(dont_expand_args)
  for rpk in _choices_from_args(expand_args):
    new_src: List[UOp] = []
    for src in root.src:
      if src.op is UOps.EXPAND:
        lnew_src = tuple(src.src[_expand_arg_to_idx(src.arg, {**rpk, **lrpk})] for lrpk in lrpks)
        # TODO: is this right for UOps.WMMA? when there's more than one, all lnew_src should be the same
        new_src.append(lnew_src[0] if len(lnew_src) == 1 or root.op is UOps.WMMA else UOp(UOps.EXPAND, root.dtype, lnew_src, dont_expand_args))
      else:
        new_src.append(src)
    new_srcs.append(UOp(root.op, root.dtype, tuple(new_src), root.arg))
  if root.op is UOps.EXPAND:
    # merge two expands
    expand_args, old_args = tuple(sorted(root.arg+expand_args)), expand_args
    assert len(expand_args) == (len(old_args) + len(root.arg))
    new_srcs = [new_srcs[_expand_arg_to_idx(old_args, rpk)].src[_expand_arg_to_idx(root.arg, rpk)] for rpk in _choices_from_args(expand_args)]
  assert prod([x[1] for x in expand_args]) == len(new_srcs)
  return UOp(UOps.EXPAND, root.dtype, tuple(new_srcs), expand_args)

acc_number = 0
def do_reduce_with_expand(root):
  global acc_number
  expands = [x for x in root.src[1:] if x.op is UOps.EXPAND]
  expands_reduce = [x for x in expands if root.src[0].op is UOps.EXPAND and all(y in root.src[0].arg for y in x.arg)]
  expands_non_reduce = [x for x in expands if x not in expands_reduce]
  const = UOp.const(root.dtype.scalar(), 0 if root.arg is ReduceOps.SUM else dtypes.min(root.dtype))
  ret = acc = UOp(UOps.DEFINE_ACC, root.dtype, (const,) + tuple(x for x in root.src[1:] if x.op is not UOps.EXPAND), (acc_number,))
  acc_number += 1
  alu_op = {ReduceOps.SUM:BinaryOps.ADD, ReduceOps.MAX:BinaryOps.MAX}[cast(ReduceOps, root.arg)]
  if len(expands_reduce):
    assert root.src[0].op is UOps.EXPAND
    expand_reduce_args = dedup(flatten([x.arg for x in expands_reduce]))
    assert prod([y[1] for y in expand_reduce_args]) == len(root.src[0].src)
    ret = functools.reduce(lambda x,y: x.alu(alu_op, y), (ret,)+root.src[0].src)
  else:
    ret = ret.alu(alu_op, root.src[0])
  ret = UOp(UOps.PHI, ret.dtype, (acc, ret))
  if len(expands_non_reduce): ret = ret * prod([sz for _,sz in flatten([x.arg for x in expands_non_reduce])])
  return ret

def do_contract(con:UOp):
  ex = con.src[0]
  assert con.dtype is not None
  # CONTRACT without EXPAND repeats the element VECTORIZED
  if ex.op is not UOps.EXPAND or not all(x in ex.arg for x in con.arg):
    assert ex.op is not UOps.EXPAND or not any(x in ex.arg for x in con.arg), "partial contract not supported"
    return UOp(UOps.VECTORIZE, con.dtype, con.src*con.dtype.count)
  # CONTRACT may remove several axes from EXPAND
  assert con.dtype.count == prod([x[1] for x in con.arg]), "dtype is wrong"
  srcs = []
  for rpk in _choices_from_args(new_ex_args:=tuple(x for x in ex.arg if x not in con.arg)):
    lsrcs = [ex.src[_expand_arg_to_idx(ex.arg, {**rpk, **lrpk})] for lrpk in _choices_from_args(con.arg)]
    srcs.append(UOp(UOps.VECTORIZE, con.dtype, tuple(lsrcs)))
  return srcs[0] if len(srcs) == 1 else UOp(UOps.EXPAND, con.dtype, tuple(srcs), new_ex_args)

def no_vectorized_alu(alu):
  if alu.dtype.count == 1: return None
  alus = tuple(UOp(alu.op, alu.dtype.scalar(),
                   tuple(UOp(UOps.GEP, s.dtype.scalar(), (s,), i) for s in alu.src), alu.arg) for i in range(alu.dtype.count))
  return UOp(UOps.VECTORIZE, alu.dtype, alus)

expander = PatternMatcher([
  (UPat({UOps.ALU, UOps.CAST, UOps.BITCAST, UOps.GEP, UOps.WMMA, UOps.LOAD, UOps.STORE,
         UOps.VECTORIZE, UOps.REDUCE, UOps.EXPAND, UOps.IF}, name="root"), do_expand),
  (NOp(UOps.REDUCE, name="root"), do_reduce_with_expand),
  (NOp(UOps.CONTRACT, name="con"), do_contract),
  # remove EXPANDs from SINK
  (NOp(UOps.SINK, name="root"),
   lambda root: UOp(UOps.SINK, root.dtype, a, root.arg)
    if len(a:=tuple(flatten(x.src if x.op is UOps.EXPAND else (x,) for x in root.src))) != len(root.src) else None),
  # BARRIERs aren't actually expanded
  (NOp(UOps.BARRIER, src=(NOp(UOps.EXPAND, name="ex"),)), lambda ex: UOp(UOps.EXPAND, None, (UOp(UOps.BARRIER, None, ex.src),)*len(ex.src), ex.arg)),
  # empty EXPAND is NOOP
  (NOp(UOps.EXPAND, src=(NOp.var('x'),), arg=()), lambda x: x),
  # no ALU on vectorized dtypes
  (UPat({UOps.ALU, UOps.CAST, UOps.BITCAST}, name="alu"), no_vectorized_alu),
])

# *** uop graph ***

def get_children_dfs(u:UOp, children:Dict[UOp, List[UOp]], in_degree:Dict[UOp, int]):
  if u in children: return
  children[u] = []
  for x in u.src:
    get_children_dfs(x, children, in_degree)
    children[x].append(u)
  in_degree[u] = len(u.src)

def graph_rewrite(sink:UOp, pm:PatternMatcher) -> UOp:
  nodes: Dict[Tuple, UOp] = {}
  replace: Dict[UOp, UOp] = {}
  def __inner_rewrite(n:UOp) -> UOp:
    if n in replace: return replace[n]
    replace_source = (n.op, n.dtype, tuple(__inner_rewrite(y) for y in n.src), n.arg)
    if found := nodes.get(replace_source): replace[n] = found
    else: nodes[replace_source] = replace[n] = found = __inner_rewrite(new_x) if (new_x := pm.rewrite(x:=UOp(*replace_source))) else x
    return found
  return __inner_rewrite(sink)

class UOpGraph:
  def __init__(self, sink:Union[UOp, List[UOp]], opts:Optional[Renderer]=None):
    self.sink: UOp = sink if isinstance(sink, UOp) else UOp(UOps.SINK, None, tuple(sink))
    assert self.sink.op is UOps.SINK, f"sink isn't sink, it's {self.sink.op}"
    # used by linearizer
    self._uops: Optional[List[UOp]] = None
    self.opts = opts
    self.folder = constant_folder if opts is None or not opts.supports_float4 else (constant_folder+float4_folding)
    if TRANSCENDENTAL >= 2 or (opts is not None and TRANSCENDENTAL >= 1 and opts.device in {"CLANG", "LLVM"}):
      self.folder = self.folder + transcendental_folding

  def __reduce__(self): return self.__class__, (self.sink, self.opts)
  def __iter__(self) -> Iterator[UOp]: return iter(self.uops)
  def __getitem__(self, index) -> UOp: return self.uops[index]

  def vars(self) -> List[Variable]: return sorted([x.arg for x in self.uops if x.op is UOps.DEFINE_VAR], key=lambda v: v.expr)
  def globals(self) -> List[Tuple[int, bool]]: return [x.arg for x in self.uops if x.op is UOps.DEFINE_GLOBAL]

  @property
  def uops(self) -> List[UOp]:
    if self._uops is None: self.linearize()
    return cast(List[UOp], self._uops)

  def graph(self):
    from tinygrad.engine.graph import graph_uops
    graph_uops(self.uops)

  def print(self):
    for i,u in enumerate(self):
      formatted_parents = [self.uops.index(x) if x.op is not UOps.CONST else f"{x.arg}" for x in u.src]
      print(f"{i:4d} {str(u.op):20s}: {str(u.dtype) if u.dtype is not None else '':25s} " f"{str(formatted_parents):32s} {u.arg}")

  cnt = 0
  def linearize(self, extra_pm:Optional[PatternMatcher]=None):
    global acc_number
    acc_number = 0

    # NOTE: relinearizering should be okay
    #assert self._uops is None, "already linearized"

    # replace UOps.STORE gate with an IF block
    @functools.lru_cache(None)
    def _replace_gates(u:UOp, gate:UOp) -> UOp:
      if u.op is UOps.LOAD and u.src[-1].op is UOps.BARRIER:
        if_uop = UOp(UOps.IF, None, (gate, u.src[-1]))
        return UOp(u.op, u.dtype, u.src[:-1]+(if_uop,), u.arg)
      if (replace_source:=tuple(_replace_gates(x, gate) for x in u.src)) != u.src:
        if u.op is UOps.STORE and u.src[3] is gate: replace_source = replace_source[:3]
        return UOp(u.op, u.dtype, replace_source, u.arg)
      return u
    sink_srcs = list(self.sink.src)
    for i, s in enumerate(sink_srcs):
      if s.op is UOps.STORE and len(s.src) == 4 and (rw:=_replace_gates(s, s.src[3])) != s:
        sink_srcs[i] = UOp(rw.op, rw.dtype, rw.src, rw.arg)
    sink = UOp(UOps.SINK, None, tuple(sink_srcs))

    # do graph rewrite
    sink = graph_rewrite(sink, self.folder)

    # expand
    UOpGraph.cnt += 1
    if UOpGraph.cnt != getenv("DEBUG_EXPAND", 0): sink = graph_rewrite(sink, self.folder+expander)

    # for PTX only
    if extra_pm: sink = graph_rewrite(sink, self.folder+extra_pm)

    # filter nodes that don't link to a sink
    # BFS toposort
    children: Dict[UOp, List[UOp]] = {}
    in_degree: Dict[UOp, int] = {}
    get_children_dfs(sink, children, in_degree)

    @functools.lru_cache(None)
    def get_recursive_children(x:UOp, end:UOps, include_self=False) -> Set[UOp]:
      if x.op is UOps.SINK: return set()
      return set.union({x} if include_self else set(), *([get_recursive_children(u, end, True) for u in children[x] if x.op is not end]))

    # scope children impact the toposort and END* insertion
    scope_children = {p:get_recursive_children(p, END_FOR_UOP[p.op][0]) for p in reversed(in_degree) if p.op in END_FOR_UOP}

    queue:List[Tuple[int, UOp]] = []
    def push(u:UOp):
      priority = 0
      # prefer uops that are loop children
      for l, ss in scope_children.items():
        if l.op is UOps.RANGE and u in ss: priority -= l.arg[0]*1000 + l.arg[1]
      heapq.heappush(queue, (priority, u))

    for u in children:
      if in_degree[u] == 0: push(u)

    scope_end: Dict[UOp, UOp] = {}
    self._uops = []
    while queue:
      p,x = heapq.heappop(queue)
      if DEBUG >= 7: print(p,x)
      if x in scope_children: scope_end[x] = x
      if x.op is UOps.DEFINE_ACC:
        idx = min([self._uops.index(l) for l in x.src if l.op is UOps.RANGE])
        self._uops.insert(idx, x)
      else: self._uops.append(x)
      for u, ss in scope_children.items():
        if x in ss:
          ss.remove(x)
          if len(ss) == 0: scope_end[u] = x
      for u in children[x]:
        in_degree[u] -= 1
        if in_degree[u] == 0: push(u)

    # end scopes in toposort order
    for u, x in scope_end.items(): self._uops.insert(self._uops.index(x)+1, UOp(END_FOR_UOP[u.op][1], None, (u,)))

    # sanity checks (NOTE: these can cause things to be skipped in BEAM)
    bad_ops = dedup([x.op for x in self._uops if x.op in {UOps.EXPAND, UOps.CONTRACT, UOps.REDUCE}])
    try:
      type_verify(self.uops)
      assert self._uops[-1].op is UOps.SINK, f"didn't end with SINK, ended with {self._uops[-1]}"
      assert len(bad_ops) == 0, f"bad UOps left in list: {bad_ops}"
      # TODO: this should be enabled, and the valid clause should be removed
      # NOTE: multiple identical stores to DEFINE_LOCAL is okay
      assert len(all_stores := [x.src[0:2]+x.src[3:] for x in self._uops if x.op is UOps.STORE and x.src[0].op is not UOps.DEFINE_LOCAL]) \
        == len(dedup(all_stores)), "repeated stores in uops"
    except AssertionError as e:
      self.print()
      if not CI: self.graph()
      raise e

    # strip the SINK
    self._uops = self._uops[:-1]

    if getenv("FUZZ_UOPS"):
      from test.external.fuzz_uops import fuzz_uops
      self._fuzz_paths = fuzz_uops(self)
