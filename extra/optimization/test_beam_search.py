import unittest
import numpy as np

from tinygrad.helpers import BEAM, Timing
from tinygrad.shape.symbolic import Variable
from tinygrad.tensor import Tensor
from tinygrad.nn import Conv2d

class TestBeamSearch(unittest.TestCase):
  def setUp(self):
    self.old_beam = BEAM.value
    BEAM.value = 2
  def tearDown(self):
    BEAM.value = self.old_beam

  def test_variable_ast_beam(self):
    a = Tensor.rand(3, 3).reshape((Variable("a", 1, 10).bind(3), 3))
    a = (a+1).realize()

  def test_big_prime_number_matmul(self):
    a = Tensor.rand(367, 367)
    b = Tensor.rand(367, 367)
    c = (a@b).realize()
    np.testing.assert_allclose(c.numpy(), a.numpy() @ b.numpy(), atol=1e-4, rtol=1e-4)

  def test_big_prime_number_max(self):
    a = -Tensor.rand(367, 367)
    b = Tensor.rand(367, 367)
    # if incorrectly padded 0, the max would be 0 instead of a negative number
    c = (a*b).max(1)
    np.testing.assert_allclose(c.numpy(), (a.numpy() * b.numpy()).max(1), atol=1e-4, rtol=1e-4)

  def test_big_prime_number_sum(self):
    a = Tensor.rand(367, 367)
    b = Tensor.rand(367, 367)
    # if incorrectly padded 0, the sum would be inf
    c = (a/b).sum(1).realize()
    np.testing.assert_allclose(c.numpy(), (a.numpy() / b.numpy()).sum(1), atol=1e-4, rtol=1e-4)

  def test_variable_big_prime_number(self):
    v = Variable("v", 1, 400).bind(367)
    a = Tensor.rand(367, 367)
    b = Tensor.rand(367, 367)
    c = (a.reshape(367, v) @ b.reshape(v, 367)).realize()
    np.testing.assert_allclose(c.numpy(), a.numpy() @ b.numpy(), atol=1e-4, rtol=1e-4)

  def test_variable_shrink_prime_number(self):
    v = Variable("v", 1, 400).bind(367)
    a = Tensor.rand(400, 367)
    b = (a.shrink(((0,v), None))+1).reshape(367,367).realize()
    np.testing.assert_allclose(b.numpy(), a.numpy()[:367]+1, atol=1e-4, rtol=1e-4)

  def test_no_mutate_rawbuffers(self):
    a = Tensor.rand(3, 3).realize()
    desired = a.numpy() + 1
    a.assign(a+1)
    actual = a.numpy()
    np.testing.assert_allclose(actual, desired)

  def test_conv_beam(self):
    c = Conv2d(3, 16, (3,3))
    x = Tensor.rand(1,3,32,32)
    with Timing():
      c(x).realize()

if __name__ == '__main__':
  unittest.main()
