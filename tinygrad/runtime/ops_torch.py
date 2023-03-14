import torch
from typing import ClassVar, Dict, Callable
from tinygrad.ops import UnaryOps, BinaryOps, MovementOps, FusedOps, LoadOps, Op
from tinygrad.helpers import getenv, dtypes
from tinygrad.interpreted import InterpretedBuffer
from tinygrad.runtime.ops_cpu import base_fxn_for_op, einsum_mulacc

device = torch.device("cuda:0" if torch.cuda.is_available() else ("mps" if getenv("MPS", 0) else "cpu"))

torch_fxn_for_op: Dict[Op, Callable] = {**base_fxn_for_op, **{
  UnaryOps.NOOP: lambda x: x.contiguous(), UnaryOps.EXP: lambda x: x.exp(), UnaryOps.LOG: lambda x: x.log(),
  BinaryOps.MAX: torch.maximum, BinaryOps.CMPEQ: lambda x,y: (x==y).float(),
  MovementOps.PAD: lambda x, padding: torch.nn.functional.pad(x, [item for sublist in padding[::-1] for item in sublist]),
  FusedOps.MULACC: einsum_mulacc(lambda s,a,b: torch.einsum(s, a.float(), b.float()).type(a.dtype), lambda x: x.stride(), lambda x,s: x.expand(s)),
  MovementOps.STRIDE: lambda x, arg: x[tuple(slice(None, None, abs(i)) for i in arg)].flip([i for i,a in enumerate(arg) if a < 0]),
  LoadOps.FROMCPU: lambda arg: torch.from_numpy(arg).requires_grad_(False).to(device)
}}

class TorchBuffer(InterpretedBuffer):
  fxn_for_op: ClassVar = torch_fxn_for_op
  def to_tinygrad_dtype(self): return {torch.float16: dtypes.float16, torch.float32: dtypes.float32}[self._buf.dtype]
  def toCPU(self): return self._buf.cpu().numpy()
