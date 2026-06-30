"""Tie-correct online IC (audit P2-4).

The ICTracker computed Spearman via 1−6Σd²/(n(n²−1)) with ordinal ranks, which is only
valid when all ranks are distinct. Discretised signal scores and flat-bar returns produce
ties constantly, biasing the IC the system uses as an alpha-decay / sizing throttle. The IC
now uses average ranks + Pearson-on-ranks, which equals scipy.stats.spearmanr under ties.
"""
import pytest

from services.signal.main import _spearman

spearmanr = pytest.importorskip("scipy.stats").spearmanr


@pytest.mark.parametrize("a,b", [
    ([1, 2, 3, 4, 5, 6, 7, 8], [1, 3, 2, 4, 6, 5, 8, 7]),       # no ties
    ([1, 1, 2, 2, 3, 3], [1, 2, 2, 3, 3, 4]),                    # ties both sides
    ([3, 3, 3, 1, 2, 2, 5, 4], [1, 1, 2, 2, 3, 3, 4, 5]),        # many ties
])
def test_matches_scipy_spearman(a, b):
    ref = spearmanr(a, b).correlation
    assert _spearman(a, b) == pytest.approx(ref, abs=1e-9)


def test_constant_input_is_zero_not_nan():
    # zero variance (all equal) must not blow up / NaN — IC is undefined → 0.0
    assert _spearman([5, 5, 5, 5], [1, 2, 3, 4]) == 0.0


def test_tie_naive_formula_would_have_been_wrong():
    # With ties, the old shortcut diverges from the true Spearman; the new one matches scipy.
    a = [1, 1, 1, 2, 2, 3]
    b = [2, 2, 1, 3, 3, 4]
    n = len(a)
    from services.signal.main import _rank
    ra, rb = _rank(a), _rank(b)
    d2 = sum((ra[i] - rb[i]) ** 2 for i in range(n))
    tie_naive = 1 - 6 * d2 / (n * (n ** 2 - 1))
    correct = _spearman(a, b)
    assert correct == pytest.approx(spearmanr(a, b).correlation, abs=1e-9)
    assert abs(correct - tie_naive) > 1e-6   # they genuinely differ under ties
