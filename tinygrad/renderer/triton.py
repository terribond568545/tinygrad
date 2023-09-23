from typing import Dict, List, Final, Callable, DefaultDict
from collections import defaultdict
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, Op
from tinygrad.helpers import dtypes, ImageDType, DEBUG, getenv
from tinygrad.codegen.linearizer import  UOp, UOps
from triton.compiler import compile as triton_compile  # type: ignore
import hashlib
import math
import re
def next_power_of_2(x):
  return 1 << (x - 1).bit_length()

def render_valid(valid):
  return '(' * (len(valid) -1) + ') and '.join(valid) if len(valid) else 'True'

#NOTE Triton requires matching dimensions for load/store, disable this and see TestOps::test_output_padded_conv_transpose2d fail to compile
def fill_dims_for_idx(idx, dims):
  return "(" + idx + "+ (" + (f"0*({'+'.join(d for d in dims)})))") if len(dims) else idx

def get_max(var):
  if isinstance(var, int): return var
  return re.sub(r'\[(.*?)\]', '', str(var))[1:-1]

#NOTE can be removed after https://github.com/gpuocelot/gpuocelot/issues/8 gets resolved
def remove_single_scalar_curly_braces(ptx_code):
  return '\n'.join([re.sub(r'\{\s*(%\w+)\s*\}', r'\1', line) for line in ptx_code.split('\n')])

def render_const(args):
  return (('-' if args<0 else '') + 'float("inf")') if math.isinf(args) else ('float("nan")' if math.isnan(args) else str(args))

def define_scalar(local_size, triton_type, args):
  if len(local_size) > 0: return f"tl.full(({','.join([str(next_power_of_2(x)) for x in local_size])},),{render_const(args)}, dtype={triton_type})"
  return f"(tl.where(1, {render_const(args)}, {render_const(args)}).to({triton_type}))"

