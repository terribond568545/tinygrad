# TODO: move Device to here and proxy buffer call
from enum import Enum
UnaryOps = Enum("UnaryOps", ["RELU", "EXP", "LOG", "NEG", "SIGN"])
BinaryOps = Enum("BinaryOps", ["ADD", "SUB", "MUL", "DIV", "POW", "CMPEQ"])
ReduceOps = Enum("ReduceOps", ["SUM", "MAX"])
MovementOps = Enum("MovementOps", ["RESHAPE", "PERMUTE", "SLICE", "EXPAND", "FLIP"])
ProcessingOps = Enum("ProcessingOps", ["CONV", "CONVT", "CONVDW"])

from tinygrad.shapetracker import ShapeTracker

import os
DEBUG = int(os.getenv("PRINT_LLOPS", "0"))
GRAPH = int(os.getenv("GRAPH", "0"))
if GRAPH:
  import atexit
  import networkx as nx
  G = nx.DiGraph()
  def save_graph_exit():
    print("saving", G)
    nx.drawing.nx_pydot.write_dot(G, '/tmp/net.dot')
  atexit.register(save_graph_exit)

global_num_max = 0
def log_op(op, ret, inp):
  if DEBUG: print(f"{op} : {', '.join([str(x.shape) for x in inp])} -> {ret.shape}")
  if GRAPH:
    def nm(x):
      global global_num_max
      if getattr(x, 'global_num', None) is None:
        setattr(x, 'global_num', global_num_max)
        global_num_max += 1
      return f"<<< {x.global_num} >>>"

    top,sop = str(op).split(".")
    top_colors = {"UnaryOps": "#c0c0c0", "ReduceOps": "#8080ff", "BinaryOps": "#c0c0c0", "MovementOps": "#80ff80", "ProcessingOps": "#ff8080"}

    for x in inp:
      G.add_edge(nm(x), nm(ret), label=sop)
      G.nodes[nm(x)]['label'] = str(x.shape)
    G.nodes[nm(ret)]['label'] = str(ret.shape)
    G.nodes[nm(ret)]['fillcolor'] = top_colors[top]
    G.nodes[nm(ret)]['style'] = 'filled'

class Ops:
  def unary_op(ctx, op:UnaryOps, x):
    ret = ctx.buffer(x.shape)
    ctx.op.unary_op(op, x, ret)
    log_op(op, ret, [x])
    return ret

  def reduce_op(ctx, op:BinaryOps, x, new_shape):
    ret = ctx.buffer(new_shape)
    ctx.op.reduce_op(op, x, ret)
    log_op(op, ret, [x])
    return ret

  def binary_op(ctx, op:ReduceOps, x, y):
    assert x.shape == y.shape
    ret = ctx.buffer(x.shape)
    ctx.op.binary_op(op, x, y, ret)
    log_op(op, ret, [x, y])
    return ret

  def movement_op(ctx, op:MovementOps, x, arg=None):
    ret = ctx.buffer(ShapeTracker(*x.shape).movement_op(op, arg).shape)
    ctx.op.movement_op(op, x, ret, arg)
    log_op(op, ret, [x])
    return ret

  def processing_op(ctx, op:ProcessingOps, x, y, out_shape, C):
    # TODO: can we do better than out_shape?
    ret = ctx.buffer(out_shape)
    ctx.op.processing_op(op, x, y, ret, C)
    log_op(op, ret, [x, y])
    return ret