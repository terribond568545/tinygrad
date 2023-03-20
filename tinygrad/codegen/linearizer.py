from typing import List, Tuple, Any, Optional, cast, Dict, DefaultDict, NamedTuple
import itertools, math
from collections import defaultdict
from enum import Enum, auto

from tinygrad.helpers import dedup, colored, ImageDType, DEBUG, prod, dtypes, mnum, DType
from tinygrad.ops import LazyOp, get_lazyops, get_buffers, FlopCounter, get_lazyop_info, map_buffers, UnaryOps
from tinygrad.lazy import LazyBuffer
from tinygrad.ops import MovementOps, ReduceOps, BinaryOps, FusedOps
from tinygrad.shape.shapetracker import ShapeTracker, strides_for_shape
from tinygrad.shape.symbolic import Variable, SumNode, ModNode

class UOps(Enum): LOOP = auto(); DEFINE_LOCAL = auto(); LOAD = auto(); ALU = auto(); CONST = auto(); ENDLOOP = auto(); STORE = auto(); LOAD4 = auto(); STORE4 = auto() # noqa: E702

class LocalBuffer(NamedTuple):
  dtype: DType = dtypes.float32
  realized: None = None

class MemOp(NamedTuple):
  i: int
  idx: Variable
  valid: Variable

class UOp(NamedTuple):
  uop: UOps
  out: Optional[str]
  vin: List[str]
  arg: Any
  def __repr__(self): return f"{str(self.uop):20s}: {self.out if self.out is not None else '':10s} {str(self.vin):32s} {self.arg}"

def check_no_mul(test, var):
  if test == var: return True
  if isinstance(test, SumNode): return any(check_no_mul(x, var) for x in test.nodes) # in a sum is okay
  if isinstance(test, ModNode) and test.b%4 == 0: return check_no_mul(test.a, var)   # removing a mod is okay
  return False