def uops_to_triton(function_name:str, uops:List[UOp]):
  local_size: List[int] = []
  depth = 1
  signatures, dims, bufs, kernel, valid = [], [], [], [], [] #type: ignore

  c: DefaultDict[str, int] = defaultdict(int)
  r: Dict[UOp, str] = {}
  def ssa(u, prefix="t"):
    nonlocal c, r
    c[prefix] += 1
    r[u]=f"{prefix}{c[prefix]-1}"
    return r[u]

  child_count: DefaultDict[UOp, int] = defaultdict(int)
  for ru in uops:
    for v in ru.vin:
      child_count[v] += 1

  def kk(s): kernel.append("  "*depth+s)
  code_for_op: Final[Dict[Op, Callable]] = {
    UnaryOps.EXP2: lambda x,: f"tl.math.exp2({x})",
    UnaryOps.LOG2: lambda x,: f"tl.math.log2({x})",
    UnaryOps.SIN: lambda x,: f"tl.sin({x})",
    UnaryOps.SQRT: lambda x,: f"tl.sqrt({x})",
    UnaryOps.NEG: lambda x,: f"-{x}",
    BinaryOps.ADD: lambda x,y,: f"({x}+{y})", BinaryOps.SUB: lambda x,y,: f"({x}-{y})",
    BinaryOps.MUL: lambda x,y,: f"({x}*{y})", BinaryOps.DIV: lambda x,y,: f"({x}/{y})",
    BinaryOps.MAX: lambda x,y,: f"tl.maximum({x},{y})",
    BinaryOps.CMPLT: lambda x,y,: f"({x}<{y})",
    BinaryOps.MOD: lambda x,y,: f"({x}%{y})",
    TernaryOps.MULACC: lambda x,y,z,: f"(({x}*{y})+{z})",
    TernaryOps.WHERE: lambda x,y,z,: f"tl.where({x},{y},{z})",
  }
  def int_div(x,y): return f"({x}//{y})"
  triton_dtypes = {dtypes.double: "tl.float64", dtypes.float32: "tl.float32", dtypes.float16: "tl.float16", dtypes.bool: "tl.int1", dtypes.int8: "tl.int8", dtypes.uint8: "tl.uint8", dtypes.int32: "tl.int32", dtypes.int64: "tl.int64"}
  signature_dtypes = {dtypes.double: "*fp64",dtypes.float32: "*fp32", dtypes.float16: "*fp16", dtypes.bool: "*i8", dtypes.int8: "*i1", dtypes.uint8: "*u8", dtypes._arg_int32: "i32", dtypes.int32: "*i32", dtypes.int64: "*i64"}
  for u in uops:
    uop,dtype,vin,args,_ = u
    if uop == UOps.LOOP:
      kk(f"for {ssa(u, 'ridx')} in range({vin[0].arg}, {r[vin[1]]}+{define_scalar([], 'tl.int32', 1)}):")
      depth += 1
    elif uop == UOps.END: depth -= 1
    elif uop == UOps.ALU:
      assert dtype is not None
      val = code_for_op[args](*[r[x] for x in vin])
      if child_count[u] <=1 or dtypes.is_int(dtype): r[u] = int_div(*[r[x] for x in vin]) if args == BinaryOps.DIV and dtypes.is_int(dtype) else val
      else: kk(f"{ssa(u, 'alu')} = ({val}).to({triton_dtypes[dtype]})")
    elif uop == UOps.LOAD:
      assert dtype is not None
      if len(vin) == 2: kk(f"{ssa(u, 'val')} = tl.load({r[vin[0]]} + { fill_dims_for_idx(r[vin[1]], dims)}, mask = {render_valid(valid)}).to({triton_dtypes[vin[0].dtype]})")# type: ignore
      else: kk(f"{ssa(u, 'val')} = tl.where({r[vin[2]]}, tl.load({r[vin[0]]}+{fill_dims_for_idx(r[vin[1]],dims)} , mask={render_valid(valid+[r[vin[2]]])}), 0.0).to({triton_dtypes[vin[0].dtype]})")# type: ignore
    elif uop == UOps.DEFINE_ACC: kk(f"{ssa(u, 'acc')} = {define_scalar(local_size, triton_dtypes[dtype], args).replace('//', '/')}") # type: ignore
    elif uop == UOps.CONST: r[u] = define_scalar([], triton_dtypes[dtype], args) # type: ignore
    elif uop == UOps.STORE:
      assert not isinstance(dtype, ImageDType), "unimplemented: image store"
      if len(vin) == 2: kk(f"{r[vin[0]]} =  {r[vin[1]].replace('//', '/')}")
      else: kk(f"tl.store({r[vin[0]]} + {r[vin[1]]}, {r[vin[2]].replace('//', '/')}, mask = {render_valid(valid)}) ")
    elif uop == UOps.DEFINE_GLOBAL:
      bufs.append(args)
      signatures.append(signature_dtypes[args[1]])
      r[u] = args[0]
    elif uop == UOps.SPECIAL:
      dims.append(args[1])
      valid.append(f"{args[1]}<{get_max(args[2])}")
      if args[1].startswith("g"): kk(f"{args[1]} = tl.program_id({args[0]}) # {args[2]}")
      elif args[1].startswith("l"):
        kk(f"{args[1]} = tl.arange({0}, {next_power_of_2(args[2])})")
        local_size.append(args[2])
      r[u] = args[1]
    else: raise NotImplementedError(f"unimplemented: {uop}")

  prg = f"import triton\nimport triton.language as tl\ntl.core.TRITON_MAX_TENSOR_NUMEL = float('inf')\n@triton.jit\ndef {function_name}("+','.join(f"{buf[0]}" for buf in bufs)+"):\n"
  for i, line in enumerate(list(filter(lambda line: "tl.arange" in line, kernel))): kernel[kernel.index(line)] +=  f"[{', '.join([':' if i == j else 'None' for j in range(len(local_size))])}]"
  prg += "\n".join(kernel)

  acc_local_size = 1
  for x in local_size: acc_local_size *= next_power_of_2(x)
  local_size = [acc_local_size] + [1] * (len(local_size) - 1)

  if DEBUG >=4: print(prg)
  hsh = hashlib.md5(prg.encode('utf-8')).hexdigest()
  fn = f"/tmp/{hsh}.py"
  with open(fn, "w") as f: f.write(prg)
  codeObject = compile(prg, fn, "exec")
  exec(codeObject, globals()) # pylint: disable=W0122\
  compiled = triton_compile(globals()[function_name], signature=",".join(signatures), device_type="cuda", debug=False, cc=(35 if getenv("CUDACPU", 0) else None))
  prg = compiled.asm["ptx"]
  if getenv("CUDACPU"): prg = remove_single_scalar_curly_braces(prg.split(".file")[0].split(".visible .func")[0])
  max_local_size =  [int(x) for x in prg.split(".maxntid ")[1].split("\n")[0].split(", ")]
  for i in range(len(local_size)): local_size[i] = min(local_size[i], max_local_size[i])
  return prg, local_size, {"binary":True, "shared":compiled.metadata["shared"]}
