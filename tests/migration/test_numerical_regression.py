"""Numerical regression tests for Selene 3O migration.

Each test verifies that the stack replacement produces numerically
equivalent results to the original Selene implementation.
"""
import numpy as np
import pytest

from tests.migration import fixtures
