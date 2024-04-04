import unittest
from tinygrad.dtype import dtypes
from tinygrad.ops import BinaryOps
from tinygrad.codegen.uops import UOpGraph, UOps, PatternMatcher, UOp

class TestPatternMatcher(unittest.TestCase):
  def test_simple_match(self):
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.CONST, "dtype": dtypes.float}, lambda x: x)])
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.int, arg=1)
    self.assertEqual(matcher.rewrite(c1), c1)
    self.assertEqual(matcher.rewrite(c2), None)

  def test_dtype_set(self):
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.CONST, "dtype": set([dtypes.float32, dtypes.float64])}, lambda x: x)])
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.float64, arg=1.0)
    c3 = UOp(UOps.CONST, dtypes.float16, arg=1.0)
    c4 = UOp(UOps.CONST, dtypes.int, arg=1)
    self.assertEqual(matcher.rewrite(c1), c1)
    self.assertEqual(matcher.rewrite(c2), c2)
    self.assertEqual(matcher.rewrite(c3), None)
    self.assertEqual(matcher.rewrite(c4), None)

  def test_vin_one(self):
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.ALU, "vin":({"uop": UOps.CONST}, {"uop": UOps.CONST})}, lambda x: x)])
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.float, arg=2.0)
    c3 = UOp(UOps.ALU, dtypes.float, (c1,c2), BinaryOps.ADD)
    self.assertEqual(matcher.rewrite(c3), c3)
    self.assertEqual(matcher.rewrite(c2), None)

  def test_vin_permutations(self):
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.ALU, "vin":[{"uop": UOps.CONST}, {"uop": UOps.ALU}]}, lambda x: x)])
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.float, arg=2.0)
    c3 = UOp(UOps.ALU, dtypes.float, (c1,c2), BinaryOps.ADD)
    c4 = UOp(UOps.ALU, dtypes.float, (c3,c2), BinaryOps.ADD)
    c5 = UOp(UOps.ALU, dtypes.float, (c2,c3), BinaryOps.ADD)
    c6 = UOp(UOps.ALU, dtypes.float, (c3,c4), BinaryOps.ADD)
    self.assertEqual(matcher.rewrite(c3), None)
    self.assertEqual(matcher.rewrite(c4), c4)
    self.assertEqual(matcher.rewrite(c5), c5)
    self.assertEqual(matcher.rewrite(c6), None)

  def test_vin_repeat(self):
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.ALU, "vin":{"uop": UOps.CONST}}, lambda x: x)])
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.float, arg=2.0)
    c3 = UOp(UOps.ALU, dtypes.float, (c1,c2), BinaryOps.ADD)
    c4 = UOp(UOps.ALU, dtypes.float, (c2,c3), BinaryOps.ADD)
    self.assertEqual(matcher.rewrite(c3), c3)
    self.assertEqual(matcher.rewrite(c4), None)

  def test_rewrite_graph_folds(self):
    uops = UOpGraph()
    uops.add(UOps.CONST, dtypes.float, arg=2.0, simplify=False)
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.CONST, "dtype": dtypes.float},
                                lambda x: UOp(UOps.CAST, dtypes.int, (UOp(UOps.ALU, x.dtype, (x,x), BinaryOps.ADD),)))])
    matcher.rewrite_graph(uops)
    def _to_key(u): return (u.uop, u.dtype, u.arg)
    self.assertEqual(_to_key(UOp(UOps.CONST, dtypes.int, arg=4)), _to_key(uops.uops[-1]))

  def test_rewrite_graph_adds(self):
    uops = UOpGraph()
    uops.add(UOps.CONST, dtypes.int, arg=2, simplify=False)
    matcher = PatternMatcher([({"__name__": "x", "uop": UOps.CONST, "dtype": dtypes.int},
                               lambda x: UOp(UOps.STORE, x.dtype, (UOp(UOps.DEFINE_GLOBAL, x.dtype, tuple(), None), x)))])
    matcher.rewrite_graph(uops)
    uops.remove_childless(set(x for x in uops if x.uop in {UOps.STORE}))
    e1 = UOp(UOps.CONST, dtypes.int, arg=2)
    e2 = UOp(UOps.DEFINE_GLOBAL, dtypes.int, tuple())
    e3 = UOp(UOps.STORE, dtypes.int, (e2,e1))
    def _to_key(u): return (u.uop, u.dtype, u.arg)
    self.assertEqual(_to_key(e1), _to_key(uops.uops[0]))
    self.assertEqual(_to_key(e2), _to_key(uops.uops[1]))
    self.assertEqual(_to_key(e3), _to_key(uops.uops[2]))
    self.assertEqual(len(uops.uops), 3)

if __name__ == '__main__':
  unittest.main(verbosity=2)
