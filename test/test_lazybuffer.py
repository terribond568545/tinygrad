#!/usr/bin/env python
import numpy as np
import unittest
from tinygrad.lazy import LazyBuffer
from tinygrad import Device
from tinygrad.tensor import Tensor

class TestLazyBuffer(unittest.TestCase):
  @unittest.skip("it doesn't work like this anymore")
  def test_fromcpu_buffer_sharing(self):
    a = np.arange(8)
    assert LazyBuffer.fromCPU(a).realized._buf is a

  def test_fromcpu_shape_tracker(self):
    def helper(a: np.ndarray):
      print(a.shape, a.strides, a.flags.c_contiguous)
      b = LazyBuffer.fromCPU(a)
      #assert b.st.contiguous == a.flags.c_contiguous
      assert b.st.shape == a.shape
      np.testing.assert_equal(a, Tensor(b).numpy())

    for ndims in range(1, 4):
      a = np.random.randn(*(4,)*ndims).astype(np.float32)
      for stride in [-2, 1, 2]:
        for start in [0, 1]:
          helper(a[(slice(start, None, stride),)*ndims])

  def test_shuffle_pad_ops_cmpeq(self):
    y = Tensor([1]).cat(Tensor([1]) == 0).numpy()
    z = Tensor([1, 0]).numpy()
    np.testing.assert_allclose(y, z)

  def test_shuffle_pad_ops_div(self):
    y = Tensor([1]).cat(Tensor([1]).div(Tensor([2.0]))).numpy()
    z = Tensor([1, 0.5]).numpy()
    np.testing.assert_allclose(y, z)

  def test_shuffle_pad_ops_log(self):
    y = Tensor([1]).cat(Tensor([1]).log()).numpy()
    z = Tensor([1, 0]).numpy()
    np.testing.assert_allclose(y, z)

  def test_shuffle_pad_ops_exp(self):
    y = Tensor([1]).cat(Tensor([1]).exp()).numpy()
    z = Tensor([1, np.e]).numpy()
    np.testing.assert_allclose(y, z)

  def test_device_0_is_the_same_device(self):
    a = Tensor([1, 2, 3], f"{Device.DEFAULT}")
    b = Tensor([1, 2, 3], f"{Device.DEFAULT}:0")
    assert a.device == b.device

if __name__ == "__main__":
  unittest.main()