class Linearizer:
  supports_float4: bool = False

  def __init__(self, ast:LazyOp, output_buffer:LazyBuffer):
    # NOTE: if there's a RESHAPE, we skip it. the output shape is set from the reduce op or a latebuf
    self.ast = ast.src[0] if ast.op == MovementOps.RESHAPE else ast

    # get the output buffers
    self.bufs = [output_buffer] + dedup(get_buffers(ast))

    # key for lookup in cache (can change, str might not be right)
    # bufs are needed because kernels like f(x) = x + x and f(x, y) = x + y have the same str(ast), but are different kernels.
    # mapping the buffers to integers is required because a-b != b-a (and how would you tell a and b apart?)
    self.key = f"ASTKernelKey ast={str(map_buffers({x:i for i,x in enumerate(self.bufs)}, ast))} bufs={self.bufs}"

  def process(self) -> None:
    if hasattr(self, "sts"): return   # already processed

    # fetch lazyop info
    self.info: FlopCounter = get_lazyop_info(self.ast)
    self.mem_estimate: int = sum(x.dtype.itemsize*(x.realized.size if x.realized is not None else prod(x.shape)) for x in self.bufs if x is not None)

    # there's only allowed to be one reduceop
    reduceops = [x for x in get_lazyops(self.ast) if x.op in ReduceOps]
    assert len(dedup(reduceops)) <= 1, "max one reduce op in an ast"
    self.reduceop = reduceops[0] if reduceops else None

    # get earlybufs, before the one reduce op
    self.earlybufs = dedup(get_buffers(self.reduceop)) if self.reduceop else []

    # create new shapetrackers inside this kernel, we will permute them
    self.sts: List[ShapeTracker] = [x.st.copy() for x in self.bufs]
    for st in self.sts: st.simplify()

    # make the output buffer shape correct in here
    self.sts[0].reshape(self.info.shape)
    self.full_buf_index: int = self.bufs.index(self.earlybufs[0]) if len(self.earlybufs) > 0 else 0

    # move all reduce axes to the end
    reduce = list(enumerate(zip(self.full_shape, self.sts[0].shape)))
    permute = tuple([i for i,(s,n) in reduce if s == n] + [i for i,(s,n) in reduce if s != n])
    self.reshape_and_permute(None, permute)

    # parameters
    self.group_for_reduce: List[int] = []
    self.upcasted: int = 0

    # group simplifies
    self.simplify_ones()
    self.simplify_merge_adjacent()

  # NOTE: this stride is only on the last view, and may not be real
  def upcasted_axis(self, i):
    return list(zip(self.sts[i].shape[self.shape_len-self.upcasted:],
                    self.sts[i].strides[self.shape_len-self.upcasted:],
                    [x!=y for x,y in zip(self.sts[0].shape[self.shape_len-self.upcasted:], self.full_shape[self.shape_len-self.upcasted:])]))
  def offsets(self, i): return [sum(t) for t in itertools.product(*[[y*x[1] for y in range(x[0])] for x in self.upcasted_axis(i)[::-1]])] if self.upcasted > 0 else [0]
  def can_float4(self, i): return any(a[0:2] == (4,1) for a in self.upcasted_axis(i))
  def acc_offsets(self, i):
    if self.upcasted == 0: return [0]
    acc_strides = [x*(1-self.upcasted_axis(i)[::-1][i][2]) for i,x in enumerate(strides_for_shape(tuple(1 if r else s for s,_,r in self.upcasted_axis(i)[::-1])))]
    return [sum(t) for t in itertools.product(*[[y*acc_strides[i] for y in range(x[0])] for i,x in enumerate(self.upcasted_axis(i)[::-1])])]

  def can_merge_float4(self, i:int, idxs:List[Variable], offset:int) -> bool:
    if offset%4 != 0: return False
    float4_index = Variable("FLOAT4_INDEX", 0, 3)
    idxy_test, valid_test = self.sts[i].expr_idxs(float4_index+offset, idxs)
    # float4_index must not be in after divide or in valid. NOTE: this forces it to always be aligned too, maybe not required?
    ret = check_no_mul(idxy_test, float4_index) and "FLOAT4_INDEX" not in (idxy_test//4).render() and "FLOAT4_INDEX" not in (valid_test//4).render()
    if DEBUG >= 4: print(f"fuse buf {i} {ret} :", check_no_mul(idxy_test, float4_index), idxy_test//4, valid_test//4)
    return ret

  def global_buf(self, i, idxs:List[Variable], store=None):
    should_upcast = self.supports_float4 and self.can_float4(i) and self.bufs[i].dtype != dtypes.float16
    cache: Dict[int, str] = {}
    store_offset: Dict[int, int] = {y:x for x,y in enumerate(self.offsets(i))}  # NOTE: for stores, these should be unique
    def op(offset):
      if offset in cache: return cache[offset]
      will_merge = should_upcast and self.can_merge_float4(i, idxs, offset)
      if store is not None:
        if offset in store_offset:
          offsets = []
          for j in range(0, 4 if will_merge else 1):
            offsets.append(store[store_offset[offset+j]])
            del store_offset[offset+j]
          self.uop(UOps.STORE4 if will_merge else UOps.STORE, None, offsets, MemOp(i, *self.sts[i].expr_idxs(offset, idxs)))
      else:
        reg = self.uop(UOps.LOAD4 if will_merge else UOps.LOAD, f"val{mnum(i)}_{mnum(offset)}", [], MemOp(i, *self.sts[i].expr_idxs(offset, idxs)))
        if will_merge:
          for j in range(0, 4): cache[offset+j] = reg+"."+"xyzw"[j]
        else:
          cache[offset] = reg
        return cache[offset]
    return [op(o) for o in self.offsets(i)]

  def linearize(self):
    # uops
    self.uops: List[UOp] = []

    # add a local buffer for multistage reduce
    if len(self.group_for_reduce):
      self.bufs.append(LocalBuffer())
      # TODO: the strides of this can be controlled
      self.sts.append(ShapeTracker(tuple([1] * self.first_reduce + self.group_for_reduce + [1] * (self.shape_len - self.upcasted - len(self.group_for_reduce) - self.first_reduce) + [x[0] for x in self.upcasted_axis(0)])))
      self.uop(UOps.DEFINE_LOCAL, None, [], ("temp", self.sts[-1].size()))

    # print
    if DEBUG >= 3: self.printbufs()

    # kernel name (before late upcast)
    self.function_name = ("r_" if self.reduceop else "E_") + '_'.join([str(x) for x in self.full_shape])
    self.display_name = ("r_" if self.reduceop else "E_") + colored('_', 'black', bright=True).join([colored(str(x), c) for x,c in zip(self.full_shape, self.colors())]) + " " * (23-len(self.function_name))

    # parse AST
    loaded_buffers = {}
    acc = []

    # ssa
    _ssa:DefaultDict[str,int] = defaultdict(int)
    def ssa(name):
      _ssa[name] += 1
      return f"{name}{_ssa[name]-1}"

    # global loop
    global_idxs = [Variable(f"gidx{i}", 0, self.full_shape[i]-1 if i < self.first_reduce else 0) for i in range(0, self.first_reduce+len(self.group_for_reduce))]
    self.uop(UOps.LOOP, None, [], (global_idxs, "global"))

    # local loop
    if self.group_for_reduce:
      # NOTE: this is assuming the global size = the local size in these dims. in general, this doesn't have to be true
      local_idxs = [Variable(f"lidx{i}", 0, self.full_shape[i]-1 if i >= self.first_reduce else 0) for i in range(0, self.first_reduce+len(self.group_for_reduce))]
      self.uop(UOps.LOOP, None, [], (local_idxs, "local"))
      gl_idxs = [x*(y.max+1)+y for x,y in zip(global_idxs, local_idxs)]
    else:
      # without local idxs, it's just the global idxs
      gl_idxs = global_idxs

    # reduce op
    if self.reduceop is not None:
      # define accumulator
      acc = [self.uop(UOps.CONST, ssa('acc'), [], {ReduceOps.SUM: 0.0, ReduceOps.MAX: -math.inf}[cast(ReduceOps, self.reduceop.op)]) for _ in self.offsets(0)]

      # reduce loop
      reduce_idxs = [Variable(f"ridx{i}", 0, self.full_shape[i]-1) for i in range(self.first_reduce+len(self.group_for_reduce), self.shape_len-self.upcasted)]
      self.uop(UOps.LOOP, None, [], (reduce_idxs, "reduce"))

      # load earlybufs
      loaded_buffers.update({b:self.global_buf(i, gl_idxs+reduce_idxs) for i,b in enumerate(self.bufs) if b in self.earlybufs and i != 0})

      # run early AST (with reduce)
      self.ast_parse(self.reduceop, [acc[off] for off in self.acc_offsets(self.full_buf_index)], loaded_buffers, ssa, do_reduce=True)

      # end the reduce loop
      self.uop(UOps.ENDLOOP, None, [], (reduce_idxs, "reduce"))

      # end the local loop, do the local reduce
      if self.group_for_reduce:
        self.global_buf(-1, local_idxs, acc)  # store accumulators
        self.uop(UOps.ENDLOOP, None, [], (local_idxs, "local"))   # this is a barrier on GPUs

        # if any group_for_reduce items aren't reduces, upcast them here
        for j in self.upcast_in_mid_reduce_axes:
          self.reshape_and_permute(None, [i for i in range(self.shape_len) if i != j] + [j])
          self.upcast()
          self.group_for_reduce.pop()

        # NOTE: this structure is the same as the reduce op above

        # define late accumulator
        acc = [self.uop(UOps.CONST, ssa('lacc'), [], {ReduceOps.SUM: 0.0, ReduceOps.MAX: -math.inf}[cast(ReduceOps, self.reduceop.op)]) for _ in self.offsets(-1)]

        # late reduce loop
        end_local_idxs = [Variable(f"tidx{i}", 0, self.full_shape[i]-1 if i >= self.first_reduce else 0) for i in range(0, self.first_reduce+len(self.group_for_reduce))]
        self.uop(UOps.LOOP, None, [], (end_local_idxs, "late_reduce"))

        # load localbufs
        loaded_buffers["LOCAL_BUFFER"] = self.global_buf(-1, end_local_idxs)

        # there's no AST here (and there's no shape for the reduce LazyOp)
        self.ast_parse(LazyOp(self.reduceop.op, ("LOCAL_BUFFER",)), [acc[off] for off in self.acc_offsets(-1)], loaded_buffers, ssa, do_reduce=True)

        # end the late reduce loop
        self.uop(UOps.ENDLOOP, None, [], (end_local_idxs, "late_reduce"))

    # load latebufs
    loaded_buffers.update({b:self.global_buf(i, global_idxs) for i,b in enumerate(self.bufs) if b not in self.earlybufs and i != 0 and not isinstance(b, LocalBuffer)})

    # run late AST
    val = self.ast_parse(self.ast, acc, loaded_buffers, ssa)

    # store
    self.global_buf(0, global_idxs, val)

    # end the global loop
    self.uop(UOps.ENDLOOP, None, [], (global_idxs, "global"))

  def uop(self, uop:UOps, out:Optional[str], vin:List[str], arg:Any):
    self.uops.append(UOp(uop, out, vin, arg))
    if DEBUG >= 4: print(self.uops[-1])
    return out

  def ast_parse(self, x, acc, loaded_buffers, ssa, do_reduce=False) -> List[str]:
    if not isinstance(x, LazyOp): return loaded_buffers[x]
    if x.op in [UnaryOps.NOOP, UnaryOps.CAST]: return self.ast_parse(x.src[0], acc, loaded_buffers, ssa)  # cast isn't an ALU op
    if x.op in ReduceOps and not do_reduce: return acc
    # MULACC fusion. TODO: this is copied from Interpreted
    if x.op == ReduceOps.SUM and isinstance(x.src[0], LazyOp) and x.src[0].op == BinaryOps.MUL:
      x = LazyOp(FusedOps.MULACC, x.src[0].src, x.arg)
    values = [self.ast_parse(v, acc, loaded_buffers, ssa) for v in x.src]
    if isinstance(x.op, (ReduceOps, FusedOps)):
      return [self.uop(UOps.ALU, val[0], list(val), {ReduceOps.SUM:BinaryOps.ADD, ReduceOps.MAX:BinaryOps.MAX, FusedOps.MULACC:FusedOps.MULACC}[x.op]) for val in zip(acc, *values)]
    else:
      return [self.uop(UOps.ALU, ssa('alu'), list(val), x.op) for val in zip(*values)]

  @property
  def first_reduce(self) -> int: return [x!=y for x,y in zip(self.sts[0].shape[:self.shape_len-self.upcasted]+(0,), self.full_shape[:self.shape_len-self.upcasted]+(1,))].index(True)

  @property
  def full_shape(self) -> Tuple[int, ...]: return self.sts[self.full_buf_index].shape

  @property
  def full_unupcasted_shape(self) -> Tuple[int, ...]: return self.full_shape[:self.shape_len-self.upcasted]

  @property
  def shape_len(self) -> int: return len(self.sts[0].shape)

  @property
  def upcast_in_mid_reduce_axes(self) -> List[int]: return [j for j in range(self.first_reduce, self.first_reduce+len(self.group_for_reduce)) if self.full_shape[j] == self.sts[0].shape[j]]

  def colors(self) -> List[str]:
    # up to first_reduce, they are all global (blue)
    colors = ["blue"] * self.first_reduce
    # between first_reduce and first_reduce + group_for_reduce, they are either local (cyan), or late upcasted (green)
    colors += ["green" if i in self.upcast_in_mid_reduce_axes else "cyan" for i in range(self.first_reduce, self.first_reduce + len(self.group_for_reduce))]
    # between first_reduce + group_for_reduce and upcasted, they are reduce (red)
    colors += ["red"] * ((self.shape_len-self.upcasted) - (self.first_reduce + len(self.group_for_reduce)))
    # upcasted dimensions are reduce (magenta) or normal (yellow)
    colors += ["magenta" if self.full_shape[i] != self.sts[0].shape[i] else "yellow" for i in range(self.shape_len-self.upcasted, self.shape_len)]
    assert len(colors) == self.shape_len, "colors size mismatch"
    return colors

  def printbufs(self, prefix=""):
    for i in range(len(self.sts)):
      print(prefix, f"{i:3d} {str(self.bufs[i].realized) if self.bufs[i] is not None else 'FAKE':47s}", self.sts[i].views)
    print(' '.join(colored(f"{s:4d}", color) for s,color in zip(self.full_shape, self.colors())))

  # ******************** base simplifiers ********************

  # apply reshape and permute to all shapetrackers
  def reshape_and_permute(self, new_shape_fxn, axis):
    for st in self.sts:
      if new_shape_fxn is not None: st.reshape(tuple(new_shape_fxn(st.shape)))
      if axis is not None: st.permute(tuple(axis))

  # drops the final dimension
  def upcast(self):
    assert self.full_shape[-1] != 1, "can't upcast a dimension with size 1"
    self.upcasted += 1

  # axis : the axis to pull from
  # amount : the amount to take
  # top : if you want to pull that amount from the top
  # insert_before : place to insert the new stuff
  def shift_to(self, axis, amount, top=False, insert_before=None):
    if insert_before is None: insert_before = self.shape_len
    move_axis = axis if top else axis+1
    if move_axis < insert_before: insert_before += 1
    self.reshape_and_permute(
      lambda x: list(x[0:axis]) + (([amount, x[axis]//amount] if top else [x[axis]//amount, amount]) if x[axis] > 1 else [1,1]) + list(x[axis+1:]),
      [i for i in range(insert_before) if i != move_axis] + [move_axis] + [i for i in range(insert_before, self.shape_len+1) if i != move_axis])

  # ******************** complex simplifiers ********************

  def simplify_ones(self):
    # remove places where the shape is all ones
    # TODO: this should be factored in to multi shape stride
    all_ones = [all(st.shape[i]==1 for st in self.sts) for i in range(self.shape_len)]
    # keep at least 1 one
    if all(all_ones): all_ones[-1] = False
    self.reshape_and_permute(lambda shape: [x for i,x in enumerate(shape) if not all_ones[i]], None)

  def simplify_merge_adjacent(self):
    if self.shape_len == 0: return
    shapes, strides = [x.shape for x in self.sts], [x.views[-1].strides for x in self.sts]

    # merge dimensions if we can, multi get_shape_strides
    # TODO: does this always preserve the reduce dimension, NO
    # TODO: move this into shapetracker, with tests!
    rets = [[(shapes[j][0], strides[j][0])] for j in range(len(shapes))]
    for i in range(1, len(shapes[0])):
      can_merge = []
      for j in range(len(shapes)):
        # TODO: added the always mergeability of 1s, is this right? if so, add to shapetracker in the 1 case
        can_merge.append((strides[j][i] != 0 and rets[j][-1][1] == shapes[j][i]*strides[j][i]) or (strides[j][i] == 0 and rets[j][-1][1] == 0))
      # more can merge than this
      mergeable = all(can_merge) and i != self.first_reduce
      for j in range(len(shapes)):
        if mergeable: rets[j][-1] = (rets[j][-1][0] * shapes[j][i], strides[j][i])
        else: rets[j].append((shapes[j][i], strides[j][i]))

    # do the reshapes
    for i,x in enumerate(rets): self.sts[i].reshape(tuple(y[0] for y in x))

  # ******************** GPU simplifiers ********************

  def required_optimizations(self, early_only=False):
    for buf_index,buf in enumerate(self.bufs):
      upcast_strides = [self.sts[buf_index].strides[i] for i in self.upcast_in_mid_reduce_axes]
      if (not early_only or buf in self.earlybufs) and isinstance(self.bufs[buf_index].dtype, ImageDType) and not (self.can_float4(buf_index) or (buf not in self.earlybufs and (1 in upcast_strides))):
        axes = [i for i,x in enumerate(self.sts[buf_index].strides) if x == 1]
        assert len(axes) == 1, f"wrong number of stride 1 axis : {axes} on buf_index {buf_index}, {self.sts[buf_index]}"
        assert self.sts[buf_index].shape[axes[0]]%4 == 0, f"axis:{axes[0]} in buffer {buf_index} is not a multiple of 4, {self.sts[buf_index].shape}"
        self.shift_to(axes[0], 4)
        self.upcast()
        assert self.can_float4(buf_index)

  def hand_coded_optimizations(self):
    # if there's images in the earlybufs, we have to make an axis the 4 loading one
    self.required_optimizations(early_only=True)

    # simplify (sets first_reduce)
    self.simplify_ones()

    # are we grouping? (requires local shape support)
    if not self.can_float4(0) and self.first_reduce <= 2 and self.first_reduce + 1 <= self.shape_len and prod(self.sts[0].shape[:self.first_reduce]) <= 2048:
      # TODO: use 1024 if it's allowed in a smarter way
      for sz in (([256, 16]) if prod(self.sts[0].shape[:self.first_reduce]) <= 32 else [16]):
        if all([st.shape[self.first_reduce] % sz == 0 or st.shape[self.first_reduce] == 1 for st in self.sts]):
          self.shift_to(self.first_reduce, sz, top=True, insert_before=self.first_reduce + len(self.group_for_reduce))
          self.group_for_reduce.append(sz)
          break

    # are we upcasting in mid reduce? (only for images)
    if self.bufs[0].dtype.name.startswith('image') and not self.can_float4(0) and self.group_for_reduce and self.first_reduce <= 2 and prod(self.sts[0].shape) > 1:
      axes = [i for i,x in enumerate(self.sts[0].strides) if x == 1]
      assert len(axes) == 1, f"wrong number of stride 1 axis : {axes}"
      if self.sts[0].shape[axes[0]]%4 == 0:
        self.shift_to(axes[0], 4, insert_before=self.first_reduce + len(self.group_for_reduce))   # insert at the end of the grouped axis
        self.group_for_reduce.append(4)

    # now do everything required
    self.required_optimizations()

    # simplify (sets first_reduce)
    self.simplify_ones()

    # use more opencl indexing if the output buffer is an image and we have room
    if self.bufs[0].dtype.name.startswith('image') and self.first_reduce+len(self.group_for_reduce) < 3:
      base_shape = self.bufs[0].dtype.shape
      if (base_shape[0]*base_shape[1]) % self.sts[0].shape[0] == 0 and self.sts[0].shape[0]//base_shape[0] != 0:
        if DEBUG >= 4: print("split opencl", base_shape, self.sts[0].shape)
        self.reshape_and_permute(lambda x: [base_shape[0], x[0]//base_shape[0]]+list(x[1:]), None)
        self.simplify_ones()

    # no more opt if we are grouping
    if self.group_for_reduce: return

    # **** below this line need to be optional and benchmarked ****

    # potentially do more upcasts of non reduce axes based on a heuristic
    while prod(self.sts[0].shape[:self.first_reduce]) >= 1024:
      xb_choices = []
      for axis, upcast_amount in itertools.product(range(self.first_reduce), [3,4]):   # consider all the non reduce axes, and a 3 or 4 reduce
        # if it mods, and some buffer has stride 0 on axis while having no stride 0 in the buftoken
        if self.full_shape[axis]%upcast_amount == 0 and any(self.sts[buf_index].strides[axis] == 0 and not any(x[1] == 0 for x in self.upcasted_axis(buf_index)) for buf_index in range(len(self.sts))):
          xb_choices.append((sum(st.strides[axis]>0 for st in self.sts), sum(st.strides[axis] for st in self.sts), axis, upcast_amount))
      if len(xb_choices):
        xb_choices = sorted(xb_choices)
        if DEBUG >= 4: print(f"float4 merging axis : {xb_choices}")
        self.shift_to(xb_choices[0][2], amount=xb_choices[0][3])
        self.upcast()
        self.simplify_ones()
      else:
        break

    # if last dim <= 5 and it's a reduce dim, upcast the reduce (loop unrolling). no simplify needed since it's just an upcast. NOTE: careful, this has broken VALIDHACKS
    if self.first_reduce < (self.shape_len-self.upcasted) and self.full_unupcasted_shape[-1] <= 5 and (len(self.offsets(self.full_buf_index)) <= 4 or not any(r for _,_,r in self.upcasted_axis(self.full_buf_index))):
      self.upcast()
