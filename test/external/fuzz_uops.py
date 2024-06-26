import numpy as np
from dataclasses import replace
from typing import Dict, List, Set, Tuple
from tinygrad.codegen.uops import UOp, UOpGraph, UOps
from tinygrad.device import Buffer, Device
from tinygrad.engine.realize import CompiledRunner
from tinygrad.helpers import DEBUG, colored, getenv
from tinygrad.shape.symbolic import Variable
from tinygrad.tensor import _to_np_dtype

def fuzz_uops(graph:Dict[UOp, List[UOp]], in_degree:Dict[UOp, int], loops_children:Dict[UOp, Set[UOp]]):
  paths: List[List[UOp]] = []
  # TODO: express DEFINE_ACC and loop children conditions in the graph, builtin.
  for p in find_all_toposorts(graph, in_degree):
    assert p[-1].op is UOps.SINK, f"didn't end with SINK, ended with {p[-1]}"
    paths.append(path:=list(p[:-1]))
    for u in path:
      if u.op is UOps.IF: path.append(UOp(UOps.ENDIF, None, (u,)))
      if u.op is UOps.RANGE:
        path.insert(max(path.index(x) for x in loops_children[u] if x in path)+1, UOp(UOps.ENDRANGE, None, (u,)))
  return paths

class UOpsFuzzerRunner(CompiledRunner):
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False):
    assert self.p.uops is not None and len(self.p.uops.fuzz_paths) >= 1
    init_rawbufs, init_name = {x:x.as_buffer() for x in rawbufs}, self.p.function_name
    init_globals = {i[0]:buf for i, buf in zip(self.p.globals, rawbufs)}
    if DEBUG >= 1: print(colored(f"fuzzing {len(self.p.uops.fuzz_paths)} UOps permutations for {init_name}", "yellow"))

    super().__call__(rawbufs, var_vals, wait)
    ground_truth = {x:np.frombuffer(x.as_buffer(), _to_np_dtype(x.dtype)) for x in rawbufs}

    for i, path in enumerate(self.p.uops.fuzz_paths):
      # setup prg
      uops = UOpGraph([])
      uops._uops = list(path)
      if DEBUG >= 6: uops.print()
      self.p = replace(self.p, name=(name:=f"{init_name}fuzz{i}"), src=Device[self.p.dname].renderer.render(name, uops), uops=uops)
      if DEBUG >= 4: print(self.p.src)
      self.lib = Device[self.p.dname].compiler.compile_cached(self.p.src)
      self.clprg = Device[self.p.dname].runtime(name, self.lib)
      for x in (rawbufs:=[init_globals[i[0]] for i in self.p.globals]): x.copyin(init_rawbufs[x])
      # verify
      super().__call__(rawbufs, var_vals, wait)
      for i, x in enumerate(rawbufs):
        try:
          np.testing.assert_allclose(np.frombuffer(x.as_buffer(), _to_np_dtype(x.dtype)), ground_truth[x], atol=1e-6, rtol=1e-6)
          if DEBUG >= 2: print(colored(name, "green"))
        except AssertionError as e:
          print(colored(name, "red"))
          raise e

def find_all_toposorts(graph:Dict[UOp, List[UOp]], in_degree:Dict[UOp, int]) -> List[Tuple[UOp, ...]]:
  visited: Set[UOp] = set()
  ret: List[Tuple[UOp, ...]] = []
  path: List[UOp] = []

  def recurse_paths(path:List[UOp]):
    for v, d in in_degree.items():
      if d != 0 or v in visited: continue
      if v.op is UOps.DEFINE_ACC and any(l not in path for l in v.src): continue
      for u in graph[v]: in_degree[u] -= 1
      if v.op is UOps.DEFINE_ACC: path.insert(min(path.index(l) for l in v.src), v)
      else: path.append(v)
      visited.add(v)
      recurse_paths(path)
      if len(ret) >= getenv("FUZZ_UOPS_MAX_PATHS", 10): return
      # backtrack
      for u in graph[v]: in_degree[u] += 1
      path.pop()
      visited.remove(v)
    if len(path) == len(in_degree): ret.append(tuple(path))
  recurse_paths(path)

  if len(ret) == 0: raise RuntimeError("detected cycle in the graph")
  # verify all paths are unique
  assert len(ret) == len(set(ret))
  return ret
