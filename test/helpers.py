from tinygrad.device import JITRunner
from tinygrad.helpers import DTYPES_DICT, dtypes
from tinygrad.ops import LazyOp, LoadOps
from tinygrad.nn.state import get_parameters

# for speed
def derandomize(x):
  if isinstance(x, LazyOp):
    new_op = LoadOps.EMPTY if x.op == LoadOps.CUSTOM else x.op
    return LazyOp(new_op, tuple([derandomize(s) for s in x.src]), None if x.op == LoadOps.CUSTOM else x.arg)
  x.op = derandomize(x.op)
  return x

def derandomize_model(model):
  for p in get_parameters(model):
    p.lazydata = derandomize(p.lazydata)
    p.realize()

def assert_jit_cache_len(fxn, expected_len):
  assert len(fxn.jit_cache) > 0
  if issubclass(type(fxn.jit_cache[0].prg), JITRunner):
    assert len(fxn.jit_cache) == expected_len
  else:
    assert len(fxn.jit_cache) == 1
    # until we have a better way of typing the prg in JitItem
    assert type(fxn.jit_cache[0].prg).__name__.endswith('Graph')
    assert len(fxn.jit_cache[0].prg.jit_cache) == expected_len

float_dtypes = [dtype for dtype in DTYPES_DICT.values() if dtypes.is_float(dtype)]
int_dtypes = [dtype for dtype in DTYPES_DICT.values() if dtypes.is_int(dtype)]
