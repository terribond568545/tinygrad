import sys
import random
from collections import defaultdict
from tqdm import tqdm
from tinygrad.helpers import dedup, ImageDType, getenv
from tinygrad.graph import print_tree
from tinygrad.codegen.linearizer import Linearizer
from tinygrad.lazy import var_vals_from_ast
from tinygrad.shape.symbolic import sym_infer
from tinygrad.ops import Device, Compiled, MemBuffer

# stuff needed to unpack a kernel
from tinygrad.ops import LazyOp, TernaryOps, BinaryOps, UnaryOps, ReduceOps, BufferOps, MemBuffer, ConstBuffer
from tinygrad.helpers import dtypes
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.view import View
from tinygrad.shape.symbolic import Variable
inf, nan = float('inf'), float('nan')

if __name__ == "__main__":
  ast_strs = dedup(open(sys.argv[1]).read().strip().split("\n"))

  # reduce kernels only
  ast_strs = [x for x in ast_strs if "ReduceOps" in x]

  # the device we are optimizing for
  device: Compiled = Device[Device.DEFAULT]
  print(f"optimizing for {Device.DEFAULT}")

  # random first kernels
  random.seed(1337)
  random.shuffle(ast_strs)
  ast_strs = ast_strs[:1000]

  print(f"loaded {len(ast_strs)} kernels")

  atm = []
  agflops = []
  for ast_str in tqdm(ast_strs):
    ast = eval(ast_str)
    lin = Linearizer(ast)

    # skip image textures
    if any(isinstance(x.dtype, ImageDType) for x in lin.bufs): continue

    # create output/input buffers
    bufsts = defaultdict(list)
    for x in lin.bufs:
      if isinstance(x, MemBuffer):
        bufsts[x.idx].append(x)
    buffer_count = len(bufsts)
    rawbufs = [None]*buffer_count
    for k,x in bufsts.items():
      rawbufs[k] = device.buffer(max(y.st.size() for y in x), x[0].dtype)
    assert all(x is not None for x in rawbufs)

    # linearize
    preopt = lin.colored_shape()
    lin.hand_coded_optimizations()
    postopt = lin.colored_shape()
    lin.linearize()

    # example var vals
    var_vals = {k:k.min for k in var_vals_from_ast(ast)}

    # time
    prg = device.to_program(lin)
    tm = min([prg(rawbufs, var_vals, force_wait=True) for _ in range(10)])
    atm.append(tm)

    # print
    #print_tree(ast)
    #for u in lin.uops: print(u)
    gflops = sym_infer(lin.info.flops, var_vals)*1e-9/tm
    agflops.append(gflops)
    if tm*1e6 > 100:
      print(f"{len(lin.uops)} uops, {lin.global_size} {lin.local_size}, {tm*1e6:.2f} us {gflops:.2f} GFLOPS", preopt, "->", postopt)

  print(f"all kernels ran in {sum(atm)*1e3:.2f} ms")

  if getenv("SHOW"):
    import matplotlib.pyplot as plt
    #plt.hist(agflops, bins=100)
    #plt.yscale('log')
    plt.scatter(atm, agflops)
    plt.xscale('log')
    plt.show()
