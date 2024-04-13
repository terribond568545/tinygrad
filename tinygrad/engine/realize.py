from typing import List, Dict, Optional, cast, Generator
from dataclasses import dataclass
from tinygrad.helpers import colored, getenv
from tinygrad.ops import ScheduleItem, BufferOps, LoadOps, copy_ast
from tinygrad.device import Runner, Device, BufferCopy, BufferXfer, update_stats
from tinygrad.buffer import Buffer
from tinygrad.shape.symbolic import Variable

@dataclass(frozen=True)
class ExecItem:
  prg: Runner
  rawbufs: List[Optional[Buffer]]
  def run(self, var_vals:Optional[Dict[Variable, int]]=None, wait=False, jit=False):
    self.prg([cast(Buffer, x).ensure_allocated() for x in self.rawbufs], var_vals if var_vals is not None else {}, wait=wait, jit=jit)

class CustomOp(Runner):
  def __init__(self, fxn):
    self.fxn = fxn
    super().__init__()
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False, jit=False): self.fxn(*rawbufs)

class EmptyOp(Runner):
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False, jit=False):
    update_stats(colored(f"empty {rawbufs[0].size:10d} {rawbufs[0].dtype}", "yellow"), 0, 0, {}, jit, 1, device=rawbufs[0].device)

def lower_schedule_item(si:ScheduleItem) -> Runner:
  assert len(set(x.device for x in si.outputs+si.inputs)) == 1 or si.ast[0].op is LoadOps.COPY
  if si.ast[0].op is BufferOps.STORE: return Device[si.outputs[0].device].get_runner(*si.ast)
  assert len(si.ast) == 1 and len(si.outputs) == 1, "only ASTRunner supports multioutput"
  out, ast = si.outputs[0], si.ast[0]
  if ast.op is LoadOps.COPY:
    if hasattr(Device[out.device].allocator, 'transfer') and out.device.split(":")[0] == si.inputs[0].device.split(":")[0]:
      return Device[si.outputs[0].device].get_runner(copy_ast(ast.arg)) if getenv("USE_COPY_KERNEL") else BufferXfer()
    return BufferCopy()
  if ast.op is LoadOps.CUSTOM: return CustomOp(ast.arg)
  if ast.op is LoadOps.EMPTY: return EmptyOp()
  raise RuntimeError(f"don't know how to lower {ast}")

def lower_schedule(schedule:List[ScheduleItem]) -> Generator[ExecItem, None, None]:
  while len(schedule): yield ExecItem(lower_schedule_item(si:=schedule.pop(0)), list(si.outputs+si.inputs))

capturing: List = []  # put classes with an add method in here

def run_schedule(schedule:List[ScheduleItem], var_vals:Optional[Dict[Variable, int]]=None):
  for ei in lower_schedule(schedule):
    if len(capturing): capturing[0].add(ei)
    ei.run(var_vals)
